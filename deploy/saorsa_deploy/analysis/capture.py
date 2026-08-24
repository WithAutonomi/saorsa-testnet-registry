"""`analysis capture` / `capture-stream` / `fetch` — node UDP packet capture.

Why the capture launches through systemd-run (V2-988 post-mortem, 2026-08-15)
----------------------------------------------------------------------------
The original version ran tcpdump in the SSH session's foreground. That is fine
until the local wrapper dies -- an operator timeout, a dropped link, a
tool-imposed command ceiling -- at which point sshd tears the session down and
takes tcpdump with it. On V2-988 the decisive 20-minute idle window died 29
SECONDS in for exactly this reason, and `setsid nohup ... &` inside the ssh
command did NOT survive either. A transient systemd unit does: it is owned by
PID 1, not by the session, and was verified to keep writing after the SSH
connection closed.

The window this protects is unrepeatable. An idle window costs an hour of
loaded soak to set up, and once clients restart the network is not in that
state again -- so a capture that dies silently does not cost a retry, it costs
the phase. A truncated window is dangerous rather than obviously broken: it
yields an all-zero lifetime histogram (nothing can be born and die inside a
short window, so everything is censored) and a downward-biased cadence, both of
which read as findings. So every capture's actual span is checked against the
request and the command exits non-zero if any host is short.

Where it captures matters and differs by VM
-------------------------------------------
NAT'd VMs run every ant-node service inside the `ant_nat` network namespace,
reached over a veth pair. Capturing on the host's primary interface there would
see post-MASQUERADE traffic mixed with the host's own TCP (telegraf to
Elasticsearch and InfluxDB); capturing on `veth-node` INSIDE the namespace sees
exactly the node traffic and nothing else, because telegraf runs on the host.
Plain VMs capture on the default-route interface with a `udp` filter, which
already excludes telegraf's TCP. DNS and NTP are excluded explicitly so a
resolver chat cannot look like a short-lived flow.

Snaplen 96 keeps only headers -- pcap still records each datagram's true wire
length, so byte accounting and the small-packet keep-alive signature survive.

File vs stream
--------------
`capture` writes a pcap on the VM: use it where the capture window must equal
the measurement window (an idle window, a probe window). `capture-stream` pipes
tcpdump into the self-contained flowstat analyser on the VM and keeps only a
~1.5 KB JSON summary: use it for loaded phases, where a 10-minute pcap is
8-21 GB and would not fit the root-disk-only 3-AZ VMs (and its write traffic
would perturb the node I/O under measurement). Both report tcpdump's kernel
drop count -- drops inflate the measured inter-packet gaps.

The remote payloads are verbatim from the V2-996/V2-988 runs apart from the
file and unit name prefixes.
"""
# ruff: noqa: E501 -- the remote bash payloads are kept verbatim from the proven scripts.
from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from saorsa_deploy.analysis import hosts, remote
from saorsa_deploy.cmd.analysis import add_deployment_args, add_host_selection_args

# A capture shorter than this fraction of the request is treated as a failure
# rather than reported as a smaller window. 0.95 tolerates tcpdump's startup and
# the SIGTERM at the end; it does not tolerate a capture that died.
MIN_FRACTION = 95

REMOTE_PREFIX = "/var/tmp/saorsa-analysis"
REMOTE_FLOWSTAT = f"{REMOTE_PREFIX}-flowstat.py"
CAPTURE_UNIT = "saorsa-cap-{tag}"
STREAM_UNIT = "saorsa-stream-{tag}"

