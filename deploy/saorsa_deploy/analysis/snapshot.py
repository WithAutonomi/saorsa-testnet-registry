"""`analysis snapshot` — per-VM counter snapshot, both network namespaces.

Why this exists rather than relying on InfluxDB alone: /proc/net/snmp is PER
NETWORK NAMESPACE. NAT-simulated VMs run every ant-node service inside the
`ant_nat` namespace, while telegraf runs in the host namespace and reads the
host's counters -- which show 0 UDP datagrams, because the host only forwards
and MASQUERADEs that traffic (measured on dev-01-ant-node-sbg5-2: host
namespace OutDatagrams 0, ant_nat namespace 134243).

So the fleet-wide Influx series covers only the plain-tier VMs. This snapshot
reads BOTH namespaces over SSH, giving exact per-VM deltas across a phase for
every VM in both tiers, from counters that cannot be diluted by telegraf's own
TCP shipping. Inside the namespace there is no telegraf at all, so veth-node's
byte counters are pure node traffic.

Datagram counts come from /proc/net/snmp (which has no byte counters) and byte
counts from /proc/net/dev; both are monotonic since boot, so a phase figure is
the difference between two snapshots taken at the phase boundaries.

The remote payload is verbatim from the V2-996/V2-988 runs.
"""

# ruff: noqa: E501 -- the remote bash payloads are kept verbatim from the proven scripts.
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.analysis import hosts, remote
from saorsa_deploy.cmd.analysis import (
    add_deployment_args,
    add_host_selection_args,
)

SNAPSHOT_SCRIPT = r"""set -u
# /proc/net/snmp Udp columns: $2 InDatagrams $3 NoPorts $4 InErrors
# $5 OutDatagrams $6 RcvbufErrors. RcvbufErrors is carried because the NAT'd
# VMs are already showing 2.2% of inbound datagrams dropped to receive-buffer
# overflow, which is a connection-health signal in its own right and one the
# patched arm could plausibly move.
snmp_udp() { awk '/^Udp:/{getline; print $2" "$5" "$6}' /proc/net/snmp 2>/dev/null; }
dev_bytes() { awk -v i="$1" '$0 ~ i":" {gsub(/.*:/,""); print $1" "$9}' /proc/net/dev 2>/dev/null; }

HOST=$(hostname)
NOW=$(date -u +%s)
IFACE=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1); exit}')

HU=$(snmp_udp); HD=$(dev_bytes "$IFACE")
if ip netns list 2>/dev/null | awk '{print $1}' | grep -qx ant_nat; then
  NAT=1
  NU=$(ip netns exec ant_nat awk '/^Udp:/{getline; print $2" "$5" "$6}' /proc/net/snmp 2>/dev/null)
  ND=$(ip netns exec ant_nat awk '$0 ~ /veth-node:/ {gsub(/.*:/,""); print $1" "$9}' /proc/net/dev 2>/dev/null)
else
  NAT=0; NU="0 0 0"; ND="0 0"
fi

set -- $HU; HUIN=${1:-0}; HUOUT=${2:-0}; HURCV=${3:-0}
set -- $HD; HDRX=${1:-0}; HDTX=${2:-0}
set -- $NU; NUIN=${1:-0}; NUOUT=${2:-0}; NURCV=${3:-0}
set -- $ND; NDRX=${1:-0}; NDTX=${2:-0}

printf '{"host":"%s","ts":%s,"nat":%s,"iface":"%s","host_udp_in":%s,"host_udp_out":%s,"host_rx_bytes":%s,"host_tx_bytes":%s,"host_udp_rcvbuf_err":%s,"ns_udp_in":%s,"ns_udp_out":%s,"ns_udp_rcvbuf_err":%s,"ns_rx_bytes":%s,"ns_tx_bytes":%s}\n' \
  "$HOST" "$NOW" "$NAT" "$IFACE" "$HUIN" "$HUOUT" "$HDRX" "$HDTX" "$HURCV" "$NUIN" "$NUOUT" "$NURCV" "$NDRX" "$NDTX"
"""


def snapshot_vm(vm: hosts.Vm, tag: str) -> dict:
    """Take one snapshot; an unreachable VM yields an error record, never a crash."""
    res = remote.run_script(vm, SNAPSHOT_SCRIPT, timeout=90)
    line = res.last_line
    if res.ok and line.startswith("{"):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            rec = None
        if rec is not None:
            rec["ip"] = vm.ip
            rec["tag"] = tag
            return rec
    return {"ip": vm.ip, "tag": tag, "error": "unreachable"}


def cmd_snapshot(args: argparse.Namespace) -> int:
    console = Console(stderr=True)
    # Bootstraps are the busiest relay servers on a testnet and hold plain
    # per-session sockets of their own; the V2-834 reconciliation needs them
    # in the kernel-side denominator as much as the node tier (V2-1274 found
    # the default `("node",)` left 7 of 13 VMs out).
    vms = hosts.select_vms(
        args.name, args.ips, args.inventory, tier=args.tier, roles=("node", "bootstrap")
    )
    if not vms:
        console.print("[bold red]Error:[/bold red] no node VMs matched.")
        return 1
    records = remote.run_parallel(vms, lambda vm: snapshot_vm(vm, args.tag))
    out = None
    if args.out and args.out != "-":
        out = Path(args.out).expanduser().open("a")
    try:
        for rec in records:
            line = json.dumps(rec, separators=(",", ":"))
            sys.stdout.write(line + "\n")
            if out:
                out.write(line + "\n")
    finally:
        if out:
            out.close()
    unreachable = [r["ip"] for r in records if "error" in r]
    console.print(
        f"snapshot tag={args.tag}: {len(records) - len(unreachable)}/{len(records)} VMs"
        + (f"; unreachable: {', '.join(unreachable)}" if unreachable else "")
    )
    return 1 if unreachable else 0


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "snapshot",
        help=(
            "Snapshot /proc UDP and byte counters on node VMs in BOTH network "
            "namespaces (NAT'd nodes live in ant_nat, invisible to telegraf). "
            "One JSON line per VM; take one at every phase boundary."
        ),
    )
    add_deployment_args(p)
    add_host_selection_args(p)
    p.add_argument("--tag", required=True, help="Label stamped on every record (e.g. p1-start).")
    p.add_argument("--out", default=None, help="Append the JSON lines to this file as well.")
    p.set_defaults(func=cmd_snapshot)
