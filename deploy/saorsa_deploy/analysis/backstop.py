"""Accept-side backstop detection (`analysis backstop`; saorsa-transport #140 / V2-1030).

#140 gives the accepting side of a QUIC connection a 25 s keep-alive
*backstop*. On a healthy idle connection it should never fire: the dialling
peer pings every 10 s and every received packet re-arms the backstop. So the
signal this extracts is "how many datagrams did the accepting side originate on
its own, rather than in response to something it just received?"

It reuses flowstat's pcap reader and adds two things flowstat does not do:

* **Role inference per flow.** A capture started mid-connection has no QUIC
  Initial to read, so the dialler is inferred from behaviour: on a quiescent
  flow the dialler's direction shows the ~10 s cadence and the other direction
  answers it within a round trip. Concretely, for each direction we count how
  many of its packets were *preceded within RESPONSE_WINDOW by a packet in the
  opposite direction*. The direction with the LOWER fraction of such
  "response-shaped" packets is the dialler; the higher is the acceptor. Flows
  where the two fractions are within 0.2 of each other are reported as
  `ambiguous` and excluded from the per-role counts (bulk flows, or flows too
  short to tell).

* **Unsolicited-packet counting on the acceptor direction.** An acceptor packet
  with no opposite-direction packet in the preceding RESPONSE_WINDOW is
  "unsolicited". A backstop PING is exactly that, and it is followed by a
  dialler ACK within an RTT. The gap between an unsolicited acceptor packet and
  the previous packet the acceptor *received* is also histogrammed: with the
  backstop live that gap clusters at ~25 s (the constant), so a spike in the
  20-30 s bin is the fingerprint, and a spike at ~5 s would mean the 2 s / 5 s
  pre-#137 timer is somehow back.

Limits, stated so nobody over-reads the output:
  * Retransmissions/PTO probes from the acceptor after loss also count as
    unsolicited. On a lossless idle window they are rare; report the count
    alongside the loaded-phase RcvbufErrors rather than as a pure backstop count.
  * Role inference needs >= MIN_PKTS_FOR_ROLE packets per direction; short
    flows are `ambiguous`.
"""
from __future__ import annotations

import json
from collections import defaultdict

from saorsa_deploy.analysis.pcap import _histogram, _percentile, read_packets

RESPONSE_WINDOW = 1.0  # s: an ack/response follows its trigger well inside this
MIN_PKTS_FOR_ROLE = 4
ROLE_MARGIN = 0.2
QUIESCENT_MEAN_LEN = 200
UNSOLICITED_GAP_EDGES = [1, 3, 6, 9, 12, 15, 20, 24, 27, 30, 45]