CAPTURE_START_SCRIPT = r"""set -u
DUR="$1"; TAG="$2"; UNIT="$3"
OUT="/var/tmp/saorsa-analysis-${TAG}.pcap"
ERR="/var/tmp/saorsa-analysis-${TAG}.tcpdump.err"

if ! command -v tcpdump >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q tcpdump >/dev/null 2>&1 \
    || { apt-get update -q >/dev/null 2>&1; \
         DEBIAN_FRONTEND=noninteractive apt-get install -y -q tcpdump >/dev/null 2>&1; } \
    || { echo "STATUS=no-tcpdump"; exit 1; }
fi

# A stale unit from an aborted attempt would make `systemd-run` fail and, worse,
# could still be writing to OUT. Clear both before starting.
systemctl reset-failed "$UNIT" >/dev/null 2>&1
systemctl stop "$UNIT" >/dev/null 2>&1
rm -f "$OUT" "$ERR"

if ip netns list 2>/dev/null | awk '{print $1}' | grep -qx ant_nat; then
  NS=1; IFACE=veth-node; PFX="ip netns exec ant_nat"
else
  NS=0; PFX=""
  IFACE=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1); exit}')
fi
[ -n "${IFACE:-}" ] || { echo "STATUS=no-iface"; exit 1; }

START=$(date -u +%s)
systemd-run --unit="$UNIT" --collect --property=Type=simple \
  bash -c "$PFX timeout $DUR tcpdump -i $IFACE -n -s 96 -w $OUT udp and not port 53 and not port 123 2>$ERR" \
  >/dev/null 2>&1 || { echo "STATUS=systemd-run-failed"; exit 1; }

# Prove it is actually capturing before reporting success. An active unit whose
# pcap is not growing is a capture that will yield nothing.
sleep 4
A=$(systemctl is-active "$UNIT" 2>/dev/null)
S1=$(stat -c %s "$OUT" 2>/dev/null || echo 0)
sleep 2
S2=$(stat -c %s "$OUT" 2>/dev/null || echo 0)
[ "$A" = "active" ] || { echo "STATUS=not-active ACTIVE=$A"; exit 1; }
[ "$S2" -gt "$S1" ] || { echo "STATUS=not-growing BYTES=$S2"; exit 1; }

echo "STATUS=started NS=$NS IFACE=$IFACE START=$START CLOCK=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
"""

CAPTURE_WAIT_SCRIPT = r"""set -u
DUR="$1"; TAG="$2"; UNIT="$3"
OUT="/var/tmp/saorsa-analysis-${TAG}.pcap"
ERR="/var/tmp/saorsa-analysis-${TAG}.tcpdump.err"

# Poll rather than `systemctl wait`: --wait cannot be attached after the fact,
# and this loop also survives the SSH link dropping mid-wait (the unit keeps
# running; a re-invocation just resumes polling).
DEADLINE=$(( $(date -u +%s) + DUR + 120 ))
while [ "$(systemctl is-active "$UNIT" 2>/dev/null)" = "active" ]; do
  [ "$(date -u +%s)" -ge "$DEADLINE" ] && break
  sleep 5
done

SZ=$(stat -c %s "$OUT" 2>/dev/null || echo 0)
DROPPED=$(grep -oE '[0-9]+ packets dropped by kernel' "$ERR" 2>/dev/null | grep -oE '^[0-9]+' | head -1)
RECVD=$(grep -oE '[0-9]+ packets received by filter' "$ERR" 2>/dev/null | grep -oE '^[0-9]+' | head -1)

# The authoritative span is the pcap's own first and last packet timestamps, not
# wall clock and not the file mtime: it is the only figure that reflects what
# the analyser will actually see.
SPAN=$(python3 - "$OUT" <<'PY' 2>/dev/null || echo "0 0 0"
import struct, sys
p = sys.argv[1]
try:
    fh = open(p, "rb")
except OSError:
    print("0 0 0"); raise SystemExit
h = fh.read(24)
if len(h) < 24:
    print("0 0 0"); raise SystemExit
magic = struct.unpack("<I", h[:4])[0]
if magic in (0xA1B2C3D4, 0xA1B23C4D):
    endian, nanos = "<", magic == 0xA1B23C4D
elif magic in (0xD4C3B2A1, 0x4D3CB2A1):
    endian, nanos = ">", magic == 0x4D3CB2A1
else:
    print("0 0 0"); raise SystemExit
rec = struct.Struct(endian + "IIII")
first = last = None
n = 0
while True:
    rh = fh.read(16)
    if len(rh) < 16:
        break
    s, f, incl, orig = rec.unpack(rh)
    if fh.read(incl) is None:
        break
    if incl and len(fh.read(0)) is None:
        break
    t = s + (f / 1e9 if nanos else f / 1e6)
    if first is None:
        first = t
    last = t
    n += 1
print(f"{first or 0:.0f} {last or 0:.0f} {n}")
PY
)
set -- $SPAN; FIRST=${1:-0}; LAST=${2:-0}; NPKT=${3:-0}
SPAN_S=$(( LAST - FIRST ))
echo "STATUS=done BYTES=$SZ PACKETS=$NPKT SPAN=$SPAN_S FIRST=$FIRST LAST=$LAST DROPPED=${DROPPED:-?} RECEIVED=${RECVD:-?}"
"""

