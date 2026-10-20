"""Manage a single ant-node systemd service on an already-provisioned host.

Motivation: a Hetzner (or any provider's) node VM can run into its storage
quota with no available headroom, while a cloud-side volume-size increase
needs human approval that can take 24-48h. In the meantime the only way to
free disk space is to remove one ``ant-node-N`` service; once the quota
increase lands, a matching service can be added back. ``ls`` shows per-service
disk usage to help pick which one to remove.

Purely imperative — driven by ``--inventory-path``/``--name``/
``--service-name``[/``--ssh-key-path``], with no manifest/orchestrator/DB
wiring, and works against any host in a given inventory, not just prod fleets.
``rm``/``add`` also update the inventory file's per-host ``nodes``/``services``
lists to match (best-effort — a failure here warns but never fails the
command, since the live SSH change has already happened by that point).
"""

import json
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console
from rich.table import Table
from saorsa_db.models.prod_service_audit import ProdServiceAuditAction

from saorsa_deploy import registry
from saorsa_deploy.analysis.json_out import console_for, emit_json
from saorsa_deploy.disk_survey import (
    FAILURE_KIND_LABELS,
    SurveyFailure,
    SurveyFailureKind,
    classify_survey_failure,
    compare_host_data,
    host_data_bytes,
    should_retry,
)
from saorsa_deploy.inventory import (
    NodeVm,
    load_node_inventory,
    update_service_in_inventory,
)
from saorsa_deploy.provisioning.bootstrap import DEFAULT_REWARDS_ADDRESS
from saorsa_deploy.provisioning.node import (
    _build_node_exec_start,
    _build_node_unit_file,
)
from saorsa_deploy.provisioning.node_services import (
    build_disk_survey_command,
    build_list_services_command,
    build_remove_service_command,
    build_reset_data_command,
    build_write_unit_and_start_command,
    parse_df_line,
    parse_node_unit,
    parse_service_list,
    parse_service_name,
)
from saorsa_deploy.provisioning.volume import LOG_DIR, MOUNT_POINT
from saorsa_deploy.ssh import clear_known_hosts

_SSH_OPTS = [
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=15",
]


def _resolve_ssh_key(vm: NodeVm, override: str | None) -> str:
    raw = override or vm.ssh_key
    return str(Path(raw).expanduser())


def _inventory_name(inventory_path: str) -> str:
    """The inventory's ``testnet_id`` (e.g. ``ant-prod-01``), used as the key
    for standalone prod-fleet DB records; falls back to the file stem."""
    path = Path(inventory_path).expanduser()
    try:
        return json.loads(path.read_text()).get("testnet_id") or path.stem
    except (ValueError, OSError):
        return path.stem


def _resolve_host(
    inventory_path: str, name: str, ssh_key_override: str | None, console: Console
) -> tuple[NodeVm, str]:
    """Load the inventory, find the named host, and resolve+validate its SSH
    key. Exits the process with a clear error on any failure — every caller
    handles this the same way, so the three commands' error messages match.
    """
    path = Path(inventory_path).expanduser()
    if not path.is_file():
        console.print(f"[bold red]Error:[/bold red] inventory not found: {path}")
        sys.exit(1)

    try:
        vms = load_node_inventory(path)
    except (ValueError, OSError) as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    matches = [vm for vm in vms if vm.name == name]
    if not matches:
        console.print(f"[bold red]Error:[/bold red] no host named {name!r} in {path}")
        sys.exit(1)

    vm = matches[0]
    ssh_key = _resolve_ssh_key(vm, ssh_key_override)
    if not os.path.isfile(ssh_key):
        console.print(
            f"[bold red]Error:[/bold red] SSH key for {vm.name} does not exist: {ssh_key}"
        )
        sys.exit(1)
    return vm, ssh_key


def _require_service_name(service_name: str, console: Console) -> None:
    try:
        parse_service_name(service_name)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


def _run_single_host_op(vm: NodeVm, ssh_key: str, op_name: str, command: str) -> list[str]:
    """Run one pyinfra shell op against a single host; return its stdout lines.

    Raises ``RuntimeError`` if pyinfra reports the host as failed.
    """
    hosts_data = [(vm.ip, {"ssh_user": vm.ssh_user, "ssh_key": ssh_key})]
    state = State(inventory=Inventory((hosts_data, {})), config=Config())
    connect_all(state)
    try:
        result = add_op(state, server.shell, name=op_name, commands=[command])
        run_ops(state)
    finally:
        disconnect_all(state)

    if state.failed_hosts:
        raise RuntimeError(f"'{op_name}' failed on {vm.name}")

    host = next(iter(result.keys()))
    return list(result[host].stdout_lines)