def analyse(paths, start=None, end=None, label=""):
    # flow key -> list of (ts, dir_index, wire_len); dir_index 0 = key[0] -> key[1]
    flows = defaultdict(list)
    first_ts = last_ts = None
    for path in paths:
        for ts, src, sport, dst, dport, wlen in read_packets(path):
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
            a, b = (src, sport), (dst, dport)
            if a <= b:
                flows[(a, b)].append((ts, 0, wlen))
            else:
                flows[(b, a)].append((ts, 1, wlen))

    if first_ts is None:
        return {"host": label, "error": "no UDP packets in window"}

    per_flow = []
    unsolicited_gaps = []
    totals = {
        "flows": 0,
        "flows_quiescent": 0,
        "flows_role_resolved": 0,
        "flows_ambiguous": 0,
        "acceptor_packets": 0,
        "acceptor_unsolicited": 0,
        "dialler_packets": 0,
        "dialler_unsolicited": 0,
        "flows_with_acceptor_unsolicited": 0,
    }

    for key, pkts in flows.items():
        pkts.sort()
        totals["flows"] += 1
        n = [0, 0]
        byts = [0, 0]
        for _, d, w in pkts:
            n[d] += 1
            byts[d] += w
        mean_len = sum(byts) / len(pkts)
        quiescent = mean_len < QUIESCENT_MEAN_LEN
        if quiescent:
            totals["flows_quiescent"] += 1

        # Per direction: fraction of packets that are response-shaped, plus the
        # list of unsolicited packets with their gap-since-last-received.
        resp = [0, 0]
        unsol = [[], []]
        last_seen = [None, None]  # last ts seen per direction
        for ts, d, _w in pkts:
            other = last_seen[1 - d]
            if other is not None and ts - other <= RESPONSE_WINDOW:
                resp[d] += 1
            else:
                unsol[d].append(None if other is None else ts - other)
            last_seen[d] = ts

        role = None
        if quiescent and n[0] >= MIN_PKTS_FOR_ROLE and n[1] >= MIN_PKTS_FOR_ROLE:
            f0, f1 = resp[0] / n[0], resp[1] / n[1]
            if abs(f0 - f1) >= ROLE_MARGIN:
                acceptor = 0 if f0 > f1 else 1
                role = {
                    "acceptor_dir": acceptor,
                    "dialler_dir": 1 - acceptor,
                    "resp_frac": [round(f0, 3), round(f1, 3)],
                }
        if role is None:
            if quiescent:
                totals["flows_ambiguous"] += 1
            continue

        totals["flows_role_resolved"] += 1
        acc, dia = role["acceptor_dir"], role["dialler_dir"]
        acc_unsol = [g for g in unsol[acc] if g is not None]
        totals["acceptor_packets"] += n[acc]
        totals["acceptor_unsolicited"] += len(acc_unsol)
        totals["dialler_packets"] += n[dia]
        totals["dialler_unsolicited"] += len([g for g in unsol[dia] if g is not None])
        if acc_unsol:
            totals["flows_with_acceptor_unsolicited"] += 1
        unsolicited_gaps.extend(acc_unsol)
        per_flow.append(
            {
                "flow": [f"{key[0][0]}:{key[0][1]}", f"{key[1][0]}:{key[1][1]}"],
                "acceptor": f"{key[acc][0]}:{key[acc][1]}",
                "packets": n,
                "resp_frac": role["resp_frac"],
                "acceptor_unsolicited": len(acc_unsol),
                "acceptor_unsolicited_gaps": [round(g, 1) for g in acc_unsol[:20]],
            }
        )

    sg = sorted(unsolicited_gaps)
    duration = last_ts - first_ts
    resolved = totals["flows_role_resolved"]
    return {
        "host": label,
        "files": paths,
        "window_seconds": round(duration, 1),
        **totals,
        "acceptor_unsolicited_per_flow_hour": (
            round(totals["acceptor_unsolicited"] / resolved / (duration / 3600), 3)
            if resolved and duration
            else None
        ),
        "acceptor_unsolicited_gap_median": round(_percentile(sg, 0.5), 1) if sg else None,
        "acceptor_unsolicited_gap_histogram": _histogram(sg, UNSOLICITED_GAP_EDGES),
        "flows_detail": per_flow,
    }


def cmd_backstop(args):
    result = analyse(args.pcaps, args.start, args.end, args.label)
    if args.no_detail:
        result.pop("flows_detail", None)
    text = json.dumps(result, indent=2)
    if args.json_out:
        with open(args.json_out, "w") as fh:
            fh.write(text + "\n")
    print(text)
    return 0


def register(subparsers):
    p = subparsers.add_parser(
        "backstop",
        help=(
            "Accept-side keep-alive backstop fingerprint from a pcap: per-flow "
            "dialler/acceptor role inference and unsolicited acceptor datagrams with "
            "their gap histogram (a live 25 s backstop spikes the 24-27 s bin)."
        ),
    )
    p.add_argument("pcaps", nargs="+")
    p.add_argument("--start", type=float, help="epoch seconds; ignore earlier packets")
    p.add_argument("--end", type=float, help="epoch seconds; ignore later packets")
    p.add_argument("--label", default="", help="host label for the report")
    p.add_argument("--json-out", help="write the JSON summary here")
    p.add_argument("--no-detail", action="store_true", help="omit per-flow detail")
    p.set_defaults(func=cmd_backstop)
