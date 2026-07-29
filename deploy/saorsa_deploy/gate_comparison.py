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
* and the eight **soft gates** -- success rate, transfer duration p50/p95 per
  file-size bucket, node CPU, node RSS, memory leak slope, outbound bytes per
  GB, and disk bytes per GB stored.

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
from dataclasses import dataclass, field
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


def success_rate(records: list) -> dict | None:
    """Success percentage over a list of per-op records.

    ``None`` for an empty list -- no operations means nothing to assess, which
    is not the same as a 0% success rate.
    """
    if not records:
        return None
    successes = sum(1 for r in records if _status_value(r) == "success")
    total = len(records)
    return {
        "percent": 100.0 * successes / total,
        "successes": successes,
        "total": total,
    }


def duration_percentiles_by_bucket(
    records: list, edges_mb: tuple[float, ...]
) -> dict[str, dict]:
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


def node_rss_statistic(
    host_metrics: list[dict], service_percentile: float = 95.0
) -> dict | None:
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
    return {
        "value": percentile(values, service_percentile / 100.0),
        "statistic": statistic,
        "services": len(values),
        "worst_service": worst_key,
        "worst_value": services[worst_key]["value"],
        "percentile": service_percentile,
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
    label = _statistic_label(stats)
    if label != "mean":
        return {"value": float(stats["p50"]), "statistic": label, "samples": samples}
    if stats.get("avg") is None:
        return None
    return {"value": float(stats["avg"]), "statistic": "mean", "samples": samples}


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
    return {
        "value": weighted / total,
        "statistic": statistic,
        "samples": total,
        "hosts": len(usable),
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
    upload_records: list = field(default_factory=list)
    download_records: list = field(default_factory=list)
    host_metrics: list[dict] = field(default_factory=list)
    rss_buckets: list[dict] = field(default_factory=list)
    # metric name -> {"value": float, "source": str, "window_start": dt, ...}
    derived: dict[str, dict] = field(default_factory=dict)

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
                    f"fewer than {config.noise_rule.min_history_runs} accepted runs "
                    "of history"
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
) -> GateResult:
    """Compare one scalar against its baseline under the noise rule."""
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
    # difference to the release. Refuse rather than mislead.
    if cand_stat and base_stat and cand_stat != base_stat:
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
            "baseline is zero, so a relative change is undefined"
            if delta is None
            else ""
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


def compare_runs(
    candidate: RunMetrics,
    baseline: RunMetrics,
    config: GateConfig,
    history: dict[str, list[float]] | None = None,
) -> dict:
    """Produce the full verdict payload for a candidate against a baseline.

    Raises :class:`ComparisonError` when the two runs are not comparable at all.
    Otherwise always returns a payload: an individual gate that cannot be
    measured is NOT ASSESSED, which never becomes a pass.
    """
    problems = check_comparability(candidate, baseline, config)
    if problems:
        raise ComparisonError("; ".join(problems))

    history = history or {}
    results: list[GateResult] = []

    results.extend(_hard_success_floors(candidate, config))
    results.extend(_soft_success_rates(candidate, baseline, config, history))
    results.extend(_soft_transfer_durations(candidate, baseline, config, history))
    results.extend(_soft_host_metrics(candidate, baseline, config, history))
    results.append(_soft_memory_leak(candidate, config))
    results.extend(_soft_derived_ratios(candidate, baseline, config, history))

    warnings: list[str] = []

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
    if candidate.window_label == WINDOW_FULL_RUN:
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

    cand_coverage = candidate.activity_coverage()
    base_coverage = baseline.activity_coverage()
    if cand_coverage and base_coverage:
        coverage_ratio = max(cand_coverage, base_coverage) / min(
            cand_coverage, base_coverage
        )
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

    verdicts = [r.verdict for r in results]
    hard_failed = any(r.verdict == FAIL for r in results if r.kind == "hard")
    soft_failed = any(r.verdict == FAIL for r in results if r.kind == "soft")
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
        },
        "baseline": {
            "deployment_id": baseline.deployment_id,
            "name": baseline.name,
            "window_start": _iso(baseline.window_start),
            "window_end": _iso(baseline.window_end),
            "activity_coverage": base_coverage,
        },
        "warnings": warnings,
        "counts": {
            PASS: verdicts.count(PASS),
            FAIL: verdicts.count(FAIL),
            GRAY: verdicts.count(GRAY),
            NOT_ASSESSED: verdicts.count(NOT_ASSESSED),
        },
        "gates": [r.to_dict() for r in results],
        # Named explicitly rather than left implicit: a reader must be able to
        # tell the difference between "this gate passed" and "this tool never
        # looks at that gate".
        "not_evaluated_by_this_tool": [
            "Data integrity (no content verification exists in the system)",
            "Crashes (panics reach journald, not the JSON logs Telegraf tails; "
            "Elasticsearch is destroyed with the testnet)",
            "Mixed-version phase (previous-version nodes storing chunks is an "
            "SSH check against chunks.mdb growth)",
            "Canaries (a separate production deployment with its own assessment)",
        ],
    }


def _iso(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


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
        if rate is None:
            results.append(
                GateResult(
                    gate=label,
                    kind="hard",
                    verdict=NOT_ASSESSED,
                    threshold=floor,
                    unit="%",
                    detail=(
                        "no per-op records for this run. Run `deployments "
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
                detail=f"{rate['successes']}/{rate['total']} succeeded",
            )
        )
    return results


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
        if cand is None or base is None:
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
                    f"{cand['successes']}/{cand['total']} vs "
                    f"{base['successes']}/{base['total']}"
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
                                f"no successful transfers in this size bucket on the "
                                f"{missing} run"
                            ),
                        )
                    )
                    continue
                delta = _relative_change(cand_value, base_value)
                verdict, noise = apply_noise_rule(
                    delta, threshold, history.get(gate), config
                )
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
                        detail=(
                            f"n={cand[bucket]['count']} vs {base[bucket]['count']}"
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
    asymmetry = tail_asymmetry(
        growths, config.memory_leak.tail_threshold_percent_per_hour
    )
    worst_key = max(slopes, key=lambda k: slopes[k]["growth_percent_per_hour"])
    worst = slopes[worst_key]

    detail = (
        f"{len(slopes)} services; tails +/-"
        f"{config.memory_leak.tail_threshold_percent_per_hour:g}%/h = "
        f"{asymmetry['above_percent']:.1f}% up vs {asymmetry['below_percent']:.1f}% "
        f"down (ratio "
        + (
            f"{asymmetry['ratio']:.2f}"
            if asymmetry["ratio"] is not None
            else "n/a"
        )
        + f"); worst {worst_key} at {worst['slope_bytes_per_hour'] / 1024**2:.1f} MB/h"
    )
    return GateResult(
        gate="Memory leak (RSS slope)",
        kind="soft",
        verdict=(
            FAIL if growth > config.memory_leak.max_growth_percent_per_hour else PASS
        ),
        candidate=growth,
        threshold=config.memory_leak.max_growth_percent_per_hour,
        unit="%/h",
        statistic=f"p{config.memory_leak.service_percentile:g}",
        detail=detail,
        noise=asymmetry,
    )


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