def _ssh_read_file(vm: NodeVm, ssh_key: str, path: str) -> str:
    """Raw one-shot SSH read of a remote file's contents (a single small probe,
    not a pyinfra op — mirrors ``prod_extend_unmanaged_volumes._post_resize_size_bytes``).
    """
    result = subprocess.run(
        ["ssh", "-i", ssh_key, *_SSH_OPTS, f"{vm.ssh_user}@{vm.ip}", f"cat {path}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to read {path} on {vm.name}: {result.stderr.strip()}")
    return result.stdout


def _format_bytes(size: int) -> str:
    # Scale on the magnitude: a negative delta reached the "< 1024" branch
    # immediately and printed as raw bytes, so a shrinking figure was the one
    # case that came out unreadable.
    sign = "-" if size < 0 else ""
    value = float(abs(size))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            body = f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
            return sign + body
        value /= 1024
    return f"{sign}{value:.1f} TiB"


def _list_services(vm: NodeVm, ssh_key: str, console: Console) -> list[dict]:
    clear_known_hosts([vm.ip], console)
    try:
        stdout_lines = _run_single_host_op(
            vm, ssh_key, "List ant-node services", build_list_services_command()
        )
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)
    return parse_service_list(stdout_lines)


def cmd_prod_services_ls(args) -> None:
    """List every ant-node-* service on a host with active/enabled state and
    on-disk data size, to help decide which service to remove. Optionally
    restricted to a single --service-name."""
    console = Console()
    service_filter = getattr(args, "service_name", None)
    if service_filter:
        _require_service_name(service_filter, console)
    vm, ssh_key = _resolve_host(args.inventory_path, args.name, args.ssh_key_path, console)
    records = _list_services(vm, ssh_key, console)
    if service_filter:
        records = [r for r in records if r["service_name"] == service_filter]

    if not records:
        target = f" matching {service_filter}" if service_filter else ""
        console.print(f"[yellow]No ant-node-* services found on {vm.name}{target}.[/yellow]")
        return

    table = Table(title=f"ant-node services on {vm.name}")
    table.add_column("Service")
    table.add_column("Active")
    table.add_column("Enabled")
    table.add_column("Disk usage", justify="right")
    for r in records:
        table.add_row(r["service_name"], r["active"], r["enabled"], _format_bytes(r["size_bytes"]))
    console.print(table)


logger = logging.getLogger(__name__)

#: Deadline for the first probe of each host. The probe walks every service's
#: data dir with ``du``, so it is not instant even on a healthy host.
SURVEY_TIMEOUT_SECS = 120
#: Deadline for a retry. Deliberately longer: the hosts that fail are the ones
#: under load, and re-running the identical probe would fail identically.
SURVEY_RETRY_TIMEOUT_SECS = 300

_SURVEY_SSH_OPTS = [
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=20",
]


