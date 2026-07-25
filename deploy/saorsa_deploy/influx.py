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
from typing import NamedTuple

import requests
from saorsa_db.models.host_metric_bucket import (
    METRIC_BYTES_IN_PER_SEC,
    METRIC_BYTES_OUT_PER_SEC,
    METRIC_CPU_PERCENT,
    METRIC_DISK_USED_BYTES,
    METRIC_MEM_PERCENT,
    METRIC_RSS_BYTES,
)

OP_MEASUREMENTS = ("upload", "download")

# Every Flux `range()` in this module must carry an explicit `stop`.
#
# Flux defaults `stop` to `now()`, which silently discards points timestamped in
# the FUTURE -- and we deliberately create hosts that produce exactly those.
# `provisioning/clock_skew.py` jumps a VM's *system* clock (nodes by a fixed
# SKEW_HOURS in a per-host random direction, clients by an explicit signed
# duration), so a forward-skewed host's Telegraf writes every point hours ahead
# of real time.
#
# The failure was silent and shape-dependent: on a long run a forward-skewed
# host merely lost its final `skew`-worth of data, while on a short run it
# vanished from the results entirely. Observed on DEV-03 (2026-07-25), where a
# +3h node VM contributed 0 of 27 hosts to `record-analysis` despite having 460
# `memory_rss` points sitting in the bucket.
#
# An absolute far-future bound rather than a relative one such as `30d`: the
# point is that no plausible clock is ever excluded, and a relative bound just
# moves the cliff somewhere less obvious. Empty future shard groups do not
# exist, so a wide stop costs nothing to scan.
FLUX_FAR_FUTURE = "2200-01-01T00:00:00Z"

PIVOT_FLUX_TEMPLATE = """from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
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
    stop: str = FLUX_FAR_FUTURE,
    timeout: int = 30,
) -> list[dict]:
    """Run the pivoted Flux query and return one dict per operation point.

    Each dict contains: ``_time`` (datetime, UTC), ``host`` (str),
    ``_measurement`` (str: "upload"|"download"), ``file_size`` (int, bytes),
    ``start_ns`` (int), ``status`` (int: 0=success, 1=failure), ``total_ms``
    (int).

    ``stop`` defaults to :data:`FLUX_FAR_FUTURE` so that clock-skewed client
    VMs — which testnet manifests create deliberately — are not silently
    dropped; see that constant's comment.
    """
    flux = PIVOT_FLUX_TEMPLATE.format(bucket=bucket, start=start, stop=stop)
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
                    "region": record.get("region", "") or None,
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


PROD_UPLOAD_FLUX_TEMPLATE = """from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "prod_upload")
  |> pivot(rowKey: ["_time", "host"], columnKey: ["_field"], valueColumn: "_value")
