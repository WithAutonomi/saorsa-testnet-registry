"""`analysis restart-nodes` — graceful cohort restart with measured stop cost.

A run that needs a cohort of nodes to go through their shutdown path (V2-1061:
every published relay on a restarting node is a *requested* teardown) drives
the restarts from here so that the window edges are recorded rather than
reconstructed, and so the shutdown cost is measured directly.

Stop and start are issued separately rather than as `systemctl restart`: the
time `systemctl stop` takes to return is exactly how long the service took to
exit after SIGTERM -- the figure a change to the shutdown path is gated on --
and a combined restart would fold the start-up into it. `systemctl stop` sends
SIGTERM, which ant-node handles by running its graceful shutdown.

VMs are restarted in the given order with a pause between VMs (not between
services on one VM), so the fleet sees a staggered wave rather than a cliff.
"""

# ruff: noqa: E501 -- the remote bash payloads are kept verbatim from the proven scripts.
from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from saorsa_deploy.analysis import hosts, remote
from saorsa_deploy.cmd.analysis import add_deployment_args, add_host_selection_args

RESTART_SCRIPT = r"""set -u
IP="$1"; shift
HOST=$(hostname)
for SVC in "$@"; do
  T0=$(date -u +%s.%N)
  systemctl stop "$SVC"
  T1=$(date -u +%s.%N)
  systemctl start "$SVC"
  T2=$(date -u +%s.%N)
  ACTIVE=$(systemctl is-active "$SVC" 2>/dev/null || true)
  STOP_S=$(awk -v a="$T0" -v b="$T1" 'BEGIN{printf "%.3f", b-a}')
  printf '{"ip":"%s","host":"%s","service":"%s","stop_start":%s,"stop_end":%s,"start_end":%s,"stop_seconds":%s,"active":"%s"}\n' \
    "$IP" "$HOST" "$SVC" "$T0" "$T1" "$T2" "$STOP_S" "$ACTIVE"
done
echo "STATUS=done"
"""


def _utc(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def restart_vm(vm: hosts.Vm, services: list[str]) -> tuple[list[dict], remote.RemoteResult]:
    res = remote.run_script(
        vm, RESTART_SCRIPT, [vm.ip, *services], timeout=120 * max(1, len(services))
    )
    records = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records, res


def cmd_restart_nodes(args: argparse.Namespace) -> int:
    console = Console()
    vms = hosts.select_vms(args.name, args.ips, args.inventory)
    out_path = Path(args.out or f"restart-{args.tag}.jsonl").expanduser()
    lines: list[dict] = []
    vm_rows = []
    all_services: list[dict] = []
    first_stop: float | None = None
    last_start: float | None = None

    for i, vm in enumerate(vms):
        if i and args.stagger_seconds:
            time.sleep(args.stagger_seconds)
        services = list(args.services) if args.services else list(vm.services)
        if not services:
            console.print(f"[yellow]{vm.ip}: no services in inventory; skipped[/yellow]")
            continue
        vm_start = _utc()
        records, res = restart_vm(vm, services)
        vm_end = _utc()
        if not records:
            console.print(
                f"[bold red]{vm.ip}: no service records returned[/bold red] {res.stderr.strip()}"
            )
        for rec in records:
            rec["tag"] = args.tag
            lines.append(rec)
            all_services.append(rec)
            first_stop = min(first_stop, rec["stop_start"]) if first_stop else rec["stop_start"]
            last_start = max(last_start, rec["start_end"]) if last_start else rec["start_end"]
        vm_rec = {
            "ip": vm.ip,
            "tag": args.tag,
            "vm_start_utc": vm_start,
            "vm_end_utc": vm_end,
            "services": len(records),
        }
        lines.append(vm_rec)
        vm_rows.append((vm, records))

    run_rec = {
        "tag": args.tag,
        "vms": len(vm_rows),
        "services": len(all_services),
        "first_stop_start_utc": _utc(first_stop) if first_stop else None,
        "last_start_end_utc": _utc(last_start) if last_start else None,
    }
    lines.append(run_rec)

    with out_path.open("a") as fh:
        for rec in lines:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")

    table = Table(title=f"restart-nodes tag={args.tag}")
    for col in ("ip", "services", "stop s median", "stop s max", "not active"):
        table.add_column(col)
    inactive_total = 0
    for vm, records in vm_rows:
        stops = [r["stop_seconds"] for r in records]
        inactive = [r["service"] for r in records if r.get("active") != "active"]
        inactive_total += len(inactive)
        table.add_row(
            vm.ip,
            str(len(records)),
            f"{statistics.median(stops):.2f}" if stops else "-",
            f"{max(stops):.2f}" if stops else "-",
            ", ".join(inactive) if inactive else "",
        )
    console.print(table)
    console.print(
        f"window: {run_rec['first_stop_start_utc']} -> {run_rec['last_start_end_utc']}; "
        f"records appended to {out_path}"
    )
    if inactive_total:
        console.print(f"[bold red]{inactive_total} service(s) not active after restart[/bold red]")
        return 1
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "restart-nodes",
        help=(
            "Gracefully stop then start ant-node services on the given VMs in order, "
            "staggered per VM, recording UTC window edges and per-service stop duration."
        ),
    )
    add_deployment_args(p)
    add_host_selection_args(p, require_ips=True)
    p.add_argument(
        "--services",
        nargs="+",
        default=None,
        help="Service names to restart on each VM (default: every service in the inventory entry).",
    )
    p.add_argument(
        "--stagger-seconds", type=float, default=30, help="Pause between VMs (default 30)."
    )
    p.add_argument("--tag", required=True, help="Stamped on every record.")
    p.add_argument("--out", default=None, help="JSONL output (default restart-<tag>.jsonl).")
    p.set_defaults(func=cmd_restart_nodes)
