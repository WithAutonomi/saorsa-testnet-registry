"""Normalised per-run metrics (V2-758).

Two soft gates came out NOT ASSESSED in the rc.1 release-train verdict because
only raw measurements existed:

* **Outbound bytes per GB of user traffic** — raw "Net OUT MB/s" cannot be
  compared across runs of different length and workload, and this figure is
  directly the bandwidth bill.
* **Disk consumed per GB stored** — occupancy and I/O were captured but the
  per-GB-stored figure was never derived.

This module computes both, plus the components each is built from, as pure
functions over data already collected elsewhere. Nothing here does I/O: the
callers (``deployments record-analysis`` and ``deployments derive-metrics``)
supply Influx points or bucket rows, and the repository persists the result.

Definitions
-----------

``outbound_bytes_per_gb_uploaded``
    Node-tier outbound bytes in the window ÷ GB successfully uploaded in the
    window.
``outbound_bytes_per_gb_traffic``
    The same numerator ÷ (GB uploaded + GB downloaded). Both ratios are stored
    because serving downloads and replicating uploads are different egress
    drivers, and which one a gate should use is a calibration question, not a
    capture question — and recapture is impossible once the run's InfluxDB is
    destroyed.
``disk_bytes_per_gb_stored``
    Growth of the node-storage volume's used bytes, summed over node hosts, ÷
    GB successfully uploaded. The expected value is well above 1: the network
    replicates each chunk, so several bytes are consumed per byte stored.

Scope and exactness
-------------------

The numerator covers whatever hosts are in the queried Influx bucket. For
``record-analysis`` that defaults to ``testnet-metrics``, which holds the node
tier (node, bootstrap and genesis VMs); client VMs report to a separate
``client-metrics`` bucket, so their egress — the workload generator, not our
bill — is excluded by construction rather than by a hostname heuristic.

Outbound bytes are summed from **raw counter deltas**, never integrated from
the stored per-second averages. ``normalise_host_metric_points`` emits one
sample per interface, so an average over those samples is a mean across a
host's interfaces rather than the host's total; integrating it would understate
a multi-NIC host. Summing the deltas sidesteps that entirely and is exact.

A window re-derived from buckets after teardown cannot do that — the raw
counters are gone — so it integrates bucket averages instead and is marked
``source="buckets"``. The two must never be compared as equivalent, which is
why every persisted row records which it is.
"""
from datetime import datetime, timezone

from saorsa_db.models.derived_metric import (
    METRIC_DISK_BYTES_PER_GB_STORED,
    METRIC_GB_DOWNLOADED,
    METRIC_GB_UPLOADED,
    METRIC_NODE_INBOUND_BYTES_TOTAL,
    METRIC_NODE_OUTBOUND_BYTES_TOTAL,
    METRIC_NODE_STORAGE_GROWTH_BYTES,
    METRIC_OUTBOUND_BYTES_PER_GB_TRAFFIC,
    METRIC_OUTBOUND_BYTES_PER_GB_UPLOADED,
)

from saorsa_deploy.influx import NODE_STORAGE_ROLE

# 1 GB == 2**30 bytes, matching how every other size in this codebase is
# rendered (`_fmt_bytes` divides by 1024**3). A metric whose unit silently
# disagreed with the reports around it would be a trap.
BYTES_PER_GB = 1024**3

UNIT_BYTES_PER_GB = "bytes_per_gb"
UNIT_BYTES = "bytes"
UNIT_GB = "gb"