STREAM_START_SCRIPT = r"""set -u
DUR="$1"; TAG="$2"; UNIT="$3"
ERR="/var/tmp/saorsa-analysis-${TAG}.tcpdump.err"
OUT="/var/tmp/saorsa-analysis-${TAG}.json"
LOG="/var/tmp/saorsa-analysis-${TAG}.log"

[ -f /var/tmp/saorsa-analysis-flowstat.py ] || { echo "STATUS=no-flowstat"; exit 1; }

systemctl reset-failed "$UNIT" >/dev/null 2>&1
systemctl stop "$UNIT" >/dev/null 2>&1
rm -f "$OUT" "$ERR" "$LOG"

if ip netns list 2>/dev/null | awk '{print $1}' | grep -qx ant_nat; then
  NS=1; IFACE=veth-node; PFX="ip netns exec ant_nat"
else
  NS=0; PFX=""
  IFACE=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1); exit}')
fi
[ -n "${IFACE:-}" ] || { echo "STATUS=no-iface"; exit 1; }

START=$(date -u +%s)
systemd-run --unit="$UNIT" --collect --property=Type=simple \
  bash -c "$PFX timeout $DUR tcpdump -i $IFACE -n -s 96 -w - udp and not port 53 and not port 123 2>$ERR | python3 /var/tmp/saorsa-analysis-flowstat.py - --label \$(hostname) --expect-seconds $DUR --json-out $OUT > $LOG 2>&1" \
  >/dev/null 2>&1 || { echo "STATUS=systemd-run-failed"; exit 1; }

sleep 5
A=$(systemctl is-active "$UNIT" 2>/dev/null)
[ "$A" = "active" ] || { echo "STATUS=not-active ACTIVE=$A"; exit 1; }
# The pipeline buffers, so there is no growing file to check here; instead
# confirm tcpdump itself is alive under the unit.
P=$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null)
[ -n "$P" ] && [ "$P" != "0" ] || { echo "STATUS=no-mainpid"; exit 1; }

echo "STATUS=started NS=$NS IFACE=$IFACE START=$START CLOCK=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
"""

STREAM_WAIT_SCRIPT = r"""set -u
DUR="$1"; TAG="$2"; UNIT="$3"
ERR="/var/tmp/saorsa-analysis-${TAG}.tcpdump.err"
OUT="/var/tmp/saorsa-analysis-${TAG}.json"
LOG="/var/tmp/saorsa-analysis-${TAG}.log"

DEADLINE=$(( $(date -u +%s) + DUR + 180 ))
while [ "$(systemctl is-active "$UNIT" 2>/dev/null)" = "active" ]; do
  [ "$(date -u +%s)" -ge "$DEADLINE" ] && break
  sleep 5
done

DROPPED=$(grep -oE '[0-9]+ packets dropped by kernel' "$ERR" 2>/dev/null | grep -oE '^[0-9]+' | head -1)
RECVD=$(grep -oE '[0-9]+ packets received by filter' "$ERR" 2>/dev/null | grep -oE '^[0-9]+' | head -1)
SPAN=$(python3 -c "
import json,sys
try:
    d=json.load(open('$OUT'))
    print('%.1f %s' % (d.get('window_seconds') or 0, d.get('window_complete')))
except Exception:
    print('0 None')
" 2>/dev/null)
set -- $SPAN; SPAN_S=${1:-0}; COMPLETE=${2:-None}
echo "STATUS=done SPAN=$SPAN_S COMPLETE=$COMPLETE DROPPED=${DROPPED:-?} RECEIVED=${RECVD:-?}"
"""

