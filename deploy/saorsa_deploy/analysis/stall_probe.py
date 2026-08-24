"""`analysis stall-probe` — reproduce a per-peer outage on a live node VM.

Built for saorsa-transport #140 (V2-1030) to reproduce ADR-014's harness outage
on the fleet. On the target VM, picks <peers> peer IPs with live UDP flows
(sampled from a 30 s tcpdump), then for each peer in turn blocks UDP in BOTH
directions for <seconds> using iptables in the namespace the nodes live in
(ant_nat on NAT'd VMs, host otherwise), lifts the block, waits a resume window,
and moves on. Peers are probed sequentially so each probe's resume window is
uninterrupted. A JSONL log is written on the VM at
/var/tmp/saorsa-analysis-probe-<tag>.jsonl and echoed to stdout, ready for
`analysis probe-score` alongside an `analysis capture` pcap over the same span.

Blocking one peer IP blocks every flow between the two VMs (each hosts up to 15
nodes), so one probe scores several flows.

The block is bidirectional -- INPUT and OUTPUT -- to match the harness. Rules
are removed with the exact same spec they were added with, and a trap removes
them on any exit so a killed wrapper cannot leave a peer blocked.

Safety: no --force anywhere; rules are `-I` at the top and `-D` by spec;
`--dry-run` prints the rule commands without applying them (use in a preflight
to check the namespace/iptables path on the real VM before the decisive window,
then one real 3 s probe against a single peer as a smoke test).

The remote payload is verbatim from V2-1059 apart from the log path.
"""
# ruff: noqa: E501 -- the remote bash payloads are kept verbatim from the proven scripts.
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys

from rich.console import Console

from saorsa_deploy.analysis import hosts
from saorsa_deploy.cmd.analysis import add_deployment_args

PROBE_LOG_TEMPLATE = "/var/tmp/saorsa-analysis-probe-{tag}.jsonl"

STALL_PROBE_SCRIPT = r"""set -u
SECS="$1"; COUNT="$2"; TAG="$3"; DRY="$4"
LOG="/var/tmp/saorsa-analysis-probe-${TAG}.jsonl"
RESUME=20

if ip netns list 2>/dev/null | awk '{print $1}' | grep -qx ant_nat; then
  NS=1; PFX="ip netns exec ant_nat"; IFACE=veth-node
  LOCAL=$($PFX ip -4 -o addr show dev "$IFACE" | awk '{print $4}' | cut -d/ -f1 | head -1)
else
  NS=0; PFX=""
  IFACE=$(ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1); exit}')
  LOCAL=$(ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1); exit}')
fi
command -v iptables >/dev/null || { echo "STATUS=no-iptables"; exit 1; }
command -v tcpdump >/dev/null || { echo "STATUS=no-tcpdump"; exit 1; }

# Peer selection: remote IPs with the most distinct flows in a 30 s sample,
# excluding anything on our own /24 and the resolver/NTP ports.
PEERS=$($PFX timeout 30 tcpdump -i "$IFACE" -n -q "udp and not port 53 and not port 123" 2>/dev/null \
  | awk -v me="$LOCAL" '{
      split($3,a,"."); split($5,b,".");
      src=a[1]"."a[2]"."a[3]"."a[4]; dst=b[1]"."b[2]"."b[3]"."b[4]; sub(/:$/,"",dst);
      peer = (src==me) ? dst : src; if (peer==me) next;
      key = peer" "$3" "$5; if (!(key in seen)) { seen[key]=1; n[peer]++ }
    } END { for (p in n) print n[p], p }' \
  | sort -rn | head -n "$COUNT" | awk '{print $2}')
[ -n "$PEERS" ] || { echo "STATUS=no-peers"; exit 1; }
echo "STATUS=peers NS=$NS IFACE=$IFACE LOCAL=$LOCAL PEERS=$(echo $PEERS | tr ' ' ',')"

CUR=""
cleanup() {
  if [ -n "$CUR" ] && [ -z "$DRY" ]; then
    $PFX iptables -D INPUT  -p udp -s "$CUR" -j DROP 2>/dev/null
    $PFX iptables -D OUTPUT -p udp -d "$CUR" -j DROP 2>/dev/null
  fi
}
trap cleanup EXIT INT TERM

: > "$LOG"
for peer in $PEERS; do
  CUR=$peer
  BS=$(date -u +%s.%N)
  if [ -n "$DRY" ]; then
    echo "DRY: $PFX iptables -I INPUT -p udp -s $peer -j DROP ; $PFX iptables -I OUTPUT -p udp -d $peer -j DROP ; sleep $SECS ; -D both"
    CUR=""; continue
  fi
  $PFX iptables -I INPUT  -p udp -s "$peer" -j DROP
  $PFX iptables -I OUTPUT -p udp -d "$peer" -j DROP
  sleep "$SECS"
  $PFX iptables -D INPUT  -p udp -s "$peer" -j DROP
  $PFX iptables -D OUTPUT -p udp -d "$peer" -j DROP
  BE=$(date -u +%s.%N)
  CUR=""
  # Verify nothing is left behind for this peer.
  LEFT=$($PFX iptables -S 2>/dev/null | grep -c -- "$peer")
  printf '{"peer":"%s","block_start":%s,"block_end":%s,"seconds":%s,"rules_left":%s,"local":"%s","ns":%s}\n' \
    "$peer" "$BS" "$BE" "$SECS" "$LEFT" "$LOCAL" "$NS" | tee -a "$LOG"
  sleep "$RESUME"
done
echo "STATUS=done LOG=$LOG"
"""


def run_stall_probe(
    vm: hosts.Vm, seconds: int, peers: int, tag: str, dry_run: bool
) -> int:
    """Run the probe, streaming its stdout so block/lift lines appear live."""
    args = [str(seconds), str(peers), tag, "1" if dry_run else ""]
    quoted = " ".join(shlex.quote(a) for a in args)
    cmd = [*vm.ssh_args(), vm.target, f"bash -s -- {quoted}"]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(STALL_PROBE_SCRIPT)
    proc.stdin.close()
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
    return proc.wait()


def cmd_stall_probe(args: argparse.Namespace) -> int:
    console = Console(stderr=True)
    (vm,) = hosts.select_vms(args.name, [args.ip], args.inventory)
    console.print(
        f"stall probe on {vm.ip} ({'NAT' if vm.nat else 'plain'}): "
        f"{args.peers} peer(s) x {args.seconds}s"
        + (" [dry-run]" if args.dry_run else "")
    )
    rc = run_stall_probe(vm, args.seconds, args.peers, args.tag, args.dry_run)
    if rc != 0:
        console.print(f"[bold red]stall probe exited {rc}[/bold red]")
    return rc


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "stall-probe",
        help=(
            "Block UDP to/from N live peers of one node VM for S seconds each "
            "(iptables in the nodes' namespace, trap-cleaned), logging block "
            "windows for `analysis probe-score`. Use --dry-run first."
        ),
    )
    add_deployment_args(p)
    p.add_argument("--ip", required=True, help="Node VM to probe from (must be in the inventory).")
    p.add_argument("--seconds", type=int, required=True, help="Block length per peer.")
    p.add_argument("--peers", type=int, required=True, help="How many peers to probe, sequentially.")
    p.add_argument("--tag", required=True, help="Log tag: /var/tmp/saorsa-analysis-probe-<tag>.jsonl.")
    p.add_argument("--dry-run", action="store_true", help="Print the iptables commands; apply nothing.")
    p.set_defaults(func=cmd_stall_probe)
