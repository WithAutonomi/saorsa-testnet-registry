"""Per-service RSS slope on ant-prod-01, from InfluxDB (record-only).

The staging memory-leak gate passed at 4.239 %/h against a 5 %/h limit on the
2026-09-29 train -- close enough to the line to be worth watching in production.
But staging's threshold does not transfer: different node density, uptime and
load. Production's median over a 6h window measured **0.572 %/h** on 2026-10-01,
an order of magnitude below the staging figure, so applying 5 % here would be
false precision.

So this prints the distribution and asserts nothing. It always exits 0. The
numbers accumulate across trains until there is enough history to set a real
threshold, which is the only honest way to get one.

`ant_node_memory` carries `vm_rss` per `(host, node_service)` but **no version
tag**, so this cannot split by rollout arm -- unlike the Elasticsearch-based
scripts here. Run it once the fleet is uniform, where that does not matter.
Note also that Influx host names are mixed-case (`ant-prod-01-cntb-AUS-1`) while
Elasticsearch's are upper-case, so the two are not directly joinable.

Usage:
    uv run scripts/rollout-watch/rss_slope.py [--window 24h] [--json-out out.json]
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    INFLUX_BUCKET,
    InfluxClient,
    RolloutCheckError,
    emit,
    markdown_table,
    parse_window,
    quiet_tls,
    window_label,
)

MEASUREMENT = "ant_node_memory"
FIELD = "vm_rss"
BYTES_PER_MB = 1048576.0


def build_flux(window: timedelta) -> str:
    hours = window.total_seconds() / 3600.0
    return f'''base = from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{window_label(window)})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and r._field == "{FIELD}")
  |> group(columns: ["host", "node_service"])
first = base |> first() |> keep(columns: ["host","node_service","_value"]) |> rename(columns: {{_value: "rss_first"}})
last  = base |> last()  |> keep(columns: ["host","node_service","_value"]) |> rename(columns: {{_value: "rss_last"}})
join(tables: {{f: first, l: last}}, on: ["host","node_service"])
  |> map(fn: (r) => ({{ r with
      pct_per_hour: (float(v: r.rss_last - r.rss_first) / float(v: r.rss_first)) * 100.0 / {hours},
      mb_per_hour: float(v: r.rss_last - r.rss_first) / {BYTES_PER_MB} / {hours} }}))
  |> keep(columns: ["host","node_service","rss_first","rss_last","pct_per_hour","mb_per_hour"])
'''


def collect(rows: list[dict]) -> list[dict]:
    """Keep the rows that carry a numeric slope.

    Flux repeats its header per result table, so a plain DictReader over the
    whole body yields rows whose values are the column names. Those are dropped
    here rather than crashing the run.
    """
    out = []
    for row in rows:
        try:
            pct = float(row["pct_per_hour"])
            mb = float(row["mb_per_hour"])
            first = float(row["rss_first"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(
            {
                "service": f"{row.get('host')}/{row.get('node_service')}",
                "pct_per_hour": pct,
                "mb_per_hour": mb,
                "rss_first_mb": first / BYTES_PER_MB,
            }
        )
    return out


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    position = fraction * (len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def summarise(records: list[dict]) -> dict:
    if not records:
        return {"measured": 0}
    pct = sorted(r["pct_per_hour"] for r in records)
    mb = sorted(r["mb_per_hour"] for r in records)
    worst = max(records, key=lambda r: r["mb_per_hour"])
    return {
        "measured": len(records),
        "median_pct_per_hour": round(statistics.median(pct), 3),
        "p95_pct_per_hour": round(_percentile(pct, 0.95), 3),
        "max_pct_per_hour": round(pct[-1], 3),
        "median_mb_per_hour": round(statistics.median(mb), 2),
        "max_mb_per_hour": round(mb[-1], 2),
        "worst_service": worst["service"],
        "top": [
            {
                "service": r["service"],
                "mb_per_hour": round(r["mb_per_hour"], 1),
                "pct_per_hour": round(r["pct_per_hour"], 2),
            }
            for r in sorted(records, key=lambda r: -r["mb_per_hour"])[:10]
        ],
    }


def render(result: dict) -> str:
    summary = result["summary"]
    lines = [
        "### Node RSS slope — ant-prod-01 (record-only, no pass rule)",
        "",
        f"Window {result['window']}, `{MEASUREMENT}.{FIELD}`, "
        f"{summary.get('measured', 0)} service(s).",
        "",
    ]
    if not summary.get("measured"):
        lines.append("_No RSS points in the window._")
        return "\n".join(lines) + "\n"
    lines += [
        markdown_table(
            ["median %/h", "p95 %/h", "max %/h", "median MB/h", "max MB/h"],
            [
                [
                    f"{summary['median_pct_per_hour']:.3f}",
                    f"{summary['p95_pct_per_hour']:.3f}",
                    f"{summary['max_pct_per_hour']:.3f}",
                    f"{summary['median_mb_per_hour']:.2f}",
                    f"{summary['max_mb_per_hour']:.2f}",
                ]
            ],
            align="rrrrr",
        ),
        "",
        "Fastest-growing services:",
        "",
    ]
    lines += [
        f"- `{row['service']}` — {row['mb_per_hour']} MB/h ({row['pct_per_hour']}%/h)"
        for row in summary["top"]
    ]
    lines += [
        "",
        "Recorded, not gated. The staging memory-leak gate's 5 %/h limit is "
        "calibrated to the staging file mix and node density and does not transfer "
        "to production, whose median sits an order of magnitude below it. "
        f"`{MEASUREMENT}` has no version tag, so this cannot be split by rollout "
        "arm; run it once the fleet is uniform.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--window", default="24h", help="Measurement window, e.g. 6h, 24h."
    )
    parser.add_argument(
        "--json-out", default=None, help="Write every figure as JSON here."
    )
    args = parser.parse_args(argv)

    quiet_tls()
    try:
        window = parse_window(args.window)
        rows = InfluxClient.from_env().query(build_flux(window))
    except RolloutCheckError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    records = collect(rows)
    result = {
        "window": window_label(window),
        "measurement": f"{MEASUREMENT}.{FIELD}",
        "summary": summarise(records),
        "services": records,
    }
    emit(result, render(result), args.json_out)
    # Record-only: this script never fails a watch.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