"""


def fetch_prod_upload_points(
    url: str,
    token: str,
    org: str,
    bucket: str,
    start: str = "0",
    stop: str = FLUX_FAR_FUTURE,
    timeout: int = 30,
) -> list[dict]:
    """Run a Flux query and return one dict per prod-upload point.

    Each dict contains: ``_time`` (datetime, UTC), ``host`` (str), ``region``
    (str), ``status`` (int: 0=success, 1=partial_upload, 2=download_failed),
    ``total_ms`` (int), ``upload_secs`` (float), ``file_size`` (int, bytes),
    ``start_ns`` (int), ``chunks_stored`` (int), ``chunks_failed`` (int),
    ``total_chunks`` (int), ``address`` (str), ``file_name`` (str),
    ``source_url`` (str), ``reason`` (str — populated on partial_upload from
    ant's JSON ``reason`` field; empty otherwise), ``storage_cost_atto`` (str —
    ANT spent in atto, uint256-shaped; empty for older ``ant`` binaries) and
    ``gas_cost_wei`` (str — gas spent in wei; empty likewise). The cost strings
    are returned verbatim; callers convert to ``Decimal`` to keep full precision.

    The default ``start: 0`` queries from the Unix epoch — prod uploaders
    are bounded runs so a wide window is the right default.
    """
    flux = PROD_UPLOAD_FLUX_TEMPLATE.format(bucket=bucket, start=start, stop=stop)
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
    return _parse_prod_upload_csv(resp.text)


def _parse_float(value: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_prod_upload_csv(body: str) -> list[dict]:
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
        if record.get("_measurement") != "prod_upload":
            continue
        if record.get("host") in (None, ""):
            continue
        try:
            points.append(
                {
                    "_time": _parse_ts(record["_time"]),
                    "host": record["host"],
                    "region": record.get("region", ""),
                    "status": _parse_int(record.get("status", "")),
                    "total_ms": _parse_int(record.get("total_ms", "")),
                    "upload_secs": _parse_float(record.get("upload_secs", "")),
                    "file_size": _parse_int(record.get("file_size", "")),
                    "start_ns": _parse_int(record.get("start_ns", "")),
                    "chunks_stored": _parse_int(record.get("chunks_stored", "")),
                    "chunks_failed": _parse_int(record.get("chunks_failed", "")),
                    "total_chunks": _parse_int(record.get("total_chunks", "")),
                    "address": record.get("address", "") or "",
                    "file_name": record.get("file_name", "") or "",
                    "source_url": record.get("source_url", "") or "",
                    "reason": record.get("reason", "") or "",
                    "storage_cost_atto": record.get("storage_cost_atto", "") or "",
                    "gas_cost_wei": record.get("gas_cost_wei", "") or "",
                }
            )
        except (KeyError, ValueError):
            continue
    return points


HOST_FLUX_CPU = """from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) =>
       r._measurement == "cpu"
       and r.cpu == "cpu-total"
       and r._field == "usage_idle")
"""

HOST_FLUX_MEM = """from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "mem" and r._field == "used_percent")
"""

HOST_FLUX_NET = """from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) =>
       r._measurement == "net"
       and r.interface != "lo"
       and (r._field == "bytes_recv" or r._field == "bytes_sent"))
"""

# Per-device disk I/O counters. Which devices exist at all is decided at
# collection time by the `devices` include-glob in telegraf_metrics.conf (which
# already keeps snapd `loop*` out), so no device filtering is needed here.
HOST_FLUX_DISKIO = """from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) =>
       r._measurement == "diskio"
       and (r._field == "read_bytes" or r._field == "write_bytes"
            or r._field == "reads" or r._field == "writes"))
"""

# Filesystem OCCUPANCY, per mountpoint. This is the numerator of "disk consumed
# per GB stored" and is a different question from HOST_FLUX_DISKIO above: diskio
# counts bytes written, which includes rewrites and compaction that leave
# occupancy unchanged, so it cannot answer how much data is resident.
#
# `used` only. `total`/`free` describe the volume the operator provisioned
# rather than anything the run did, and every extra field is another series per
# mountpoint per host.
#
# Not filtered to the node storage mountpoint here: which mountpoint that is
# varies per host (see the `volume_role` tag), and the whole-run rollup
# deliberately keeps every mountpoint. The bucket writer is what narrows it.
HOST_FLUX_DISK = """from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) =>
       r._measurement == "disk"
       and r._field == "used")
"""

# Per-service resident set size. Telegraf's node-tier config runs four
# `[[inputs.procstat]]` blocks (ant-node-*, ant-bootstrap-*, ant-bootstrap,
# ant-genesis-node) plus a regex processor deriving the `node_service` tag from
# `cgroup_full`, so each of the 15 services on a VM is individually
# identifiable. Field name and tag verified against a live bucket: procstat also
# emits `memory_usage`, but that is a percentage of host total, not bytes.
#
# The node tier lands in `testnet-metrics`; client-tier procstat is tagged
# `metric_source=ant-client` and lands in the separate `client-metrics` bucket,
# so no extra filter is needed to keep the two apart.
HOST_FLUX_PROCSTAT = """from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) =>
       r._measurement == "procstat"
       and r._field == "memory_rss")
