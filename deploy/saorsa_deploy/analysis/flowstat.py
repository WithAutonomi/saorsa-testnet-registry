#!/usr/bin/env python3
"""Per-flow analysis of node UDP captures (`saorsa-deploy analysis flowstat`).

Built for the V2-996 keep-alive A/B and kept self-contained on purpose: this
file is pushed to node VMs by `analysis capture-stream` and run there with the
system python3 (`python3 flowstat.py - --label L --expect-seconds N --json-out
PATH`), so it must not import anything from saorsa_deploy or any third-party
module.

Reads pcap files directly -- no tcpdump, no tshark, no third-party modules --
so the same script runs on a node VM and on the operator's machine.

Why this exists: the connection lifecycle events in saorsa-core 0.27.0 are
`debug!`, and a 990-node fleet at debug is not viable (telegraf tops out around
2700 lines/s and ES becomes a sample, not a copy). So per-flow analysis of
packet captures stands in for lifecycle logs: a QUIC connection is a UDP
4-tuple, its lifetime is first-packet..last-packet, and a keep-alive cadence is
the inter-packet gap on an otherwise quiescent flow.

Two things it is careful about:

* **Cadence is measured per direction.** Both endpoints send their own
  keep-alives; merging the directions into one gap series halves the apparent
  interval.
* **Lifetimes at the window edges are censored.** A flow already open when the
  capture started, or still open when it ended, has no observable birth or
  death. Those are counted separately and excluded from the lifetime histogram,
  because counting them as short-lived would manufacture exactly the
  idle-timeout signature this run is looking for.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from collections import defaultdict

# Link types we may encounter capturing on a named interface.
LINKTYPE_ETHERNET = 1
LINKTYPE_LINUX_SLL = 113
LINKTYPE_LINUX_SLL2 = 276
LINKTYPE_RAW = 101

# Gap histogram edges (seconds). Chosen around the two cadences under test:
# the baseline's ~2s effective interval and the patched arm's 10s dial-only.
GAP_EDGES = [0.25, 0.5, 1.0, 1.5, 2.5, 4.0, 6.0, 8.0, 12.0, 20.0, 40.0]

# Lifetime histogram edges (seconds). 30-45 is explicit: that bucket is the
# idle-timeout death signature the comparison gates on.
LIFETIME_EDGES = [1, 5, 10, 20, 30, 45, 60, 120, 300, 600, 1200]

# A flow whose mean packet is under this is carrying keep-alives/ACKs rather
# than application payload.
QUIESCENT_MEAN_LEN = 200


def _histogram(values, edges):
    counts = [0] * (len(edges) + 1)
    for v in values:
        placed = False
        for i, e in enumerate(edges):
            if v < e:
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    labels = []
    prev = 0.0
    for e in edges:
        labels.append(f"{prev:g}-{e:g}")
        prev = e
    labels.append(f">={prev:g}")
    return dict(zip(labels, counts))


def _percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _round_pct(sorted_vals, q, digits=3):
    return round(_percentile(sorted_vals, q), digits) if sorted_vals else None


def _parse_ipv4(buf, off):
    if len(buf) < off + 20:
        return None
    ihl = (buf[off] & 0x0F) * 4
    proto = buf[off + 9]
    if proto != 17:
        return None
    src = ".".join(str(b) for b in buf[off + 12 : off + 16])
    dst = ".".join(str(b) for b in buf[off + 16 : off + 20])
    return src, dst, off + ihl


def _parse_ipv6(buf, off):
    if len(buf) < off + 40:
        return None
    nxt = buf[off + 6]
    if nxt != 17:  # no extension-header walking; QUIC traffic has none
        return None
    src = ":".join(f"{buf[off+8+i]:02x}{buf[off+9+i]:02x}" for i in range(0, 16, 2))
    dst = ":".join(f"{buf[off+24+i]:02x}{buf[off+25+i]:02x}" for i in range(0, 16, 2))
    return src, dst, off + 40


def _l3_offset(linktype, buf):
    """Return (ethertype, offset) for the L3 header, or (None, None)."""
    if linktype == LINKTYPE_ETHERNET:
        if len(buf) < 14:
            return None, None
        ethertype = struct.unpack_from(">H", buf, 12)[0]
        off = 14
        while ethertype in (0x8100, 0x88A8):  # VLAN tags
            if len(buf) < off + 4:
                return None, None
            ethertype = struct.unpack_from(">H", buf, off + 2)[0]
            off += 4
        return ethertype, off
    if linktype == LINKTYPE_LINUX_SLL:
        if len(buf) < 16:
            return None, None
        return struct.unpack_from(">H", buf, 14)[0], 16
    if linktype == LINKTYPE_LINUX_SLL2:
        if len(buf) < 20:
            return None, None
        return struct.unpack_from(">H", buf, 0)[0], 20
    if linktype == LINKTYPE_RAW:
        if not buf:
            return None, None
        version = buf[0] >> 4
        return (0x0800 if version == 4 else 0x86DD), 0
    return None, None


def read_packets(path):
    """Yield (ts, src, sport, dst, dport, wire_len) for every UDP datagram.

    `path` may be "-", which reads a pcap STREAM from stdin. That is how the
    loaded phase is measured: at ~35k packets/s a ten-minute capture is ~21 GB,
    which will not fit on the root-disk-only 3-AZ VMs and whose write traffic
    would perturb the node I/O being measured. Streaming keeps disk out of it
    entirely. The cost is that falling behind makes the kernel drop packets, so
    the caller must report tcpdump's drop count rather than assume none.
    """
    if path == "-":
        return _read_stream(sys.stdin.buffer)
    return _read_stream_from_file(path)


def _read_stream_from_file(path):
    with open(path, "rb") as fh:
        yield from _read_stream(fh)


def _read_stream(fh):
    header = fh.read(24)
    if len(header) < 24:
        return
    magic = struct.unpack("<I", header[:4])[0]
    if magic in (0xA1B2C3D4, 0xA1B23C4D):
        endian = "<"
        nanos = magic == 0xA1B23C4D
    elif magic in (0xD4C3B2A1, 0x4D3CB2A1):
        endian = ">"
        nanos = magic == 0x4D3CB2A1
    else:
        raise SystemExit(f"not a pcap stream (magic {magic:#x})")
    linktype = struct.unpack(endian + "I", header[20:24])[0]
    rec = struct.Struct(endian + "IIII")
    while True:
        rh = fh.read(16)
        if len(rh) < 16:
            return
        ts_sec, ts_frac, incl_len, orig_len = rec.unpack(rh)
        data = fh.read(incl_len)
        if len(data) < incl_len:
            return
        ts = ts_sec + (ts_frac / 1e9 if nanos else ts_frac / 1e6)
        ethertype, off = _l3_offset(linktype, data)
        if ethertype == 0x0800:
            parsed = _parse_ipv4(data, off)
        elif ethertype == 0x86DD:
            parsed = _parse_ipv6(data, off)
        else:
            continue
        if not parsed:
            continue
        src, dst, l4 = parsed
        if len(data) < l4 + 8:
            continue
        sport, dport = struct.unpack_from(">HH", data, l4)
        yield ts, src, sport, dst, dport, orig_len


def analyse(paths, start=None, end=None, host_label=""):
    # Per direction: [first, last, packets, bytes, prev_ts, gaps]
    dirs = {}
    first_ts = None
    last_ts = None
    total_pkts = 0
    total_bytes = 0
    minute_flows = defaultdict(set)

    for path in paths:
        for ts, src, sport, dst, dport, wlen in read_packets(path):
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
            total_pkts += 1
            total_bytes += wlen
            key = (src, sport, dst, dport)
            d = dirs.get(key)
            if d is None:
                dirs[key] = [ts, ts, 1, wlen, ts, []]
            else:
                gap = ts - d[4]
                d[1] = ts
                d[2] += 1
                d[3] += wlen
                d[4] = ts
                d[5].append(gap)
            flow = tuple(sorted([(src, sport), (dst, dport)]))
            minute_flows[int(ts // 60)].add(flow)

    if first_ts is None:
        return {"host": host_label, "error": "no UDP packets in window"}

    # Merge directions into flows.
    flows = {}
    for (src, sport, dst, dport), d in dirs.items():
        key = tuple(sorted([(src, sport), (dst, dport)]))
        f = flows.setdefault(
            key, {"first": d[0], "last": d[1], "pkts": 0, "bytes": 0, "dirs": []}
        )
        f["first"] = min(f["first"], d[0])
        f["last"] = max(f["last"], d[1])
        f["pkts"] += d[2]
        f["bytes"] += d[3]
        f["dirs"].append(d)

    duration = last_ts - first_ts
    # Edge margin: a flow first seen within this of the capture start was
    # almost certainly already open, and one last seen within it of the end is
    # almost certainly still open. Sized above the largest cadence under test.
    margin = 15.0

    born = [f for f in flows.values() if f["first"] > first_ts + margin]
    died = [f for f in flows.values() if f["last"] < last_ts - margin]
    # Intersect by identity: `f in born` would compare dicts by value, which is
    # both wrong (two identical-looking flows would collide) and quadratic.
    born_ids = {id(f) for f in born}
    died_ids = {id(f) for f in died}
    observed = [f for f in flows.values() if id(f) in born_ids and id(f) in died_ids]

    lifetimes = [f["last"] - f["first"] for f in observed]
    censored_lifetimes = [
        f["last"] - f["first"]
        for f in flows.values()
        if id(f) not in born_ids or id(f) not in died_ids
    ]

    # Cadence: per-direction gaps on quiescent directions only.
    quiescent_gaps = []
    per_dir_medians = []
    for d in dirs.values():
        first, last, pkts, byts, _prev, gaps = d
        if pkts < 5 or not gaps:
            continue
        if byts / pkts >= QUIESCENT_MEAN_LEN:
            continue
        quiescent_gaps.extend(gaps)
        sg = sorted(gaps)
        per_dir_medians.append(_percentile(sg, 0.5))

    sorted_medians = sorted(per_dir_medians)
    sorted_gaps = sorted(quiescent_gaps)
    concurrency = {str(m): len(s) for m, s in sorted(minute_flows.items())}
    conc_values = sorted(len(s) for s in minute_flows.values())

    return {
        "host": host_label,
        "files": paths,
        "window_start_epoch": first_ts,
        "window_end_epoch": last_ts,
        "window_seconds": round(duration, 1),
        "packets": total_pkts,
        "bytes": total_bytes,
        "packets_per_sec": round(total_pkts / duration, 2) if duration else None,
        "bytes_per_sec": round(total_bytes / duration, 1) if duration else None,
        "flows_total": len(flows),
        "flows_born_in_window": len(born),
        "flows_died_in_window": len(died),
        "flows_fully_observed": len(observed),
        "flows_censored": len(flows) - len(observed),
        "new_flows_per_minute": (
            round(len(born) / (duration / 60), 2) if duration else None
        ),
        "concurrent_flows_median": _percentile(conc_values, 0.5),
        "concurrent_flows_max": conc_values[-1] if conc_values else None,
        "concurrent_flows_per_minute": concurrency,
        "quiescent_directions": len(per_dir_medians),
        "cadence_median_of_flow_medians": _round_pct(sorted_medians, 0.5),
        "cadence_p05_of_flow_medians": _round_pct(sorted_medians, 0.05),
        "cadence_p95_of_flow_medians": _round_pct(sorted_medians, 0.95),
        "cadence_gap_histogram": _histogram(sorted_gaps, GAP_EDGES),
        "cadence_gap_samples": len(sorted_gaps),
        "lifetime_histogram_observed": _histogram(lifetimes, LIFETIME_EDGES),
        "lifetime_median_observed": (
            round(_percentile(sorted(lifetimes), 0.5), 1) if lifetimes else None
        ),
        "lifetime_histogram_censored": _histogram(censored_lifetimes, LIFETIME_EDGES),
        "_lifetimes": lifetimes,
    }


def add_arguments(ap):
    ap.add_argument(
        "pcaps", nargs="+", help="pcap file(s), or '-' to read a pcap stream from stdin"
    )
    ap.add_argument("--start", type=float, help="epoch seconds; ignore earlier packets")
    ap.add_argument("--end", type=float, help="epoch seconds; ignore later packets")
    ap.add_argument("--label", default="", help="host label for the report")
    ap.add_argument("--json-out", help="write the JSON summary here")
    ap.add_argument(
        "--lifetimes-out",
        help="also write the raw fully-observed lifetimes (one float per line), "
        "so buckets not in LIFETIME_EDGES (e.g. 25-30 s for V2-1030 gate 7) can "
        "be computed without changing the edges the A/B arms were reported on",
    )
    ap.add_argument(
        "--expect-seconds",
        type=float,
        help="the measurement window this capture was supposed to cover; "
        "exit non-zero if the pcap spans materially less",
    )
    ap.add_argument(
        "--expect-tolerance",
        type=float,
        default=0.95,
        help="fraction of --expect-seconds that counts as complete (default 0.95)",
    )
    return ap


def run(args):
    """Analyse per ``args``; returns the process exit code (2 = short capture)."""
    result = analyse(args.pcaps, args.start, args.end, args.label)
    if args.lifetimes_out:
        with open(args.lifetimes_out, "w") as fh:
            for v in result.get("_lifetimes", []):
                fh.write(f"{v:.3f}\n")
    result.pop("_lifetimes", None)

    # A capture that died early does not announce itself. On V2-988 the decisive
    # 20-minute idle window died 29 seconds in; every downstream number was
    # computed, printed and believed, and the failure only surfaced because the
    # cadence disagreed with a phase measured an hour earlier. By then clients
    # had restarted and the window was gone.
    #
    # The specific trap: a truncated window does not produce obviously-empty
    # output. It produces an ALL-ZERO lifetime histogram (nothing can be born
    # AND die inside 29s, so everything is censored) -- which reads exactly like
    # "no connections died", the healthiest possible result. And it biases
    # cadence downward, because the >=5-packet quiescent filter can only admit
    # directions whose gaps are short enough to fit. Both failure modes look
    # like findings, which is why this has to be an error and not a warning.
    if args.expect_seconds:
        span = result.get("window_seconds") or 0
        floor = args.expect_seconds * args.expect_tolerance
        result["expected_window_seconds"] = args.expect_seconds
        result["window_complete"] = span >= floor
        if span < floor:
            text = json.dumps(result, indent=2)
            if args.json_out:
                with open(args.json_out, "w") as fh:
                    fh.write(text + "\n")
            print(text)
            print(
                f"\nFATAL [{args.label or 'capture'}]: pcap spans {span:.1f}s but the "
                f"measurement window was {args.expect_seconds:.0f}s "
                f"(floor {floor:.0f}s).\n"
                "The capture died early. Every derived figure below is an artifact:\n"
                "  - the lifetime histogram is all-zero because nothing can be born and\n"
                "    die inside a short window, which is NOT 'no connections died';\n"
                "  - the cadence is biased low by the >=5-packet quiescent filter.\n"
                "Do not report these numbers. Re-run the window.",
                file=sys.stderr,
            )
            return 2

    text = json.dumps(result, indent=2)
    if args.json_out:
        with open(args.json_out, "w") as fh:
            fh.write(text + "\n")
    print(text)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    add_arguments(ap)
    return run(ap.parse_args(argv))


def cmd_flowstat(args):
    return run(args)


def register(subparsers):
    """`saorsa-deploy analysis flowstat` — same options as the standalone script."""
    p = subparsers.add_parser(
        "flowstat",
        help=(
            "Per-flow analysis of a node UDP pcap: per-direction keep-alive cadence, "
            "edge-censored lifetime histogram, concurrency and churn. Exits 2 if the "
            "pcap spans less than --expect-seconds."
        ),
    )
    add_arguments(p)
    p.set_defaults(func=cmd_flowstat)


if __name__ == "__main__":
    sys.exit(main())
