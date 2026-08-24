"""Per-phase metric extraction (`analysis phase-extract`).

Everything here has to run before teardown: the run's Elasticsearch and
InfluxDB are destroyed with the testnet, and a results comment is what
survives.

Sources, and why each is the one used:

* **UDP datagrams / bytes** come from the phase-boundary `/proc` snapshots
  (`analysis snapshot`), not from InfluxDB, because /proc/net/snmp is
  per-network-namespace and the NAT-simulated VMs run their nodes inside
  `ant_nat` while telegraf reads the host namespace. InfluxDB still supplies
  the 1-minute series, but only for the plain tier, and that limit is reported
  rather than papered over.
* **Dial attempts and successes** come from ES phrase counts at INFO.
* **Connection closes and lifetimes do NOT appear here at all** -- the
  lifecycle events are `debug!` in saorsa-core and both close-side phrases
  return zero at INFO. They come from `analysis flowstat` over packet captures.
* **Audit and wire-traffic counters** are cumulative per service, emitted every
  300 s, so a phase delta covers whole samples only.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

from saorsa_deploy.analysis import hosts as hosts_mod
from saorsa_deploy.analysis.es import EsRun, per_host_udp_means
from saorsa_deploy.analysis.relay import RELAY_PHRASES
from saorsa_deploy.cmd.analysis import add_deployment_args, add_window_args

DIAL_SUCCESS = ["Connected to peer at", "Accepted connection from peer at"]
DIAL_FAILURE = ["Connection attempt failed", "connect_happy_eyeballs failed", "dial failed for all"]
# Recorded every phase so the zero is evidence, not an assumption: it is what
# justifies taking lifetimes from packet capture instead of logs.
CLOSE_PROBES = ["Disconnected from peer", "QUIC disconnect for peer"]
COUNTERS = [
    "tail.wire_tx_bytes",
    "tail.wire_tx_count",
    "tail.wire_rx_bytes",
    "tail.responsible_audit_fail_timeout",
    "tail.subtree_audit_fail_timeout",
    "tail.responsible_audit_pass",
    "tail.subtree_audit_pass",
]


def services_per_node_vm(vms: list[hosts_mod.Vm]) -> int:
    """Median service count per node VM: the divisor for per-node rates."""
    counts = [len(v.services) for v in vms if v.services]
    if not counts:
        raise ValueError("inventory lists no services on any node VM")
    return int(statistics.median(counts))


def load_snapshot(path: str | Path) -> dict[str, dict]:
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        r = json.loads(line)
        if "error" in r:
            continue
        out[r["ip"]] = r
    return out


def _stats(vals: list[float]) -> dict | None:
    vals = sorted(vals)
    if not vals:
        return None
    return {
        "n": len(vals),
        "mean": round(statistics.mean(vals), 2),
        "sd": round(statistics.pstdev(vals), 2) if len(vals) > 1 else 0.0,
        "median": round(statistics.median(vals), 2),
        "min": round(vals[0], 2),
        "max": round(vals[-1], 2),
    }


def snapshot_delta(before_path: str | Path, after_path: str | Path, node_services: int) -> dict:
    """Exact per-VM UDP deltas between two phase-boundary snapshots."""
    a, b = load_snapshot(before_path), load_snapshot(after_path)
    rows = []
    for ip, rb in b.items():
        ra = a.get(ip)
        if not ra:
            continue
        secs = rb["ts"] - ra["ts"]
        if secs <= 0:
            continue
        nat = rb["nat"] == 1
        # On a NAT'd VM the node traffic is inside the namespace; on a plain VM
        # it is the host's own. Picking per-VM keeps one comparable series.
        pfx = "ns_" if nat else "host_"
        d_out = rb[pfx + "udp_out"] - ra[pfx + "udp_out"]
        d_in = rb[pfx + "udp_in"] - ra[pfx + "udp_in"]
        d_tx = rb[pfx + "tx_bytes"] - ra[pfx + "tx_bytes"]
        d_rx = rb[pfx + "rx_bytes"] - ra[pfx + "rx_bytes"]
        d_err = rb[pfx + "udp_rcvbuf_err"] - ra[pfx + "udp_rcvbuf_err"]
        if min(d_out, d_in, d_tx, d_rx) < 0:
            continue  # counter reset (reboot); excluded rather than clamped
        rows.append(
            {
                "ip": ip,
                "host": rb["host"],
                "nat": nat,
                "seconds": secs,
                "udp_out_per_sec": d_out / secs,
                "udp_in_per_sec": d_in / secs,
                "tx_bytes_per_sec": d_tx / secs,
                "rx_bytes_per_sec": d_rx / secs,
                "rcvbuf_err": d_err,
                "udp_out_per_sec_per_node": d_out / secs / node_services,
                "tx_bytes_per_sec_per_node": d_tx / secs / node_services,
            }
        )

    out: dict = {
        "vms": len(rows),
        "node_services_per_vm": node_services,
        "window_seconds": statistics.median(r["seconds"] for r in rows) if rows else None,
    }
    tiers = (
        ("all", rows),
        ("nat", [r for r in rows if r["nat"]]),
        ("plain", [r for r in rows if not r["nat"]]),
    )
    for tier, sel in tiers:
        out[tier] = {
            "udp_out_per_sec_per_vm": _stats([r["udp_out_per_sec"] for r in sel]),
            "udp_out_per_sec_per_node": _stats([r["udp_out_per_sec_per_node"] for r in sel]),
            "tx_bytes_per_sec_per_vm": _stats([r["tx_bytes_per_sec"] for r in sel]),
            "tx_bytes_per_sec_per_node": _stats([r["tx_bytes_per_sec_per_node"] for r in sel]),
            "rcvbuf_err_total": sum(r["rcvbuf_err"] for r in sel),
        }
    out["per_vm"] = rows
    return out


def influx_udp_summary(
    rows: list[dict], node_hosts: set[str], nat_hosts: set[str], node_services: int
) -> dict:
    """Plain-tier per-VM/per-node UDP rate from InfluxDB, NAT tier excluded.

    Which hosts are NAT'd comes from the inventory -- NOT inferred from whether
    the series looks empty. A NAT'd VM still emits a trickle of host-namespace
    UDP (~0.1/s), so "max > 0" does not separate the tiers.
    """
    by_host = per_host_udp_means(rows)
    plain = {
        h: v for h, v in by_host.items()
        if h.lower() in node_hosts and h.lower() not in nat_hosts
    }
    nat_seen = {h: v for h, v in by_host.items() if h.lower() in nat_hosts}
    per_host_mean = list(plain.values())
    nat_means = list(nat_seen.values())
    bucket_counts: dict[str, int] = {}
    for r in rows:
        bucket_counts[r.get("host", "?")] = bucket_counts.get(r.get("host", "?"), 0) + 1
    plain_buckets = [bucket_counts[h] for h in plain]
    return {
        "hosts_reporting": len(by_host),
        "hosts_plain_tier": len(plain),
        "hosts_nat_excluded": len(nat_seen),
        "buckets_per_host_median": statistics.median(plain_buckets) if plain_buckets else None,
        "plain_per_vm_mean_of_means": (
            round(statistics.mean(per_host_mean), 2) if per_host_mean else None
        ),
        "plain_per_vm_sd_across_hosts": (
            round(statistics.pstdev(per_host_mean), 2) if len(per_host_mean) > 1 else None
        ),
        "plain_per_node_mean": (
            round(statistics.mean(per_host_mean) / node_services, 2) if per_host_mean else None
        ),
        "nat_residual_per_vm_mean": round(statistics.mean(nat_means), 3) if nat_means else None,
        "note": (
            "NAT'd VMs are EXCLUDED, not zero-valued: telegraf reads the host "
            "namespace and their nodes run inside ant_nat, so this series sees "
            "only their host-namespace residual (reported above as a check that "
            "it is negligible). NAT-tier figures come from the /proc snapshots."
        ),
    }


def cmd_phase_extract(args) -> int:
    import requests

    requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
    run = EsRun(args.name)
    vms = hosts_mod.node_vms(args.name, args.inventory)
    node_services = services_per_node_vm(vms)
    node_hosts = {v.name.lower() for v in vms}
    nat_hosts = {v.name.lower() for v in vms if v.nat}
    start, end = args.start, args.end
    result: dict = {"label": args.label, "start": start, "end": end}

    result["dial_success"] = {p: run.count_phrase(p, start, end) for p in DIAL_SUCCESS}
    result["dial_failure"] = {p: run.count_phrase(p, start, end) for p in DIAL_FAILURE}
    result["relay"] = {p: run.count_phrase(p, start, end) for _, p in RELAY_PHRASES}
    result["close_probes"] = {p: run.count_phrase(p, start, end) for p in CLOSE_PROBES}

    connects = result["dial_success"]["Connected to peer at"]["count"]
    fails = max(v["count"] for v in result["dial_failure"].values())
    attempts = connects + fails
    result["dial_success_rate"] = round(connects / attempts, 4) if attempts else None
    result["dial_failure_note"] = (
        "failures aggregated as MAX across the three phrases, not sum: they are "
        "one failure logged at three layers (counts track within 0.5%)"
    )

    result["counters"] = {}
    for f in COUNTERS:
        try:
            result["counters"][f] = run.counter_delta(f, start, end)
        except Exception as e:  # noqa: BLE001
            result["counters"][f] = {"error": str(e)}
    result["counter_note"] = (
        "cumulative per-service counters emitted every 300s; a window's delta "
        "covers whole 300s samples only, so effective coverage is up to 5 min "
        "shorter than the window"
    )

    try:
        rows = run.udp_datagrams(start, end, every="1m")
        result["influx_udp_out_1m"] = influx_udp_summary(rows, node_hosts, nat_hosts, node_services)
    except Exception as e:  # noqa: BLE001
        result["influx_udp_out_1m"] = {"error": str(e)}

    if args.snap_before and args.snap_after:
        result["snapshot_delta"] = snapshot_delta(args.snap_before, args.snap_after, node_services)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"extract-{args.label}.json"
    path.write_text(json.dumps(result, indent=2))
    print(f"wrote {path}")
    skip = ("counters", "relay", "dial_success", "dial_failure", "close_probes")
    print(json.dumps({k: v for k, v in result.items() if k not in skip}, indent=2)[:2000])
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "phase-extract",
        help=(
            "Per-phase record set: dial success, relay events, cumulative counter "
            "deltas, InfluxDB UDP rate (plain tier) and optional snapshot deltas."
        ),
    )
    add_deployment_args(p)
    p.add_argument(
        "--label", required=True, help="Phase label, e.g. p1 (output extract-<label>.json)."
    )
    add_window_args(p)
    p.add_argument(
        "--snap-before", default=None, help="`analysis snapshot` JSONL at the window start."
    )
    p.add_argument(
        "--snap-after", default=None, help="`analysis snapshot` JSONL at the window end."
    )
    p.add_argument("--out-dir", default=".")
    p.set_defaults(func=cmd_phase_extract)
