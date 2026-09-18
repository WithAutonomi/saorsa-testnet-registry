"""V2-1274: reconcile the V2-834 traffic counters against the kernel, per VM.

Joins three sources over one measurement window and prints the per-VM table
the results comment needs, with a pass/fail per invariant:

* `analysis snapshot` records (one JSON line per VM per tag) -- the I1
  denominator: /proc/net/snmp Udp In/OutDatagrams in the namespace the node
  services actually run in (`ant_nat` on NAT'd VMs, host otherwise).
* the run's Elasticsearch -- the numerators: max-minus-min of every cumulative
  `tail.*` counter per host and service (via `EsRun.counter_delta`), plus the
  raw per-sample series for the monotonicity check (I4).
* the run's InfluxDB -- the I2 denominator: `net.bytes_sent` / `net.bytes_recv`
  spread over the window on the interface the snapshot saw as the default route.

Invariants (see V2-1274 for the reasoning):

  I1  sum(sock_tx_datagrams) vs dOutDatagrams                          <= 2%
      sum(sock_rx_datagrams) vs dInDatagrams                           <= 2%
  I2  (sock_tx_bytes + 42*sock_tx_datagrams) / d net.bytes_sent        >= 98%
      (sock_rx_bytes + 42*sock_rx_datagrams) / d net.bytes_recv        >= 98%
  I3  sock_tx_bytes - (udp_tx + forwarded_to_target + relay_carrier_udp_tx
        + failed_dial_tx + stateless_tx)         in [0, 5%] of sock_tx_bytes
      sock_rx_bytes - (udp_rx + relay_target_rx + relay_carrier_udp_rx
        + failed_dial_rx)                        in [0, 5%] of sock_rx_bytes
  I4  no cumulative field decreases between consecutive samples per
      (host, service)

`sock_*` are the process's real-socket totals -- every QUIC socket plus the
relay server's per-session plain sockets (saorsa-transport 4b13fec2 or
later; earlier builds left the relay's plain sockets out and the relay
fields had to be added by hand).

Usage:

  uv run python scripts/v2_834_reconcile.py --name DEV-01 \
      --start 2026-09-18T10:30:00Z --end 2026-09-18T13:30:00Z \
      --snapshots snapshots.jsonl --start-tag win-start --end-tag win-end \
      --out reconcile.md --json-out reconcile.json

Needs the same environment as `saorsa-deploy analysis` (DATABASE_URL and the
S3 credentials that resolve the run's ES/Influx state). Read-only everywhere.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))

from saorsa_deploy.analysis.es import (  # noqa: E402
    HOST_FIELD,
    SERVICE_FIELD,
    TIMESTAMP_FIELD,
    EsRun,
)

MESSAGE_FIELD = "tail.message"
# Per-endpoint lines carry this discriminator (transport 69cc8761+): a
# dual-stack node runs one endpoint per address family and each has its own
# cumulative series. Aggregating both as one series makes the idle family's
# zeros the window minimum. Lines without it bucket under "-".
ENDPOINT_FIELD = "tail.endpoint.keyword"

# Which summary line each field lives on. Pinning the aggregation to the
# message keeps the relay server's per-session lines (same field names as
# its summary, V2-1202) out of the relay-summary deltas.
FIELD_MESSAGE = {
    **dict.fromkeys(
        [
            "sock_tx_bytes",
            "sock_tx_datagrams",
            "sock_tx_errors",
            "sock_rx_bytes",
            "sock_rx_datagrams",
            "stateless_tx_bytes",
            "stateless_tx_count",
            "failed_dial_tx_bytes",
            "failed_dial_rx_bytes",
            "failed_dial_count",
        ],
        "socket traffic summary (cumulative)",
    ),
    **dict.fromkeys(
        [
            "udp_tx_bytes",
            "udp_rx_bytes",
            "virtual_udp_tx_bytes",
            "virtual_udp_rx_bytes",
            "relay_carrier_udp_tx_bytes",
            "relay_carrier_udp_rx_bytes",
        ],
        "endpoint traffic summary (cumulative)",
    ),
    **dict.fromkeys(
        [
            "forwarded_to_target_bytes",
            "forwarded_to_target_count",
            "relay_target_rx_bytes",
            "relay_target_rx_count",
            "forwarded_to_client_bytes",
            "target_send_failed_bytes",
            "stream_rx_keepalive_count",
            "stream_tx_keepalive_count",
        ],
        "relay traffic summary (cumulative)",
    ),
    **dict.fromkeys(
        [
            "stream_tx_bytes",
            "stream_rx_bytes",
            "keepalive_tx_count",
            "keepalive_rx_count",
            "dropped_oversized_bytes",
        ],
        "relay client traffic summary (cumulative)",
    ),
    **dict.fromkeys(
        [
            "lost_bytes",
            "lost_packets",
            "congestion_events",
            "black_holes_detected",
            "tx_acks",
            "tx_ping",
            "tx_crypto",
            "rx_acks",
            "rx_ping",
            "rx_crypto",
        ],
        "connection overhead summary (cumulative)",
    ),
    **dict.fromkeys(
        [
            "wire_tx_bytes",
            "wire_rx_bytes",
            "wire_failed_tx_bytes",
            "wire_rejected_rx_bytes",
            "wire_dropped_rx_bytes",
        ],
        "wire traffic summary (cumulative)",
    ),
    **dict.fromkeys(
        [
            "get_success_tx_bytes",
            "put_success_tx_bytes",
            "get_rx_bytes",
            "put_rx_bytes",
        ],
        "chunk rpc traffic summary (cumulative)",
    ),
    "manifest_rx_count": "upgrade traffic summary (cumulative)",
    **dict.fromkeys(
        ["completed_payments_ok_count", "merkle_payment_ok_count"],
        "evm rpc summary (cumulative)",
    ),
}


def field_filters(f: str) -> list[dict]:
    filters: list[dict] = [{"exists": {"field": f"tail.{f}"}}]
    msg = FIELD_MESSAGE.get(f)
    if msg:
        filters.append({"match_phrase": {MESSAGE_FIELD: msg}})
    return filters


# Ethernet (14) + IPv4 (20) + UDP (8) header bytes per datagram. telegraf's
# net.bytes_* come from /proc/net/dev, which counts the L2 frame; sock_*_bytes
# counts UDP payload only. Confirmed empirically on the first DEV-01 run.
HEADER_BYTES = 42

# Every cumulative field the table needs. Deltas are fetched per host (summed
# over that host's services) via `EsRun.counter_delta`.
DELTA_FIELDS = [
    "sock_tx_bytes",
    "sock_tx_datagrams",
    "sock_rx_bytes",
    "sock_rx_datagrams",
    "stateless_tx_bytes",
    "failed_dial_tx_bytes",
    "failed_dial_rx_bytes",
    "udp_tx_bytes",
    "udp_rx_bytes",
    "virtual_udp_tx_bytes",
    "virtual_udp_rx_bytes",
    "relay_carrier_udp_tx_bytes",
    "relay_carrier_udp_rx_bytes",
    "forwarded_to_target_bytes",
    "forwarded_to_target_count",
    "relay_target_rx_bytes",
    "relay_target_rx_count",
    "forwarded_to_client_bytes",
    "stream_tx_bytes",
    "stream_rx_bytes",
    "wire_rx_bytes",
    "wire_rejected_rx_bytes",
    "wire_dropped_rx_bytes",
]

# Fields checked for monotonicity. Every cumulative field on every summary
# line; `group` and the live gauges (`active_*`, `live_min_mtu`) are excluded
# because they are not cumulative.
MONOTONIC_FIELDS = DELTA_FIELDS + [
    "sock_tx_errors",
    "stateless_tx_count",
    "failed_dial_count",
    "lost_bytes",
    "lost_packets",
    "congestion_events",
    "black_holes_detected",
    "tx_acks",
    "tx_ping",
    "tx_crypto",
    "rx_acks",
    "rx_ping",
    "rx_crypto",
    "keepalive_tx_count",
    "keepalive_rx_count",
    "dropped_oversized_bytes",
    "target_send_failed_bytes",
    "stream_rx_keepalive_count",
    "stream_tx_keepalive_count",
    "wire_tx_bytes",
    "wire_failed_tx_bytes",
    "get_success_tx_bytes",
    "put_success_tx_bytes",
    "get_rx_bytes",
    "put_rx_bytes",
    "manifest_rx_count",
    "completed_payments_ok_count",
    "merkle_payment_ok_count",
]

I1_TOLERANCE = 0.02
I2_FLOOR = 0.98
I3_TOLERANCE = 0.05


def norm_host(h: str) -> str:
    """ES upper-cases `tag.host`; Influx and `hostname` are lower-case."""
    return h.strip().lower()


def load_snapshots(path: Path, start_tag: str, end_tag: str) -> dict[str, dict]:
    """Per host: the two snapshot records and the derived window deltas."""
    by_host: dict[str, dict[str, dict]] = defaultdict(dict)
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if "error" in rec or "host" not in rec:
            continue
        by_host[norm_host(rec["host"])][rec["tag"]] = rec

    out: dict[str, dict] = {}
    for host, tags in by_host.items():
        a, b = tags.get(start_tag), tags.get(end_tag)
        if not a or not b:
            out[host] = {"error": f"missing snapshot tag(s) for {host}"}
            continue
        nat = bool(int(a.get("nat", 0)))
        pre = "ns_" if nat else "host_"
        out[host] = {
            "ip": a.get("ip"),
            "nat": nat,
            "iface": a.get("iface"),
            "d_udp_out": int(b[pre + "udp_out"]) - int(a[pre + "udp_out"]),
            "d_udp_in": int(b[pre + "udp_in"]) - int(a[pre + "udp_in"]),
            "span_s": int(b["ts"]) - int(a["ts"]),
        }
    return out


def fetch_deltas(run: EsRun, start: str, end: str) -> dict[str, dict[str, float]]:
    """{host: {field: delta}} -- max-minus-min per (host, service, endpoint),
    summed per host. Same idea as `EsRun.counter_delta`, plus the endpoint
    split and the message pin (see `ENDPOINT_FIELD` / `FIELD_MESSAGE`)."""
    per_host: dict[str, dict[str, float]] = defaultdict(dict)
    for f in DELTA_FIELDS:
        field = f"tail.{f}"
        body = {
            "size": 0,
            "track_total_hits": False,
            "query": {
                "bool": {
                    "filter": [
                        *field_filters(f),
                        {"range": {TIMESTAMP_FIELD: {"gte": start, "lte": end}}},
                    ]
                }
            },
            "aggs": {
                "per_host": {
                    "terms": {"field": HOST_FIELD, "size": 200},
                    "aggs": {
                        "per_service": {
                            "terms": {"field": SERVICE_FIELD, "size": 40},
                            "aggs": {
                                "per_endpoint": {
                                    "terms": {
                                        "field": ENDPOINT_FIELD,
                                        "size": 8,
                                        "missing": "-",
                                    },
                                    "aggs": {
                                        "lo": {"min": {"field": field}},
                                        "hi": {"max": {"field": field}},
                                    },
                                }
                            },
                        }
                    },
                }
            },
        }
        r = run.search(body)
        for hb in r["aggregations"]["per_host"]["buckets"]:
            total = 0.0
            for sb in hb["per_service"]["buckets"]:
                for eb in sb["per_endpoint"]["buckets"]:
                    lo, hi = eb["lo"]["value"], eb["hi"]["value"]
                    if lo is not None and hi is not None:
                        total += hi - lo
            per_host[norm_host(hb["key"])][f] = total
    return per_host


def fetch_net_spread(
    run: EsRun, start: str, end: str
) -> dict[str, dict[str, dict[str, float]]]:
    """{host: {interface: {bytes_sent: spread, bytes_recv: spread}}}."""
    st = run.monitoring()
    rows = run.flux(
        f'''
from(bucket: "{st["influxdb_bucket"]}")
  |> range(start: {start}, stop: {end})
  |> filter(fn: (r) => r._measurement == "net" and (r._field == "bytes_sent" or r._field == "bytes_recv"))
  |> filter(fn: (r) => r.testnet_name == "{run.name}")
  |> group(columns: ["host", "interface", "_field"])
  |> spread()
  |> keep(columns: ["host", "interface", "_field", "_value"])
'''
    )
    out: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        try:
            out[norm_host(r["host"])][r["interface"]][r["_field"]] = float(r["_value"])
        except (KeyError, ValueError):
            continue
    return out


def _dig(doc: dict, dotted: str):
    if dotted in doc:  # flat dotted key
        return doc[dotted]
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def check_monotonic(run: EsRun, start: str, end: str) -> dict:
    """I4: per (host, service, endpoint, field), every sample >= the previous one."""
    host_src = HOST_FIELD.removesuffix(".keyword")
    svc_src = SERVICE_FIELD.removesuffix(".keyword")
    ep_src = ENDPOINT_FIELD.removesuffix(".keyword")
    checked = 0
    decreases: list[dict] = []
    for f in MONOTONIC_FIELDS:
        field = f"tail.{f}"
        docs = run.hits(
            field_filters(f),
            start,
            end,
            fields=[TIMESTAMP_FIELD, host_src, svc_src, ep_src, field],
        )
        series: dict[tuple[str, str, str], list[tuple[str, float]]] = defaultdict(list)
        for d in docs:
            host = _dig(d, host_src)
            svc = _dig(d, svc_src)
            ep = _dig(d, ep_src) or "-"
            val = _dig(d, field)
            ts = _dig(d, TIMESTAMP_FIELD)
            if host is None or val is None or ts is None:
                continue
            series[(norm_host(str(host)), str(svc), str(ep))].append(
                (str(ts), float(val))
            )
        for (host, svc, ep), samples in series.items():
            samples.sort()
            checked += 1
            prev = None
            for ts, val in samples:
                if prev is not None and val < prev[1]:
                    decreases.append(
                        {
                            "host": host,
                            "service": svc,
                            "endpoint": ep,
                            "field": f,
                            "prev": {"ts": prev[0], "value": prev[1]},
                            "next": {"ts": ts, "value": val},
                        }
                    )
                    break
                prev = (ts, val)
    return {"series_checked": checked, "decreases": decreases}


def log_shipping_estimate(run: EsRun, start: str, end: str) -> dict[str, float | None]:
    """Bytes of log documents ES holds per host for the window (context only).

    Doc count per host times the index's mean stored document size. `_stats`
    needs the index monitor privilege the read-only key may lack; then the
    estimate is None and the table says so.
    """
    import requests  # local: keep module import cheap

    try:
        resp = requests.get(
            f"{run.es_url()}/{run.node_index()}/_stats/store,docs",
            headers=run.es_headers(),
            timeout=60,
            verify=False,
        )
        resp.raise_for_status()
        s = resp.json()["_all"]["primaries"]
        mean_doc = s["store"]["size_in_bytes"] / max(1, s["docs"]["count"])
    except Exception:  # noqa: BLE001 -- best effort by design
        return {}
    body = {
        "size": 0,
        "track_total_hits": True,
        "query": {
            "bool": {
                "filter": [{"range": {TIMESTAMP_FIELD: {"gte": start, "lte": end}}}]
            }
        },
        "aggs": {"per_host": {"terms": {"field": HOST_FIELD, "size": 200}}},
    }
    r = run.search(body)
    return {
        norm_host(b["key"]): b["doc_count"] * mean_doc
        for b in r["aggregations"]["per_host"]["buckets"]
    }


def pct(num: float, den: float) -> float | None:
    return None if not den else num / den


def build_rows(
    snaps: dict[str, dict],
    deltas: dict[str, dict[str, float]],
    net: dict[str, dict[str, dict[str, float]]],
    shipping: dict[str, float | None],
) -> list[dict]:
    rows = []
    for host in sorted(set(snaps) | set(deltas)):
        s = snaps.get(host, {})
        d = deltas.get(host, {})
        g = lambda k: d.get(k, 0.0)  # noqa: E731
        row: dict = {
            "host": host,
            "tier": "nat" if s.get("nat") else "plain",
            "ip": s.get("ip"),
        }
        if "error" in s or not s:
            row["error"] = s.get("error", "no snapshot")
            rows.append(row)
            continue

        # I1 -- datagrams vs the namespace's UDP counters
        tx_dg = g("sock_tx_datagrams")
        rx_dg = g("sock_rx_datagrams")
        row["d_udp_out"] = s["d_udp_out"]
        row["sum_tx_datagrams"] = tx_dg
        row["i1_tx"] = pct(tx_dg, s["d_udp_out"])
        row["d_udp_in"] = s["d_udp_in"]
        row["sum_rx_datagrams"] = rx_dg
        row["i1_rx"] = pct(rx_dg, s["d_udp_in"])

        # I2 -- header-corrected bytes vs telegraf's interface counters
        iface = s.get("iface")
        n = net.get(host, {}).get(iface, {}) if iface else {}
        row["iface"] = iface
        row["d_net_bytes_sent"] = n.get("bytes_sent")
        row["d_net_bytes_recv"] = n.get("bytes_recv")
        tx_num = g("sock_tx_bytes") + HEADER_BYTES * tx_dg
        rx_num = g("sock_rx_bytes") + HEADER_BYTES * rx_dg
        row["i2_tx_numerator"] = tx_num
        row["i2_rx_numerator"] = rx_num
        row["i2_tx"] = pct(tx_num, n.get("bytes_sent") or 0)
        row["i2_rx"] = pct(rx_num, n.get("bytes_recv") or 0)
        row["log_shipping_bytes"] = shipping.get(host)

        # I3 -- itemisation vs socket totals. virtual_udp_* deliberately
        # absent (rides the carrier); bytes_relayed deliberately absent (its
        # dir-1 half is inside udp_tx via the carrier connection, its dir-2
        # half IS forwarded_to_target).
        tx_item = (
            g("udp_tx_bytes")
            + g("forwarded_to_target_bytes")
            + g("relay_carrier_udp_tx_bytes")
            + g("failed_dial_tx_bytes")
            + g("stateless_tx_bytes")
        )
        rx_item = (
            g("udp_rx_bytes")
            + g("relay_target_rx_bytes")
            + g("relay_carrier_udp_rx_bytes")
            + g("failed_dial_rx_bytes")
        )
        row["i3_tx_residual"] = pct(g("sock_tx_bytes") - tx_item, g("sock_tx_bytes"))
        row["i3_rx_residual"] = pct(g("sock_rx_bytes") - rx_item, g("sock_rx_bytes"))
        row["virtual_udp_tx_bytes"] = g("virtual_udp_tx_bytes")

        row["sock_tx_bytes"] = g("sock_tx_bytes")
        row["sock_rx_bytes"] = g("sock_rx_bytes")

        def within(v, tol):
            return v is not None and abs(v - 1.0) <= tol

        row["pass_i1"] = within(row["i1_tx"], I1_TOLERANCE) and within(
            row["i1_rx"], I1_TOLERANCE
        )
        row["pass_i2"] = (
            row["i2_tx"] is not None
            and row["i2_rx"] is not None
            and row["i2_tx"] >= I2_FLOOR
            and row["i2_rx"] >= I2_FLOOR
        )
        row["pass_i3"] = all(
            v is not None and 0.0 <= v <= I3_TOLERANCE
            for v in (row["i3_tx_residual"], row["i3_rx_residual"])
        )
        rows.append(row)
    return rows


def fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.2f}%"


def fmt_int(v) -> str:
    return "n/a" if v is None else f"{int(v):,}"


def render_markdown(rows: list[dict], mono: dict, args: argparse.Namespace) -> str:
    lines = [
        f"### V2-834 reconciliation -- {args.name}, {args.start} -> {args.end}",
        "",
        "| host | tier | dOutDgrams | sum tx dg | I1 tx | dInDgrams | sum rx dg | I1 rx | "
        "d net tx B | I2 tx | d net rx B | I2 rx | log-ship B | I3 tx resid | I3 rx resid | I1 | I2 | I3 |",
        "| -- | -- | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: | :-: | :-: | :-: |",
    ]
    for r in rows:
        if "error" in r:
            lines.append(
                f"| {r['host']} | {r['tier']} | ERROR: {r['error']} |" + " |" * 15
            )
            continue
        ok = lambda b: "PASS" if b else "FAIL"  # noqa: E731
        lines.append(
            f"| {r['host']} | {r['tier']} | {fmt_int(r['d_udp_out'])} | {fmt_int(r['sum_tx_datagrams'])} | "
            f"{fmt_pct(r['i1_tx'])} | {fmt_int(r['d_udp_in'])} | {fmt_int(r['sum_rx_datagrams'])} | "
            f"{fmt_pct(r['i1_rx'])} | {fmt_int(r['d_net_bytes_sent'])} | {fmt_pct(r['i2_tx'])} | "
            f"{fmt_int(r['d_net_bytes_recv'])} | {fmt_pct(r['i2_rx'])} | {fmt_int(r['log_shipping_bytes'])} | "
            f"{fmt_pct(r['i3_tx_residual'])} | {fmt_pct(r['i3_rx_residual'])} | "
            f"{ok(r['pass_i1'])} | {ok(r['pass_i2'])} | {ok(r['pass_i3'])} |"
        )
    good = [r for r in rows if "error" not in r]
    lines += [
        "",
        f"VMs: {len(rows)} ({sum(1 for r in good if r['tier'] == 'nat')} NAT'd); "
        f"I1 pass {sum(r['pass_i1'] for r in good)}/{len(good)}, "
        f"I2 pass {sum(r['pass_i2'] for r in good)}/{len(good)}, "
        f"I3 pass {sum(r['pass_i3'] for r in good)}/{len(good)}.",
        "",
        f"I4: {mono['series_checked']} (host, service, field) series checked; "
        f"{len(mono['decreases'])} decrease(s)"
        + (" -- **FAIL**" if mono["decreases"] else " -- PASS"),
    ]
    for d in mono["decreases"][:50]:
        lines.append(
            f"- {d['host']} svc {d['service']} `{d['field']}`: "
            f"{d['prev']['value']} @ {d['prev']['ts']} -> {d['next']['value']} @ {d['next']['ts']}"
        )
    lines += [
        "",
        "Numerators: I1 = sock_tx/rx_datagrams vs the namespace's Udp Out/InDatagrams. "
        f"I2 = sock_tx/rx_bytes + {HEADER_BYTES} B x datagrams (Ethernet+IPv4+UDP) over net.bytes_sent/recv. "
        "I3 residual = sock_*_bytes minus (udp + forwarded_to_target | relay_target_rx + relay_carrier + "
        "failed_dial [+ stateless on tx]); virtual_udp_* and bytes_relayed deliberately excluded. "
        "Log-ship B = ES doc count for the host x mean stored doc size (context only).",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--name", required=True, help="Deployment name (e.g. DEV-01).")
    ap.add_argument("--start", required=True, help="Window start, ISO-8601 UTC.")
    ap.add_argument("--end", required=True, help="Window end, ISO-8601 UTC.")
    ap.add_argument(
        "--snapshots",
        required=True,
        type=Path,
        help="JSONL from `analysis snapshot --out`.",
    )
    ap.add_argument("--start-tag", default="win-start")
    ap.add_argument("--end-tag", default="win-end")
    ap.add_argument(
        "--out", type=Path, help="Write the Markdown table here (also printed)."
    )
    ap.add_argument("--json-out", type=Path, help="Write every figure as JSON here.")
    ap.add_argument(
        "--skip-monotonic",
        action="store_true",
        help="Skip I4 (the slow per-sample export).",
    )
    args = ap.parse_args()

    run = EsRun(args.name)
    snaps = load_snapshots(args.snapshots, args.start_tag, args.end_tag)
    deltas = fetch_deltas(run, args.start, args.end)
    net = fetch_net_spread(run, args.start, args.end)
    shipping = log_shipping_estimate(run, args.start, args.end)
    mono = (
        {"series_checked": 0, "decreases": [], "skipped": True}
        if args.skip_monotonic
        else check_monotonic(run, args.start, args.end)
    )
    rows = build_rows(snaps, deltas, net, shipping)

    md = render_markdown(rows, mono, args)
    sys.stdout.write(md)
    if args.out:
        args.out.write_text(md)
    if args.json_out:
        args.json_out.write_text(
            json.dumps(
                {"rows": rows, "monotonic": mono, "deltas": deltas, "net": net},
                indent=2,
            )
        )
    good = [r for r in rows if "error" not in r]
    all_pass = (
        bool(good)
        and all(r["pass_i1"] and r["pass_i2"] and r["pass_i3"] for r in good)
        and not mono["decreases"]
        and len(good) == len(rows)
    )
    return 0 if all_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