def _to_float(value) -> float | None:
    """Flux CSV values arrive as strings; a malformed one is skipped, not fatal.

    Deliberately a local copy of the influx module's private helper rather than
    an import of it: this module owns how it reads raw points, and one blank
    field must never abort a whole run's extraction.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def sum_net_counter_deltas(
    points: list[dict], field: str, start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    """Total bytes moved, summed from raw ``net`` counter deltas.

    Groups by ``(host, interface)``, walks consecutive points in time order and
    adds each positive delta. Negative deltas are dropped as counter resets
    (the counters restart when a VM reboots), exactly as the rate path does.

    Summing deltas rather than integrating the stored averages is deliberate:
    the rate samples are emitted per interface and pooled by averaging, so a
    multi-NIC host's total is not recoverable from them.

    A delta is attributed to the window containing the **end** of its interval,
    matching where ``normalise_host_metric_points`` timestamps a rate sample.

    Returns ``{"total": int, "by_host": {host: int}, "hosts": int}``.
    """
    groups: dict[tuple, list[tuple]] = {}
    for p in points:
        host = p.get("host")
        interface = p.get("interface")
        if not host or not interface or interface == "lo":
            continue
        if p.get("_field") != field:
            continue
        value = _to_float(p.get("_value"))
        if value is None:
            continue
        groups.setdefault((host, interface), []).append((p["_time"], value))

    by_host: dict[str, float] = {}
    for (host, _interface), series in groups.items():
        series.sort(key=lambda pair: pair[0])
        for (prev_ts, prev_val), (curr_ts, curr_val) in zip(series, series[1:]):
            if curr_ts <= prev_ts:
                continue
            delta = curr_val - prev_val
            if delta < 0:
                continue
            if start is not None and curr_ts < start:
                continue
            if end is not None and curr_ts >= end:
                continue
            by_host[host] = by_host.get(host, 0.0) + delta

    return {
        "total": sum(by_host.values()),
        "by_host": by_host,
        "hosts": len(by_host),
    }


def sum_node_storage_growth(
    points: list[dict], start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    """Growth of the node-storage volume, summed across node hosts.

    Reads the raw ``disk`` points, keeps only those whose ``volume_role`` tag is
    exactly ``node-storage``, and takes ``last - first`` per host within the
    window.

    ``last - first``, not ``max - min``: a host that filled its volume and was
    then pruned has genuinely not consumed that peak, and reporting the peak
    would make a pruning run look like a regression. ``peak_growth`` (the
    max-min sum) is returned alongside so the case is visible rather than
    silently smoothed away.

    Hosts with no tagged mountpoint contribute nothing and are counted in
    ``hosts_without_series`` — on a run provisioned before the tag existed that
    will be every host, and the caller must then decline to publish the metric
    rather than report a growth of zero.
    """
    series: dict[str, list[tuple]] = {}
    hosts_seen: set = set()
    for p in points:
        host = p.get("host")
        if not host:
            continue
        hosts_seen.add(host)
        if p.get("volume_role") != NODE_STORAGE_ROLE:
            continue
        value = _to_float(p.get("_value"))
        if value is None:
            continue
        ts = p["_time"]
        if start is not None and ts < start:
            continue
        if end is not None and ts >= end:
            continue
        series.setdefault(host, []).append((ts, value))

    by_host: dict[str, float] = {}
    peak_by_host: dict[str, float] = {}
    for host, pairs in series.items():
        pairs.sort(key=lambda pair: pair[0])
        values = [value for _ts, value in pairs]
        by_host[host] = values[-1] - values[0]
        peak_by_host[host] = max(values) - min(values)

    return {
        "total": sum(by_host.values()),
        "by_host": by_host,
        "hosts": len(by_host),
        "hosts_without_series": len(hosts_seen - set(series)),
        "peak_growth": sum(peak_by_host.values()),
    }


def sum_node_storage_growth_from_endpoints(endpoints: list[dict]) -> dict:
    """The same growth, from bucket endpoints rather than raw points.

    Used when the window is re-derived after the monitoring stack is gone.
    ``endpoints`` comes from ``repo.get_host_metric_endpoints`` and holds a
    bucket *average* at each edge, so the figure is approximate to within about
    half a bucket width of growth at each end.

    There is no ``peak_growth`` here: an aggregate over buckets cannot
    distinguish a pruned run from a monotonic one.
    """
    by_host: dict[str, float] = {}
    for entry in endpoints:
        if entry.get("volume_role") != NODE_STORAGE_ROLE:
            continue
        first, last = entry.get("first"), entry.get("last")
        if first is None or last is None:
            continue
        host = entry["host"]
        by_host[host] = by_host.get(host, 0.0) + (last - first)
    return {
        "total": sum(by_host.values()),
        "by_host": by_host,
        "hosts": len(by_host),
        "hosts_without_series": 0,
        "peak_growth": None,
    }


def sum_outbound_from_buckets(
    buckets: list[dict], bucket_secs: int, metric: str
) -> dict:
    """Total bytes over a window, integrated from bucketed per-second averages.

    The post-teardown path: raw counters are gone, so each bucket contributes
    ``avg × bucket_secs``. Approximate in two ways, both bounded and both
    recorded as ``source="buckets"`` on the persisted row:

    * a bucket that lost scrapes still counts a full width;
    * the per-interface pooling in the rate samples means a multi-NIC host's
      average is a mean across its interfaces rather than a sum.

    Prefer :func:`sum_net_counter_deltas` whenever the raw points are available.
    """
    by_host: dict[str, float] = {}
    for row in buckets:
        if row.get("metric") != metric:
            continue
        avg = row.get("avg")
        if avg is None:
            continue
        host = row["host"]
        by_host[host] = by_host.get(host, 0.0) + (avg * bucket_secs)
    return {
        "total": sum(by_host.values()),
        "by_host": by_host,
        "hosts": len(by_host),
    }


def infer_bucket_secs(buckets: list[dict], default: int = 300) -> int:
    """Infer the stored bucket width from consecutive ``bucket_start`` values.

    Integrating an average into a total needs the width the buckets were
    written at, and that is not recorded anywhere on the row. Asking the
    operator to remember it invites a silently wrong answer: passing 60 for
    300-second buckets understates every byte total fivefold, with nothing to
    signal it.

    Uses the **most common** gap rather than the smallest or the mean: a run
    with a gap in collection has one oversized interval, and a boundary between
    two hosts' series has none at all, neither of which should move the width.

    Falls back to ``default`` when there are too few buckets to tell.
    """
    starts = sorted({row["bucket_start"] for row in buckets})
    if len(starts) < 2:
        return default
    gaps: dict[int, int] = {}
    for prev, curr in zip(starts, starts[1:]):
        gap = int((curr - prev).total_seconds())
        if gap > 0:
            gaps[gap] = gaps.get(gap, 0) + 1
    if not gaps:
        return default
    return max(gaps, key=lambda gap: (gaps[gap], -gap))


def format_metric_value(value, unit: str) -> str:
    """Render a derived metric in the unit a reader can act on.

    Bytes-per-GB is shown as GB per GB — an amplification factor — because that
    is how the gate is reasoned about ("we push 4x the data we store"); a raw
    byte count per GB is unreadable at fleet scale.
    """
    if value is None:
        return "-"
    if unit == UNIT_BYTES_PER_GB:
        return f"{value / BYTES_PER_GB:.2f} GB/GB"
    if unit == UNIT_BYTES:
        gb = value / BYTES_PER_GB
        if abs(gb) >= 1:
            return f"{gb:.2f} GB"
        return f"{value / (1024 * 1024):.0f} MB"
    return f"{value:.2f} GB"


def sum_traffic_gb(
    records: list, start: datetime | None = None, end: datetime | None = None
) -> dict:
    """GB of user traffic from per-op records, counting successes only.

    ``records`` are ``DeploymentUploadRecord`` / ``DeploymentDownloadRecord``
    rows. They are used rather than ``deployment_file_results`` because the
    aggregate table has no per-op timestamps and so cannot be windowed at all.

    An op is in the window when its ``finished_at`` is — the bytes moved on the
    network as the transfer completed, and an op still running at the window
    edge has not delivered a whole file to anyone.

    Failures are excluded from the denominator: a failed upload stored nothing,
    so counting it would flatter the ratio exactly when the run went worst.
    Their byte total is returned separately, because a large
    ``failed_bytes`` alongside a small ``bytes`` is worth seeing.
    """
    total = 0
    failed = 0
    ops = 0
    failed_ops = 0
    for record in records:
        finished = getattr(record, "finished_at", None)
        if finished is None:
            continue
        # Postgres hands back aware timestamps and SQLite naive ones; comparing
        # the two raises. Everything stored here is UTC, so attaching it states
        # a fact rather than converting.
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        if start is not None and finished < start:
            continue
        if end is not None and finished >= end:
            continue
        size = getattr(record, "file_size_bytes", 0) or 0
        status = getattr(record, "status", None)
        status_value = getattr(status, "value", status)
        if status_value == "success":
            total += size
            ops += 1
        else:
            failed += size
            failed_ops += 1
    return {
        "bytes": total,
        "gb": total / BYTES_PER_GB,
        "ops": ops,
        "failed_bytes": failed,
        "failed_ops": failed_ops,
    }


def _ratio(numerator: float, denominator: float) -> float | None:
    """``None`` rather than zero or infinity when the denominator is empty.

    A run that uploaded nothing has no meaningful bytes-per-GB, and publishing
    0 (or a division-by-zero crash) would put a fabricated value into a
    baseline comparison.
    """
    if not denominator:
        return None
    return numerator / denominator


def build_metric_rows(
    *,
    outbound: dict,
    inbound: dict,
    storage_growth: dict | None,
    uploaded: dict,
    downloaded: dict,
    window_start: datetime,
    window_end: datetime,
    window_label: str,
    source: str,
) -> list[dict]:
    """Assemble persistable rows from the computed components.

    Returns a list of kwargs dicts for ``repo.upsert_derived_metric``. A ratio
    whose denominator is empty, and the storage metric when no host reported a
    node-storage series, are **omitted entirely** rather than written as null:
    an absent row means "not measured", which is honest, while a null row would
    still be a row for a comparison to trip over.
    """
    gb_uploaded = uploaded["gb"]
    gb_downloaded = downloaded["gb"]
    gb_traffic = gb_uploaded + gb_downloaded

    common = {
        "window_label": window_label,
        "window_start": window_start,
        "window_end": window_end,
        "source": source,
    }
    rows: list[dict] = [
        {
            **common,
            "metric": METRIC_NODE_OUTBOUND_BYTES_TOTAL,
            "unit": UNIT_BYTES,
            "value": outbound["total"],
            "details": {
                "hosts": outbound["hosts"],
                "by_host": outbound["by_host"],
            },
        },
        {
            **common,
            "metric": METRIC_NODE_INBOUND_BYTES_TOTAL,
            "unit": UNIT_BYTES,
            "value": inbound["total"],
            "details": {"hosts": inbound["hosts"]},
        },
        {
            **common,
            "metric": METRIC_GB_UPLOADED,
            "unit": UNIT_GB,
            "value": gb_uploaded,
            "details": {
                "ops": uploaded["ops"],
                "bytes": uploaded["bytes"],
                "failed_ops": uploaded["failed_ops"],
                "failed_bytes": uploaded["failed_bytes"],
            },
        },
        {
            **common,
            "metric": METRIC_GB_DOWNLOADED,
            "unit": UNIT_GB,
            "value": gb_downloaded,
            "details": {
                "ops": downloaded["ops"],
                "bytes": downloaded["bytes"],
                "failed_ops": downloaded["failed_ops"],
                "failed_bytes": downloaded["failed_bytes"],
            },
        },
    ]

    if storage_growth is not None and storage_growth["hosts"]:
        rows.append(
            {
                **common,
                "metric": METRIC_NODE_STORAGE_GROWTH_BYTES,
                "unit": UNIT_BYTES,
                "value": storage_growth["total"],
                "details": {
                    "hosts": storage_growth["hosts"],
                    "hosts_without_series": storage_growth["hosts_without_series"],
                    "peak_growth_bytes": storage_growth["peak_growth"],
                    "by_host": storage_growth["by_host"],
                },
            }
        )

    per_gb_uploaded = _ratio(outbound["total"], gb_uploaded)
    if per_gb_uploaded is not None:
        rows.append(
            {
                **common,
                "metric": METRIC_OUTBOUND_BYTES_PER_GB_UPLOADED,
                "unit": UNIT_BYTES_PER_GB,
                "value": per_gb_uploaded,
                "numerator": outbound["total"],
                "denominator": gb_uploaded,
                "details": {
                    "hosts": outbound["hosts"],
                    "upload_ops": uploaded["ops"],
                },
            }
        )

    per_gb_traffic = _ratio(outbound["total"], gb_traffic)
    if per_gb_traffic is not None:
        rows.append(
            {
                **common,
                "metric": METRIC_OUTBOUND_BYTES_PER_GB_TRAFFIC,
                "unit": UNIT_BYTES_PER_GB,
                "value": per_gb_traffic,
                "numerator": outbound["total"],
                "denominator": gb_traffic,
                "details": {
                    "hosts": outbound["hosts"],
                    "gb_uploaded": gb_uploaded,
                    "gb_downloaded": gb_downloaded,
                },
            }
        )

    if storage_growth is not None and storage_growth["hosts"]:
        disk_per_gb = _ratio(storage_growth["total"], gb_uploaded)
        if disk_per_gb is not None:
            rows.append(
                {
                    **common,
                    "metric": METRIC_DISK_BYTES_PER_GB_STORED,
                    "unit": UNIT_BYTES_PER_GB,
                    "value": disk_per_gb,
                    "numerator": storage_growth["total"],
                    "denominator": gb_uploaded,
                    "details": {
                        "hosts": storage_growth["hosts"],
                        "hosts_without_series": storage_growth[
                            "hosts_without_series"
                        ],
                        "upload_ops": uploaded["ops"],
                    },
                }
            )

    return rows


def compute_from_points(
    *,
    points_by_measurement: dict,
    upload_records: list,
    download_records: list,
    window_start: datetime,
    window_end: datetime,
    window_label: str,
    source: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict]:
    """Compute every metric row from raw Influx points — the exact path.

    ``start``/``end`` bound which samples count; pass ``None`` for the whole
    run. ``window_start``/``window_end`` are what gets recorded, which for the
    whole run is the observed metric span rather than the (unbounded) filter.
    """
    outbound = sum_net_counter_deltas(
        points_by_measurement.get("net", []), "bytes_sent", start=start, end=end
    )
    inbound = sum_net_counter_deltas(
        points_by_measurement.get("net", []), "bytes_recv", start=start, end=end
    )
    storage_growth = sum_node_storage_growth(
        points_by_measurement.get("disk", []), start=start, end=end
    )
    return build_metric_rows(
        outbound=outbound,
        inbound=inbound,
        storage_growth=storage_growth,
        uploaded=sum_traffic_gb(upload_records, start=start, end=end),
        downloaded=sum_traffic_gb(download_records, start=start, end=end),
        window_start=window_start,
        window_end=window_end,
        window_label=window_label,
        source=source,
    )
