"""Reclaim disk held open by the Elasticsearch log forwarder, fleet-wide.

``telegraf-ant-elasticsearch`` tails the node logs with inotify and never
closes an fd, so every rotated log the prune deletes stays allocated on disk
for the lifetime of the process. See
:mod:`saorsa_deploy.provisioning.telegraf_restart` for the mechanism and the
measured scale.

This command surveys every host in an inventory for how much unlinked disk its
forwarder is holding, shows what a restart would return, and — once confirmed —
restarts them and reports the space actually reclaimed.

Restarts run in parallel, unlike the strictly-sequential drain in ``prod
services rm``. The two are not comparable operations: a drain permanently
deletes a node's records and has to let the network re-replicate between each
removal, whereas this deletes nothing and puts no load on the network at all.
The bound exists only so the whole fleet's log shipping does not stop in the
same second, which would put a synchronised hole in Elasticsearch and then
reconnect 66 forwarders at once.
"""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from saorsa_deploy.analysis.json_out import console_for, emit_json
from saorsa_deploy.inventory import NodeVm, load_node_inventory
from saorsa_deploy.provisioning.telegraf_restart import (
    LOG_FORWARDER_SERVICE,
    build_probe_command,
    build_restart_command,
    parse_df,
    parse_held,
    parse_restarted,
    parse_state,
)

#: Probing runs `lsof` over one process; restarting waits on systemd. Neither
#: is the multi-minute `du` walk the disk survey does, so these deadlines are
#: far shorter than the survey's.
PROBE_TIMEOUT_SECS = 60
RESTART_TIMEOUT_SECS = 120

_SSH_OPTS = [
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=20",
]

#: Parallelism for the measure pass. Read-only and cheap, so it matches the
#: disk survey's fan-out.
DEFAULT_PROBE_PARALLEL = 16

#: Parallelism for the restart pass — deliberately lower. See the module
#: docstring: this is about not blinding Elasticsearch fleet-wide at once, not
#: about protecting the hosts.
DEFAULT_RESTART_PARALLEL = 8

_GIB = 1024**3


def _format_bytes(size: int) -> str:
    sign = "-" if size < 0 else ""
    value = float(abs(size))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            body = f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
            return sign + body
        value /= 1024
    return f"{sign}{value:.1f} TiB"


def _resolve_ssh_key(vm: NodeVm, override: str | None) -> str:
    raw = override or vm.ssh_key
    return str(Path(raw).expanduser())


def _ssh_capture(
    vm: NodeVm, ssh_key: str, command: str, timeout: int
) -> tuple[bool, list[str], str]:
    """One-shot SSH returning ``(ok, stdout_lines, error)``. Never raises.

    Mirrors ``prod_services._survey_one_host``'s bare-subprocess approach rather
    than pyinfra: these are single short probes across many hosts in parallel,
    which is what pyinfra's per-host connection setup makes expensive.
    """
    try:
        result = subprocess.run(
            ["ssh", "-i", ssh_key, *_SSH_OPTS, f"{vm.ssh_user}@{vm.ip}", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, [], f"timed out after {timeout}s"
    except OSError as e:
        return False, [], str(e)
    if result.returncode != 0:
        return False, [], (result.stderr.strip() or f"exit {result.returncode}")
    return True, result.stdout.splitlines(), ""


def _probe_one_host(vm: NodeVm, ssh_key: str) -> dict:
    """Measure one host. Never raises — a failure is returned as ``error``."""
    ok, lines, err = _ssh_capture(vm, ssh_key, build_probe_command(), PROBE_TIMEOUT_SECS)
    if not ok:
        return {"vm": vm, "error": err}
    return {
        "vm": vm,
        "state": parse_state(lines),
        "held": parse_held(lines),
        "disk": parse_df(lines),
    }


def _restart_one_host(vm: NodeVm, ssh_key: str) -> dict:
    """Restart one host's forwarder, returning before/after figures."""
    ok, lines, err = _ssh_capture(vm, ssh_key, build_restart_command(), RESTART_TIMEOUT_SECS)
    if not ok:
        return {"vm": vm, "error": err}
    if not parse_restarted(lines):
        return {
            "vm": vm,
            "error": f"systemctl restart {LOG_FORWARDER_SERVICE} did not succeed",
        }
    return {
        "vm": vm,
        "held": parse_held(lines),
        "disk_before": parse_df(lines),
        "disk_after": parse_df(lines, after=True),
    }


def _select_vms(args, console: Console) -> tuple[list[NodeVm], str, Path]:
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
            console.print(
                f"[bold red]Error:[/bold red] --limit-to matched no VMs ({sorted(wanted)})"
            )
            sys.exit(1)
    if not vms:
        console.print("[bold red]Error:[/bold red] no hosts to act on after filters.")
        sys.exit(1)

    try:
        name = json.loads(inv_path.read_text()).get("testnet_id") or inv_path.stem
    except (ValueError, OSError):
        name = inv_path.stem
    return vms, name, inv_path


def _run_pass(fn, vms, ssh_key_for, max_parallel: int) -> list[dict]:
    results: list[dict] = []
    workers = min(max(1, max_parallel), len(vms))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fn, vm, ssh_key_for(vm)) for vm in vms]
        for fut in as_completed(futures):
            results.append(fut.result())
    return results


