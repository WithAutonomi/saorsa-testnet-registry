"""Deterministic candidate-vs-baseline gate comparison (V2-721).

The release process requires that a *tool*, not an agent and not a human,
computes the staging verdict: it reads the registry Postgres, compares a run
against a baseline run, applies versioned thresholds, and emits a per-gate
PASS / FAIL / GRAY verdict. Same inputs must always produce an identical report.

Everything here is a pure function over data the caller has already fetched.
The CLI wrapper does the I/O; this module does the arithmetic. That split is
what makes determinism testable -- and it is the same split
``derived_metrics.py`` uses.

Scope
-----

This module evaluates only what the registry actually stores:

* the two **success floors** (hard),
* and the nine **soft gates** -- success rate, transfer duration p50/p95 per
  file-size bucket, within-window transfer-duration drift per bucket, node CPU,
  node RSS, memory leak slope, outbound bytes per GB, and disk bytes per GB
  stored.

The other four hard gates from the process spec -- data integrity, crashes,
mixed-version chunk storage, and production canaries -- are deliberately absent.
Nothing in Postgres records them: content verification does not exist anywhere
in the system, node panics reach stderr/journald rather than the JSON logs
Telegraf tails (and Elasticsearch dies with the testnet), and the canary is a
separate deployment with its own assessment. The accompanying skill gathers
those observationally and merges them into the release-issue report.

Three invariants
----------------

These come from what V2-758 learned building the metrics this consumes, and
each one silently produces a wrong verdict if dropped:

1. **Never compare across ``source`` values.** ``capture`` sums raw counter
   deltas and is exact; ``buckets`` integrates averaged rates and is
   approximate. Applying a +-10% threshold across that boundary measures the
   method, not the release.
2. **Absence is NOT ASSESSED, never zero.** A ratio with an empty denominator
   produces no row at all. Treating a missing row as 0.0 would trip every
   threshold and read as a catastrophic regression.
3. **Windows of materially different length are not comparable.** Replication
   continues after an upload finishes, so a short window truncates the disk
   numerator while still counting the full denominator -- measured at 7.75 vs
   3.85 GB/GB on the same DEV-03 run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from saorsa_db.models.derived_metric import (
    METRIC_DISK_BYTES_PER_GB_STORED,
    WINDOW_FULL_RUN,
)
from saorsa_db.models.host_metric_bucket import (
    METRIC_CPU_PERCENT,
    METRIC_RSS_BYTES,
)

from saorsa_deploy.gate_config import GateConfig

# Verdicts. GRAY is the process's "one criterion but not the other" outcome and
# means RM judgment; NOT_ASSESSED means the tool had nothing to measure and is
# emphatically not a pass.
PASS = "PASS"
FAIL = "FAIL"
GRAY = "GRAY"
NOT_ASSESSED = "NOT ASSESSED"

BYTES_PER_MB = 1024 * 1024

# Client-tier hosts report to a separate InfluxDB bucket (`client-metrics`) and
# so are already excluded from `testnet-metrics` by construction. This prefix
# check is a second line of defence for a rollup that predates that split, where
# a client VM's CPU would otherwise be pooled into the node-tier figure.
_CLIENT_HOST_MARKERS = ("uploader", "downloader", "-client", "canary")


class ComparisonError(ValueError):
    """Raised when two runs cannot be validly compared at all.

    Distinct from a gate failing: this means the comparison itself is
    meaningless (mismatched source, incomparable window lengths), and the right
    response is to refuse rather than to print a verdict nobody should trust.
    """


# ---------------------------------------------------------------------------
# Small statistics helpers
# ---------------------------------------------------------------------------


def percentile(values: list[float], fraction: float) -> float | None:
    """Linear-interpolated percentile over an unsorted list.

    Deliberately implemented rather than pulled from numpy: the whole point of
    this module is that a report is reproducible, and pinning percentile
    semantics to whatever a third-party default happens to be today invites the
    number to move under us. Matches ``statistics.quantiles``' inclusive method,
    which is the same linear interpolation numpy's default ``linear`` uses.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[int(position)])
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def stdev(values: list[float]) -> float | None:
    """Sample standard deviation (n-1). ``None`` below two points."""
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def linear_slope(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope of y against x. ``None`` when x has no spread.

    Used for the memory-leak check, where x is hours since the window start and
    y is RSS in bytes, so the slope is bytes per hour.
    """
    if len(points) < 2:
        return None
    n = len(points)
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    denominator = sum((p[0] - mean_x) ** 2 for p in points)
    if denominator == 0:
        return None
    numerator = sum((p[0] - mean_x) * (p[1] - mean_y) for p in points)
    return numerator / denominator


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive timestamp.

    Postgres returns aware values but SQLite drops the offset, and comparing the
    two raises. Every timestamp this system stores is UTC, so this is a
    restatement of fact rather than a conversion.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_client_host(host: str) -> bool:
    """Whether a hostname looks like a client VM rather than a node-tier VM."""
    lowered = (host or "").lower()
    return any(marker in lowered for marker in _CLIENT_HOST_MARKERS)


# ---------------------------------------------------------------------------
# Task 2 -- statistics over per-op records
# ---------------------------------------------------------------------------


def bucket_label(size_bytes: int, edges_mb: tuple[float, ...]) -> str | None:
    """Name the half-open file-size bucket ``size_bytes`` falls in.

    ``None`` when it falls outside every edge, which is reported rather than
    silently pooled into the nearest bucket: a file size the config does not
    describe means the config and the workload have diverged.
    """
    size_mb = size_bytes / BYTES_PER_MB
    for lower, upper in zip(edges_mb, edges_mb[1:]):
        if lower <= size_mb < upper:
            # Rendered from the edges, not the observed sizes, so the same
            # bucket has the same label on both sides of a comparison even when
            # one side happens to contain no file at the extreme.
            return f"{lower:g}-{upper:g}MB"
    return None


def filter_ops_to_window(
    records: list,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list:
    """Restrict per-op records to a window on ``finished_at``.

    Windowed on completion, not start: an operation belongs to the window it
    finished in, matching how the per-GB denominators are windowed. Records with
    no ``finished_at`` never completed and are excluded from a windowed view --
    but they are still counted as failures by :func:`success_rate`, which looks
    at ``status``.
    """
    if start is None and end is None:
        return list(records)
    kept = []
    for record in records:
        finished = getattr(record, "finished_at", None)
        if finished is None:
            continue
        finished = _as_utc(finished)
        if start is not None and finished < _as_utc(start):
            continue
        if end is not None and finished >= _as_utc(end):
            continue
        kept.append(record)
    return kept


def _status_value(record) -> str:
    status = getattr(record, "status", None)
    return getattr(status, "value", status)


#: A `skipped` operation was refused by the TRANSPORT, not by the network: a
#: browser-path client cannot carry a file above ant-core's 1 GB cap. The content
#: is stored and healthy, so a skip is neither a success nor a failure and must
#: not enter a success-rate denominator -- with a six-address list containing two
#: over-cap entries it would cap the gate at 66.7% and fail every run.
_EXCLUDED_FROM_RATES = ("skipped",)


def assessable_ops(records: list) -> list:
    """Records that a success rate can legitimately be computed over."""
    return [r for r in records if _status_value(r) not in _EXCLUDED_FROM_RATES]


def success_rate(records: list) -> dict | None:
    """Success percentage over a list of per-op records.

    ``None`` for an empty list -- no operations means nothing to assess, which
    is not the same as a 0% success rate. Skipped operations are excluded from
    both halves of the fraction, and the count of them is reported separately so
    the exclusion is visible rather than silent.
    """
    if not records:
        return None
    skipped = len(records) - len(assessable_ops(records))
    assessable = assessable_ops(records)
    if not assessable:
        # Every operation was skipped: there is no rate, but there were
        # certainly operations, so say so rather than returning None (which
        # reads as "this run did no transfers").
        return {"percent": None, "successes": 0, "total": 0, "skipped": skipped}
    successes = sum(1 for r in assessable if _status_value(r) == "success")
    total = len(assessable)
    return {
        "percent": 100.0 * successes / total,
        "successes": successes,
        "total": total,
        "skipped": skipped,
    }


def verification_counts(records: list) -> dict:
    """Content-verification tallies over a list of download records.

    ``verified`` is the denominator the integrity gate is assessed on: only
    ``ok`` + ``mismatch``, because an ``unavailable`` download was never
    checked against anything and a NULL was never checkable at all (the
    download failed, or the client predates verification). Counting either as a
    pass would report an unverified run as a verified one, which is exactly the
    mistake this gate exists to stop.

    ``coverage`` is verified over SUCCESSFUL downloads -- what fraction of the
    content this run fetched was actually checked. ``None`` when the run
    downloaded nothing.
    """
    ok = mismatch = unavailable = 0
    for record in records:
        value = getattr(record, "verify_status", None)
        if value == "ok":
            ok += 1
        elif value == "mismatch":
            mismatch += 1
        elif value == "unavailable":
            unavailable += 1
    successes = sum(1 for r in records if _status_value(r) == "success")
    verified = ok + mismatch
    return {
        "ok": ok,
        "mismatch": mismatch,
        "unavailable": unavailable,
        "verified": verified,
        "successful_downloads": successes,
        "coverage": (verified / successes) if successes else None,
    }


def duration_percentiles_by_bucket(records: list, edges_mb: tuple[float, ...]) -> dict[str, dict]:
    """p50/p95 transfer duration per file-size bucket, in milliseconds.

    Only **successful** operations contribute. A failed transfer's duration
    measures how long it took to give up, which is not a transfer time, and
    mixing the two lets a change in the failure mix move the percentile without
    any transfer getting slower.

    Percentiles are exact here: per-op rows carry their own ``duration_ms``.
    This is the one place the comparison gets a true percentile rather than a
    mean, which is why the transfer-duration gates are the spec's statistic and
    the CPU/RSS gates are not.
    """
    grouped: dict[str, list[float]] = {}
    for record in records:
        if _status_value(record) != "success":
            continue
        duration = getattr(record, "duration_ms", None)
        if duration is None:
            continue
        label = bucket_label(getattr(record, "file_size_bytes", 0) or 0, edges_mb)
        if label is None:
            continue
        grouped.setdefault(label, []).append(float(duration))

    result = {}
    for label in sorted(grouped):
        values = grouped[label]
        result[label] = {
            "p50_ms": percentile(values, 0.50),
            "p95_ms": percentile(values, 0.95),
            "count": len(values),
        }
    return result


def duration_drift_by_bucket(
    records: list,
    edges_mb: tuple[float, ...],
    window_start: datetime | None,
    window_end: datetime | None,
    min_ops_per_half: int,
) -> dict[str, dict]:
    """Within-window transfer-duration drift, per file-size bucket (V2-1717).

    Every other duration statistic here pools the whole window into one
    percentile, so a degradation that starts part-way through is diluted by the
    healthy earlier part: on STG-01 #699 native uploads slowed ~30% in the back
    third of the steady-state window at every file size, and the pooled 0-32MB
    p50 gate -- the bucket with the most samples and the clearest degradation --
    passed at +4.78%. This measures the slope instead of the level.

    The statistic, per bucket:

    1. Split the window at its **midpoint by time**. Not by clock hour: hour
       buckets leave a partial final slice (#699's fourth hour was 10 minutes,
       n=84 against ~500) which is noise dressed as a trend.
    2. Per ``client_hostname``, take the p50 of ``duration_ms`` in each half and
       that host's drift as ``second / first - 1``.
    3. A host needs ``min_ops_per_half`` successful ops in **each** half, or it
       is dropped. Dropped, never counted as 0% drift -- a host with nothing to
       compare has not demonstrated flatness.
    4. The bucket's drift is the **median across hosts**.

    Per-host-then-median rather than pooling every op: a host that only
    transfers late in the window cannot then manufacture a trend out of being
    slower than a different host was earlier.

    The split is on ``finished_at`` because it is the one timestamp here in a
    SINGLE frame. It is assigned where the operation is recorded, not by the
    client, so every host's completions sit on one clock and a boundary drawn
    through them means the same thing for all of them. ``started_at`` carries
    the client's own clock, and the staging profile deliberately skews three
    uploaders and one downloader by +-2h -- which is exactly what
    ``print_file_ops._clock_skew_seconds`` estimates, from the gap between the
    two. Measured on STG-01 #699: four clients report
    ``finished_at - started_at - duration_ms`` of +-7200s while every other
    client reports 0-2s, and yet **every** client's ``finished_at`` spans the
    same 03:10 -> 08:26. So a skewed client is not displaced into one half and
    is not excluded; it is assessed like any other, which is what splitting in
    one frame buys. **If an op timestamp ever becomes client-framed, this
    statistic needs a frame of its own** -- halves drawn through mixed clocks
    would put a skewed client's early ops in the late half and read its drift
    backwards.

    On a ``full-run`` window the two halves are not two halves of the
    measurement phase -- that window opens when the first host started reporting
    (which the node clock skew drags up to 3h before T0) and closes after
    whatever followed. The split is still the window's midpoint, because the
    window is what the caller chose; prefer a labelled phase window for this
    gate.
    """
    if window_start is None or window_end is None:
        return {}
    start, end = _as_utc(window_start), _as_utc(window_end)
    if end <= start:
        return {}
    midpoint = start + (end - start) / 2

    # bucket -> host -> (first half durations, second half durations)
    grouped: dict[str, dict[str, tuple[list[float], list[float]]]] = {}
    for record in records:
        if _status_value(record) != "success":
            continue
        duration = getattr(record, "duration_ms", None)
        finished = getattr(record, "finished_at", None)
        host = getattr(record, "client_hostname", None)
        # Both op tables carry `client_hostname` in a unique constraint, so a
        # row without one cannot come from the registry. If one ever does it is
        # skipped rather than pooled into a shared pseudo-host, which would
        # quietly reinstate the pooling this statistic exists to avoid.
        if duration is None or finished is None or not host:
            continue
        finished = _as_utc(finished)
        if finished < start or finished >= end:
            continue
        label = bucket_label(getattr(record, "file_size_bytes", 0) or 0, edges_mb)
        if label is None:
            continue
        halves = grouped.setdefault(label, {}).setdefault(host, ([], []))
        halves[0 if finished < midpoint else 1].append(float(duration))

    result: dict[str, dict] = {}
    for label in sorted(grouped):
        drifts: list[tuple[float, str, float, float, int, int]] = []
        excluded = 0
        for host in sorted(grouped[label]):
            first, second = grouped[label][host]
            if len(first) < min_ops_per_half or len(second) < min_ops_per_half:
                excluded += 1
                continue
            first_p50 = percentile(first, 0.50)
            second_p50 = percentile(second, 0.50)
            if not first_p50 or second_p50 is None:
                excluded += 1
                continue
            drifts.append(
                (
                    100.0 * (second_p50 / first_p50 - 1.0),
                    host,
                    first_p50,
                    second_p50,
                    len(first),
                    len(second),
                )
            )
        if not drifts:
            # The bucket is reported as present-but-unassessable rather than
            # omitted, so the gate can say WHY it has no number.
            result[label] = {
                "drift_percent": None,
                "hosts": 0,
                "excluded_hosts": excluded,
            }
            continue
        worst = max(drifts)
        result[label] = {
            "drift_percent": percentile([d[0] for d in drifts], 0.50),
            "hosts": len(drifts),
            "excluded_hosts": excluded,
            # The worst host is always surfaced, as the RSS gate surfaces the
            # worst service: the median is the verdict, but a reader needs a
            # concrete "19.0 s -> 22.3 s" to act on.
            "worst_host": worst[1],
            "worst_drift_percent": worst[0],
            "worst_first_p50_ms": worst[2],
            "worst_second_p50_ms": worst[3],
            "worst_first_count": worst[4],
            "worst_second_count": worst[5],
        }
    return result


# ---------------------------------------------------------------------------
# Task 3 -- statistics over host metrics
# ---------------------------------------------------------------------------


def node_cpu_statistic(host_metrics: list[dict]) -> dict | None:
    """Node-tier CPU across hosts, sample-weighted.

    Prefers a true ``p50`` when percentile capture recorded one and falls back
    to the sample-weighted mean otherwise, reporting which it used in
    ``statistic``. The process spec names the *median*; buckets recorded before
    percentile capture landed hold only ``{avg,min,max,samples}``, and no
    backfill is possible because the source InfluxDB is destroyed with the
    monitoring stack. Silently reporting a mean as a median would be the kind of
    error that never surfaces, so the statistic used is carried through to the
    report.

    Hosts are pooled sample-weighted rather than averaged: a host that reported
    for half the window should carry half the weight.
    """
    return _pooled_host_statistic(
        [
            entry.get(METRIC_CPU_PERCENT)
            for entry in host_metrics
            if not is_client_host(entry.get("host", ""))
        ]
    )


def node_rss_by_service(host_metrics: list[dict]) -> dict[str, dict]:
    """Per-service RSS, keyed ``host/service``, never pooled into a host total.

    One leaking service among fifteen on a VM is exactly the signal worth
    catching, and a host mean dilutes it to invisibility.
    """
    result: dict[str, dict] = {}
    for entry in host_metrics:
        host = entry.get("host", "")
        if is_client_host(host):
            continue
        for service, stats in (entry.get("rss_by_service") or {}).items():
            statistic = _single_host_statistic(stats)
            if statistic is not None:
                result[f"{host}/{service}"] = statistic
    return result


def node_rss_statistic(host_metrics: list[dict], service_percentile: float = 95.0) -> dict | None:
    """A percentile of per-service RSS across the fleet.

    Never a fleet *mean*: the signal worth catching is a subset of services
    growing, and a mean over ~1000 services dilutes that away.

    But never the fleet *maximum* either, which is what this originally did. The
    maximum of a thousand samples is an extreme order statistic that moves with
    the single unluckiest service. Measured on STG-01 470 -> 474 the max moved
    +64% (1.314 -> 2.156 GiB) while the median moved +3.8% and the p95 +1.8% --
    one outlier reported as a fleet-wide regression.

    The percentile is configurable, and 100 restores worst-service behaviour.
    The worst service is reported in the detail regardless, so an outlier is
    never hidden -- it just does not on its own decide the gate.
    """
    services = node_rss_by_service(host_metrics)
    if not services:
        return None
    values = [v["value"] for v in services.values()]
    labels = {v["statistic"] for v in services.values()}
    statistic = labels.pop() if len(labels) == 1 else "mean"
    worst_key = max(services, key=lambda k: services[k]["value"])
    # Same percentile across services, taken over their means -- the fallback for
    # a baseline that predates percentile capture. Same shape, shared statistic.
    mean_values = [v["mean_value"] for v in services.values() if v.get("mean_value") is not None]
    return {
        "value": percentile(values, service_percentile / 100.0),
        "statistic": statistic,
        "services": len(values),
        "worst_service": worst_key,
        "worst_value": services[worst_key]["value"],
        "percentile": service_percentile,
        "mean_value": (
            percentile(mean_values, service_percentile / 100.0) if mean_values else None
        ),
    }


def _statistic_label(stats: dict) -> str:
    """Name the statistic a stat dict's percentile represents.

    Three distinct things, which must never be silently interchanged:

    * ``p50`` -- the exact whole-run median, computed from every raw sample at
      extraction time and stored in the rollup.
    * ``p50~`` -- a window's median ESTIMATED from per-bucket medians, because
      percentiles do not compose across buckets the way avg/min/max do.
    * ``mean`` -- no percentile was captured at all (any run recorded before
      percentile capture landed; no backfill is possible).

    Comparing any two of these against each other would attribute the
    difference in method to the release, which is why the label travels with
    the value and a mismatch is refused.
    """
    if stats.get("p50") is None:
        return "mean"
    return "p50~" if stats.get("percentile_approx") else "p50"


def _single_host_statistic(stats: dict | None) -> dict | None:
    if not isinstance(stats, dict):
        return None
    samples = int(stats.get("samples") or 0)
    if samples <= 0:
        return None
    mean_value = None if stats.get("avg") is None else float(stats["avg"])
    label = _statistic_label(stats)
    if label != "mean":
        return {
            "value": float(stats["p50"]),
            "statistic": label,
            "samples": samples,
            "mean_value": mean_value,
        }
    if mean_value is None:
        return None
    return {
        "value": mean_value,
        "statistic": "mean",
        "samples": samples,
        "mean_value": mean_value,
    }


def _pooled_host_statistic(stats_list: list) -> dict | None:
    """Pool per-host stat dicts, sample-weighted, preferring a percentile.

    Mixed availability is resolved by degrading the whole pool to the mean: a
    pool that averaged some hosts' medians with other hosts' means would be
    neither, and would shift silently as more runs gained percentile capture.
    """
    usable = [s for s in stats_list if isinstance(s, dict) and (s.get("samples") or 0) > 0]
    if not usable:
        return None
    labels = {_statistic_label(s) for s in usable}
    statistic = labels.pop() if len(labels) == 1 else "mean"
    key = "avg" if statistic == "mean" else "p50"
    weighted, total = 0.0, 0
    for stats in usable:
        value = stats.get(key)
        if value is None:
            continue
        samples = int(stats["samples"])
        weighted += float(value) * samples
        total += samples
    if total == 0:
        return None
    # Always carry the sample-weighted mean alongside the chosen statistic, so a
    # comparison against a run that never captured percentiles can fall back to
    # the statistic both sides share instead of dropping the gate.
    mean_weighted, mean_total = 0.0, 0
    for stats in usable:
        avg = stats.get("avg")
        if avg is None:
            continue
        samples = int(stats["samples"])
        mean_weighted += float(avg) * samples
        mean_total += samples
    return {
        "value": weighted / total,
        "statistic": statistic,
        "samples": total,
        "hosts": len(usable),
        "mean_value": (mean_weighted / mean_total) if mean_total else None,
    }


def rss_leak_slopes(
    buckets: list[dict],
    window_hours: float,
    min_points: int,
) -> dict[str, dict]:
    """Fitted RSS growth per service over the final ``window_hours`` of the run.

    Returns one entry per ``host/service`` with the fitted slope in bytes per
    hour and that slope expressed as a percentage of the service's own mean RSS
    over the regression window. The normalisation is what makes one threshold
    apply fleet-wide: an absolute bytes-per-hour limit would be far harsher on a
    small service than a large one.

    Services with fewer than ``min_points`` buckets in the window are omitted
    entirely rather than passed -- a regression through three points is noise,
    and the caller reports the omission as NOT ASSESSED.
    """
    rss_buckets = [
        b
        for b in buckets
        if b.get("metric") == METRIC_RSS_BYTES
        and b.get("avg") is not None
        and not is_client_host(b.get("host", ""))
    ]
    if not rss_buckets:
        return {}

    # The cutoff is computed PER HOST, from that host's own latest bucket, not
    # once from the fleet-wide maximum.
    #
    # The staging profile deliberately skews node clocks by up to +-3h in a
    # per-host random direction, and Telegraf timestamps a point by the host's
    # own clock. A fleet-wide `max(bucket_start)` is therefore whatever the most
    # forward-skewed host thinks the time is, and a "final 2h" window measured
    # back from it can miss every other host's final hours entirely. Measured on
    # STG-01 run 470: a fleet-wide cutoff found 15 services with enough points,
    # against 997 services that actually reported.
    #
    # A host's own clock is internally consistent whatever its offset, so its
    # own last bucket is the right anchor for its own final two hours.
    host_latest: dict[str, float] = {}
    for bucket in rss_buckets:
        host = bucket.get("host", "")
        ts = _as_utc(bucket["bucket_start"]).timestamp()
        if ts > host_latest.get(host, float("-inf")):
            host_latest[host] = ts

    grouped: dict[str, list[tuple[float, float]]] = {}
    for bucket in rss_buckets:
        host = bucket.get("host", "")
        cutoff = host_latest[host] - window_hours * 3600
        ts = _as_utc(bucket["bucket_start"]).timestamp()
        if ts < cutoff:
            continue
        key = f"{host}/{bucket.get('service') or ''}"
        grouped.setdefault(key, []).append(((ts - cutoff) / 3600.0, float(bucket["avg"])))

    result: dict[str, dict] = {}
    for key in sorted(grouped):
        points = sorted(grouped[key])
        if len(points) < min_points:
            continue
        slope = linear_slope(points)
        if slope is None:
            continue
        mean_rss = sum(p[1] for p in points) / len(points)
        if mean_rss <= 0:
            continue
        result[key] = {
            "slope_bytes_per_hour": slope,
            "growth_percent_per_hour": 100.0 * slope / mean_rss,
            "mean_rss_bytes": mean_rss,
            "points": len(points),
        }
    return result


# ---------------------------------------------------------------------------
# Task 4 -- the comparison engine
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    """One gate's outcome, ready for both the JSON payload and the report."""

    gate: str
    kind: str  # "hard" or "soft"
    verdict: str
    candidate: float | None = None
    baseline: float | None = None
    delta_percent: float | None = None
    threshold: float | None = None
    unit: str = ""
    statistic: str | None = None
    detail: str = ""
    noise: dict | None = None
    # Reported but never blocking, whatever its verdict. The WASM client tier
    # starts advisory: its gates would otherwise be judged against floors
    # calibrated on the native transport, which it does not yet meet (92.1%
    # upload / 94.9% download against 99% / 99.5% on the first measured run) --
    # so every run would hard-fail on a tier that is not yet what ships.
    advisory: bool = False
    #: Judged against an absolute number on this run alone, with no baseline.
    #: The hard floors are the archetype; the `[pointer]` floors are the same
    #: shape but must never reach the exit code, so they are soft AND single-run.
    #: `--new-baseline` blanks every gate that needs a baseline, and this is what
    #: keeps a gate that does not need one from being blanked with them.
    single_run: bool = False

    def to_dict(self) -> dict:
        return {
            "gate": self.gate,
            "kind": self.kind,
            "verdict": self.verdict,
            "candidate": self.candidate,
            "baseline": self.baseline,
            "delta_percent": self.delta_percent,
            "threshold": self.threshold,
            "unit": self.unit,
            "statistic": self.statistic,
            "detail": self.detail,
            "noise": self.noise,
            "advisory": self.advisory,
            "single_run": self.single_run,
        }


@dataclass
class RunMetrics:
    """Everything the comparison needs about one run.

    Assembled by the CLI from the repository; a plain container so the engine
    stays a pure function and the tests need no database.
    """

    deployment_id: int
    name: str
    window_label: str
    window_start: datetime | None = None
    window_end: datetime | None = None
    #: The `saorsa-deploy` version that created the run's registry row.
    #: Reported in the header and nothing else: two runs produced by different
    #: tool versions are still compared, because the tool version is context
    #: for reading a difference, not itself a gate.
    saorsa_deploy_version: str | None = None
    upload_records: list = field(default_factory=list)
    download_records: list = field(default_factory=list)
    host_metrics: list[dict] = field(default_factory=list)
    rss_buckets: list[dict] = field(default_factory=list)
    # metric name -> {"value": float, "source": str, "window_start": dt, ...}
    derived: dict[str, dict] = field(default_factory=dict)
    # One dict per recorded log-string count: event_key, event_group, count,
    # hosts, total_documents, observed_start, observed_end, pattern_version.
    # A list rather than a dict because the gate evaluates GROUPS, and the
    # grouping is a property of the pattern catalogue, not of the rows.
    log_event_counts: list[dict] = field(default_factory=list)
    #: Pointer operations, in the dict shape `pointer_results` consumes (the CLI
    #: normalises `deployment_pointer_records` rows into it). A list of dicts
    #: rather than ORM rows for the same reason every other field here is plain
    #: data: the engine stays a pure function and the tests need no database.
    pointer_records: list[dict] = field(default_factory=list)

    def window_seconds(self) -> float | None:
        if self.window_start is None or self.window_end is None:
            return None
        return (_as_utc(self.window_end) - _as_utc(self.window_start)).total_seconds()

    def activity_seconds(self) -> float | None:
        """Span from the first to the last completed client operation."""
        finished = [
            _as_utc(r.finished_at)
            for r in list(self.upload_records) + list(self.download_records)
            if getattr(r, "finished_at", None) is not None
        ]
        if len(finished) < 2:
            return None
        return (max(finished) - min(finished)).total_seconds()

    def activity_coverage(self) -> float | None:
        """Fraction of the window in which clients were actually transferring.

        The per-GB numerators (storage growth, outbound bytes) accumulate across
        the whole window, including idle stretches, while the denominator only
        accumulates while clients are running. So a window that is mostly idle
        inflates every ratio -- and a window-LENGTH check cannot see it, because
        the length can be perfectly ordinary.

        Measured across four recorded runs, disk GB/GB against coverage: 39.2%
        -> 14.86, 52.2% -> 10.08, 54.6% -> 9.34, 61.0% -> 9.12. Run 471's
        recorded window is ~10h around a ~4h measurement, and its ratio is
        inflated by roughly 55% purely as a result.
        """
        window = self.window_seconds()
        activity = self.activity_seconds()
        if not window or activity is None or window <= 0:
            return None
        return activity / window


def _relative_change(candidate: float, baseline: float) -> float | None:
    """Percentage change from baseline to candidate.

    ``None`` when the baseline is zero: the change is then undefined rather than
    infinite, and reporting it as a huge percentage would trip a threshold on
    what is really a missing measurement.
    """
    if baseline == 0:
        return None
    return 100.0 * (candidate - baseline) / abs(baseline)


def apply_noise_rule(
    delta_percent: float | None,
    threshold: float,
    history: list[float] | None,
    config: GateConfig,
) -> tuple[str, dict | None]:
    """Combine the percentage threshold with the 2-sigma variance criterion.

    The process spec: a regression is real when it exceeds the percentage
    threshold **and**, once ~5 accepted runs of history exist for the metric,
    also exceeds ``sigma_multiplier`` times that metric's run-to-run standard
    deviation. Tripping one but not the other is GRAY -- RM judgment either way.
    Below the history minimum the percentage threshold stands alone.

    ``history`` is a list of that metric's values on prior accepted runs.
    Choosing which runs are "accepted" is a judgment call and belongs to the
    operator; only the arithmetic lives here.

    **The sigma band is RELATIVE and a gate measured in POINTS cannot use it.**
    ``sigma_percent`` divides by ``abs(mean)``, so for a metric whose history
    sits near zero -- the within-window duration DRIFT gate (V2-1717) is one --
    the band comes out enormous and ``exceeds_sigma`` is unreachable: supply
    history for such a gate and a genuine breach is silently downgraded FAIL ->
    GRAY, for good. It is harmless for ``success_rate_drop_pp`` only because
    that metric's mean sits near 100. Latent today, since ``_collect_history``
    populates only the two per-GB ratios; a points gate that wants the
    criterion needs its own band expressed in points, not this one.
    """
    if delta_percent is None:
        return NOT_ASSESSED, None
    exceeds_percent = delta_percent > threshold

    values = history or []
    if len(values) < config.noise_rule.min_history_runs:
        return (
            (FAIL if exceeds_percent else PASS),
            {
                "history_runs": len(values),
                "criterion": "percentage only",
                "reason": (
                    f"fewer than {config.noise_rule.min_history_runs} accepted runs of history"
                ),
            },
        )

    deviation = stdev(values)
    if deviation is None or deviation == 0:
        # Zero variance across five runs is not evidence that any change is
        # significant; it far more likely means the history is degenerate
        # (the same run repeated, or a constant). Fall back rather than treat
        # every delta as a 2-sigma event.
        return (
            (FAIL if exceeds_percent else PASS),
            {
                "history_runs": len(values),
                "criterion": "percentage only",
                "reason": "history has no variance",
            },
        )

    mean = sum(values) / len(values)
    # The sigma band expressed in the same percentage units as `delta_percent`,
    # so the two criteria are directly comparable.
    sigma_percent = 100.0 * (config.noise_rule.sigma_multiplier * deviation) / abs(mean)
    exceeds_sigma = delta_percent > sigma_percent

    noise = {
        "history_runs": len(values),
        "criterion": f"percentage AND {config.noise_rule.sigma_multiplier}-sigma",
        "sigma_percent": sigma_percent,
        "exceeds_percent": exceeds_percent,
        "exceeds_sigma": exceeds_sigma,
    }
    if exceeds_percent and exceeds_sigma:
        return FAIL, noise
    if exceeds_percent or exceeds_sigma:
        return GRAY, noise
    return PASS, noise


def _compare_scalar(
    gate: str,
    candidate: dict | float | None,
    baseline: dict | float | None,
    threshold: float,
    config: GateConfig,
    unit: str,
    history: list[float] | None = None,
    missing_detail: str = "",
    zero_is_a_value: bool = False,
) -> GateResult:
    """Compare one scalar against its baseline under the noise rule.

    ``zero_is_a_value`` says a zero baseline is a real measurement rather than a
    missing one, and makes the gate read a zero baseline exactly as the
    connection-error count gates do: 0 vs 0 is PASS (the phenomenon occurred on
    neither run) and 0 -> anything is FAIL (it appeared where it never did).
    That is right for a RATE whose zero is the hoped-for steady state -- the
    pointer stale-read and lost-write rates were 0.000% on both STG-01 #655 and
    #627 and read NOT ASSESSED, next to count gates reading PASS for the same
    0 vs 0. It is wrong for a node metric: a zero CPU or disk ratio is a broken
    capture, and PASS would paper over it, so the default keeps the undefined
    delta as NOT ASSESSED.
    """
    cand_stat = candidate.get("statistic") if isinstance(candidate, dict) else None
    base_stat = baseline.get("statistic") if isinstance(baseline, dict) else None
    cand_value = candidate.get("value") if isinstance(candidate, dict) else candidate
    base_value = baseline.get("value") if isinstance(baseline, dict) else baseline

    if cand_value is None or base_value is None:
        which = []
        if cand_value is None:
            which.append("candidate")
        if base_value is None:
            which.append("baseline")
        return GateResult(
            gate=gate,
            kind="soft",
            verdict=NOT_ASSESSED,
            candidate=cand_value,
            baseline=base_value,
            threshold=threshold,
            unit=unit,
            statistic=cand_stat or base_stat,
            detail=missing_detail or f"no measurement on the {' and '.join(which)} run",
        )

    # Mixed statistics would compare a median against a mean and attribute the
    # difference to the release, so they are never compared as-is. But refusing
    # outright drops the gate entirely, which silently removed node CPU and node
    # RSS from every comparison whose baseline predated percentile capture --
    # precisely the run those gates were last measured against. Measured on
    # STG-01 504 vs 474: both dropped, though 474 carried a mean for each and CPU
    # had FAILED on that same mean the previous train.
    #
    # So when one side has NO percentile at all, degrade both to the mean they
    # share and label it, mirroring _pooled_host_statistic's within-run
    # degradation. An exact p50 against a windowed p50~ is a different case and
    # stays refused: both captured percentiles, so that mismatch is a real
    # methodological difference to be fixed by matching the windows, not papered
    # over by coarsening a comparison that could be made properly.
    degraded = ""
    if cand_stat and base_stat and cand_stat != base_stat:
        cand_mean = candidate.get("mean_value") if isinstance(candidate, dict) else None
        base_mean = baseline.get("mean_value") if isinstance(baseline, dict) else None
        can_degrade = (
            "mean" in {cand_stat, base_stat} and cand_mean is not None and base_mean is not None
        )
        if not can_degrade:
            return GateResult(
                gate=gate,
                kind="soft",
                verdict=NOT_ASSESSED,
                candidate=cand_value,
                baseline=base_value,
                threshold=threshold,
                unit=unit,
                statistic=f"{cand_stat} vs {base_stat}",
                detail=(
                    f"candidate reports {cand_stat} but baseline reports {base_stat}; "
                    "comparing them would measure the statistic, not the release"
                ),
            )
        degraded = f"degraded to mean: candidate reported {cand_stat}, baseline {base_stat}"
        cand_value, base_value = float(cand_mean), float(base_mean)
        cand_stat = base_stat = "mean"

    if zero_is_a_value and base_value == 0:
        appeared = cand_value > 0
        return GateResult(
            gate=gate,
            kind="soft",
            verdict=FAIL if appeared else PASS,
            candidate=cand_value,
            baseline=base_value,
            threshold=threshold,
            unit=unit,
            statistic=cand_stat or base_stat,
            detail=(
                "The baseline never recorded this, so a relative change is "
                "undefined — but it appeared on the candidate."
                if appeared
                else "neither run recorded this at all"
            ),
        )

    delta = _relative_change(cand_value, base_value)
    verdict, noise = apply_noise_rule(delta, threshold, history, config)
    return GateResult(
        gate=gate,
        kind="soft",
        verdict=verdict,
        candidate=cand_value,
        baseline=base_value,
        delta_percent=delta,
        threshold=threshold,
        unit=unit,
        statistic=cand_stat or base_stat,
        noise=noise,
        detail=(
            "baseline is zero, so a relative change is undefined" if delta is None else degraded
        ),
    )


def check_comparability(
    candidate: RunMetrics, baseline: RunMetrics, config: GateConfig
) -> list[str]:
    """Reasons these two runs cannot be validly compared. Empty means proceed.

    Enforces the invariants that make a comparison mean anything: the same
    window label, the same derived-metric ``source``, and window lengths within
    ``max_window_length_ratio`` of each other.
    """
    problems: list[str] = []

    if candidate.window_label != baseline.window_label:
        problems.append(
            f"window labels differ: candidate '{candidate.window_label}' vs "
            f"baseline '{baseline.window_label}'. A steady-state slice and a "
            "full run are not the same measurement."
        )

    cand_sources = {m.get("source") for m in candidate.derived.values() if m.get("source")}
    base_sources = {m.get("source") for m in baseline.derived.values() if m.get("source")}
    if cand_sources and base_sources and cand_sources != base_sources:
        problems.append(
            f"derived-metric sources differ: candidate {sorted(cand_sources)} vs "
            f"baseline {sorted(base_sources)}. 'capture' sums raw counter deltas "
            "and is exact; 'buckets' integrates averaged rates and is "
            "approximate. Comparing them measures the method, not the release."
        )

    cand_window = candidate.window_seconds()
    base_window = baseline.window_seconds()
    if cand_window and base_window and cand_window > 0 and base_window > 0:
        ratio = max(cand_window, base_window) / min(cand_window, base_window)
        if ratio > config.max_window_length_ratio:
            problems.append(
                f"window lengths differ by {ratio:.2f}x "
                f"({cand_window / 3600:.2f}h vs {base_window / 3600:.2f}h), above the "
                f"{config.max_window_length_ratio}x limit. Replication continues "
                "after an upload completes, so a shorter window truncates the disk "
                "numerator while still counting the full denominator."
            )

    return problems


#: Transport of a client operation. NULL on every row predating the WASM client,
#: which was necessarily native, so NULL reads as native rather than unknown.
NATIVE = "native"
WASM = "wasm"


def _records_for(records: list, transport: str) -> list:
    if transport == NATIVE:
        return [r for r in records if getattr(r, "client_kind", None) in (None, NATIVE)]
    return [r for r in records if getattr(r, "client_kind", None) == transport]


def _has_wasm(run: RunMetrics) -> bool:
    return bool(_records_for(run.upload_records, WASM) or _records_for(run.download_records, WASM))


def _transport_view(run: RunMetrics, transport: str) -> RunMetrics:
    """The same run restricted to one client transport.

    Only the client-operation fields are filtered. Host metrics, RSS buckets,
    derived ratios and log counts are node-tier measurements -- one fleet serves
    both transports, so splitting them would be meaningless.
    """
    return replace(
        run,
        upload_records=_records_for(run.upload_records, transport),
        download_records=_records_for(run.download_records, transport),
    )


def _client_gates(
    candidate: RunMetrics,
    baseline: RunMetrics,
    config: GateConfig,
    history: dict,
) -> list[GateResult]:
    """Gates computed from client operations, evaluated per transport.

    A native and a WASM client are not the same instrument. On the first
    measured run the WASM tier was ~2.2x slower on upload and ~4.2x on download,
    so a merged percentile describes neither -- and its value moves with however
    many VMs of each the run happened to use, which makes two runs
    incomparable to each other as well.

    When neither run has WASM records the gates are emitted exactly as before,
    with no transport suffix, so every existing comparison is unchanged.

    The two KINDS of WASM gate are treated separately. A FLOOR ("99% of uploads
    succeeded") is an absolute number calibrated on the native transport, which
    the WASM tier does not meet for a reason that is not the release under test
    -- a NAT'd peer is unreachable over WebRTC-Direct until relayed WebRTC
    exists -- so it stays advisory. A RELATIVE gate asks only whether this
    release made the WASM tier worse than the baseline did, needs no absolute
    calibration, and counts. Which is which is config, not code.
    """

    def floor_gates(run_c):
        return _hard_success_floors(run_c, config)

    def relative_gates(run_c, run_b):
        return [
            *_soft_success_rates(run_c, run_b, config, history),
            *_soft_transfer_durations(run_c, run_b, config, history),
            *_soft_transfer_duration_drift(run_c, run_b, config, history),
        ]

    if not (_has_wasm(candidate) or _has_wasm(baseline)):
        return [*floor_gates(candidate), *relative_gates(candidate, baseline)]

    results: list[GateResult] = []
    for transport in (NATIVE, WASM):
        cand_view = _transport_view(candidate, transport)
        base_view = _transport_view(baseline, transport)
        groups = (
            (floor_gates(cand_view), config.wasm_floor_gates_advisory),
            (relative_gates(cand_view, base_view), config.wasm_relative_gates_advisory),
        )
        for gates, wasm_advisory in groups:
            advisory = transport == WASM and wasm_advisory
            for gate in gates:
                results.append(
                    replace(
                        gate,
                        gate=f"{gate.gate} [{transport}]",
                        advisory=advisory,
                        detail=(
                            f"{gate.detail} — advisory, not blocking"
                            if advisory and gate.detail
                            else ("advisory, not blocking" if advisory else gate.detail)
                        ),
                    )
                )
    return results


#: The NOT ASSESSED reason every relative gate carries under ``--new-baseline``.
#: Fixed string, not prose: the skills match on it.
NEW_BASELINE_REASON = "new baseline"


def empty_baseline(candidate: RunMetrics) -> RunMetrics:
    """A stand-in baseline holding nothing, for ``--new-baseline``.

    Carries the candidate's window label so the comparability check would pass
    if it ran -- it does not -- and no measurements at all, so every gate that
    needs a baseline reads as unmeasurable rather than as a zero.
    """
    return RunMetrics(
        deployment_id=0,
        name="none",
        window_label=candidate.window_label,
    )


def compare_runs(
    candidate: RunMetrics,
    baseline: RunMetrics | None,
    config: GateConfig,
    history: dict[str, list[float]] | None = None,
    pattern_set=None,
    new_baseline: bool = False,
) -> dict:
    """Produce the full verdict payload for a candidate against a baseline.

    Raises :class:`ComparisonError` when the two runs are not comparable at all.
    Otherwise always returns a payload: an individual gate that cannot be
    measured is NOT ASSESSED, which never becomes a pass.

    ``pattern_set`` is the log-string catalogue (``log_patterns.LogPatternSet``)
    naming the connection-error groups and how each combines. It is passed in
    rather than loaded here so this stays a pure function of its inputs; with
    ``None`` the groups are still evaluated, derived from the stored rows.

    ``new_baseline`` declares that there IS no comparable prior run -- the
    staging profile changed shape, so nothing recorded before it is a valid
    baseline for anything recorded after. The single-run gates (the success
    floors) are still evaluated, because they are absolute; every relative gate
    is emitted as NOT ASSESSED with the reason ``new baseline``. Emitted, not
    omitted: a gate that vanishes from the report reads as one nobody thought
    about, and the whole point of this report is that an unmeasured gate can
    never be mistaken for a passing one. The comparability checks are skipped
    because there is nothing on the other side to be comparable to.
    """
    if new_baseline:
        baseline = empty_baseline(candidate)
    elif baseline is None:
        raise ComparisonError("a baseline run is required unless new_baseline is set.")
    else:
        problems = check_comparability(candidate, baseline, config)
        if problems:
            raise ComparisonError("; ".join(problems))

    history = history or {}
    results: list[GateResult] = []

    results.extend(_client_gates(candidate, baseline, config, history))
    results.append(_hard_data_integrity(candidate, config))
    results.extend(_pointer_gates(candidate, baseline, config, history))
    results.extend(_soft_host_metrics(candidate, baseline, config, history))
    results.append(_soft_memory_leak(candidate, config))
    results.extend(_soft_derived_ratios(candidate, baseline, config, history))
    results.extend(_soft_log_event_counts(candidate, baseline, config, history, pattern_set))

    if new_baseline:
        # Every soft gate in this module is relative to the baseline (the leak
        # check is absolute, but the spec groups it with the rest, and a single
        # run's slope has nothing to be judged against here either). The hard
        # floors stand, and so does anything else marked `single_run` -- the
        # `[pointer]` floors, which are absolute but must never reach the exit
        # code and so are soft rather than hard.
        results = [
            r
            if r.kind == "hard" or r.single_run
            else replace(
                r,
                verdict=NOT_ASSESSED,
                baseline=None,
                delta_percent=None,
                noise=None,
                detail=NEW_BASELINE_REASON,
            )
            for r in results
        ]

    warnings: list[str] = (
        [] if new_baseline else list(log_count_warnings(candidate, baseline, pattern_set))
    )

    # Neither comparability guard inspects what the windows CONTAIN.
    #
    # A `full-run` window is the span of the observed metrics, not the shape of
    # the run: it opens when the first host started reporting -- which the
    # deliberate -3h node clock skew drags well before T0 -- and closes when the
    # last one stopped, sweeping in any post-measurement phase. On STG-01 #474
    # that meant 3h05m of pre-run data plus the mixed-version hour, against a
    # baseline that never ran a mixed-version phase at all. Both guards passed
    # (length 1.13x, coverage 1.12x) because length and idleness were fine; the
    # windows still described different things.
    #
    # This cannot be detected from stored data -- nothing records phase
    # boundaries -- so it is stated rather than measured.
    if candidate.window_label == WINDOW_FULL_RUN and not new_baseline:
        warnings.append(
            "Comparing on the 'full-run' window. That window is the span of each "
            "run's observed metrics, not the shape of the run: it includes "
            "whatever preceded the measurement phase (the deliberate node clock "
            "skew pushes a host's first points up to 3h before T0) and whatever "
            "followed it, including a mixed-version extension the baseline may "
            "not have run. The length and activity-coverage guards check the "
            "windows' size and idleness, not what phases they contain. For a "
            "like-for-like comparison, re-derive the measurement phase on BOTH "
            "runs -- `deployments derive-metrics --id <id> --window-start ... "
            "--window-end ... --label steady-state` -- and compare with "
            "--window-label steady-state."
        )

    if new_baseline:
        warnings.append(
            "Recorded as a NEW BASELINE: every gate relative to a prior run is "
            "NOT ASSESSED. Only the single-run success floors were evaluated. "
            "This run becomes the baseline the next one is compared against."
        )

    cand_coverage = candidate.activity_coverage()
    base_coverage = baseline.activity_coverage()
    if cand_coverage and base_coverage and not new_baseline:
        coverage_ratio = max(cand_coverage, base_coverage) / min(cand_coverage, base_coverage)
        if coverage_ratio > config.max_activity_coverage_ratio:
            warnings.append(
                f"Activity coverage differs by {coverage_ratio:.2f}x "
                f"({100 * cand_coverage:.1f}% of the candidate's window had client "
                f"operations running, against {100 * base_coverage:.1f}% of the "
                "baseline's). The per-GB numerators accumulate through idle time "
                "while the denominators do not, so the ratio gates below are "
                "inflated on whichever side is idler. Treat them as indicative "
                "rather than comparable."
            )

    # Advisory gates are reported in full but excluded from the overall verdict,
    # so a tier whose thresholds are not yet calibrated cannot block a ship.
    #
    # A COUNTED [wasm] gate (the relative ones) still never reaches the exit
    # code: `compare` exits 2 only on HARD FAIL, and every WASM gate that counts
    # is soft. A WASM regression is therefore reported and folded into the
    # overall verdict as a SOFT FAIL -- a judgment call, like every other soft
    # gate -- rather than blocking the ship on a tier that is a test instrument.
    blocking = [r for r in results if not r.advisory]
    verdicts = [r.verdict for r in blocking]
    hard_failed = any(r.verdict == FAIL for r in blocking if r.kind == "hard")
    soft_failed = any(r.verdict == FAIL for r in blocking if r.kind == "soft")
    if hard_failed:
        overall = "HARD FAIL"
    elif soft_failed:
        overall = "SOFT FAIL"
    elif GRAY in verdicts:
        overall = "GRAY"
    else:
        overall = "PASS"

    return {
        "schema": 1,
        "overall": overall,
        "threshold_config_version": config.version,
        "threshold_config_path": config.source_path,
        "outbound_ratio_metric": config.outbound_ratio_metric,
        "window_label": candidate.window_label,
        "candidate": {
            "deployment_id": candidate.deployment_id,
            "name": candidate.name,
            "window_start": _iso(candidate.window_start),
            "window_end": _iso(candidate.window_end),
            "activity_coverage": cand_coverage,
            "saorsa_deploy_version": candidate.saorsa_deploy_version,
        },
        "new_baseline": new_baseline,
        "baseline": (
            {
                "deployment_id": None,
                "name": None,
                "window_start": None,
                "window_end": None,
                "activity_coverage": None,
                "saorsa_deploy_version": None,
            }
            if new_baseline
            else {
                "deployment_id": baseline.deployment_id,
                "name": baseline.name,
                "window_start": _iso(baseline.window_start),
                "window_end": _iso(baseline.window_end),
                "activity_coverage": base_coverage,
                "saorsa_deploy_version": baseline.saorsa_deploy_version,
            }
        ),
        "warnings": warnings,
        "counts": {
            PASS: verdicts.count(PASS),
            FAIL: verdicts.count(FAIL),
            GRAY: verdicts.count(GRAY),
            NOT_ASSESSED: verdicts.count(NOT_ASSESSED),
        },
        "gates": [r.to_dict() for r in results],
    }


def _iso(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


def _rate_detail(rate: dict) -> str:
    """`N/M succeeded`, naming the skips when there were any.

    Stated rather than left implicit: with an over-cap entry in the address list
    the denominator is smaller than the number of rows, and a reader comparing
    the two would otherwise think operations had gone missing.
    """
    detail = f"{rate['successes']}/{rate['total']} succeeded"
    if rate.get("skipped"):
        detail += f" ({rate['skipped']} skipped by the transport, excluded)"
    return detail


def _hard_success_floors(run: RunMetrics, config: GateConfig) -> list[GateResult]:
    results = []
    for label, records, floor in (
        ("Upload success floor", run.upload_records, config.upload_success_floor_percent),
        (
            "Download success floor",
            run.download_records,
            config.download_success_floor_percent,
        ),
    ):
        rate = success_rate(records)
        if rate is None or rate["percent"] is None:
            results.append(
                GateResult(
                    gate=label,
                    kind="hard",
                    verdict=NOT_ASSESSED,
                    threshold=floor,
                    unit="%",
                    detail=(
                        "every operation was skipped by the transport; no rate to assess"
                        if rate is not None
                        else "no per-op records for this run. Run `deployments "
                        "record-results` while the monitoring stack is still up."
                    ),
                )
            )
            continue
        results.append(
            GateResult(
                gate=label,
                kind="hard",
                verdict=PASS if rate["percent"] >= floor else FAIL,
                candidate=rate["percent"],
                threshold=floor,
                unit="%",
                detail=_rate_detail(rate),
            )
        )
    return results


#: The integrity gate's NOT ASSESSED reason. A fixed string, not prose: the
#: staging skill matches on it to tell "nothing verified" apart from "verified
#: and clean", which are the two readings a bare absence would conflate.
NO_VERIFIED_DOWNLOADS_REASON = "no verified downloads recorded"

DATA_INTEGRITY_GATE = "Data integrity (download content verification)"


def _hard_data_integrity(run: RunMetrics, config: GateConfig) -> GateResult:
    """Did any download return content that was not what was uploaded?

    Candidate-only, like the success floors and for the same reason: a
    mismatch is wrong in absolute terms, not relative to what the last release
    managed, so there is nothing a baseline would contribute. It therefore
    survives ``--new-baseline`` intact.

    Deliberately separate from the download success floor. A download that
    fails is an availability problem; one that returns different bytes is a
    correctness problem, and folding the second into the first would let a
    run with corrupt content pass on a high enough success rate -- the same
    line the production upload verifier draws.

    Not split per transport: the browser tier publishes no hash and verifies
    nothing yet (V2-1360), so its rows carry NULL and simply do not
    contribute. A `[wasm]` row reading NOT ASSESSED on every run would say
    only that the feature is not built there.
    """
    counts = verification_counts(run.download_records)
    threshold = config.download_verify_mismatch_max
    coverage = counts["coverage"]
    coverage_text = (
        f", coverage {100 * coverage:.1f}% of "
        f"{counts['successful_downloads']} successful download(s)"
        if coverage is not None
        else ""
    )
    detail = (
        f"{counts['ok']} ok / {counts['mismatch']} mismatch / "
        f"{counts['unavailable']} unavailable{coverage_text}"
    )
    if counts["verified"] == 0:
        return GateResult(
            gate=DATA_INTEGRITY_GATE,
            kind="hard",
            verdict=NOT_ASSESSED,
            threshold=threshold,
            unit="mismatches",
            single_run=True,
            detail=(
                f"{NO_VERIFIED_DOWNLOADS_REASON}. {detail}. Content "
                "verification needs an uploader that publishes the file's "
                "SHA-256 on its address line; a run whose uploads predate "
                "that, or whose downloads all failed, has nothing to check."
            ),
        )
    return GateResult(
        gate=DATA_INTEGRITY_GATE,
        kind="hard",
        verdict=PASS if counts["mismatch"] <= threshold else FAIL,
        candidate=float(counts["mismatch"]),
        threshold=threshold,
        unit="mismatches",
        single_run=True,
        detail=detail,
    )


def _soft_success_rates(
    candidate: RunMetrics,
    baseline: RunMetrics,
    config: GateConfig,
    history: dict[str, list[float]],
) -> list[GateResult]:
    results = []
    for label, cand_records, base_records in (
        ("Upload success rate", candidate.upload_records, baseline.upload_records),
        ("Download success rate", candidate.download_records, baseline.download_records),
    ):
        cand = success_rate(cand_records)
        base = success_rate(base_records)
        if cand is None or base is None or cand["percent"] is None or base["percent"] is None:
            results.append(
                GateResult(
                    gate=label,
                    kind="soft",
                    verdict=NOT_ASSESSED,
                    candidate=cand["percent"] if cand else None,
                    baseline=base["percent"] if base else None,
                    threshold=config.success_rate_drop_pp,
                    unit="pp",
                    detail="no per-op records on one or both runs",
                )
            )
            continue
        # Percentage POINTS, not a relative percentage: a fall from 99.6 to 98.9
        # is 0.7 pp, and expressing it relative to a number already near 100
        # would make the threshold nearly unreachable.
        drop_pp = base["percent"] - cand["percent"]
        verdict, noise = apply_noise_rule(
            drop_pp, config.success_rate_drop_pp, history.get(label), config
        )
        results.append(
            GateResult(
                gate=label,
                kind="soft",
                verdict=verdict,
                candidate=cand["percent"],
                baseline=base["percent"],
                delta_percent=drop_pp,
                threshold=config.success_rate_drop_pp,
                unit="pp",
                noise=noise,
                detail=(
                    f"{cand['successes']}/{cand['total']} vs {base['successes']}/{base['total']}"
                ),
            )
        )
    return results


def _soft_transfer_durations(
    candidate: RunMetrics,
    baseline: RunMetrics,
    config: GateConfig,
    history: dict[str, list[float]],
) -> list[GateResult]:
    results = []
    for op_label, cand_records, base_records in (
        ("Upload", candidate.upload_records, baseline.upload_records),
        ("Download", candidate.download_records, baseline.download_records),
    ):
        cand = duration_percentiles_by_bucket(cand_records, config.file_size_buckets_mb)
        base = duration_percentiles_by_bucket(base_records, config.file_size_buckets_mb)
        # Union, not intersection: a bucket present on only one side means the
        # two runs did not do the same work, and hiding it would let a workload
        # change pass unremarked.
        for bucket in sorted(set(cand) | set(base)):
            for stat_key, threshold in (
                ("p50_ms", config.transfer_duration_p50_increase_percent),
                ("p95_ms", config.transfer_duration_p95_increase_percent),
            ):
                gate = f"{op_label} duration {stat_key[:3]} [{bucket}]"
                cand_value = (cand.get(bucket) or {}).get(stat_key)
                base_value = (base.get(bucket) or {}).get(stat_key)
                if cand_value is None or base_value is None:
                    missing = "candidate" if cand_value is None else "baseline"
                    results.append(
                        GateResult(
                            gate=gate,
                            kind="soft",
                            verdict=NOT_ASSESSED,
                            candidate=cand_value,
                            baseline=base_value,
                            threshold=threshold,
                            unit="ms",
                            statistic=stat_key[:3],
                            detail=(
                                f"no successful transfers in this size bucket on the {missing} run"
                            ),
                        )
                    )
                    continue
                delta = _relative_change(cand_value, base_value)
                verdict, noise = apply_noise_rule(delta, threshold, history.get(gate), config)
                results.append(
                    GateResult(
                        gate=gate,
                        kind="soft",
                        verdict=verdict,
                        candidate=cand_value,
                        baseline=base_value,
                        delta_percent=delta,
                        threshold=threshold,
                        unit="ms",
                        statistic=stat_key[:3],
                        noise=noise,
                        detail=(f"n={cand[bucket]['count']} vs {base[bucket]['count']}"),
                    )
                )
    return results


def _soft_transfer_duration_drift(
    candidate: RunMetrics,
    baseline: RunMetrics,
    config: GateConfig,
    history: dict[str, list[float]],
) -> list[GateResult]:
    """Within-window duration drift, candidate against baseline (V2-1717).

    Drift against drift, not the candidate's drift alone. A fleet whose disks
    are filling may legitimately slow a little across any run's own window, and
    the baseline is the control for that; an absolute drift alarm would fire on
    the workload rather than on the release. So the comparison is in
    **percentage points of extra drift** -- the same reason
    ``success_rate_drop_pp`` is in points: both sides are already percentages,
    and a relative change of a number near zero is undefined noise (a baseline
    drifting +0.35% against a candidate drifting +14.38% is a "+4000%"
    regression on one run and a sign flip on the next).
    """
    results = []
    threshold = config.transfer_duration_drift_increase_pp
    for op_label, cand_records, base_records in (
        ("Upload", candidate.upload_records, baseline.upload_records),
        ("Download", candidate.download_records, baseline.download_records),
    ):
        cand = duration_drift_by_bucket(
            cand_records,
            config.file_size_buckets_mb,
            candidate.window_start,
            candidate.window_end,
            config.transfer_duration_drift_min_ops_per_half,
        )
        base = duration_drift_by_bucket(
            base_records,
            config.file_size_buckets_mb,
            baseline.window_start,
            baseline.window_end,
            config.transfer_duration_drift_min_ops_per_half,
        )
        # Union, as the pooled duration gates use: a bucket on only one side
        # means the two runs did not do the same work, which is itself worth
        # reporting rather than hiding.
        for bucket in sorted(set(cand) | set(base)):
            gate = f"{op_label} duration drift [{bucket}]"
            cand_entry = cand.get(bucket) or {}
            base_entry = base.get(bucket) or {}
            cand_value = cand_entry.get("drift_percent")
            base_value = base_entry.get("drift_percent")
            if cand_value is None or base_value is None:
                missing, entry = (
                    ("candidate", cand_entry) if cand_value is None else ("baseline", base_entry)
                )
                excluded = entry.get("excluded_hosts", 0)
                reason = (
                    f"no client in this size bucket on the {missing} run had "
                    f"{config.transfer_duration_drift_min_ops_per_half} successful "
                    "transfers in each half of the window"
                    if excluded
                    else f"no successful transfers in this size bucket on the {missing} run"
                )
                results.append(
                    GateResult(
                        gate=gate,
                        kind="soft",
                        verdict=NOT_ASSESSED,
                        candidate=cand_value,
                        baseline=base_value,
                        threshold=threshold,
                        unit="pp",
                        statistic="drift",
                        detail=reason,
                    )
                )
                continue
            delta_pp = cand_value - base_value
            verdict, noise = apply_noise_rule(delta_pp, threshold, history.get(gate), config)
            results.append(
                GateResult(
                    gate=gate,
                    kind="soft",
                    verdict=verdict,
                    candidate=cand_value,
                    baseline=base_value,
                    delta_percent=delta_pp,
                    threshold=threshold,
                    unit="pp",
                    statistic="drift",
                    noise=noise,
                    detail=(
                        f"median of {cand_entry['hosts']} client(s)"
                        + (
                            f", {cand_entry['excluded_hosts']} below the per-half minimum"
                            if cand_entry.get("excluded_hosts")
                            else ""
                        )
                        + f"; worst {cand_entry['worst_host']} "
                        f"{cand_entry['worst_first_p50_ms'] / 1000:.1f} s → "
                        f"{cand_entry['worst_second_p50_ms'] / 1000:.1f} s "
                        f"({cand_entry['worst_drift_percent']:+.1f}%, "
                        f"n={cand_entry['worst_first_count']}/"
                        f"{cand_entry['worst_second_count']})"
                    ),
                )
            )
    return results


def _soft_host_metrics(
    candidate: RunMetrics,
    baseline: RunMetrics,
    config: GateConfig,
    history: dict[str, list[float]],
) -> list[GateResult]:
    return [
        _compare_scalar(
            gate="Node CPU",
            candidate=node_cpu_statistic(candidate.host_metrics),
            baseline=node_cpu_statistic(baseline.host_metrics),
            threshold=config.node_cpu_increase_percent,
            config=config,
            unit="%",
            history=history.get("Node CPU"),
            missing_detail=(
                "no node-tier CPU series. The run may predate host-metric "
                "bucketing, or its buckets were pruned."
            ),
        ),
        _rss_gate(candidate, baseline, config, history),
    ]


def _rss_gate(
    candidate: RunMetrics,
    baseline: RunMetrics,
    config: GateConfig,
    history: dict[str, list[float]],
) -> GateResult:
    percentile_label = f"p{config.node_rss_service_percentile:g}"
    gate = f"Node RSS ({percentile_label} of services)"
    cand = node_rss_statistic(candidate.host_metrics, config.node_rss_service_percentile)
    base = node_rss_statistic(baseline.host_metrics, config.node_rss_service_percentile)
    result = _compare_scalar(
        gate=gate,
        candidate=cand,
        baseline=base,
        threshold=config.node_rss_increase_percent,
        config=config,
        unit="bytes",
        history=history.get(gate),
        missing_detail=(
            "no per-service RSS series. Telegraf's procstat node_service tag "
            "is the source; a run predating that capture has none."
        ),
    )
    if cand and base:
        # The outlier is always surfaced even when the percentile gate passes,
        # so a single leaking service is never silently absorbed.
        result.detail = (
            f"{cand['services']} vs {base['services']} services; "
            f"worst candidate {cand['worst_service']} at "
            f"{cand['worst_value'] / 1024**3:.3f} GiB"
        )
    return result


def tail_asymmetry(growths: list[float], threshold: float) -> dict:
    """How lopsided the distribution of fitted slopes is.

    A leak confined to a small subset of services does not move the median, so
    the median gate alone would miss it. But the *shape* of the distribution
    still gives it away: fitting a line through a noisy signal produces slopes
    symmetric about zero, so under noise alone the fraction of services above
    ``+threshold`` matches the fraction below ``-threshold``. That makes the
    negative tail a free, self-calibrating reference for the positive one --
    no separate estimate of the noise level is needed, and it re-calibrates
    itself on every run.

    Measured across four recorded runs the ratio sat between 0.98 and 1.41.
    Reported rather than gated on: four runs is far too little history to set a
    threshold, and none of them is known to contain a real leak.
    """
    if not growths:
        return {"above_percent": 0.0, "below_percent": 0.0, "ratio": None}
    total = len(growths)
    above = sum(1 for g in growths if g > threshold)
    below = sum(1 for g in growths if g < -threshold)
    return {
        "criterion": "tail asymmetry (reported, not gated)",
        "tail_threshold_percent_per_hour": threshold,
        "above_percent": 100.0 * above / total,
        "below_percent": 100.0 * below / total,
        # None rather than infinity when nothing is in the negative tail: with
        # no noise reference the ratio says nothing, and a large number would
        # read as strong evidence.
        "ratio": (above / below) if below else None,
        "services": total,
    }


def _soft_memory_leak(run: RunMetrics, config: GateConfig) -> GateResult:
    """The leak check is absolute, not a comparison against the baseline.

    The spec's condition is "non-flat RSS slope over the final 2h of steady
    state" -- a property of the run itself. A baseline that was also leaking
    should not excuse the candidate.
    """
    slopes = rss_leak_slopes(
        run.rss_buckets,
        config.memory_leak.window_hours,
        config.memory_leak.min_points,
    )
    if not slopes:
        return GateResult(
            gate="Memory leak (RSS slope)",
            kind="soft",
            verdict=NOT_ASSESSED,
            threshold=config.memory_leak.max_growth_percent_per_hour,
            unit="%/h",
            detail=(
                f"no service has {config.memory_leak.min_points}+ RSS buckets in the "
                f"final {config.memory_leak.window_hours}h of the window"
            ),
        )
    growths = [s["growth_percent_per_hour"] for s in slopes.values()]
    # A CENTRAL statistic, not a tail one. Fitting a line through a couple of
    # hours of jittery RSS produces a spread of slopes symmetric about zero, so
    # a tail percentile measures how noisy RSS is rather than whether it leaks.
    # A real leak shifts the centre; symmetric fit noise does not.
    growth = percentile(growths, config.memory_leak.service_percentile / 100.0)
    asymmetry = tail_asymmetry(growths, config.memory_leak.tail_threshold_percent_per_hour)
    worst_key = max(slopes, key=lambda k: slopes[k]["growth_percent_per_hour"])
    worst = slopes[worst_key]

    detail = (
        f"{len(slopes)} services; tails +/-"
        f"{config.memory_leak.tail_threshold_percent_per_hour:g}%/h = "
        f"{asymmetry['above_percent']:.1f}% up vs {asymmetry['below_percent']:.1f}% "
        f"down (ratio "
        + (f"{asymmetry['ratio']:.2f}" if asymmetry["ratio"] is not None else "n/a")
        + f"); worst {worst_key} at {worst['slope_bytes_per_hour'] / 1024**2:.1f} MB/h"
    )
    return GateResult(
        gate="Memory leak (RSS slope)",
        kind="soft",
        verdict=(FAIL if growth > config.memory_leak.max_growth_percent_per_hour else PASS),
        candidate=growth,
        threshold=config.memory_leak.max_growth_percent_per_hour,
        unit="%/h",
        statistic=f"p{config.memory_leak.service_percentile:g}",
        detail=detail,
        noise=asymmetry,
    )


# ---------------------------------------------------------------------------
# Connection-error log-string counts (V2-842)
# ---------------------------------------------------------------------------


#: Suffix on every gate computed from pointer operations. A pointer client is a
#: different instrument from a transfer client -- its success rate is over paid
#: writes and corroborated reads, not over file transfers -- so its gates are
#: named apart and can never be mistaken for the file-op ones.
POINTER = "pointer"


def _pointer_summary(run: RunMetrics):
    """The run's pointer summary, or None when it ran no pointer clients."""
    if not run.pointer_records:
        return None
    from saorsa_deploy.pointer_results import summarise

    return summarise(run.pointer_records)


def _handover_summary(run: RunMetrics):
    """The run's handover summary, or None when it ran no pointer clients."""
    if not run.pointer_records:
        return None
    from saorsa_deploy.pointer_results import summarise_handovers

    return summarise_handovers(run.pointer_records)


def _pointer_gates(
    candidate: RunMetrics,
    baseline: RunMetrics,
    config: GateConfig,
    history: dict,
) -> list[GateResult]:
    """The `[pointer]` gates: four floors plus seven relative comparisons.

    Emitted whenever the CANDIDATE has pointer records, and NOT ASSESSED --
    never omitted -- when the baseline has none. A baseline recorded before this
    tier existed has none by construction, which is every baseline on the first
    pointer-carrying train; a gate that vanished from the report would read as
    one nobody thought about.

    Nothing here reaches the exit code, which stays the native file-op hard
    floors'. The two FLOORS are additionally advisory by default
    (`pointer_floor_gates_advisory`), for the same reason the WASM floors are:
    they are uncalibrated numbers on a tier whose expected stale-read rate is
    not yet known from any run, so gating a ship on them would be gating on a
    guess. Promotion is a decision for after a few trains.
    """
    cand = _pointer_summary(candidate)
    if cand is None:
        return []
    base = _pointer_summary(baseline)
    cand_handovers = _handover_summary(candidate)
    advisory_floor = config.pointer_floor_gates_advisory
    results: list[GateResult] = []

    # The two HANDOVER floors (V2-1354). A candidate that ran pointer clients
    # with `transfer_interval_secs: 0` -- which is every train whose RC predates
    # the handover commands -- has no `transfer` records, so both read NOT
    # ASSESSED. Emitted rather than omitted, like every other pointer gate: a
    # gate missing from the report reads as one nobody thought about.
    #
    # Single-run floors, so they survive `--new-baseline` the way the write and
    # read floors do. Neither reaches the exit code.
    handover_floors = (
        (
            f"handover completion [{POINTER}]",
            cand_handovers.completion_rate if cand_handovers else None,
            config.pointer_handover_completion_floor_percent,
            (
                f"{cand_handovers.completed}/{cand_handovers.completion_denominator} "
                f"handovers completed; {cand_handovers.refused_silent_peer} refused "
                "before paying on a silent peer and excluded"
                if cand_handovers
                else ""
            ),
        ),
        (
            f"handover final on sweep [{POINTER}]",
            cand_handovers.sweep_finality_ok_rate if cand_handovers else None,
            config.pointer_handover_final_floor_percent,
            (
                f"{cand_handovers.aged_finality_ok}/"
                f"{cand_handovers.aged_finality_total} aged finality checks found "
                "the whole close group holding the handover"
                if cand_handovers
                else ""
            ),
        ),
    )
    for label, rate, floor, detail in handover_floors:
        if rate is None:
            results.append(
                GateResult(
                    gate=label,
                    kind="soft",
                    verdict=NOT_ASSESSED,
                    threshold=floor,
                    unit="%",
                    advisory=advisory_floor,
                    single_run=True,
                    detail="candidate recorded no pointer handovers",
                )
            )
            continue
        results.append(
            GateResult(
                gate=label,
                kind="soft",
                verdict=PASS if rate >= floor else FAIL,
                candidate=rate,
                threshold=floor,
                unit="%",
                advisory=advisory_floor,
                single_run=True,
                detail=(f"{detail} — advisory, not blocking" if advisory_floor else detail),
            )
        )

    for label, rate, floor, detail in (
        (
            f"Pointer write success floor [{POINTER}]",
            cand.write_success_rate,
            config.pointer_write_success_floor_percent,
            f"{cand.write_ok}/{cand.write_attempts} writes stored",
        ),
        (
            f"Pointer read success floor [{POINTER}]",
            cand.read_success_rate,
            config.pointer_read_success_floor_percent,
            f"{cand.read_ok}/{cand.read_attempts} reads returned the expected state",
        ),
    ):
        if rate is None:
            results.append(
                GateResult(
                    gate=label,
                    kind="soft",
                    verdict=NOT_ASSESSED,
                    threshold=floor,
                    unit="%",
                    advisory=advisory_floor,
                    single_run=True,
                    detail="no pointer operations of this kind on the candidate",
                )
            )
            continue
        results.append(
            GateResult(
                gate=label,
                kind="soft",
                verdict=PASS if rate >= floor else FAIL,
                candidate=rate,
                threshold=floor,
                unit="%",
                advisory=advisory_floor,
                single_run=True,
                detail=(f"{detail} — advisory, not blocking" if advisory_floor else detail),
            )
        )

    # Relative gates. Every one is a "higher is worse" comparison, which is what
    # `_compare_scalar` applies the noise rule to.
    #
    # The stale-read rate is compared, never floored: ADR-0016 says an eclipsed
    # reader can be handed an older validly-signed state and cannot tell, so a
    # non-zero rate is the expected steady state and only a RISE is a finding.
    relative = (
        (
            f"Pointer stale-read rate [{POINTER}]",
            cand.stale_read_rate,
            base.stale_read_rate if base else None,
            config.pointer_stale_read_rate_increase_percent,
            "%",
            f"{cand.stale_reads}/{cand.read_attempts} reads served a stale state",
            True,
        ),
        (
            f"Pointer lost-write rate [{POINTER}]",
            cand.lost_write_rate,
            base.lost_write_rate if base else None,
            config.pointer_lost_write_rate_increase_percent,
            "%",
            f"{cand.lost_writes}/{cand.write_attempts} writes did not land",
            True,
        ),
        (
            f"Pointer write latency p50 [{POINTER}]",
            cand.write_latency(0.50),
            base.write_latency(0.50) if base else None,
            config.pointer_write_latency_p50_increase_percent,
            "ms",
            "",
            False,
        ),
        (
            f"Pointer write latency p95 [{POINTER}]",
            cand.write_latency(0.95),
            base.write_latency(0.95) if base else None,
            config.pointer_write_latency_p95_increase_percent,
            "ms",
            "",
            False,
        ),
        (
            f"Pointer read latency p50 [{POINTER}]",
            cand.read_latency(0.50),
            base.read_latency(0.50) if base else None,
            config.pointer_read_latency_p50_increase_percent,
            "ms",
            "",
            False,
        ),
        (
            f"Pointer read latency p95 [{POINTER}]",
            cand.read_latency(0.95),
            base.read_latency(0.95) if base else None,
            config.pointer_read_latency_p95_increase_percent,
            "ms",
            "",
            False,
        ),
        (
            f"Pointer ANT cost per write [{POINTER}]",
            # In atto. A relative comparison, so the unit cancels; converting to
            # whole ANT first would only lose precision.
            float(cand.mean_ant_atto_per_write)
            if cand.mean_ant_atto_per_write is not None
            else None,
            float(base.mean_ant_atto_per_write)
            if base and base.mean_ant_atto_per_write is not None
            else None,
            config.pointer_cost_per_write_increase_percent,
            "atto",
            f"over {cand.paid_write_count} paid write(s)",
            False,
        ),
    )
    # The two RATES carry zero_is_a_value: 0.000% on both runs is the tier
    # behaving, not a missing measurement, and must read PASS the way a count
    # gate's "neither run logged this" does. The latencies and the cost do not:
    # a zero there is an empty sample.
    for label, cand_value, base_value, threshold, unit, detail, zero_is_a_value in relative:
        result = _compare_scalar(
            gate=label,
            candidate=cand_value,
            baseline=base_value,
            threshold=threshold,
            config=config,
            unit=unit,
            history=history.get(label),
            missing_detail=(
                POINTER_NO_BASELINE_REASON if base is None else "no measurement on one or both runs"
            ),
            zero_is_a_value=zero_is_a_value,
        )
        if detail and result.verdict != NOT_ASSESSED:
            result = replace(result, detail=f"{result.detail} {detail}".strip())
        results.append(result)
    return results


#: The NOT ASSESSED reason a pointer relative gate carries when the baseline ran
#: no pointer clients. A fixed string, like NEW_BASELINE_REASON, because the
#: skills match on it.
POINTER_NO_BASELINE_REASON = "baseline has no pointer records"


def _group_definitions(
    candidate_rows: list[dict],
    baseline_rows: list[dict],
    pattern_set=None,
) -> list[tuple[str, str, str, bool]]:
    """Ordered ``(key, label, aggregate, advisory)`` for every group either run recorded.

    The catalogue is the authority on how a group's members combine, what it
    is called and whether it counts towards the verdict, and its order is the
    reporting order. A group present in the stored rows but absent from the
    catalogue is still evaluated -- the counts were taken deliberately and
    dropping them would be a silent loss -- using its key as the label,
    ``max`` as the aggregate (the conservative choice, because it can only
    understate a sum) and counted, because an unknown group is more likely a
    retired connection-error group than a retired environmental one, and a
    silently discounted regression is the worse mistake.
    """
    known = []
    seen = set()
    if pattern_set is not None:
        for group in pattern_set.groups:
            known.append((group.key, group.label, group.aggregate, bool(group.advisory)))
            seen.add(group.key)
    extra = sorted(
        {
            row["event_group"]
            for row in list(candidate_rows) + list(baseline_rows)
            if row.get("event_group") and row["event_group"] not in seen
        }
    )
    return known + [(key, key, "max", False) for key in extra]


def log_event_group_values(rows: list[dict], key: str, aggregate: str) -> dict | None:
    """Combine one group's per-pattern rows into the single value it gates on.

    ``max`` when the members are the same event logged at several layers --
    summing them would count one occurrence once per layer. Measured on the
    DEV-02 comparison, ``Connection attempt failed`` and
    ``connect_happy_eyeballs failed`` sat at 71,309 and 71,160: summing would
    have reported ~142k connection failures where ~71k occurred.

    ``None`` when the group has no rows at all, which is what a run recorded
    before these counts existed looks like. That is NOT a zero -- a zero is a
    measurement saying the string never appeared.
    """
    members = [row for row in rows if row.get("event_group") == key]
    if not members:
        return None
    counts = [int(row.get("count") or 0) for row in members]
    value = max(counts) if aggregate == "max" else sum(counts)

    hosts = [row["hosts"] for row in members if row.get("hosts") is not None]
    starts = [
        _as_utc(row["observed_start"]) for row in members if row.get("observed_start") is not None
    ]
    ends = [_as_utc(row["observed_end"]) for row in members if row.get("observed_end") is not None]
    totals = [row["total_documents"] for row in members if row.get("total_documents") is not None]

    span_hours = None
    if starts and ends:
        seconds = (max(ends) - min(starts)).total_seconds()
        if seconds > 0:
            span_hours = seconds / 3600.0

    return {
        "value": float(value),
        # The fleet spread is carried through to the report because a count
        # concentrated on two hosts of seventy is a sick node or a service
        # restart, not a fleet-wide regression -- and the raw count cannot tell
        # the difference.
        "hosts": max(hosts) if hosts else None,
        "rate_per_hour": (value / span_hours) if span_hours else None,
        # Every forwarded node log line in the same scope, so a rise in errors
        # can be told apart from a rise in logging.
        "total_documents": max(totals) if totals else None,
        "members": {row["event_key"]: int(row.get("count") or 0) for row in members},
    }


def _fmt_count(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}"


def _log_event_detail(side: str, values: dict | None) -> str:
    if values is None:
        return ""
    parts = [f"{side} {_fmt_count(values['value'])}"]
    if values.get("rate_per_hour") is not None:
        parts.append(f"{values['rate_per_hour']:,.0f}/h")
    if values.get("hosts") is not None:
        parts.append(f"{values['hosts']} hosts")
    if values.get("total_documents"):
        share = 100.0 * values["value"] / values["total_documents"]
        parts.append(f"{share:.3f}% of lines")
    return " ".join(parts)


def _soft_log_event_counts(
    candidate: RunMetrics,
    baseline: RunMetrics,
    config: GateConfig,
    history: dict[str, list[float]],
    pattern_set=None,
) -> list[GateResult]:
    """One gate per pattern group, on the raw counts.

    Absence is NOT ASSESSED on both sides of the transition, and deliberately
    so. These counts cannot be backfilled -- the run's Elasticsearch is
    destroyed at teardown -- so every run recorded before the measurement
    existed has none, and the first comparison to use these gates will report
    NOT ASSESSED for all of them. Recording a missing count as zero would show
    the candidate's real counts against a baseline of nothing and read as a
    catastrophic regression on the very first run.
    """
    results = []
    for key, label, aggregate, advisory in _group_definitions(
        candidate.log_event_counts, baseline.log_event_counts, pattern_set
    ):
        gate = f"Connection errors: {label}"
        cand = log_event_group_values(candidate.log_event_counts, key, aggregate)
        base = log_event_group_values(baseline.log_event_counts, key, aggregate)

        # An advisory group measures the environment the run happened in (the
        # payment RPC being rate-limited, say), not the release. Its row is
        # rendered like any other so the figure sits beside the gates it
        # confounds, but it never reaches the overall verdict -- the same
        # treatment as the uncalibrated WASM floors, for a different reason.
        def _advise(detail: str) -> str:
            return f"{detail} — advisory, not blocking" if advisory else detail

        if cand is None or base is None:
            which = []
            if cand is None:
                which.append("candidate")
            if base is None:
                which.append("baseline")
            results.append(
                GateResult(
                    gate=gate,
                    kind="soft",
                    verdict=NOT_ASSESSED,
                    candidate=cand["value"] if cand else None,
                    baseline=base["value"] if base else None,
                    threshold=config.connection_error_increase_percent,
                    unit="count",
                    detail=_advise(
                        f"no recorded log counts on the {' and '.join(which)} run. "
                        "Run `deployments record-log-counts` while that run's "
                        "Elasticsearch is still up; it cannot be backfilled."
                    ),
                    advisory=advisory,
                )
            )
            continue

        delta = _relative_change(cand["value"], base["value"])
        detail = "; ".join(
            part
            for part in (
                _log_event_detail("cand", cand),
                _log_event_detail("base", base),
            )
            if part
        )

        # A zero baseline makes a relative change undefined, and the two ways
        # that happens are opposite findings, so neither may fall through to
        # the generic "undefined" NOT ASSESSED:
        #
        # * both zero -- the string never occurred on either run. That is a
        #   measurement and a good one; reporting it as NOT ASSESSED would mark
        #   a permanently-eliminated error as unmeasurable forever.
        # * baseline zero, candidate not -- the phenomenon appeared where it
        #   previously did not occur at all. No percentage can express it, but
        #   it exceeds any increase threshold by any reading, and NOT ASSESSED
        #   would read as "could not be measured" for the clearest regression
        #   this gate can see.
        if base["value"] == 0:
            appeared = cand["value"] > 0
            results.append(
                GateResult(
                    gate=gate,
                    kind="soft",
                    verdict=FAIL if appeared else PASS,
                    candidate=cand["value"],
                    baseline=base["value"],
                    threshold=config.connection_error_increase_percent,
                    unit="count",
                    detail=_advise(
                        f"{detail}. The baseline never logged this, so a relative "
                        "change is undefined — but it appeared on the candidate."
                        if appeared
                        else "neither run logged this at all"
                    ),
                    advisory=advisory,
                )
            )
            continue

        verdict, noise = apply_noise_rule(
            delta, config.connection_error_increase_percent, history.get(gate), config
        )
        if aggregate == "max" and len(cand["members"]) > 1:
            # Naming the members keeps the "one phenomenon, two layers"
            # decision visible in the report rather than buried in a YAML file.
            detail += (
                "; one phenomenon from " + ", ".join(sorted(cand["members"])) + " (max, not sum)"
            )
        results.append(
            GateResult(
                gate=gate,
                kind="soft",
                verdict=verdict,
                candidate=cand["value"],
                baseline=base["value"],
                delta_percent=delta,
                threshold=config.connection_error_increase_percent,
                unit="count",
                noise=noise,
                detail=_advise(detail),
                advisory=advisory,
            )
        )
    return results


#: How far above the baseline's per-hour rate an advisory (environmental)
#: group must sit before the comparison repeats its `confounds` note as a
#: warning. 2x rather than the 10% gate threshold because this is not a gate:
#: it is a reason to doubt OTHER gates, and a reader told to doubt the upload
#: durations every time the public RPC had a slightly worse hour would learn to
#: ignore the warning. On the run that motivated it the rate was 8,900/h
#: against a reference whose client-side payment errors were 2.6x lower.
CONFOUNDER_RISE_FACTOR = 2.0


def _confounder_warnings(candidate: RunMetrics, baseline: RunMetrics, pattern_set) -> list[str]:
    """One warning per advisory group whose rate is well above the baseline's.

    The catalogue says which gates the group casts doubt on (`confounds`);
    this puts that sentence at the top of the report, with the two per-hour
    rates, whenever the candidate's rate is `CONFOUNDER_RISE_FACTOR` times the
    baseline's or the baseline has no counts to compare against. A rise in an
    environmental group is not a finding about the release, but a duration
    regression read without it would be -- wrongly.
    """
    warnings: list[str] = []
    if pattern_set is None:
        return warnings
    for group in pattern_set.groups:
        if not group.advisory or not group.confounds:
            continue
        cand = log_event_group_values(candidate.log_event_counts, group.key, group.aggregate)
        if cand is None or cand["value"] <= 0:
            continue
        base = log_event_group_values(baseline.log_event_counts, group.key, group.aggregate)
        cand_rate = cand.get("rate_per_hour") or cand["value"]
        if base is None:
            comparison = (
                f"{cand_rate:,.0f}/h on the candidate ({cand['hosts']} hosts); the "
                "baseline recorded no such counts, so there is nothing to say how "
                "unusual that is"
            )
        else:
            base_rate = base.get("rate_per_hour") or base["value"]
            if base_rate > 0 and cand_rate < CONFOUNDER_RISE_FACTOR * base_rate:
                continue
            comparison = (
                f"{cand_rate:,.0f}/h on the candidate vs {base_rate:,.0f}/h on the baseline"
            )
        warnings.append(
            f"{group.label}: {comparison}. This group is advisory and measures "
            f"the environment, not the release, but it confounds {group.confounds}. "
            "Read those gates against this figure before calling a duration delta "
            "a regression."
        )
    return warnings


def log_count_warnings(candidate: RunMetrics, baseline: RunMetrics, pattern_set=None) -> list[str]:
    """Caveats a reader needs before acting on a count delta.

    Three things a count comparison cannot see for itself. The first is the
    environment: an advisory catalogue group (the payment RPC being
    rate-limited) running well above the baseline's rate, which casts doubt on
    the gates it `confounds` -- see :func:`_confounder_warnings`. Then the two
    the counts themselves cannot reveal:

    * **Pattern-version drift.** The catalogue is versioned config and its
      phrases will change. Two counts of different strings are not a
      comparison, and nothing else in the payload would reveal it.
    * **Unequal observed spans.** Counts scale with how long a run logged for,
      and the window guard permits lengths differing by up to 1.25x -- 25% of
      spurious delta against a 10% threshold. The per-hour rate is in each
      gate's detail; this says when to read it.
    """
    warnings: list[str] = []
    if candidate.log_event_counts:
        warnings.extend(_confounder_warnings(candidate, baseline, pattern_set))
    if not candidate.log_event_counts or not baseline.log_event_counts:
        return warnings

    cand_versions = {row.get("pattern_version") for row in candidate.log_event_counts}
    base_versions = {row.get("pattern_version") for row in baseline.log_event_counts}
    if cand_versions != base_versions:
        warnings.append(
            f"Log-pattern catalogue versions differ: candidate "
            f"{sorted(v for v in cand_versions if v is not None)} vs baseline "
            f"{sorted(v for v in base_versions if v is not None)}. The counted "
            "strings are versioned config and may have changed between them; a "
            "delta across a phrase change measures the phrase, not the release. "
            "Check the catalogue's history before reading the connection-error "
            "gates."
        )

    cand_span = _observed_span_hours(candidate.log_event_counts)
    base_span = _observed_span_hours(baseline.log_event_counts)
    if cand_span and base_span:
        ratio = max(cand_span, base_span) / min(cand_span, base_span)
        if ratio > 1.1:
            warnings.append(
                f"The two runs' logs span materially different periods "
                f"({cand_span:.2f}h candidate vs {base_span:.2f}h baseline, "
                f"{ratio:.2f}x). The connection-error gates compare raw counts, "
                "which scale with how long a run logged for, so read the "
                "per-hour rate in each gate's detail before treating a delta "
                "near the threshold as a regression."
            )
    return warnings


def _observed_span_hours(rows: list[dict]) -> float | None:
    starts = [
        _as_utc(row["observed_start"]) for row in rows if row.get("observed_start") is not None
    ]
    ends = [_as_utc(row["observed_end"]) for row in rows if row.get("observed_end") is not None]
    if not starts or not ends:
        return None
    seconds = (max(ends) - min(starts)).total_seconds()
    return seconds / 3600.0 if seconds > 0 else None


def _soft_derived_ratios(
    candidate: RunMetrics,
    baseline: RunMetrics,
    config: GateConfig,
    history: dict[str, list[float]],
) -> list[GateResult]:
    results = []
    for gate, metric, threshold in (
        (
            "Outbound bytes per GB",
            config.outbound_ratio_metric,
            config.outbound_bytes_per_gb_increase_percent,
        ),
        (
            "Disk bytes per GB stored",
            METRIC_DISK_BYTES_PER_GB_STORED,
            config.disk_bytes_per_gb_increase_percent,
        ),
    ):
        cand = (candidate.derived.get(metric) or {}).get("value")
        base = (baseline.derived.get(metric) or {}).get("value")
        results.append(
            _compare_scalar(
                gate=gate,
                candidate=cand,
                baseline=base,
                threshold=threshold,
                config=config,
                unit="bytes/GB",
                history=history.get(gate),
                missing_detail=(
                    f"'{metric}' was not derived for one or both runs. A ratio with "
                    "an empty denominator produces no row rather than a zero; disk "
                    "occupancy in particular is absent from every run recorded "
                    "before 2026-07-25 and cannot be backfilled."
                ),
            )
        )
    return results
