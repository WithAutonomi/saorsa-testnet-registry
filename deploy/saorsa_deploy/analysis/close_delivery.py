"""Were peers told when a relay endpoint was torn down? (`analysis close-delivery`)

Built for saorsa-transport #138 / V2-1061 (ADR-013). When a NAT'd node discards
a relay allocation it closes the relay endpoint, which queues a CONNECTION_CLOSE
to every peer connected through the relay address. On 0.36.0 the tunnel was
destroyed first and the frame never left, so the peer -- the dialler, pinging
every 10 s -- kept sending into the void until its 30 s idle timeout.

The peer side logs no close reason, so this reads the fingerprint off a pcap
taken on a peer/relay-server VM and joins it with the exported
`Proactive relay torn down` documents (`analysis teardowns`), each carrying the
`relay_addr` that was closed and when:

  TOLD     the flow to that relay address ends with an inbound datagram (the
           close), or with at most one outbound after the last inbound.
  UNTOLD   >= 2 outbound datagrams after the last inbound: the dialler's
           keep-alives into a dead address until its idle timeout.

A flow whose remote matches a torn-down address but whose last packet falls
outside [ts - before, ts + after] is reported as unscored rather than folded in:
it ended for some other reason. A teardown with no matching flow at all is
counted too -- on a sample VM that simply had no connection through that relay
it is expected, and the gate in the run issue is on the scored count.
"""
from __future__ import annotations

import json
from collections import defaultdict

from rich.console import Console
from rich.table import Table

from saorsa_deploy.analysis.pcap import load_jsonl, parse_ts, read_packets


def _norm_addr(addr: str) -> str:
    """Normalise "ip:port" / "[v6]:port" to the "ip:port" form read_packets yields."""
    addr = addr.strip()
    if addr.startswith("["):
        host, _, port = addr[1:].partition("]:")
        return f"{host.lower()}:{port}"
    return addr


def load_teardowns(path):
    rows = []
    for raw in load_jsonl(path):
        if "relay_addr" not in raw or "ts" not in raw:
            continue
        rows.append(
            {
                **raw,
                "ts": parse_ts(raw["ts"]),
                "relay_addr": _norm_addr(str(raw["relay_addr"])),
            }
        )
    return rows


def analyse(pcap, teardowns, local, before=5.0, after=60.0):
    by_addr = defaultdict(list)
    for t in teardowns:
        by_addr[t["relay_addr"]].append(t)

    # (local_port, remote_addr) -> sorted list of (ts, "in"|"out")
    flows = defaultdict(list)
    for ts, src, sport, dst, dport, _w in read_packets(pcap):
        if src == local:
            remote = f"{dst}:{dport}"
            if remote in by_addr:
                flows[(sport, remote)].append((ts, "out"))
        elif dst == local:
            remote = f"{src}:{sport}"
            if remote in by_addr:
                flows[(dport, remote)].append((ts, "in"))

    rows = []
    unscored = 0
    # A teardown "has flows" if the pcap carries any traffic to its address,
    # whether or not that flow ended inside the scoring window.
    addrs_seen = {remote for _, remote in flows}
    for (lport, remote), pk in flows.items():
        pk.sort()
        first, last = pk[0][0], pk[-1][0]
        # Nearest teardown for this address whose window contains the flow's end.
        candidates = [
            t for t in by_addr[remote] if t["ts"] - before <= last <= t["ts"] + after
        ]
        if not candidates:
            unscored += 1
            continue
        t = min(candidates, key=lambda c: abs(c["ts"] - last))
        inbound = [ts for ts, d in pk if d == "in"]
        last_inbound = inbound[-1] if inbound else None
        out_after = (
            sum(1 for ts, d in pk if d == "out" and ts > last_inbound)
            if last_inbound is not None
            else sum(1 for _, d in pk if d == "out")
        )
        last_dir = pk[-1][1]
        told = last_dir == "in" or out_after <= 1
        rows.append(
            {
                "local_port": lport,
                "remote": remote,
                "allocation_id": t.get("allocation_id"),
                "teardown_host": t.get("host"),
                "teardown_ts": t["ts"],
                "first_ts": first,
                "last_ts": last,
                "last_direction": last_dir,
                "packets": len(pk),
                "outbound_after_last_inbound": out_after,
                "seconds_from_teardown_to_last": round(last - t["ts"], 2),
                "verdict": "TOLD" if told else "UNTOLD",
            }
        )

    told = sum(1 for r in rows if r["verdict"] == "TOLD")
    scored = len(rows)
    return {
        "pcap": pcap,
        "local": local,
        "window_seconds": {"before": before, "after": after},
        "teardowns": len(teardowns),
        "teardowns_without_flows": sum(
            1 for t in teardowns if t["relay_addr"] not in addrs_seen
        ),
        "scored": scored,
        "told": told,
        "untold": scored - told,
        "told_fraction": round(told / scored, 3) if scored else None,
        "unscored_outside_window": unscored,
        "rows": rows,
    }


def _print(result, console: Console) -> None:
    table = Table(title="Close delivery per flow")
    for col in ("remote", "alloc", "verdict", "last dir", "out after last in", "Δ teardown→last"):
        table.add_column(col)
    for r in result["rows"]:
        table.add_row(
            r["remote"],
            str(r["allocation_id"] or ""),
            r["verdict"],
            r["last_direction"],
            str(r["outbound_after_last_inbound"]),
            f"{r['seconds_from_teardown_to_last']:+.1f}s",
        )
    console.print(table)
    console.print(
        f"scored={result['scored']} told={result['told']} untold={result['untold']} "
        f"told_fraction={result['told_fraction']} "
        f"unscored_outside_window={result['unscored_outside_window']} "
        f"teardowns={result['teardowns']} "
        f"teardowns_without_flows={result['teardowns_without_flows']}"
    )


def cmd_close_delivery(args):
    teardowns = load_teardowns(args.teardowns)
    result = analyse(args.pcap, teardowns, args.local, args.before, args.after)
    _print(result, Console())
    if args.json_out:
        with open(args.json_out, "w") as fh:
            fh.write(json.dumps(result, indent=2) + "\n")
    return 0


def register(subparsers):
    p = subparsers.add_parser(
        "close-delivery",
        help=(
            "Score flows to torn-down relay addresses TOLD / UNTOLD from a peer-side "
            "pcap joined with the `analysis teardowns` export (saorsa-transport #138)."
        ),
    )
    p.add_argument("pcap")
    p.add_argument(
        "--teardowns",
        required=True,
        help="JSONL of `Proactive relay torn down` docs with ts and relay_addr",
    )
    p.add_argument("--local", required=True, help="local IP as it appears in the pcap")
    p.add_argument(
        "--before", type=float, default=5.0, help="seconds before a teardown a flow may end"
    )
    p.add_argument(
        "--after", type=float, default=60.0, help="seconds after a teardown a flow may end"
    )
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_close_delivery)