"""


# Tag value marking the device that backs the node storage volume. Written by
# the Telegraf config's volume_role regex processor.
NODE_STORAGE_ROLE = "node-storage"

# diskio counter field -> the stat name it becomes (suffixed `_per_sec`).
DISKIO_FIELD_TARGETS = {
    "read_bytes": "read_bytes",
    "write_bytes": "write_bytes",
    "reads": "reads",
    "writes": "writes",
}

# Default bucket width for `bucket_host_metric_samples`, in seconds.
#
# Windowed re-aggregation is *exact* at any bucket width -- a sample-weighted
# mean of bucket averages is the true mean of the underlying samples, and
# MIN(min)/MAX(max)/SUM(samples) are the true extremes and count. Bucket width
# therefore costs only how finely a window *edge* can be placed and how smooth a
# trend plot is, not the accuracy of any comparison. 5 minutes keeps a run's
# stored rows at roughly a fifth of the 1-minute alternative, which matters:
# the registry runs on the smallest managed Postgres tier.
DEFAULT_BUCKET_SECS = 300


def _run_flux(url: str, token: str, org: str, flux: str, timeout: int = 30) -> str:
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
    return resp.text


def _parse_flux_csv(body: str) -> list[dict]:
    """Parse an annotated Flux CSV response into row dicts with a parsed ``_time``.

    Keeps every column from the response as-is except ``_time``, which is
    converted to a tz-aware ``datetime``. Rows without ``_time``, ``_value``
    or ``host`` are skipped. Blank lines and ``#``-prefixed metadata rows
    reset the table header.
    """
    rows: list[dict] = []
    reader = csv.reader(io.StringIO(body))
    header: list[str] | None = None
    for row in reader:
        if not row or not any(row):
            header = None
            continue
        if row[0].startswith("#"):
            header = None
            continue
        if header is None:
            header = row
            continue
        record = dict(zip(header, row))
        time_str = record.get("_time")
        if not time_str:
            continue
        if record.get("host") in (None, ""):
            continue
        try:
            record["_time"] = _parse_ts(time_str)
        except (KeyError, ValueError):
            continue
        rows.append(record)
    return rows


def fetch_host_metric_points(
    url: str,
    token: str,
    org: str,
    bucket: str,
    start: str = "0",
    stop: str = FLUX_FAR_FUTURE,
    timeout: int = 30,
) -> dict:
    """Run six Flux queries (cpu/mem/net/disk/diskio/procstat) and return raw points.

    Returns a dict with keys ``cpu``, ``mem``, ``net``, ``disk``, ``diskio``,
    ``procstat``, each mapping to a list of row dicts from
    :func:`_parse_flux_csv`.

    The default ``start: 0`` queries from the Unix epoch — i.e. every host
    metric point in the bucket — so a run's full history is recorded rather
    than only a trailing window. Pass a Flux duration (e.g. ``-6h``) to
    restrict it.

    ``stop`` defaults to :data:`FLUX_FAR_FUTURE` rather than Flux's implicit
    ``now()``, so a forward clock-skewed host's future-dated points are
    included instead of silently vanishing; see that constant's comment.
    """
    result: dict = {}
    for key, template in (
        ("cpu", HOST_FLUX_CPU),
        ("mem", HOST_FLUX_MEM),
        ("net", HOST_FLUX_NET),
        ("disk", HOST_FLUX_DISK),
        ("diskio", HOST_FLUX_DISKIO),
        ("procstat", HOST_FLUX_PROCSTAT),
    ):
        flux = template.format(bucket=bucket, start=start, stop=stop)
        body = _run_flux(url, token, org, flux, timeout=timeout)
        result[key] = _parse_flux_csv(body)
    return result


def _to_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _stats(samples: list[float]) -> dict:
    if not samples:
        return {"avg": None, "min": None, "max": None, "samples": 0}
    return {
        "avg": sum(samples) / len(samples),
        "min": min(samples),
        "max": max(samples),
        "samples": len(samples),
    }


def _usage_stats(pairs: list[tuple]) -> dict:
    """Stats for an occupancy series, plus its endpoints and net growth.

    ``first``/``last`` are the earliest and latest readings by timestamp, and
    ``growth_bytes`` is ``last - first`` — what the run actually added, which is
    the numerator of "disk consumed per GB stored".

    ``max - min`` is deliberately NOT used as the growth figure: it would report
    a run that filled the disk and was then pruned as if nothing had been
    reclaimed. It is still recorded (as ``min``/``max``) so that case is
    visible, and a ``growth_bytes`` well below ``max - min`` is exactly that
    signal.
    """
    if not pairs:
        return {
            "avg": None,
            "min": None,
            "max": None,
            "samples": 0,
            "first": None,
            "last": None,
            "growth_bytes": None,
        }
    ordered = sorted(pairs, key=lambda pair: pair[0])
    values = [value for _ts, value in ordered]
    stats = _stats(values)
    stats["first"] = ordered[0][1]
    stats["last"] = ordered[-1][1]
    stats["growth_bytes"] = ordered[-1][1] - ordered[0][1]
    return stats


class NormalisedHostMetrics(NamedTuple):
    """Output of :func:`normalise_host_metric_points`.

    ``samples`` is the flat, timestamped series — every counter already turned
    into a rate, every gauge passed through. ``time_bounds``, ``devices`` and
    ``mounts`` carry what the sample list alone cannot express: the observation
    window of a host (which includes points that contributed no sample), the
    existence of a device that produced no rate at all because only one point
    was collected for it, and which mountpoint on each host holds the node
    storage.
    """

    samples: list[dict]
    time_bounds: dict[str, list]
    devices: dict[str, dict]
    # host -> {mountpoint: volume_role|None}, from the `disk` measurement.
    # Defaulted so the three-field construction in older callers and tests still
    # builds; never mutated by any consumer.
    mounts: dict[str, dict] = {}


def normalise_host_metric_points(points_by_measurement: dict) -> NormalisedHostMetrics:
    """Turn raw Influx points into flat, timestamped, per-series samples.

    This is the delta/normalise stage, deliberately separate from any
    windowing. ``net`` and the four ``diskio`` series are **per-second deltas
    computed within an interface/device** — bucketing the raw monotonic
    counters first and differencing afterwards would produce nonsense, so the
    rate must be derived here, before any window is applied. ``cpu``, ``mem``
    and ``procstat`` RSS are gauges and pass straight through.

    Each sample is a dict with ``host``, ``metric``, ``device``,
    ``volume_role``, ``service``, ``ts`` and ``value``. A rate sample is
    timestamped at the **end** of the interval it describes: the rate is a
    property of the interval that just closed, and dating it to the interval's
    start would place the sample before the data that produced it existed.

    Negative deltas are dropped as counter resets (the counters restart when a
    VM reboots), as are non-advancing timestamps.
    """
    samples: list[dict] = []
    time_bounds: dict[str, list] = {}
    devices: dict[str, dict] = {}
    mounts: dict[str, dict] = {}

    def track_time(host: str, ts: datetime) -> None:
        bounds = time_bounds.setdefault(host, [None, None])
        if bounds[0] is None or ts < bounds[0]:
            bounds[0] = ts
        if bounds[1] is None or ts > bounds[1]:
            bounds[1] = ts

    def emit(
        host: str,
        metric: str,
        ts: datetime,
        value: float,
        device: str | None = None,
        volume_role: str | None = None,
        service: str | None = None,
    ) -> None:
        samples.append(
            {
                "host": host,
                "metric": metric,
                "device": device,
                "volume_role": volume_role,
                "service": service,
                "ts": ts,
                "value": value,
            }
        )

    for p in points_by_measurement.get("cpu", []):
        host = p.get("host")
        idle = _to_float(p.get("_value"))
        if not host or idle is None:
            continue
        emit(host, METRIC_CPU_PERCENT, p["_time"], 100.0 - idle)
        track_time(host, p["_time"])

    for p in points_by_measurement.get("mem", []):
        host = p.get("host")
        val = _to_float(p.get("_value"))
        if not host or val is None:
            continue
        emit(host, METRIC_MEM_PERCENT, p["_time"], val)
        track_time(host, p["_time"])

    # Per-service RSS is a gauge, so unlike net/diskio it needs no delta stage.
    # Keyed by the `node_service` tag, never by PID: services restart and PIDs
    # churn, while `ant-node-3` is stable and is what makes per-node analysis
    # possible on a host running 15 of them.
    for p in points_by_measurement.get("procstat", []):
        host = p.get("host")
        service = p.get("node_service")
        val = _to_float(p.get("_value"))
        if not host or not service or val is None:
            continue
        emit(host, METRIC_RSS_BYTES, p["_time"], val, service=service)
        track_time(host, p["_time"])

    # Filesystem occupancy is a gauge, like RSS -- no delta stage. Keyed by the
    # `path` tag (the mountpoint), which is the only key that identifies the
    # node storage volume on both raw-ext4 and LVM hosts; see the `disk`
    # volume_role processor in telegraf_metrics.conf.
    for p in points_by_measurement.get("disk", []):
        host = p.get("host")
        mount = p.get("path")
        val = _to_float(p.get("_value"))
        if not host or not mount or val is None:
            continue
        # Only the exact marker counts, exactly as for diskio devices: never
        # infer the storage volume from the tag merely being present.
        role = (
            NODE_STORAGE_ROLE if p.get("volume_role") == NODE_STORAGE_ROLE else None
        )
        seen = mounts.setdefault(host, {})
        if role is not None or mount not in seen:
            seen[mount] = role
        emit(
            host,
            METRIC_DISK_USED_BYTES,
            p["_time"],
            val,
            device=mount,
            volume_role=role,
        )
        track_time(host, p["_time"])

    net_groups: dict[tuple[str, str, str], list[dict]] = {}
    for p in points_by_measurement.get("net", []):
        host = p.get("host")
        interface = p.get("interface")
        field = p.get("_field")
        if not host or not interface or interface == "lo":
            continue
        if field not in ("bytes_recv", "bytes_sent"):
            continue
        net_groups.setdefault((host, interface, field), []).append(p)

    for (host, _interface, field), pts in net_groups.items():
        pts.sort(key=lambda p: p["_time"])
        metric = (
            METRIC_BYTES_IN_PER_SEC
            if field == "bytes_recv"
            else METRIC_BYTES_OUT_PER_SEC
        )
        for prev, curr in zip(pts, pts[1:]):
            prev_val = _to_float(prev.get("_value"))
            curr_val = _to_float(curr.get("_value"))
            if prev_val is None or curr_val is None:
                continue
            delta_bytes = curr_val - prev_val
            delta_sec = (curr["_time"] - prev["_time"]).total_seconds()
            if delta_bytes < 0 or delta_sec <= 0:
                continue
            # Pooled across a host's interfaces (unlike disk devices, which are
            # kept separate) -- the interfaces of one host carry the same
            # traffic role, so their sum is the host's bandwidth.
            emit(host, metric, curr["_time"], delta_bytes / delta_sec)
            track_time(host, prev["_time"])
            track_time(host, curr["_time"])

    # diskio counters are monotonic per device, exactly like the net counters
    # above: group by (host, device, field), walk consecutive pairs for a
    # per-second rate, and drop any negative delta (the counters reset when the
    # VM reboots) or non-advancing timestamp.
    diskio_groups: dict[tuple[str, str, str], list[dict]] = {}
    diskio_roles: dict[tuple[str, str], str] = {}
    for p in points_by_measurement.get("diskio", []):
        host = p.get("host")
        # The diskio device tag is `name` (e.g. "sdb"), not `device`.
        device = p.get("name")
        field = p.get("_field")
        if not host or not device:
            continue
        if field not in DISKIO_FIELD_TARGETS:
            continue
        # Only the exact "node-storage" marker counts. Any other value is just
        # an ordinary device -- never infer the storage volume from the tag
        # merely being present.
        if p.get("volume_role") == NODE_STORAGE_ROLE:
            diskio_roles[(host, device)] = NODE_STORAGE_ROLE
        diskio_groups.setdefault((host, device, field), []).append(p)

    for (host, device, field), pts in diskio_groups.items():
        pts.sort(key=lambda p: p["_time"])
        role = diskio_roles.get((host, device))
        # Registered even when the pair loop below yields nothing: a device seen
        # exactly once has no rate but does exist, and the rollup reports it
        # with zero samples rather than omitting it.
        devices.setdefault(host, {}).setdefault(device, role)
        if role is not None:
            devices[host][device] = role
        metric = f"{DISKIO_FIELD_TARGETS[field]}_per_sec"
        for prev, curr in zip(pts, pts[1:]):
            prev_val = _to_float(prev.get("_value"))
            curr_val = _to_float(curr.get("_value"))
            if prev_val is None or curr_val is None:
                continue
            delta = curr_val - prev_val
            delta_sec = (curr["_time"] - prev["_time"]).total_seconds()
            if delta < 0 or delta_sec <= 0:
                continue
            emit(
                host,
                metric,
                curr["_time"],
                delta / delta_sec,
                device=device,
                volume_role=role,
            )
            track_time(host, prev["_time"])
            track_time(host, curr["_time"])

    return NormalisedHostMetrics(
        samples=samples, time_bounds=time_bounds, devices=devices, mounts=mounts
    )


def aggregate_host_metrics(points_by_measurement: dict) -> list[dict]:
    """Aggregate raw host metric points into per-host summary dicts.

    For each host seen across the input points, produces one entry with:

    * ``cpu_percent`` — stats of ``100 - cpu.usage_idle`` (``cpu=cpu-total``)
    * ``mem_percent`` — stats of ``mem.used_percent``
    * ``bytes_in_per_sec`` / ``bytes_out_per_sec`` — stats of per-second
      deltas of ``net.bytes_recv`` / ``net.bytes_sent``, computed
      separately per interface (``lo`` excluded) then pooled.
    * ``disk_io_by_device`` — a dict keyed by kernel device name (``sdb``,
      ``dm-0``), each holding ``volume_role`` plus ``read_bytes_per_sec`` /
      ``write_bytes_per_sec`` / ``reads_per_sec`` / ``writes_per_sec`` stats of
      per-second deltas of the ``diskio`` counters.

      Deliberately NOT pooled across devices the way the net counters are
      pooled across interfaces: a host's root disk and its node storage volume
      are not interchangeable, and averaging an idle root disk against a busy
      data volume produces a number that describes neither. The device whose
      ``volume_role`` is ``"node-storage"`` is the node storage volume (see
      :func:`~saorsa_deploy.provisioning.telegraf_metrics.build_data_location_env_command`).
    * ``disk_usage_by_mount`` — a dict keyed by mountpoint
      (``/mnt/ant-node-storage``), each holding ``volume_role`` plus a
      ``used_bytes`` stat dict that additionally carries ``first``, ``last`` and
      ``growth_bytes``. This is OCCUPANCY, not I/O: it answers how much data is
      resident, which ``disk_io_by_device`` cannot, since bytes written include
      rewrites and compaction.
    * ``rss_by_service`` — a dict keyed by the ``node_service`` tag
      (``ant-node-3``), each holding stats of that service's resident set size
      in bytes. Likewise never pooled into a host total: the signal worth
      catching is *one* service leaking, which a 15-service mean would dilute
      fifteenfold. Empty for hosts that run no node services.
    * ``metrics_started_at`` / ``metrics_ended_at`` — earliest/latest
      timestamp of any point contributing to that host.

    Each stat dict is ``{"avg", "min", "max", "samples"}``; missing series
    produce all-``None`` stats with ``samples=0``.

    This is the whole-run view. It is now a thin consumer of
    :func:`normalise_host_metric_points` — the degenerate case of bucketing
    where a single bucket spans everything — so it and
    :func:`bucket_host_metric_samples` cannot drift apart in how they compute a
    rate. Its signature and output are unchanged.

    A caller that also wants buckets should normalise once and call
    :func:`aggregate_normalised_host_metrics` directly, rather than paying for
    the delta stage twice.
    """
    return aggregate_normalised_host_metrics(
        normalise_host_metric_points(points_by_measurement)
    )


def aggregate_normalised_host_metrics(
    normalised: NormalisedHostMetrics,
) -> list[dict]:
    """Collapse normalised samples to one whole-run entry per host.

    The windowing stage's degenerate case: a single bucket spanning everything.
    See :func:`aggregate_host_metrics` for the output shape.
    """
    grouped: dict[tuple, list[float]] = {}
    services: dict[str, set] = {}
    # Occupancy needs its samples in time order, which the flat value lists
    # above deliberately discard: growth over the run is last - first, and that
    # is the whole point of collecting it.
    usage_series: dict[tuple[str, str], list[tuple]] = {}
    for s in normalised.samples:
        key = (s["host"], s["metric"], s["device"], s["service"])
        grouped.setdefault(key, []).append(s["value"])
        if s["metric"] == METRIC_RSS_BYTES:
            services.setdefault(s["host"], set()).add(s["service"])
        elif s["metric"] == METRIC_DISK_USED_BYTES:
            usage_series.setdefault((s["host"], s["device"]), []).append(
                (s["ts"], s["value"])
            )

    def stats_for(
        host: str,
        metric: str,
        device: str | None = None,
        service: str | None = None,
    ) -> dict:
        return _stats(grouped.get((host, metric, device, service), []))

    results: list[dict] = []
    for host in sorted(normalised.time_bounds):
        started_at, ended_at = normalised.time_bounds[host]
        if started_at is None or ended_at is None:
            continue
        results.append(
            {
                "host": host,
                "metrics_started_at": started_at,
                "metrics_ended_at": ended_at,
                "cpu_percent": stats_for(host, METRIC_CPU_PERCENT),
                "mem_percent": stats_for(host, METRIC_MEM_PERCENT),
                "bytes_in_per_sec": stats_for(host, METRIC_BYTES_IN_PER_SEC),
                "bytes_out_per_sec": stats_for(host, METRIC_BYTES_OUT_PER_SEC),
                "disk_io_by_device": {
                    device: {
                        "volume_role": role,
                        **{
                            f"{target}_per_sec": stats_for(
                                host, f"{target}_per_sec", device=device
                            )
                            for target in DISKIO_FIELD_TARGETS.values()
                        },
                    }
                    for device, role in sorted(
                        normalised.devices.get(host, {}).items()
                    )
                },
                "disk_usage_by_mount": {
                    mount: {
                        "volume_role": role,
                        "used_bytes": _usage_stats(
                            usage_series.get((host, mount), [])
                        ),
                    }
                    for mount, role in sorted(
                        normalised.mounts.get(host, {}).items()
                    )
                },
                "rss_by_service": {
                    service: stats_for(
                        host, METRIC_RSS_BYTES, service=service
                    )
                    for service in sorted(services.get(host, ()))
                },
            }
        )
    return results


def _floor_to_bucket(ts: datetime, bucket_secs: int) -> datetime:
    epoch_secs = int(ts.timestamp())
    return datetime.fromtimestamp(
        epoch_secs - (epoch_secs % bucket_secs), tz=timezone.utc
    )


def bucket_host_metric_samples(
    normalised: NormalisedHostMetrics,
    bucket_secs: int = DEFAULT_BUCKET_SECS,
    node_storage_only: bool = True,
) -> list[dict]:
    """Collapse normalised samples into fixed-width time buckets.

    Returns one dict per ``(host, metric, device, volume_role, service,
    bucket_start)`` with ``avg``/``min``/``max``/``samples``, ready for
    ``DeploymentRepository.replace_host_metric_buckets``.

    Because the delta stage has already run, bucketing here is a pure
    windowing operation: every value being bucketed is a finished rate or a
    gauge reading, never a raw counter.

    ``node_storage_only`` restricts every per-device series — the four diskio
    rates and ``disk_used_bytes`` — to the device (or mountpoint) tagged
    ``node-storage``. On a typical VM the untagged devices are the root disk's
    boot/ESP partitions, whose counters merely duplicate the whole-disk
    counters — on one measured staging run they were 348 of 421 device series,
    the majority of all stored rows, for no signal. The whole-run rollup keeps
    every device and mountpoint regardless; this only bounds what is stored per
    bucket.
    """
    if bucket_secs <= 0:
        raise ValueError("bucket_secs must be positive")

    grouped: dict[tuple, list[float]] = {}
    for s in normalised.samples:
        if (
            node_storage_only
            and s["device"] is not None
            and s["volume_role"] != NODE_STORAGE_ROLE
        ):
            continue
        key = (
            s["host"],
            s["metric"],
            s["device"],
            s["volume_role"],
            s["service"],
            _floor_to_bucket(s["ts"], bucket_secs),
        )
        grouped.setdefault(key, []).append(s["value"])

    rows: list[dict] = []
    for key in sorted(
        grouped,
        key=lambda k: (k[0], k[1], k[2] or "", k[4] or "", k[5]),
    ):
        host, metric, device, volume_role, service, bucket_start = key
        stats = _stats(grouped[key])
        rows.append(
            {
                "host": host,
                "metric": metric,
                "device": device,
                "volume_role": volume_role,
                "service": service,
                "bucket_start": bucket_start,
                "avg": stats["avg"],
                "min": stats["min"],
                "max": stats["max"],
                "samples": stats["samples"],
            }
        )
    return rows


def node_storage_device(entry: dict) -> tuple[str, dict] | None:
    """Return the ``(device, stats)`` of a host entry's node storage volume.

    ``None`` when the host has no device tagged ``node-storage`` — a client VM,
    or a host provisioned before the tag existed.
    """
    for device, stats in (entry.get("disk_io_by_device") or {}).items():
        if stats.get("volume_role") == NODE_STORAGE_ROLE:
            return device, stats
    return None


def node_storage_mount(entry: dict) -> tuple[str, dict] | None:
    """Return the ``(mountpoint, stats)`` of a host entry's node storage volume.

    The occupancy counterpart of :func:`node_storage_device`. ``None`` when the
    host has no mountpoint tagged ``node-storage`` — a client VM, or a host
    provisioned before the ``disk`` tag existed (i.e. every run before this
    capture was added, since occupancy was never archived).
    """
    for mount, stats in (entry.get("disk_usage_by_mount") or {}).items():
        if stats.get("volume_role") == NODE_STORAGE_ROLE:
            return mount, stats
    return None


# A host's observation window may legitimately end a little before the others
# (a missed final scrape, a late start). This threshold is far larger than any
# scrape gap at the 10s collection interval, so anything beyond it is a genuine
# time offset rather than jitter.
CLOCK_OFFSET_THRESHOLD_SECS = 900


def detect_clock_offsets(
    aggregates: list[dict], threshold_secs: int = CLOCK_OFFSET_THRESHOLD_SECS
) -> list[tuple[str, float]]:
    """Return ``(host, offset_seconds)`` for hosts whose clock is out of step.

    Fixing the Flux ``stop`` bound means a forward clock-skewed host's points
    are no longer silently discarded — but including them is not the whole
    story. Their timestamps are still displaced by the skew, so slicing "the
    steady-state window" lands on a *different* stretch of that host's run than
    everyone else's. Surfacing the offset is what stops that becoming a second
    silent error in place of the first.

    The reference is the **median** of the per-host ``metrics_ended_at``, not
    the local clock: it needs no assumption about the machine running this
    command, and a handful of skewed hosts cannot drag it.

    A negative offset means the host's data ends *earlier* than its peers —
    either a backward-skewed clock or a host that stopped reporting, which is
    equally worth knowing about.
    """
    ends = sorted(
        a["metrics_ended_at"] for a in aggregates if a.get("metrics_ended_at")
    )
    if len(ends) < 2:
        return []
    mid = len(ends) // 2
    median = ends[mid] if len(ends) % 2 else ends[mid - 1]

    offsets = []
    for a in aggregates:
        ended = a.get("metrics_ended_at")
        if ended is None:
            continue
        delta = (ended - median).total_seconds()
        if abs(delta) > threshold_secs:
            offsets.append((a["host"], delta))
    offsets.sort(key=lambda pair: abs(pair[1]), reverse=True)
    return offsets


def peak_rss_service(entry: dict) -> tuple[str, dict] | None:
    """Return the ``(service, stats)`` of the host's heaviest node service.

    Picked by peak (``max``) RSS, which is what matters when hunting a leak.
    A *summary* scalar only — deliberately not a substitute for the per-service
    breakdown, since the whole point of keying RSS by service is that one
    service leaking is invisible in any host-level number.

    ``None`` for hosts running no node services, and for entries recorded
    before RSS capture existed.
    """
    best: tuple[str, dict] | None = None
    for service, stats in (entry.get("rss_by_service") or {}).items():
        if stats.get("max") is None:
            continue
        if best is None or stats["max"] > best[1]["max"]:
            best = (service, stats)
    return best