def _short(name: str, inventory_name: str) -> str:
    """Drop the inventory prefix every host shares.

    Rich truncates the host column otherwise, and ``ant-prod-01-ovh-GR…`` hides
    exactly the part that identifies the host while keeping the part that is
    the same on all 66 rows.
    """
    prefix = f"{inventory_name}-"
    return name[len(prefix) :] if name.startswith(prefix) else name


def _print_probe_table(
    console: Console, results: list[dict], min_held: int, inventory_name: str
) -> None:
    table = Table(title=f"{LOG_FORWARDER_SERVICE} — unlinked disk held open")
    table.add_column("Host")
    table.add_column("Provider")
    table.add_column("State")
    table.add_column("Held", justify="right")
    table.add_column("fds", justify="right")
    table.add_column("Free now", justify="right")
    table.add_column("Free if restarted", justify="right")

    ok = [r for r in results if "error" not in r]
    # Tightest host first: the whole point is which hosts the reclaim rescues,
    # and that is ordered by what they have left, not by what they are holding.
    ok.sort(key=lambda r: (r["disk"] or {}).get("free_bytes", 1 << 62))
    for r in ok:
        held = (r["held"] or {}).get("held_bytes", 0)
        fds = (r["held"] or {}).get("fds", 0)
        free = (r["disk"] or {}).get("free_bytes")
        state = (r["state"] or {}).get("state", "?")
        selected = held >= min_held
        style = None if selected else "dim"
        table.add_row(
            _short(r["vm"].name, inventory_name),
            r["vm"].provider,
            state if state == "active" else f"[yellow]{state}[/yellow]",
            _format_bytes(held),
            str(fds),
            _format_bytes(free) if free is not None else "-",
            _format_bytes(free + held) if free is not None else "-",
            style=style,
        )
    console.print(table)



def _emit_probe_json(args, console, inventory_name, results, min_held, total_held) -> None:
    """Write the per-host probe as JSON when `--json-out` was given.

    The fleet-wide held total is the figure the disk survey's free-space column
    cannot be read without -- the forwarder holds pruned logs open at roughly
    261 MiB/host/day, which is most of the fleet's routine free-space erosion,
    and attributing it to chunk growth turns healthy hosts into drain
    candidates. A consumer wanting that number should not have to parse a Rich
    table to get it.

    Unreachable hosts are emitted with their error rather than dropped: a host
    missing from the probe makes the total short, and a consumer that cannot
    see the gap will read the short total as a real one.
    """
    json_out = getattr(args, "json_out", None)
    if not json_out:
        return
    hosts = []
    for r in results:
        held = r.get("held") or {}
        hosts.append({
            "host": r["vm"].name,
            "provider": getattr(r["vm"], "provider", None),
            "error": r.get("error"),
            "held_bytes": held.get("held_bytes"),
            "fds": held.get("fds"),
            "free_bytes": held.get("free_bytes"),
            "state": held.get("state"),
        })
    emit_json(
        {
            "inventory": inventory_name,
            "probed_at": datetime.now(timezone.utc).isoformat(),
            "dry_run": bool(getattr(args, "dry_run", False)),
            "min_held_bytes": min_held,
            "host_count": len(results),
            "reachable_count": sum(1 for r in results if "error" not in r),
            "total_held_bytes": total_held,
            "hosts": hosts,
        },
        json_out,
        console,
    )


