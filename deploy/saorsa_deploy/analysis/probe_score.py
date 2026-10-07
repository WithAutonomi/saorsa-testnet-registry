"""Score a stall probe from a pcap (`analysis probe-score`; saorsa-transport #140 / V2-1030).

`analysis stall-probe` blocks UDP to/from one peer VM at a time, for a fixed
number of seconds, and writes a JSONL log of {peer, block_start, block_end,
seconds}. This joins that log with the pcap captured on the same VM
(`analysis capture`, same interface/namespace) and scores every flow to a
blocked peer:

  SURVIVED  the same 4-tuple carries packets in BOTH directions within
            --resume-window seconds after block_end (a QUIC connection that
            outlived the stall: the dialler's probe or our backstop got an
            answer).
  DIED      the same 4-tuple carries nothing after block_end, or only one
            direction (a one-sided retry into a closed connection is not
            survival). Whether a replacement flow to the same peer appeared
            later is reported separately (`replaced`) -- it is a reconnect,
            not survival.
  NOT_ELIGIBLE  the flow was not active in the --active-window before the block
            (nothing to survive), so it is excluded from the denominator.

Only flows that were quiescent-ish before the block matter for the mechanism,
but the probe runs in the idle window, so eligibility is just "active before".

Output: per-probe rows plus a summary by block length. The gate in the run
issue is on the summary: 21 s -> survival >= 80%; 31 s -> death >= 80%.
"""
from __future__ import annotations

from collections import defaultdict

from saorsa_deploy.analysis.json_out import add_json_out_arg, console_for, emit_json
from saorsa_deploy.analysis.pcap import load_jsonl, read_packets


def score(pcap, probes, local, resume_window=15.0, active_window=30.0):
    peers = {p["peer"] for p in probes}

    # per peer -> per flow (local_port, peer_port) -> list of (ts, direction)
    # direction: "out" = local->peer, "in" = peer->local
    flows = defaultdict(lambda: defaultdict(list))
    for ts, src, sport, dst, dport, _w in read_packets(pcap):
        if src == local and dst in peers:
            flows[dst][(sport, dport)].append((ts, "out"))
        elif dst == local and src in peers:
            flows[src][(dport, sport)].append((ts, "in"))

    rows = []
    summary = defaultdict(lambda: {"eligible": 0, "survived": 0, "died": 0, "replaced": 0})
    for p in probes:
        peer = p["peer"]
        bs, be, secs = float(p["block_start"]), float(p["block_end"]), int(p["seconds"])
        peer_flows = flows.get(peer, {})
        for (lport, pport), pk in peer_flows.items():
            pk.sort()
            before = [t for t, _ in pk if bs - active_window <= t < bs]
            if not before:
                continue
            after = [(t, d) for t, d in pk if be <= t <= be + resume_window]
            dirs_after = {d for _, d in after}
            during = [t for t, _ in pk if bs <= t < be]
            survived = {"in", "out"} <= dirs_after
            # replacement: any *other* flow to this peer born after the block
            replaced = any(
                k != (lport, pport) and min(t for t, _ in v) >= be for k, v in peer_flows.items()
            )
            s = summary[secs]
            s["eligible"] += 1
            s["survived" if survived else "died"] += 1
            if not survived and replaced:
                s["replaced"] += 1
            rows.append(
                {
                    "peer": peer,
                    "seconds": secs,
                    "local_port": lport,
                    "peer_port": pport,
                    "packets_before": len(before),
                    "packets_during_block": len(during),
                    "packets_after": len(after),
                    "dirs_after": sorted(dirs_after),
                    "first_resume_offset": round(after[0][0] - be, 2) if after else None,
                    "result": "SURVIVED" if survived else "DIED",
                    "replaced": replaced if not survived else None,
                }
            )

    return {
        "pcap": pcap,
        "probes": len(probes),
        "flows_scored": len(rows),
        "summary_by_seconds": {
            str(k): {
                **v,
                "survival_pct": (
                    round(100 * v["survived"] / v["eligible"], 1) if v["eligible"] else None
                ),
            }
            for k, v in sorted(summary.items())
        },
        "rows": rows,
    }


def cmd_probe_score(args):
    probes = load_jsonl(args.probes)
    out = score(args.pcap, probes, args.local, args.resume_window, args.active_window)
    emit_json(out, args.json_out, console_for(args.json_out), echo=True)
    return 0


def register(subparsers):
    p = subparsers.add_parser(
        "probe-score",
        help=(
            "Join a stall-probe JSONL log with the pcap captured over the same span "
            "and score each flow to a blocked peer SURVIVED / DIED (with reconnects "
            "reported separately)."
        ),
    )
    p.add_argument("pcap")
    p.add_argument("--probes", required=True, help="JSONL written by `analysis stall-probe`")
    p.add_argument(
        "--local", required=True, help="local node-side IP as it appears in the pcap"
    )
    p.add_argument("--resume-window", type=float, default=15.0)
    p.add_argument("--active-window", type=float, default=30.0)
    add_json_out_arg(p)
    p.set_defaults(func=cmd_probe_score)
