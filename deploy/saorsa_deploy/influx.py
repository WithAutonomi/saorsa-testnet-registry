"""InfluxDB Flux helpers for reading uploader/downloader file-op metrics.

The testnet client VMs run telegraf, which writes one Influx point per
upload/download operation to measurements `upload` and `download` in the
`testnet-metrics` bucket. See the monitoring stack for credentials.

Point schema (per operation):
    tags:   host, region
    fields: file_size (bytes), start_ns (ns since epoch), status (0=ok, 1=fail),
            total_ms, upload_secs|download_secs

This module exposes two pure-ish entry points:

* :func:`fetch_operation_points` — executes a Flux query that pivots the five
  fields onto one row per operation and returns a list of dicts.
* :func:`aggregate_file_results` — takes those dicts and produces one
  aggregate row per ``(measurement, host, file_size)`` group, ready to hand
  to the repository's ``upsert_file_result``.

Splitting them keeps the aggregation unit-testable without hitting a real
Influx instance.
"""
import csv
import io
from datetime import datetime, timezone

import requests

OP_MEASUREMENTS = ("upload", "download")

PIVOT_FLUX_TEMPLATE = """from(bucket: "{bucket}")
  |> range(start: {start})
  |> filter(fn: (r) => r._measurement == "upload" or r._measurement == "download")
  |> filter(fn: (r) =>
       r._field == "file_size" or
       r._field == "start_ns" or
       r._field == "status" or
       r._field == "total_ms")
  |> pivot(rowKey: ["_time", "host"], columnKey: ["_field"], valueColumn: "_value")
"""


def _parse_int(value: str) -> int | None:
    if value is None or value == "":
        return None
    return int(float(value))


def _parse_ts(value: str) -> datetime:
    """Parse an Influx RFC3339 timestamp into a tz-aware datetime."""
    # RFC3339 with nanosecond precision: trim to microseconds Python can parse.
    text = value.replace("Z", "+00:00")
    if "." in text:
        head, tail = text.split(".", 1)
        if "+" in tail:
            frac, tz = tail.split("+", 1)
            frac = frac[:6]
            text = f"{head}.{frac}+{tz}"
        elif "-" in tail:
            frac, tz = tail.split("-", 1)
            frac = frac[:6]
            text = f"{head}.{frac}-{tz}"
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def fetch_operation_points(
    url: str,
    token: str,
    org: str,
    bucket: str,
    start: str = "-30d",
    timeout: int = 30,
) -> list[dict]:
    """Run the pivoted Flux query and return one dict per operation point.

    Each dict contains: ``_time`` (datetime, UTC), ``host`` (str),
    ``_measurement`` (str: "upload"|"download"), ``file_size`` (int, bytes),
    ``start_ns`` (int), ``status`` (int: 0=success, 1=failure), ``total_ms``
    (int).
    """
    flux = PIVOT_FLUX_TEMPLATE.format(bucket=bucket, start=start)
    resp = requests.post(
        f"{url.rstrip('/')}/api/v2/query",
        params={"org": org},
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        },
        data=flux,
        timeout=timeout,
    )
    resp.raise_for_status()
    return _parse_pivoted_csv(resp.text)


def _parse_pivoted_csv(body: str) -> list[dict]:
    points: list[dict] = []
    reader = csv.reader(io.StringIO(body))
    header: list[str] | None = None
    for row in reader:
        if not row or not any(row):
            header = None
            continue
        if row[0].startswith("#"):
            continue
        if header is None:
            header = row
            continue
        record = dict(zip(header, row))
        if record.get("_measurement") not in OP_MEASUREMENTS:
            continue
        if record.get("host") in (None, ""):
            continue
        try:
            points.append(
                {
                    "_time": _parse_ts(record["_time"]),
                    "host": record["host"],
                    "_measurement": record["_measurement"],
                    "file_size": _parse_int(record.get("file_size", "")),
                    "start_ns": _parse_int(record.get("start_ns", "")),
                    "status": _parse_int(record.get("status", "")),
                    "total_ms": _parse_int(record.get("total_ms", "")),
                }
            )
        except (KeyError, ValueError):
            continue
    return points


def aggregate_file_results(points: list[dict]) -> list[dict]:
    """Group operation points by (measurement, host, file_size) and compute stats.

    Returns one dict per group with keys matching the repository's
    ``upsert_file_result`` parameters: ``op_type`` (str), ``client_hostname``,
    ``file_size_bytes``, ``first_started_at``, ``last_finished_at``,
    ``success_count``, ``failure_count``, ``avg_duration_ms``,
    ``min_duration_ms``, ``max_duration_ms``.

    Duration stats are computed only from successful operations
    (``status == 0``) because a failed op's ``total_ms`` reflects a timeout or
    abort, not a transfer time. Groups with zero successful ops report
    ``None`` for all three duration stats.
    """
    groups: dict[tuple[str, str, int], list[dict]] = {}
    for p in points:
        if p.get("file_size") is None:
            continue
        key = (p["_measurement"], p["host"], p["file_size"])
        groups.setdefault(key, []).append(p)

    results: list[dict] = []
    for (measurement, host, file_size), pts in groups.items():
        success = [p for p in pts if p.get("status") == 0]
        failure = [p for p in pts if p.get("status") == 1]
        success_durations = [
            p["total_ms"] for p in success if p.get("total_ms") is not None
        ]

        starts = [p["start_ns"] for p in pts if p.get("start_ns") is not None]
        if starts:
            first_started_at = datetime.fromtimestamp(
                min(starts) / 1_000_000_000, tz=timezone.utc
            )
        else:
            first_started_at = min(p["_time"] for p in pts)

        last_finished_at = max(p["_time"] for p in pts)

        if success_durations:
            avg_ms = sum(success_durations) // len(success_durations)
            min_ms = min(success_durations)
            max_ms = max(success_durations)
        else:
            avg_ms = min_ms = max_ms = None

        results.append(
            {
                "op_type": measurement,
                "client_hostname": host,
                "file_size_bytes": file_size,
                "first_started_at": first_started_at,
                "last_finished_at": last_finished_at,
                "success_count": len(success),
                "failure_count": len(failure),
                "avg_duration_ms": avg_ms,
                "min_duration_ms": min_ms,
                "max_duration_ms": max_ms,
            }
        )

    results.sort(
        key=lambda r: (r["client_hostname"], r["op_type"], r["file_size_bytes"])
    )
    return results