# Remote artefacts by kind, for `fetch`.
FETCH_KINDS = {
    "pcap": "{prefix}-{tag}.pcap",
    "json": "{prefix}-{tag}.json",
    "log": "{prefix}-{tag}.log",
    "err": "{prefix}-{tag}.tcpdump.err",
    "probe": "{prefix}-probe-{tag}.jsonl",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _field(line: str, key: str) -> str | None:
    m = re.search(rf"(?:^|\s){re.escape(key)}=(\S+)", line)
    return m.group(1) if m else None


def _run_capture(
    vms: list[hosts.Vm],
    seconds: int,
    tag: str,
    unit: str,
    start_script: str,
    wait_script: str,
    is_complete,
    label: str,
) -> int:
    console = Console()
    args = [str(seconds), tag, unit]
    console.print(f"== starting {label} ({seconds}s, tag={tag}) at {_now()}")
    started = remote.run_parallel(
        vms, lambda vm: remote.run_script(vm, start_script, args, timeout=seconds + 300)
    )
    bad = []
    for res in started:
        console.print(f"{res.vm.ip} {res.last_line}")
        if "STATUS=started" not in res.last_line:
            bad.append(f"{res.vm.ip} {res.last_line or res.stderr.strip()}")
    if bad:
        console.print("[bold red]FATAL: some hosts did not start capturing:[/bold red]")
        for b in bad:
            console.print(f"  {b}")
        console.print("Stop here and fix it -- the window is not repeatable.")
        return 1

    console.print(f"== all {len(vms)} host(s) capturing; waiting for completion")
    done = remote.run_parallel(
        vms, lambda vm: remote.run_script(vm, wait_script, args, timeout=seconds + 600)
    )
    console.print(f"== capture summary at {_now()}")
    rc = 0
    for res in done:
        line = res.last_line or "STATUS=no-result"
        ok, note = is_complete(line)
        console.print(f"{res.vm.ip} {line}  {note}")
        if not ok:
            rc = 1
    if rc:
        console.print(
            "\n[bold red]FATAL: at least one capture is short. Do NOT proceed to the next "
            "phase -- the measurement window did not happen on every sample VM.[/bold red]"
        )
    return rc


def cmd_capture(args: argparse.Namespace) -> int:
    vms = hosts.select_vms(args.name, args.ips, args.inventory)
    min_span = args.seconds * MIN_FRACTION // 100

    def complete(line: str):
        span = _field(line, "SPAN")
        try:
            span_s = int(span) if span is not None else 0
        except ValueError:
            span_s = 0
        if span_s < min_span:
            return False, f"<-- SHORT: spanned {span_s}s, expected >={min_span}s of {args.seconds}s"
        return True, f"ok({span_s}s)"

    return _run_capture(
        vms,
        args.seconds,
        args.tag,
        CAPTURE_UNIT.format(tag=args.tag),
        CAPTURE_START_SCRIPT,
        CAPTURE_WAIT_SCRIPT,
        complete,
        "captures",
    )


def flowstat_source() -> str:
    return (Path(__file__).parent / "flowstat.py").read_text()


def cmd_capture_stream(args: argparse.Namespace) -> int:
    console = Console()
    vms = hosts.select_vms(args.name, args.ips, args.inventory)
    source = flowstat_source()
    pushed = remote.run_parallel(vms, lambda vm: remote.push_file(vm, source, REMOTE_FLOWSTAT))
    failed = [r for r in pushed if "STATUS=pushed" not in r.stdout]
    if failed:
        console.print("[bold red]FATAL: could not push the flowstat analyser to:[/bold red]")
        for r in failed:
            console.print(f"  {r.vm.ip} {r.stderr.strip()}")
        return 1

    def complete(line: str):
        if "COMPLETE=True" in line:
            return True, "ok"
        return False, "<-- SHORT or MISSING: the measurement window did not happen here"

    return _run_capture(
        vms,
        args.seconds,
        args.tag,
        STREAM_UNIT.format(tag=args.tag),
        STREAM_START_SCRIPT,
        STREAM_WAIT_SCRIPT,
        complete,
        "streamed captures",
    )


def cmd_fetch(args: argparse.Namespace) -> int:
    console = Console()
    vms = hosts.select_vms(args.name, args.ips, args.inventory)
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    unknown = [k for k in kinds if k not in FETCH_KINDS]
    if unknown:
        console.print(f"[bold red]Error:[/bold red] unknown kind(s): {', '.join(unknown)}")
        return 1
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    def fetch_vm(vm: hosts.Vm) -> list[tuple[str, str, bool, str]]:
        rows = []
        for kind in kinds:
            remote_path = FETCH_KINDS[kind].format(prefix=REMOTE_PREFIX, tag=args.tag)
            ext = remote_path.split(f"-{args.tag}", 1)[-1].lstrip(".")
            if kind == "probe":
                ext = "probe.jsonl"
            local = out_dir / f"{vm.ip}-{args.tag}.{ext}"
            res = remote.fetch_file(vm, remote_path, local)
            rows.append((vm.ip, remote_path, res.ok, str(local) if res.ok else res.stderr.strip()))
        return rows

    fetched = 0
    missing = 0
    for rows in remote.run_parallel(vms, fetch_vm):
        for ip, remote_path, ok, detail in rows:
            if ok:
                fetched += 1
                console.print(f"{ip} {remote_path} -> {detail}")
            else:
                missing += 1
                console.print(f"{ip} {remote_path} [yellow]missing[/yellow] ({detail})")
    console.print(f"fetched {fetched} file(s); {missing} missing")
    return 0 if fetched else 1


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "capture",
        help=(
            "File pcap of node UDP on the given VMs through a transient systemd unit "
            "(survives the SSH session), blocking until done and verifying the span. "
            "Use for idle/probe windows where the capture must equal the window."
        ),
    )
    add_deployment_args(p)
    add_host_selection_args(p, require_ips=True)
    p.add_argument("--seconds", type=int, required=True, help="Capture duration.")
    p.add_argument("--tag", required=True, help="Names the pcap: /var/tmp/saorsa-analysis-<tag>.pcap.")
    p.set_defaults(func=cmd_capture)

    s = subparsers.add_parser(
        "capture-stream",
        help=(
            "Stream tcpdump into the flowstat analyser on the VM (nothing touches "
            "disk); only a JSON summary survives. Use for loaded phases."
        ),
    )
    add_deployment_args(s)
    add_host_selection_args(s, require_ips=True)
    s.add_argument("--seconds", type=int, required=True, help="Capture duration.")
    s.add_argument("--tag", required=True, help="Names the summary: /var/tmp/saorsa-analysis-<tag>.json.")
    s.set_defaults(func=cmd_capture_stream)

    f = subparsers.add_parser(
        "fetch",
        help="Copy capture/stream/probe artefacts for a tag back from the VMs.",
    )
    add_deployment_args(f)
    add_host_selection_args(f, require_ips=True)
    f.add_argument("--tag", required=True)
    f.add_argument("--out-dir", required=True, help="Local directory; files land as <ip>-<tag>.<ext>.")
    f.add_argument(
        "--kinds",
        default="pcap,json,log,err,probe",
        help="Comma-separated subset of pcap,json,log,err,probe (default: all).",
    )
    f.set_defaults(func=cmd_fetch)