def _survey_one_host(vm: NodeVm, ssh_key: str, timeout: int = SURVEY_TIMEOUT_SECS) -> dict:
    """SSH one host, run the disk-survey probe, return its parsed services +
    device disk figures, or a classified failure. Never raises.

    A failure result carries ``error`` (the message) plus ``failure_kind``, so
    the caller can tell "host is slow" from "host is down" — the distinction
    V2-1147 turns on, since a saturated host is exactly what a disk-pressure
    survey most needs to see and is also the one most likely to time out.
    """
    try:
        result = subprocess.run(
            [
                "ssh",
                "-i",
                ssh_key,
                *_SURVEY_SSH_OPTS,
                f"{vm.ssh_user}@{vm.ip}",
                build_disk_survey_command(),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        kind, msg = classify_survey_failure(timed_out=True)
        return {"vm": vm, "error": msg, "failure_kind": kind}
    except OSError as e:
        kind, msg = classify_survey_failure(os_error=str(e))
        return {"vm": vm, "error": msg, "failure_kind": kind}
    if result.returncode != 0:
        kind, msg = classify_survey_failure(returncode=result.returncode, stderr=result.stderr)
        return {"vm": vm, "error": msg, "failure_kind": kind}
    lines = result.stdout.splitlines()
    return {
        "vm": vm,
        "services": parse_service_list(lines),
        "disk": parse_df_line(lines),
    }


def _failure_of(result: dict, attempts: int = 1) -> SurveyFailure:
    """Turn a failed probe result into a SurveyFailure. Tolerates a result with
    no ``failure_kind`` (a test double, or an older caller)."""
    kind = result.get("failure_kind") or SurveyFailureKind.PROBE_FAILED
    if not isinstance(kind, SurveyFailureKind):
        kind = SurveyFailureKind(str(kind))
    return SurveyFailure(
        host=result["vm"].name,
        kind=kind,
        message=result.get("error") or "",
        attempts=attempts,
    )


def _retry_failed_hosts(
    failed: list[dict],
    ssh_key_for,
    console: Console,
    *,
    retries: int,
    backoff_secs: float,
    timeout: int,
    sleep=time.sleep,
) -> tuple[list[dict], dict[str, SurveyFailure]]:
    """Re-probe the hosts that failed the parallel pass, one at a time.

    Serial and after the fan-out on purpose: sixteen concurrent ``du`` walks
    are themselves part of why a loaded host misses its deadline, so retrying
    inside the worker would retry into the same storm. The retry also gets a
    longer deadline than the first attempt — re-running an identical 120s probe
    against a host that just missed 120s would fail the same way.

    Returns the results that succeeded on retry, and the failures that remain
    (keyed by host, carrying the total attempt count).
    """
    recovered: list[dict] = []
    remaining: dict[str, SurveyFailure] = {}
    pending = list(failed)

    for host_result in pending:
        vm = host_result["vm"]
        failure = _failure_of(host_result)
        if retries <= 0 or not should_retry(failure.kind):
            remaining[vm.name] = failure
            continue

        attempts = 1
        for attempt in range(1, retries + 1):
            if backoff_secs > 0:
                sleep(backoff_secs * attempt)
            console.print(
                f"[dim]Retrying [cyan]{vm.name}[/cyan] "
                f"({failure.label}), attempt {attempt + 1}...[/dim]"
            )
            result = _survey_one_host(vm, ssh_key_for(vm), timeout)
            attempts += 1
            if "error" not in result:
                console.print(f"[green]Recovered {vm.name} on retry.[/green]")
                recovered.append(result)
                break
            failure = _failure_of(result, attempts=attempts)
        else:
            remaining[vm.name] = failure

    return recovered, remaining


def _print_survey_failures(console: Console, failures: dict[str, SurveyFailure]) -> None:
    """Render the hosts that could not be surveyed, grouped by reason.

    Grouped rather than listed flat because the reasons are not equivalent: a
    timeout means the host is probably up but saturated (its disk still counts,
    we just failed to read it), while a refusal means it may genuinely be gone.
    """
    if not failures:
        return
    console.print(
        f"[bold yellow]Warning:[/bold yellow] {len(failures)} host(s) could not "
        f"be surveyed — the totals below EXCLUDE them:"
    )
    by_kind: dict[SurveyFailureKind, list[SurveyFailure]] = {}
    for failure in failures.values():
        by_kind.setdefault(failure.kind, []).append(failure)
    for kind in SurveyFailureKind:
        group = by_kind.get(kind)
        if not group:
            continue
        console.print(f"  [yellow]{FAILURE_KIND_LABELS[kind]}[/yellow]:")
        for failure in sorted(group, key=lambda f: f.host):
            attempts = f" after {failure.attempts} attempts" if failure.attempts > 1 else ""
            console.print(f"    [yellow]{failure.host}[/yellow]{attempts}: {failure.message}")


def _signed_bytes(value: int) -> str:
    return ("+" if value >= 0 else "-") + _format_bytes(abs(value))


def _print_stored_failures(console: Console, survey, label: str) -> None:
    """Render a stored survey's failed-host set.

    A NULL ``failed_hosts`` means the survey predates V2-1147 and its failed set
    is *unknown*; it is reported as such rather than as "no host failed", which
    is a different claim and the one that made survey 39 look like a reclaim.
    """
    failed = getattr(survey, "failed_hosts", None)
    if failed is None:
        console.print(
            f"[dim]({label}: recorded before failed-host tracking — whether any "
            f"host was missed is unknown)[/dim]"
        )
        return
    if not failed:
        return
    attempted = getattr(survey, "attempted_host_count", None)
    of_attempted = f" of {attempted} attempted" if attempted else ""
    console.print(
        f"[bold yellow]Warning:[/bold yellow] the {label} survey missed "
        f"{len(failed)} host(s){of_attempted}, so its totals are short:"
    )
    for host, detail in sorted(failed.items()):
        detail = detail if isinstance(detail, dict) else {}
        kind = detail.get("kind", "unknown")
        message = detail.get("message", "")
        console.print(f"  [yellow]{host}[/yellow] ({kind}): {message}")


def _print_coverage_comparison(
    console: Console,
    inventory_name: str,
    latest_host_data: dict[str, int],
    surveyed_hosts: set,
    failures: dict[str, SurveyFailure],
) -> None:
    """Compare this survey's node-data total against the previous stored one,
    like-for-like over the hosts both runs reached.

    This is the half of V2-1147 that actually misled: a retry reduces how often
    a host drops out, but any host can still drop, and a whole-survey total
    computed over whatever remains reads a missing host as reclaimed disk.
    """
    repo = registry.get_prod_fleet_repository()
    if repo is None:
        return
    try:
        previous = repo.latest_disk_surveys(inventory_name, limit=1)
        if not previous:
            return
        prev = previous[0]
        prev_host_data = repo.disk_survey_host_data_bytes(prev.id)
    except Exception:
        logger.warning("Could not read the previous survey for comparison", exc_info=True)
        return
    if not prev_host_data:
        return

    comparison = compare_host_data(prev_host_data, latest_host_data)
    console.print(
        f"[dim]Previous survey {prev.surveyed_at.isoformat(timespec='minutes')} "
        f"covered {len(prev_host_data)} host(s).[/dim]"
    )
    if comparison.dropped_hosts:
        console.print(
            f"[bold yellow]Coverage dropped:[/bold yellow] "
            f"{len(comparison.dropped_hosts)} host(s) counted last time are "
            f"missing from this survey: "
            f"{', '.join(comparison.dropped_hosts)}"
        )
    if comparison.added_hosts:
        console.print(
            f"[dim]{len(comparison.added_hosts)} host(s) new to this survey: "
            f"{', '.join(comparison.added_hosts)}[/dim]"
        )
    _print_delta_lines(console, comparison)


def _print_delta_lines(console, comparison) -> None:
    """Print the raw and like-for-like deltas together — never one alone.

    When coverage is identical the two are the same number, so only one line is
    printed. When it differs, both are shown and the raw one is labelled as
    spanning different host sets, so it can never be read as a clean total.
    """
    raw = (
        f"{_format_bytes(comparison.previous_total)} → "
        f"{_format_bytes(comparison.latest_total)} "
        f"(Δ {_signed_bytes(comparison.raw_delta)})"
    )
    if comparison.coverage_changed:
        console.print(f"[dim]Raw total: {raw} — across DIFFERING host sets.[/dim]")
    else:
        console.print(f"[dim]Total node data: {raw}.[/dim]")
    if not comparison.common_hosts:
        console.print(
            "[yellow]No host was covered by both surveys — no like-for-like "
            "comparison is possible.[/yellow]"
        )
        return
    if not comparison.coverage_changed:
        return
    per_host = comparison.common_delta_per_host
    console.print(
        f"[bold]Like-for-like[/bold] over the {len(comparison.common_hosts)} "
        f"host(s) in both surveys: "
        f"{_format_bytes(comparison.previous_common_total)} → "
        f"{_format_bytes(comparison.latest_common_total)} "
        f"(Δ {_signed_bytes(comparison.common_delta)}, "
        f"{_signed_bytes(int(per_host))}/host)."
    )
    if comparison.misleading:
        direction = "growth" if comparison.common_delta > 0 else "a reduction"
        console.print(
            f"[bold red]The raw total is misleading:[/bold red] it points the "
            f"opposite way to the like-for-like figure, which shows {direction}. "
            f"Read the like-for-like line, not the raw one."
        )


def cmd_prod_services_disk_survey(args) -> None:
    """Survey disk usage across EVERY host in a production inventory: list all
    ant-node-* services with their data-dir sizes, ascending (smallest first =
    deletion candidates), plus each host's free space. Persists the survey to
    the registry DB (best-effort) unless --no-store."""
    console = Console()

    inv_path = Path(args.inventory_path).expanduser()
    if not inv_path.is_file():
        console.print(f"[bold red]Error:[/bold red] inventory not found: {inv_path}")
        sys.exit(1)
    try:
        vms = load_node_inventory(inv_path, provider_filter=args.provider)
    except (ValueError, OSError) as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    limit_to = getattr(args, "limit_to", None)
    if limit_to:
        wanted = {n.strip() for n in limit_to.split(",") if n.strip()}
        vms = [vm for vm in vms if vm.name in wanted]
    if not vms:
        console.print("[bold red]Error:[/bold red] no hosts to survey after filters.")
        sys.exit(1)

    try:
        inventory_name = json.loads(inv_path.read_text()).get("testnet_id") or inv_path.stem
    except (ValueError, OSError):
        inventory_name = inv_path.stem

    max_parallel = max(1, int(getattr(args, "max_parallel", None) or 16))
    retries = getattr(args, "survey_retries", None)
    retries = 1 if retries is None else max(0, int(retries))
    backoff = getattr(args, "retry_backoff_secs", None)
    backoff = 5.0 if backoff is None else max(0.0, float(backoff))

    def ssh_key_for(vm):
        return _resolve_ssh_key(vm, args.ssh_key_path)

    console.print(f"Surveying {len(vms)} host(s) from [cyan]{inventory_name}[/cyan]...")
    attempted_host_count = len(vms)
    results = []
    with ThreadPoolExecutor(max_workers=min(max_parallel, len(vms))) as pool:
        futures = {pool.submit(_survey_one_host, vm, ssh_key_for(vm)): vm for vm in vms}
        for fut in as_completed(futures):
            results.append(fut.result())

    # Retry the failures serially, now that the fan-out load has dropped.
    first_pass_failures = [r for r in results if "error" in r]
    remaining_failures: dict[str, SurveyFailure] = {}
    if first_pass_failures:
        console.print(
            f"[yellow]{len(first_pass_failures)} host(s) failed the first pass; "
            f"retrying serially.[/yellow]"
        )
        recovered, remaining_failures = _retry_failed_hosts(
            first_pass_failures,
            ssh_key_for,
            console,
            retries=retries,
            backoff_secs=backoff,
            timeout=SURVEY_RETRY_TIMEOUT_SECS,
        )
        results = [r for r in results if "error" not in r] + recovered

    services: list[dict] = []
    host_disk: dict = {}
    for r in results:
        vm = r["vm"]
        if "error" in r:
            continue
        disk = r.get("disk")
        if disk is not None:
            host_disk[vm.name] = {"provider": vm.provider, **disk}
        for svc in r["services"]:
            services.append(
                {
                    "host": vm.name,
                    "provider": vm.provider,
                    "service_name": svc["service_name"],
                    "size_bytes": svc["size_bytes"],
                    "active": svc["active"] == "active",
                    "enabled": svc["enabled"] == "enabled",
                }
            )

    services.sort(key=lambda s: s["size_bytes"])
    surveyed_at = datetime.now(timezone.utc)

    # All services, ascending by size (smallest = deletion candidates first).
    table = Table(title=f"{inventory_name} — all node services by disk usage (ascending)")
    table.add_column("Host")
    table.add_column("Provider")
    table.add_column("Service")
    table.add_column("Active")
    table.add_column("Size", justify="right")
    table.add_column("Host free", justify="right")
    for s in services:
        hd = host_disk.get(s["host"])
        free = _format_bytes(hd["free_bytes"]) if hd else "?"
        table.add_row(
            s["host"],
            s["provider"] or "",
            s["service_name"],
            "active" if s["active"] else "[red]inactive[/red]",
            _format_bytes(s["size_bytes"]),
            free,
        )
    console.print(table)

    # Per-host free space, most-pressured first.
    if host_disk:
        htable = Table(title="Host free space (most pressured first)")
        htable.add_column("Host")
        htable.add_column("Provider")
        htable.add_column("Free", justify="right")
        htable.add_column("Used", justify="right")
        htable.add_column("Total", justify="right")
        for host, hd in sorted(host_disk.items(), key=lambda kv: kv[1]["free_bytes"]):
            htable.add_row(
                host,
                hd.get("provider") or "",
                _format_bytes(hd["free_bytes"]),
                _format_bytes(hd["used_bytes"]),
                _format_bytes(hd["total_bytes"]),
            )
        console.print(htable)

    total = sum(s["size_bytes"] for s in services)
    surveyed_hosts = {s["host"] for s in services} | set(host_disk)
    console.print(
        f"[dim]{len(services)} services across {len(host_disk)} host(s) "
        f"of {attempted_host_count} attempted; "
        f"total node data {_format_bytes(total)}.[/dim]"
    )

    _print_survey_failures(console, remaining_failures)
    _print_coverage_comparison(
        console,
        inventory_name,
        host_data_bytes(services),
        surveyed_hosts,
        remaining_failures,
    )

    if getattr(args, "no_store", False):
        console.print("[dim]--no-store: survey not persisted.[/dim]")
        return
    survey_id = registry.record_disk_survey(
        inventory_name=inventory_name,
        surveyed_at=surveyed_at,
        services=services,
        host_disk=host_disk,
        failed_hosts={h: f.to_dict() for h, f in remaining_failures.items()},
        attempted_host_count=attempted_host_count,
    )
    if survey_id is not None:
        console.print(f"[green]Stored survey id {survey_id}[/green] in the registry.")
    else:
        console.print("[dim]Survey not stored (DATABASE_URL unset or unreachable).[/dim]")


def _survey_as_dict(survey) -> dict:
    """A stored survey's scalar fields, without its per-service child rows."""
    if survey is None:
        return None
    return {
        "id": survey.id,
        "surveyed_at": survey.surveyed_at.isoformat(),
        "host_count": survey.host_count,
        "service_count": survey.service_count,
        "total_data_bytes": survey.total_data_bytes,
        "attempted_host_count": survey.attempted_host_count,
        "failed_hosts": survey.failed_hosts,
    }


def _emit_survey_report_json(args, console, payload: dict) -> None:
    """Write the survey comparison as JSON when `--json-out` was given.

    Exists so a reporting tool can read today-vs-yesterday without parsing the
    Rich tables above, whose host column is truncated to the terminal width --
    `cntb-US-cent…` is not a host a consumer can key on.

    Emitted at every exit, including the ones where there is nothing to
    compare. "One survey on record" and "the command failed before it got
    here" are different states and a missing file cannot distinguish them.
    """
    json_out = getattr(args, "json_out", None)
    if not json_out:
        return
    emit_json(payload, json_out, console)


def cmd_prod_services_survey_report(args) -> None:
    """Compare the two most recent disk surveys for an inventory and summarise
    the change: per-host free-disk delta, total node-data delta, and the
    services added/removed/reset between them (from the audit trail). This is
    the 'today vs yesterday' read-back over the stored survey history."""
    console = console_for(getattr(args, "json_out", None))
    repo = registry.get_prod_fleet_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set — no survey "
            "history to report. Run `prod services disk-survey` with a database."
        )
        sys.exit(1)

    inventory_name = _inventory_name(args.inventory_path)
    surveys = repo.latest_disk_surveys(inventory_name, limit=2)
    base = {
        "inventory": inventory_name,
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "latest": None,
        "previous": None,
    }
    if not surveys:
        console.print(f"[yellow]No surveys recorded for {inventory_name}.[/yellow]")
        _emit_survey_report_json(args, console, base)
        return

    latest = surveys[0]
    base["latest"] = _survey_as_dict(latest)
    base["host_disk"] = {"latest": latest.host_disk or {}}
    base["services"] = [
        {
            "host": svc.host,
            "service": svc.service_name,
            "size_bytes": svc.size_bytes,
            "active": getattr(svc, "active", None),
        }
        for svc in sorted(latest.services or [], key=lambda x: x.size_bytes or 0)
    ]
    console.print(
        f"[bold]{inventory_name}[/bold] latest survey "
        f"[cyan]{latest.surveyed_at.isoformat()}[/cyan]: "
        f"{latest.service_count} services on {latest.host_count} hosts, "
        f"total node data {_format_bytes(latest.total_data_bytes)}."
    )
    _print_stored_failures(console, latest, "latest")
    if len(surveys) < 2:
        console.print("[dim]Only one survey on record — nothing to compare yet.[/dim]")
        _emit_survey_report_json(args, console, base)
        return

    prev = surveys[1]
    base["previous"] = _survey_as_dict(prev)
    base["host_disk"]["previous"] = prev.host_disk or {}
    base["free_delta"] = []
    console.print(f"[dim]Comparing against previous survey {prev.surveyed_at.isoformat()}.[/dim]")
    _print_stored_failures(console, prev, "previous")

    # Per-host free-disk delta (freed disk is positive).
    dtable = Table(title="Free-disk change per host (freed = positive)")
    dtable.add_column("Host")
    dtable.add_column("Free then", justify="right")
    dtable.add_column("Free now", justify="right")
    dtable.add_column("Δ freed", justify="right")
    prev_disk = prev.host_disk or {}
    for host, hd in sorted((latest.host_disk or {}).items()):
        pf = (prev_disk.get(host) or {}).get("free_bytes")
        nf = hd.get("free_bytes", 0)
        delta = (nf - pf) if pf is not None else None
        dtable.add_row(
            host,
            _format_bytes(pf) if pf is not None else "?",
            _format_bytes(nf),
            _signed_bytes(delta) if delta is not None else "?",
        )
        base["free_delta"].append({"host": host, "free_then": pf, "free_now": nf, "delta": delta})
    console.print(dtable)

    # The node-data delta, like-for-like over the hosts both surveys reached.
    # Computed from the per-host child rows rather than the stored whole-survey
    # totals, because a host missing from one run makes those totals describe
    # different fleets (V2-1147).
    try:
        prev_host_data = repo.disk_survey_host_data_bytes(prev.id)
        latest_host_data = repo.disk_survey_host_data_bytes(latest.id)
    except Exception:
        logger.warning("Could not load per-host survey data", exc_info=True)
        prev_host_data = latest_host_data = {}
    if prev_host_data or latest_host_data:
        comparison = compare_host_data(prev_host_data, latest_host_data)
        base["node_data"] = {
            "previous_total": comparison.previous_total,
            "latest_total": comparison.latest_total,
            "raw_delta": comparison.raw_delta,
            "common_delta": comparison.common_delta,
            "common_delta_per_host": comparison.common_delta_per_host,
            "common_host_count": len(comparison.common_hosts),
            "dropped_hosts": list(comparison.dropped_hosts),
            "added_hosts": list(comparison.added_hosts),
            "coverage_changed": comparison.coverage_changed,
            "misleading": comparison.misleading,
            "per_host_previous": prev_host_data,
            "per_host_latest": latest_host_data,
        }
        if comparison.dropped_hosts:
            console.print(
                f"[bold yellow]Coverage dropped:[/bold yellow] "
                f"{len(comparison.dropped_hosts)} host(s) in the previous survey "
                f"are missing from the latest: "
                f"{', '.join(comparison.dropped_hosts)}"
            )
        if comparison.added_hosts:
            console.print(
                f"[dim]{len(comparison.added_hosts)} host(s) new to the latest "
                f"survey: {', '.join(comparison.added_hosts)}[/dim]"
            )
        _print_delta_lines(console, comparison)
    else:
        console.print(
            f"[dim]Total node data: {_format_bytes(prev.total_data_bytes)} → "
            f"{_format_bytes(latest.total_data_bytes)} "
            f"(Δ {_signed_bytes(latest.total_data_bytes - prev.total_data_bytes)}) "
            f"— per-host detail unavailable, so this spans whatever each run "
            f"reached.[/dim]"
        )

    # Audit events between the two surveys.
    events = repo.list_service_audit(
        inventory_name, since=prev.surveyed_at, until=latest.surveyed_at
    )
    base["audit"] = [
        {
            "occurred_at": e.occurred_at.isoformat(),
            "host": e.host,
            "service": e.service_name,
            "action": e.action.value,
            "size_bytes": e.size_bytes,
        }
        for e in events
    ]
    if not events:
        console.print("[dim]No add/remove/reset events between the two surveys.[/dim]")
        base["reclaimed_bytes"] = 0
        _emit_survey_report_json(args, console, base)
        return
    etable = Table(title="Service changes between the surveys (audit trail)")
    etable.add_column("When")
    etable.add_column("Host")
    etable.add_column("Service")
    etable.add_column("Action")
    etable.add_column("Size", justify="right")
    reclaimed = 0
    for e in events:
        if e.action == ProdServiceAuditAction.REMOVED and e.size_bytes:
            reclaimed += e.size_bytes
        etable.add_row(
            e.occurred_at.isoformat(timespec="minutes"),
            e.host,
            e.service_name,
            e.action.value,
            _format_bytes(e.size_bytes) if e.size_bytes is not None else "",
        )
    console.print(etable)
    console.print(
        f"[green]Reclaimed via removals in this window: {_format_bytes(reclaimed)}.[/green]"
    )
    base["reclaimed_bytes"] = reclaimed
    _emit_survey_report_json(args, console, base)


def _update_inventory(
    inventory_path: str, vm: NodeVm, service_name: str, *, added: bool, console: Console
) -> None:
    """Best-effort: the live SSH change has already succeeded by the time this
    runs, so an inventory-write failure is reported but never fails the
    command — the operator would just need to reconcile the file by hand.
    """
    try:
        update_service_in_inventory(inventory_path, vm.name, service_name, added=added)
    except (ValueError, OSError) as e:
        console.print(
            f"[bold yellow]Warning:[/bold yellow] {service_name} was "
            f"{'added on' if added else 'removed from'} {vm.name}, but the "
            f"inventory file could not be updated: {e}"
        )
        return
    console.print(f"[dim]Inventory updated: {inventory_path}[/dim]")


def cmd_prod_services_rm(args) -> None:
    """Stop, disable, and delete one ant-node service's unit + data/log dirs,
    freeing the disk space it was using. Never touches sibling services."""
    console = Console()
    _require_service_name(args.service_name, console)
    vm, ssh_key = _resolve_host(args.inventory_path, args.name, args.ssh_key_path, console)

    records = _list_services(vm, ssh_key, console)
    if not any(r["service_name"] == args.service_name for r in records):
        console.print(
            f"[bold red]Error:[/bold red] {args.service_name} not found on "
            f"{vm.name}. Run `prod services ls` to see what's there."
        )
        sys.exit(1)

    try:
        _run_single_host_op(
            vm,
            ssh_key,
            f"Remove {args.service_name}",
            build_remove_service_command(args.service_name),
        )
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    console.print(
        f"[bold green]✓[/bold green] Removed {args.service_name} from {vm.name} "
        "(unit, data dir, and log dir deleted)."
    )
    _update_inventory(args.inventory_path, vm, args.service_name, added=False, console=console)
    removed_size = next(
        (r["size_bytes"] for r in records if r["service_name"] == args.service_name),
        None,
    )
    registry.record_service_audit(
        _inventory_name(args.inventory_path),
        vm.name,
        args.service_name,
        ProdServiceAuditAction.REMOVED,
        datetime.now(timezone.utc),
        size_bytes=removed_size,
    )


def cmd_prod_services_reset_data(args) -> None:
    """Stop one ant-node service, wipe its data dir except node_identity.key,
    then start it again — so it re-bootstraps with the SAME peer ID (unchanged
    XOR range / record responsibility). Reclaims the record-store bytes while
    keeping the node's identity; never touches the unit, port, log dir, or any
    sibling service."""
    console = Console()
    _require_service_name(args.service_name, console)
    vm, ssh_key = _resolve_host(args.inventory_path, args.name, args.ssh_key_path, console)

    records = _list_services(vm, ssh_key, console)
    if not any(r["service_name"] == args.service_name for r in records):
        console.print(
            f"[bold red]Error:[/bold red] {args.service_name} not found on "
            f"{vm.name}. Run `prod services ls` to see what's there."
        )
        sys.exit(1)

    try:
        _run_single_host_op(
            vm,
            ssh_key,
            f"Reset data for {args.service_name}",
            build_reset_data_command(args.service_name),
        )
    except RuntimeError as e:
        console.print(
            f"[bold red]Error:[/bold red] {e} "
            "(the service may be left stopped, or its identity key was missing "
            "— check `prod services ls` and the host)."
        )
        sys.exit(1)

    console.print(
        f"[bold green]✓[/bold green] Reset {args.service_name} on {vm.name}: "
        "data wiped, identity (node_identity.key) retained, service restarted. "
        "It will re-bootstrap with the same peer ID."
    )
    wiped_size = next(
        (r["size_bytes"] for r in records if r["service_name"] == args.service_name),
        None,
    )
    registry.record_service_audit(
        _inventory_name(args.inventory_path),
        vm.name,
        args.service_name,
        ProdServiceAuditAction.RESET,
        datetime.now(timezone.utc),
        size_bytes=wiped_size,
    )


def cmd_prod_services_add(args) -> None:
    """Add a new ant-node service at --service-name, cloning its config
    (binary, evm network, NAT wrapping, log level, bootstrap peers, restart
    policy, backtrace env, upgrade channel, rewards address) from an existing
    running sibling service on the same host."""
    console = Console()
    _require_service_name(args.service_name, console)
    vm, ssh_key = _resolve_host(args.inventory_path, args.name, args.ssh_key_path, console)

    records = _list_services(vm, ssh_key, console)
    if any(r["service_name"] == args.service_name for r in records):
        console.print(
            f"[bold red]Error:[/bold red] {args.service_name} already exists on {vm.name}."
        )
        sys.exit(1)

    active = [r for r in records if r["active"] == "active"]
    if not active:
        console.print(
            f"[bold red]Error:[/bold red] {vm.name} has no active ant-node "
            "service to clone config from — add needs at least one running "
            "sibling."
        )
        sys.exit(1)
    sibling = min(active, key=lambda r: r["index"])

    try:
        sibling_content = _ssh_read_file(
            vm, ssh_key, f"/etc/systemd/system/{sibling['service_name']}.service"
        )
        parsed = parse_node_unit(sibling_content)
    except (RuntimeError, ValueError) as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    new_index = parse_service_name(args.service_name)
    delta = new_index - sibling["index"]
    new_port = parsed["port"] + delta if parsed["port"] is not None else None

    root_dir = f"{MOUNT_POINT}/data/{args.service_name}"
    log_dir = f"{LOG_DIR}/{args.service_name}"

    exec_start = _build_node_exec_start(
        bootstrap_peers=parsed["bootstrap_peers"],
        root_dir=root_dir,
        log_dir=log_dir,
        port=new_port,
        log_level=parsed["log_level"],
        testnet=parsed["testnet"],
        upgrade_channel=parsed["upgrade_channel"],
        rewards_address=parsed["rewards_address"] or DEFAULT_REWARDS_ADDRESS,
        evm_network=parsed["evm_network"],
    )
    unit_content = _build_node_unit_file(
        args.service_name,
        exec_start,
        is_nat=parsed["is_nat"],
        restart_policy=parsed["restart_policy"],
        restart_sec=parsed["restart_sec"],
        limit_nofile=parsed["limit_nofile"],
        rust_backtrace=parsed["rust_backtrace"],
        coredumps=parsed["coredumps"],
        colorbt_show_hidden=parsed["colorbt_show_hidden"],
    )

    add_command = f"mkdir -p {root_dir} {log_dir}\n" + build_write_unit_and_start_command(
        args.service_name, unit_content
    )
    try:
        _run_single_host_op(vm, ssh_key, f"Add {args.service_name}", add_command)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    console.print(
        f"[bold green]✓[/bold green] Added and started {args.service_name} on "
        f"{vm.name} (cloned from {sibling['service_name']})."
    )
    _update_inventory(args.inventory_path, vm, args.service_name, added=True, console=console)
    registry.record_service_audit(
        _inventory_name(args.inventory_path),
        vm.name,
        args.service_name,
        ProdServiceAuditAction.ADDED,
        datetime.now(timezone.utc),
    )
