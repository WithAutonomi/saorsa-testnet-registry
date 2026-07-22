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
  |> range(start: {start})
  |> filter(fn: (r) => r._measurement == "prod_upload")
  |> pivot(rowKey: ["_time", "host"], columnKey: ["_field"], valueColumn: "_value")
"""


def fetch_prod_upload_points(
    url: str,
    token: str,
    org: str,
    bucket: str,
    start: str = "0",
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
    flux = PROD_UPLOAD_FLUX_TEMPLATE.format(bucket=bucket, start=start)
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
  |> range(start: {start})
  |> filter(fn: (r) =>
       r._measurement == "cpu"
       and r.cpu == "cpu-total"
       and r._field == "usage_idle")
"""

HOST_FLUX_MEM = """from(bucket: "{bucket}")
  |> range(start: {start})
  |> filter(fn: (r) => r._measurement == "mem" and r._field == "used_percent")
"""

HOST_FLUX_NET = """from(bucket: "{bucket}")
  |> range(start: {start})
  |> filter(fn: (r) =>
       r._measurement == "net"
       and r.interface != "lo"
       and (r._field == "bytes_recv" or r._field == "bytes_sent"))
"""

# Per-device disk I/O counters. Which devices exist at all is decided at
# collection time by the `devices` include-glob in telegraf_metrics.conf (which
# already keeps snapd `loop*` out), so no device filtering is needed here.
HOST_FLUX_DISKIO = """from(bucket: "{bucket}")
  |> range(start: {start})
  |> filter(fn: (r) =>
       r._measurement == "diskio"
       and (r._field == "read_bytes" or r._field == "write_bytes"
            or r._field == "reads" or r._field == "writes"))
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
    timeout: int = 30,
) -> dict:
    """Run four Flux queries (cpu/mem/net/diskio) and return the raw points.

    Returns a dict with keys ``cpu``, ``mem``, ``net``, ``diskio``, each
    mapping to a list of row dicts from :func:`_parse_flux_csv`.

    The default ``start: 0`` queries from the Unix epoch — i.e. every host
    metric point in the bucket — so a run's full history is recorded rather
    than only a trailing window. Pass a Flux duration (e.g. ``-6h``) to
    restrict it.
    """
    result: dict = {}
    for key, template in (
        ("cpu", HOST_FLUX_CPU),
        ("mem", HOST_FLUX_MEM),
        ("net", HOST_FLUX_NET),
        ("diskio", HOST_FLUX_DISKIO),
    ):
        flux = template.format(bucket=bucket, start=start)
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
      :func:`~saorsa_deploy.provisioning.telegraf_metrics.build_data_device_env_command`).
    * ``metrics_started_at`` / ``metrics_ended_at`` — earliest/latest
      timestamp of any point contributing to that host.

    Each stat dict is ``{"avg", "min", "max", "samples"}``; missing series
    produce all-``None`` stats with ``samples=0``.
    """
    hosts: dict[str, dict] = {}

    def host_entry(name: str) -> dict:
        return hosts.setdefault(
            name,
            {
                "cpu_samples": [],
                "mem_samples": [],
                "bytes_in_samples": [],
                "bytes_out_samples": [],
                # device -> {"volume_role": str|None, "<target>": [rates]}
                "disk_devices": {},
                "metrics_started_at": None,
                "metrics_ended_at": None,
            },
        )

    def track_time(entry: dict, ts: datetime) -> None:
        if entry["metrics_started_at"] is None or ts < entry["metrics_started_at"]:
            entry["metrics_started_at"] = ts
        if entry["metrics_ended_at"] is None or ts > entry["metrics_ended_at"]:
            entry["metrics_ended_at"] = ts

    for p in points_by_measurement.get("cpu", []):
        host = p.get("host")
        idle = _to_float(p.get("_value"))
        if not host or idle is None:
            continue
        entry = host_entry(host)
        entry["cpu_samples"].append(100.0 - idle)
        track_time(entry, p["_time"])

    for p in points_by_measurement.get("mem", []):
        host = p.get("host")
        val = _to_float(p.get("_value"))
        if not host or val is None:
            continue
        entry = host_entry(host)
        entry["mem_samples"].append(val)
        track_time(entry, p["_time"])

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
        entry = host_entry(host)
        target = (
            "bytes_in_samples" if field == "bytes_recv" else "bytes_out_samples"
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
            entry[target].append(delta_bytes / delta_sec)
            track_time(entry, prev["_time"])
            track_time(entry, curr["_time"])

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
        entry = host_entry(host)
        dev_entry = entry["disk_devices"].setdefault(
            device,
            {"volume_role": diskio_roles.get((host, device))},
        )
        target = DISKIO_FIELD_TARGETS[field]
        samples = dev_entry.setdefault(target, [])
        for prev, curr in zip(pts, pts[1:]):
            prev_val = _to_float(prev.get("_value"))
            curr_val = _to_float(curr.get("_value"))
            if prev_val is None or curr_val is None:
                continue
            delta = curr_val - prev_val
            delta_sec = (curr["_time"] - prev["_time"]).total_seconds()
            if delta < 0 or delta_sec <= 0:
                continue
            samples.append(delta / delta_sec)
            track_time(entry, prev["_time"])
            track_time(entry, curr["_time"])

    results: list[dict] = []
    for host in sorted(hosts.keys()):
        h = hosts[host]
        if h["metrics_started_at"] is None or h["metrics_ended_at"] is None:
            continue
        results.append(
            {
                "host": host,
                "metrics_started_at": h["metrics_started_at"],
                "metrics_ended_at": h["metrics_ended_at"],
                "cpu_percent": _stats(h["cpu_samples"]),
                "mem_percent": _stats(h["mem_samples"]),
                "bytes_in_per_sec": _stats(h["bytes_in_samples"]),
                "bytes_out_per_sec": _stats(h["bytes_out_samples"]),
                "disk_io_by_device": {
                    device: {
                        "volume_role": dev["volume_role"],
                        **{
                            f"{target}_per_sec": _stats(dev.get(target, []))
                            for target in DISKIO_FIELD_TARGETS.values()
                        },
                    }
                    for device, dev in sorted(h["disk_devices"].items())
                },
            }
        )
    return results


def node_storage_device(entry: dict) -> tuple[str, dict] | None:
    """Return the ``(device, stats)`` of a host entry's node storage volume.

    ``None`` when the host has no device tagged ``node-storage`` — a client VM,
    or a host provisioned before the tag existed.
    """
    for device, stats in (entry.get("disk_io_by_device") or {}).items():
        if stats.get("volume_role") == NODE_STORAGE_ROLE:
            return device, stats
    return None
