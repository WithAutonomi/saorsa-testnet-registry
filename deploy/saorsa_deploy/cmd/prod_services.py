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
import os
import subprocess
import sys
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
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
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
        console.print(
            f"[bold red]Error:[/bold red] no host named {name!r} in {path}"
        )
        sys.exit(1)

    vm = matches[0]
    ssh_key = _resolve_ssh_key(vm, ssh_key_override)
    if not os.path.isfile(ssh_key):
        console.print(
            f"[bold red]Error:[/bold red] SSH key for {vm.name} does not "
            f"exist: {ssh_key}"
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
        raise RuntimeError(
            f"Failed to read {path} on {vm.name}: {result.stderr.strip()}"
        )
    return result.stdout


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


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
        table.add_row(
            r["service_name"], r["active"], r["enabled"], _format_bytes(r["size_bytes"])
        )
    console.print(table)


_SURVEY_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=20",
]


def _survey_one_host(vm: NodeVm, ssh_key: str) -> dict:
    """SSH one host, run the disk-survey probe, return its parsed services +
    device disk figures (or an ``error`` string). Never raises."""
    try:
        result = subprocess.run(
            [
                "ssh", "-i", ssh_key, *_SURVEY_SSH_OPTS,
                f"{vm.ssh_user}@{vm.ip}", build_disk_survey_command(),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"vm": vm, "error": "ssh timed out"}
    except OSError as e:
        return {"vm": vm, "error": str(e)}
    if result.returncode != 0:
        return {"vm": vm, "error": result.stderr.strip() or f"ssh exit {result.returncode}"}
    lines = result.stdout.splitlines()
    return {
        "vm": vm,
        "services": parse_service_list(lines),
        "disk": parse_df_line(lines),
    }


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

    console.print(
        f"Surveying {len(vms)} host(s) from [cyan]{inventory_name}[/cyan]..."
    )
    results = []
    with ThreadPoolExecutor(max_workers=min(16, len(vms))) as pool:
        futures = {
            pool.submit(_survey_one_host, vm, _resolve_ssh_key(vm, args.ssh_key_path)): vm
            for vm in vms
        }
        for fut in as_completed(futures):
            results.append(fut.result())

    services: list[dict] = []
    host_disk: dict = {}
    failures: list[tuple[str, str]] = []
    for r in results:
        vm = r["vm"]
        if "error" in r:
            failures.append((vm.name, r["error"]))
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
            s["host"], s["provider"] or "", s["service_name"],
            "active" if s["active"] else "[red]inactive[/red]",
            _format_bytes(s["size_bytes"]), free,
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
                host, hd.get("provider") or "",
                _format_bytes(hd["free_bytes"]),
                _format_bytes(hd["used_bytes"]),
                _format_bytes(hd["total_bytes"]),
            )
        console.print(htable)

    total = sum(s["size_bytes"] for s in services)
    console.print(
        f"[dim]{len(services)} services across {len(host_disk)} host(s); "
        f"total node data {_format_bytes(total)}.[/dim]"
    )

    if failures:
        console.print(
            f"[bold yellow]Warning:[/bold yellow] {len(failures)} host(s) failed to survey:"
        )
        for name, err in failures:
            console.print(f"  [yellow]{name}[/yellow]: {err}")

    if getattr(args, "no_store", False):
        console.print("[dim]--no-store: survey not persisted.[/dim]")
        return
    survey_id = registry.record_disk_survey(
        inventory_name=inventory_name,
        surveyed_at=surveyed_at,
        services=services,
        host_disk=host_disk,
    )
    if survey_id is not None:
        console.print(f"[green]Stored survey id {survey_id}[/green] in the registry.")
    else:
        console.print(
            "[dim]Survey not stored (DATABASE_URL unset or unreachable).[/dim]"
        )


def cmd_prod_services_survey_report(args) -> None:
    """Compare the two most recent disk surveys for an inventory and summarise
    the change: per-host free-disk delta, total node-data delta, and the
    services added/removed/reset between them (from the audit trail). This is
    the 'today vs yesterday' read-back over the stored survey history."""
    console = Console()
    repo = registry.get_prod_fleet_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set — no survey "
            "history to report. Run `prod services disk-survey` with a database."
        )
        sys.exit(1)

    inventory_name = _inventory_name(args.inventory_path)
    surveys = repo.latest_disk_surveys(inventory_name, limit=2)
    if not surveys:
        console.print(f"[yellow]No surveys recorded for {inventory_name}.[/yellow]")
        return

    latest = surveys[0]
    console.print(
        f"[bold]{inventory_name}[/bold] latest survey "
        f"[cyan]{latest.surveyed_at.isoformat()}[/cyan]: "
        f"{latest.service_count} services on {latest.host_count} hosts, "
        f"total node data {_format_bytes(latest.total_data_bytes)}."
    )
    if len(surveys) < 2:
        console.print("[dim]Only one survey on record — nothing to compare yet.[/dim]")
        return

    prev = surveys[1]
    console.print(
        f"[dim]Comparing against previous survey {prev.surveyed_at.isoformat()}.[/dim]"
    )

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
            (("+" if delta >= 0 else "") + _format_bytes(delta)) if delta is not None else "?",
        )
    console.print(dtable)
    console.print(
        f"[dim]Total node data: {_format_bytes(prev.total_data_bytes)} → "
        f"{_format_bytes(latest.total_data_bytes)} "
        f"(Δ {_format_bytes(latest.total_data_bytes - prev.total_data_bytes)}).[/dim]"
    )

    # Audit events between the two surveys.
    events = repo.list_service_audit(
        inventory_name, since=prev.surveyed_at, until=latest.surveyed_at
    )
    if not events:
        console.print("[dim]No add/remove/reset events between the two surveys.[/dim]")
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
            e.host, e.service_name, e.action.value,
            _format_bytes(e.size_bytes) if e.size_bytes is not None else "",
        )
    console.print(etable)
    console.print(
        f"[green]Reclaimed via removals in this window: {_format_bytes(reclaimed)}.[/green]"
    )


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
        _inventory_name(args.inventory_path), vm.name, args.service_name,
        ProdServiceAuditAction.REMOVED, datetime.now(timezone.utc),
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
        _inventory_name(args.inventory_path), vm.name, args.service_name,
        ProdServiceAuditAction.RESET, datetime.now(timezone.utc),
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
            f"[bold red]Error:[/bold red] {args.service_name} already exists "
            f"on {vm.name}."
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

    add_command = (
        f"mkdir -p {root_dir} {log_dir}\n"
        + build_write_unit_and_start_command(args.service_name, unit_content)
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
        _inventory_name(args.inventory_path), vm.name, args.service_name,
        ProdServiceAuditAction.ADDED, datetime.now(timezone.utc),
    )
