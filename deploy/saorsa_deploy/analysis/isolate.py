"""`analysis isolate` — cut a cohort of node VMs off the network, then restore it.

A run that needs nodes to *lose the network* (V2-1085: the runtime
re-bootstrap fallback only fires on a node that has no connections at all)
drives the cut from here so the window edges are recorded per VM rather than
reconstructed from logs, and so applying and lifting are the same idempotent
operation on every host.

The cut is an nftables table, ``inet ant_isolate``, that drops every UDP
datagram on the ``input``, ``output`` **and** ``forward`` hooks. Three hooks
because a NAT'd VM runs its nodes inside the ``ant_nat`` namespace: their
traffic reaches the host over a veth and is *forwarded*, so an input/output
rule alone would leave the NAT tier connected. Only UDP is dropped -- ant-node
speaks QUIC, while SSH, Telegraf → InfluxDB and log forwarding → Elasticsearch
are TCP and stay up, so the run keeps observing the VM it cut. DNS (UDP 53) is
exempt so those TCP reconnections can still resolve names.

Applying is idempotent (a second apply is a no-op that still records the
timestamp), lifting deletes the table if present, and ``--status`` only reads.
The QUIC idle timeout is 30 s, so a cut must be held longer than that before
every connection is gone.
"""

# ruff: noqa: E501 -- the remote bash payloads are kept verbatim.
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from saorsa_deploy.analysis import hosts, remote
from saorsa_deploy.cmd.analysis import add_deployment_args, add_host_selection_args

TABLE = "inet ant_isolate"

RULESET = """table inet ant_isolate {
  chain input   { type filter hook input   priority -10; policy accept; udp sport 53 accept; udp dport 53 accept; meta l4proto udp drop; }
  chain output  { type filter hook output  priority -10; policy accept; udp sport 53 accept; udp dport 53 accept; meta l4proto udp drop; }
  chain forward { type filter hook forward priority -10; policy accept; udp sport 53 accept; udp dport 53 accept; meta l4proto udp drop; }
}
"""

# $1 = ip, $2 = action (apply|lift|status). Prints one JSON record and STATUS=done.
ISOLATE_SCRIPT = r"""set -u
IP="$1"; ACTION="$2"
HOST=$(hostname)
present() { nft list table inet ant_isolate >/dev/null 2>&1 && echo true || echo false; }
BEFORE=$(present)
RC=0
case "$ACTION" in
  apply)
    if [ "$BEFORE" = false ]; then
      nft -f - <<'NFT' || RC=$?
__RULESET__
NFT
    fi
    ;;
  lift)
    if [ "$BEFORE" = true ]; then
      nft delete table inet ant_isolate || RC=$?
    fi
    ;;
  status) ;;
  *) echo "unknown action: $ACTION" >&2; RC=2 ;;
esac
AFTER=$(present)
TS=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
printf '{"ip":"%s","host":"%s","action":"%s","present_before":%s,"present_after":%s,"ts_utc":"%s","rc":%s}\n' \
  "$IP" "$HOST" "$ACTION" "$BEFORE" "$AFTER" "$TS" "$RC"
echo "STATUS=done"
""".replace("__RULESET__", RULESET.rstrip("\n"))


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def expected_present(action: str) -> bool | None:
    """The table state each action must leave behind (``None`` for status)."""
    return {"apply": True, "lift": False, "status": None}[action]


def parse_record(res: remote.RemoteResult) -> dict | None:
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def isolate_vm(vm: hosts.Vm, action: str) -> dict:
    local_start = _utc()
    res = remote.run_script(vm, ISOLATE_SCRIPT, [vm.ip, action], timeout=60)
    rec = parse_record(res)
    if rec is None:
        rec = {
            "ip": vm.ip,
            "host": None,
            "action": action,
            "present_before": None,
            "present_after": None,
            "ts_utc": None,
            "rc": res.returncode or 1,
            "error": (res.stderr or res.stdout).strip()[-400:],
        }
    rec["nat"] = vm.nat
    rec["local_start_utc"] = local_start
    rec["local_end_utc"] = _utc()
    return rec


def cmd_isolate(args: argparse.Namespace) -> int:
    console = Console()
    action = "apply" if args.apply else "lift" if args.lift else "status"
    vms = hosts.select_vms(args.name, args.ips, args.inventory, tier=getattr(args, "tier", "all"))
    if not vms:
        console.print("[bold red]no VMs selected[/bold red]")
        return 1
    records = remote.run_parallel(vms, lambda vm: isolate_vm(vm, action))
    want = expected_present(action)
    for rec in records:
        rec["tag"] = args.tag
        rec["ok"] = rec.get("rc") == 0 and (want is None or rec.get("present_after") is want)

    out_path = Path(args.out or f"isolate-{args.tag}.jsonl").expanduser()
    with out_path.open("a") as fh:
        for rec in records:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        fh.write(
            json.dumps(
                {
                    "tag": args.tag,
                    "action": action,
                    "vms": len(records),
                    "failed": sum(1 for r in records if not r["ok"]),
                    "first_ts_utc": min(
                        (r["ts_utc"] for r in records if r.get("ts_utc")), default=None
                    ),
                    "last_ts_utc": max(
                        (r["ts_utc"] for r in records if r.get("ts_utc")), default=None
                    ),
                },
                separators=(",", ":"),
            )
            + "\n"
        )

    table = Table(title=f"isolate {action} tag={args.tag}")
    for col in ("ip", "nat", "before", "after", "ts (remote UTC)", "ok"):
        table.add_column(col)
    for rec in records:
        table.add_row(
            rec["ip"],
            "nat" if rec["nat"] else "plain",
            str(rec.get("present_before")),
            str(rec.get("present_after")),
            str(rec.get("ts_utc")),
            "[green]yes[/green]"
            if rec["ok"]
            else f"[bold red]no[/bold red] {rec.get('error', '')}",
        )
    console.print(table)
    console.print(f"records appended to {out_path}")
    failed = [r["ip"] for r in records if not r["ok"]]
    if failed:
        console.print(
            f"[bold red]{len(failed)} VM(s) not in the expected state: {', '.join(failed)}[/bold red]"
        )
        return 1
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "isolate",
        help=(
            "Drop all UDP (input, output and forward -- covers the ant_nat namespace) on "
            "the given node VMs, or lift the drop, recording the UTC edge per VM. SSH, "
            "Telegraf and log forwarding are TCP and stay up."
        ),
    )
    add_deployment_args(p)
    add_host_selection_args(p, require_ips=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true", help="Install the UDP drop.")
    mode.add_argument("--lift", action="store_true", help="Remove the UDP drop.")
    mode.add_argument("--status", action="store_true", help="Report whether the drop is present.")
    p.add_argument("--tag", required=True, help="Stamped on every record.")
    p.add_argument("--out", default=None, help="JSONL output (default isolate-<tag>.jsonl).")
    p.set_defaults(func=cmd_isolate)