def cmd_prod_restart_telegraf(args) -> None:
    """Restart the ES log forwarder across a production fleet to reclaim the
    disk it is holding in deleted log files."""
    console = console_for(getattr(args, "json_out", None))

    vms, inventory_name, inv_path = _select_vms(args, console)
    min_held = int(round(float(getattr(args, "min_held_gb", 0) or 0) * _GIB))

    def ssh_key_for(vm):
        return _resolve_ssh_key(vm, args.ssh_key_path)

    console.print(
        f"[bold]prod restart-telegraf[/bold] — probing {len(vms)} host(s) "
        f"from [cyan]{inventory_name}[/cyan]"
    )
    console.print(f"  Inventory: {inv_path}")
    if args.provider:
        console.print(f"  Provider filter: {args.provider}")
    if getattr(args, "limit_to", None):
        console.print(f"  Limit to: {args.limit_to}")
    console.print()

    probe_parallel = int(getattr(args, "max_parallel", None) or DEFAULT_PROBE_PARALLEL)
    results = _run_pass(_probe_one_host, vms, ssh_key_for, probe_parallel)

    failures = [r for r in results if "error" in r]
    reachable = [r for r in results if "error" not in r]
    _print_probe_table(console, results, min_held, inventory_name)

    if failures:
        console.print(f"[yellow]{len(failures)} host(s) could not be probed:[/yellow]")
        for r in failures:
            console.print(f"  [yellow]- {r['vm'].name}: {r['error']}[/yellow]")

    total_held = sum((r["held"] or {}).get("held_bytes", 0) for r in reachable)
    console.print(
        f"\n{len(reachable)} host(s) probed, holding "
        f"[bold]{_format_bytes(total_held)}[/bold] in deleted files."
    )

    candidates = [r for r in reachable if (r["held"] or {}).get("held_bytes", 0) >= min_held]
    _emit_probe_json(args, console, inventory_name, results, min_held, total_held)
    if min_held > 0:
        console.print(
            f"{len(candidates)} host(s) at or above the "
            f"--min-held-gb {getattr(args, 'min_held_gb', 0)} threshold."
        )

    if getattr(args, "dry_run", False):
        console.print("[dim]--dry-run: nothing restarted.[/dim]")
        return

    if not candidates:
        console.print("Nothing to restart.")
        return

    candidate_held = sum((r["held"] or {}).get("held_bytes", 0) for r in candidates)
    console.print(
        f"Restarting [bold]{LOG_FORWARDER_SERVICE}[/bold] on "
        f"{len(candidates)} host(s) would reclaim ~{_format_bytes(candidate_held)}."
    )
    if not args.yes:
        try:
            answer = input(f"Proceed on {len(candidates)} host(s)? [y/N] ")
        except EOFError:
            console.print("[bold red]Aborted.[/bold red]")
            sys.exit(1)
        if answer.strip().lower() not in ("y", "yes"):
            console.print("[bold red]Aborted.[/bold red]")
            sys.exit(1)

    restart_parallel = int(getattr(args, "restart_parallel", None) or DEFAULT_RESTART_PARALLEL)
    console.print(
        f"\nRestarting, {restart_parallel} at a time "
        "(bounded so fleet log shipping does not stop all at once)..."
    )
    restart_results = _run_pass(
        _restart_one_host,
        [r["vm"] for r in candidates],
        ssh_key_for,
        restart_parallel,
    )

    table = Table(title="Reclaimed")
    table.add_column("Host")
    table.add_column("Held", justify="right")
    table.add_column("Free before", justify="right")
    table.add_column("Free after", justify="right")
    table.add_column("Reclaimed", justify="right")

    ok, bad = [], []
    total_reclaimed = 0
    for r in restart_results:
        if "error" in r:
            bad.append(r)
            continue
        ok.append(r)
        before = (r["disk_before"] or {}).get("free_bytes")
        after = (r["disk_after"] or {}).get("free_bytes")
        held = (r["held"] or {}).get("held_bytes", 0)
        reclaimed = (after - before) if (before is not None and after is not None) else None
        if reclaimed:
            total_reclaimed += reclaimed
        table.add_row(
            _short(r["vm"].name, inventory_name),
            _format_bytes(held),
            _format_bytes(before) if before is not None else "-",
            _format_bytes(after) if after is not None else "-",
            _format_bytes(reclaimed) if reclaimed is not None else "-",
        )

    console.print(table)
    console.print(
        f"\n[bold]Summary[/bold] — {LOG_FORWARDER_SERVICE} restarted on "
        f"{len(ok)} host(s), reclaiming {_format_bytes(total_reclaimed)}."
    )
    # The freed space is measured immediately after systemd reports the restart
    # done, so a host whose old process is still exiting can under-report. It
    # shows up as reclaimed < held rather than as an error.
    if total_reclaimed < candidate_held * 0.9:
        console.print(
            "[dim]Reclaimed is below the held figure on some hosts — the old "
            "process may still have been exiting when df was read. Re-run with "
            "--dry-run to see the settled figures.[/dim]"
        )
    if bad:
        console.print(f"  [red]FAILED:[/red] {len(bad)}")
        for r in bad:
            console.print(f"    [red]- {r['vm'].name}: {r['error']}[/red]")
        sys.exit(1)
