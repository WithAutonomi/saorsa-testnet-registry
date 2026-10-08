"""Deterministic gate comparison (V2-721).

The release process requires a tool, not an agent, to compute the staging
verdict, and requires the same inputs to always produce an identical report.
These tests pin the arithmetic, the three comparability invariants, and the
rule that a gate which cannot be measured is NOT ASSESSED rather than a pass.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from saorsa_deploy.gate_comparison import (
    DATA_INTEGRITY_GATE,
    FAIL,
    GRAY,
    NOT_ASSESSED,
    PASS,
    ComparisonError,
    RunMetrics,
    apply_noise_rule,
    bucket_label,
    check_comparability,
    compare_runs,
    duration_percentiles_by_bucket,
    filter_ops_to_window,
    linear_slope,
    node_cpu_statistic,
    node_rss_statistic,
    percentile,
    rss_leak_slopes,
    stdev,
    success_rate,
    verification_counts,
)
from saorsa_deploy.gate_config import load_gate_config
from saorsa_deploy.gate_report import render_report

UTC = timezone.utc
MB = 1024 * 1024
T0 = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def config():
    return load_gate_config()


@dataclass
class FakeOp:
    """Stands in for a DeploymentUploadRecord / DeploymentDownloadRecord row."""

    status: str
    file_size_bytes: int
    duration_ms: int | None
    finished_at: datetime | None = None
    #: Content verification (V2-757). None on an upload row, on a failed
    #: download, and on every row recorded before verification existed.
    verify_status: str | None = None


def op(status="success", size_mb=100, duration_ms=1000, minutes=0, verify_status=None):
    return FakeOp(
        status=status,
        file_size_bytes=size_mb * MB,
        duration_ms=duration_ms,
        finished_at=T0 + timedelta(minutes=minutes),
        verify_status=verify_status,
    )


# ---------------------------------------------------------------------------
# Statistics primitives
# ---------------------------------------------------------------------------


def test_percentile_interpolates_linearly():
    values = [1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 1.0) == 4.0
    assert percentile(values, 0.5) == 2.5


def test_percentile_of_empty_is_none_not_zero():
    # A zero would trip every downstream threshold as a catastrophic
    # improvement or regression; absence must stay absent.
    assert percentile([], 0.5) is None


def test_percentile_is_order_independent():
    assert percentile([3.0, 1.0, 2.0], 0.5) == percentile([1.0, 2.0, 3.0], 0.5)


def test_stdev_needs_two_points():
    assert stdev([5.0]) is None
    assert stdev([2.0, 4.0]) == pytest.approx(1.4142135, rel=1e-5)


def test_linear_slope_recovers_a_known_gradient():
    points = [(0.0, 100.0), (1.0, 200.0), (2.0, 300.0)]
    assert linear_slope(points) == pytest.approx(100.0)


def test_linear_slope_is_none_without_x_spread():
    assert linear_slope([(1.0, 5.0), (1.0, 9.0)]) is None


# ---------------------------------------------------------------------------
# File-size bucketing
# ---------------------------------------------------------------------------


def test_bucket_label_is_half_open(config):
    edges = config.file_size_buckets_mb
    # 64 is an edge: it belongs to the bucket that starts at 64, not the one
    # that ends there. Otherwise a file would land in two buckets.
    assert bucket_label(64 * MB, edges) == "64-128MB"
    assert bucket_label(63 * MB, edges) == "32-64MB"


def test_bucket_label_outside_every_edge_is_none(config):
    assert bucket_label(99_999 * MB, config.file_size_buckets_mb) is None


def test_bucket_labels_come_from_edges_not_observed_sizes(config):
    # Same label on both sides of a comparison even when the two runs happen to
    # contain different actual file sizes within the bucket.
    edges = config.file_size_buckets_mb
    assert bucket_label(70 * MB, edges) == bucket_label(120 * MB, edges)


# ---------------------------------------------------------------------------
# Per-op statistics
# ---------------------------------------------------------------------------


def test_success_rate_counts_failures():
    records = [op(), op(), op(status="failure")]
    rate = success_rate(records)
    assert rate["percent"] == pytest.approx(66.6666, rel=1e-3)
    assert rate["successes"] == 2
    assert rate["total"] == 3


def test_success_rate_of_no_records_is_none():
    assert success_rate([]) is None


def test_duration_percentiles_exclude_failures(config):
    # A failed transfer's duration is how long it took to give up. Mixing it in
    # lets a change in the failure mix move the percentile with nothing getting
    # slower.
    records = [
        op(duration_ms=100),
        op(duration_ms=200),
        op(status="failure", duration_ms=999_999),
    ]
    result = duration_percentiles_by_bucket(records, config.file_size_buckets_mb)
    assert result["64-128MB"]["count"] == 2
    assert result["64-128MB"]["p50_ms"] == pytest.approx(150.0)


def test_duration_percentiles_are_grouped_per_bucket(config):
    records = [op(size_mb=20, duration_ms=10), op(size_mb=1000, duration_ms=5000)]
    result = duration_percentiles_by_bucket(records, config.file_size_buckets_mb)
    assert set(result) == {"0-32MB", "768-1536MB"}
    assert result["0-32MB"]["p50_ms"] == 10.0
    assert result["768-1536MB"]["p50_ms"] == 5000.0


def test_filter_ops_to_window_is_half_open():
    records = [op(minutes=0), op(minutes=30), op(minutes=60)]
    kept = filter_ops_to_window(records, T0, T0 + timedelta(minutes=60))
    assert len(kept) == 2


def test_filter_ops_drops_never_finished_ops():
    records = [op(minutes=0), FakeOp("failure", 100 * MB, None, None)]
    assert len(filter_ops_to_window(records, T0, T0 + timedelta(hours=1))) == 1


# ---------------------------------------------------------------------------
# Host metrics
# ---------------------------------------------------------------------------


def test_node_cpu_is_sample_weighted_not_a_mean_of_means():
    # A host reporting for a tenth of the window must carry a tenth of the
    # weight. A plain mean of the two averages would give 55.0.
    host_metrics = [
        {"host": "node-1", "cpu_percent": {"avg": 10.0, "samples": 900}},
        {"host": "node-2", "cpu_percent": {"avg": 100.0, "samples": 100}},
    ]
    result = node_cpu_statistic(host_metrics)
    assert result["value"] == pytest.approx(19.0)
    assert result["statistic"] == "mean"


def test_node_cpu_prefers_a_true_percentile_when_present():
    host_metrics = [
        {"host": "node-1", "cpu_percent": {"avg": 10.0, "p50": 8.0, "samples": 100}},
    ]
    result = node_cpu_statistic(host_metrics)
    assert result["statistic"] == "p50"
    assert result["value"] == pytest.approx(8.0)


def test_mixed_percentile_availability_degrades_the_whole_pool_to_mean():
    # Averaging one host's median with another's mean would be neither, and the
    # blend would shift silently as more runs gained percentile capture.
    host_metrics = [
        {"host": "node-1", "cpu_percent": {"avg": 10.0, "p50": 8.0, "samples": 100}},
        {"host": "node-2", "cpu_percent": {"avg": 20.0, "samples": 100}},
    ]
    assert node_cpu_statistic(host_metrics)["statistic"] == "mean"


def test_exact_and_approximate_percentiles_are_labelled_differently():
    """A window's median is estimated; the rollup's is exact.

    Percentiles do not compose across buckets the way avg/min/max do, so a
    windowed figure is an estimate from per-bucket medians. Passing it off as
    the same statistic as the rollup's exact whole-run median would let the
    method's error be read as a change in the release.
    """
    exact = [{"host": "n1", "cpu_percent": {"avg": 10.0, "p50": 8.0, "samples": 100}}]
    approx = [
        {
            "host": "n1",
            "cpu_percent": {
                "avg": 10.0,
                "p50": 8.0,
                "percentile_approx": True,
                "samples": 100,
            },
        }
    ]
    assert node_cpu_statistic(exact)["statistic"] == "p50"
    assert node_cpu_statistic(approx)["statistic"] == "p50~"


def test_comparing_an_exact_against_an_approximate_percentile_is_refused(config):
    candidate = _full_run(1, "STG-01", 20.0, 1e9)
    baseline = _full_run(2, "STG-00", 20.0, 1e9)
    candidate.host_metrics = [
        {"host": "n1", "cpu_percent": {"avg": 20.0, "p50": 20.0, "samples": 100}}
    ]
    baseline.host_metrics = [
        {
            "host": "n1",
            "cpu_percent": {
                "avg": 20.0,
                "p50": 20.0,
                "percentile_approx": True,
                "samples": 100,
            },
        }
    ]
    verdict = compare_runs(candidate, baseline, config)
    cpu = next(g for g in verdict["gates"] if g["gate"] == "Node CPU")
    assert cpu["verdict"] == NOT_ASSESSED
    assert "p50 vs p50~" in cpu["statistic"]


def test_percentile_against_a_baseline_without_one_degrades_to_the_shared_mean(
    config,
):
    """A baseline predating percentile capture must not silently drop the gate.

    Refusing on any statistic mismatch removed node CPU and node RSS from every
    comparison against such a baseline -- which is exactly the run the gates were
    last measured on. Both sides carry a mean, so the comparison degrades to it
    and says so, rather than reporting NOT ASSESSED.
    """
    candidate = _full_run(1, "STG-01", 20.0, 1e9)
    baseline = _full_run(2, "STG-00", 20.0, 1e9)
    candidate.host_metrics = [
        {"host": "n1", "cpu_percent": {"avg": 55.0, "p50": 50.0, "samples": 100}}
    ]
    # No p50 at all -- the shape of every run recorded before percentile capture.
    baseline.host_metrics = [{"host": "n1", "cpu_percent": {"avg": 50.0, "samples": 100}}]

    cpu = next(
        g for g in compare_runs(candidate, baseline, config)["gates"] if g["gate"] == "Node CPU"
    )
    assert cpu["verdict"] != NOT_ASSESSED
    assert cpu["statistic"] == "mean"
    # Compared on the means (55 vs 50), NOT the candidate's p50 of 50 against the
    # baseline's mean of 50, which would have reported a spurious 0% delta.
    assert cpu["delta_percent"] == pytest.approx(10.0)
    assert "degraded to mean" in cpu["detail"]


def test_degrading_to_the_mean_still_applies_the_threshold(config):
    """The degraded comparison is a real gate, not a formality."""
    candidate = _full_run(1, "STG-01", 20.0, 1e9)
    baseline = _full_run(2, "STG-00", 20.0, 1e9)
    candidate.host_metrics = [
        {"host": "n1", "cpu_percent": {"avg": 80.0, "p50": 50.0, "samples": 100}}
    ]
    baseline.host_metrics = [{"host": "n1", "cpu_percent": {"avg": 50.0, "samples": 100}}]

    cpu = next(
        g for g in compare_runs(candidate, baseline, config)["gates"] if g["gate"] == "Node CPU"
    )
    assert cpu["verdict"] == FAIL  # +60% against a 15% threshold


def test_no_shared_statistic_is_still_refused(config):
    """Degradation needs a mean on BOTH sides; without one, refuse as before."""
    candidate = _full_run(1, "STG-01", 20.0, 1e9)
    baseline = _full_run(2, "STG-00", 20.0, 1e9)
    candidate.host_metrics = [{"host": "n1", "cpu_percent": {"p50": 50.0, "samples": 100}}]
    baseline.host_metrics = [{"host": "n1", "cpu_percent": {"avg": 50.0, "samples": 100}}]

    cpu = next(
        g for g in compare_runs(candidate, baseline, config)["gates"] if g["gate"] == "Node CPU"
    )
    assert cpu["verdict"] == NOT_ASSESSED


def test_client_hosts_are_excluded_from_node_cpu():
    host_metrics = [
        {"host": "node-1", "cpu_percent": {"avg": 10.0, "samples": 100}},
        {"host": "stg-01-uploader-3", "cpu_percent": {"avg": 90.0, "samples": 100}},
    ]
    assert node_cpu_statistic(host_metrics)["value"] == pytest.approx(10.0)


def _rss_fleet(values):
    return [
        {
            "host": "node-1",
            "rss_by_service": {
                f"ant-node-{i}": {"avg": float(v), "samples": 10} for i, v in enumerate(values)
            },
        }
    ]


def test_rss_percentile_is_robust_to_a_single_outlier():
    # The maximum of a large fleet is an extreme order statistic that moves
    # with the single unluckiest service. Measured on STG-01 470 -> 474 the max
    # moved +64% while the median moved +3.8% and the p95 +1.8%.
    fleet = _rss_fleet([100.0] * 99 + [100_000.0])
    assert node_rss_statistic(fleet, 95.0)["value"] == pytest.approx(100.0)


def test_rss_percentile_100_restores_worst_service_behaviour():
    fleet = _rss_fleet([100.0] * 99 + [100_000.0])
    assert node_rss_statistic(fleet, 100.0)["value"] == pytest.approx(100_000.0)


def test_rss_statistic_always_surfaces_the_worst_service():
    # A single leaking service must never be silently absorbed by the
    # percentile, even when the gate passes.
    fleet = _rss_fleet([100.0] * 99 + [100_000.0])
    result = node_rss_statistic(fleet, 95.0)
    assert result["worst_value"] == 100_000.0
    assert result["worst_service"] == "node-1/ant-node-99"
    assert result["services"] == 100


def test_rss_percentile_still_catches_a_fleet_wide_shift():
    low = node_rss_statistic(_rss_fleet([100.0] * 100), 95.0)["value"]
    high = node_rss_statistic(_rss_fleet([200.0] * 100), 95.0)["value"]
    assert high == pytest.approx(2 * low)


def test_no_host_metrics_gives_none_not_zero():
    assert node_cpu_statistic([]) is None
    assert node_rss_statistic([]) is None


# ---------------------------------------------------------------------------
# Memory-leak slope
# ---------------------------------------------------------------------------


def _rss_buckets(values, service="ant-node-1", host="node-1", step_minutes=5):
    return [
        {
            "host": host,
            "metric": "rss_bytes",
            "service": service,
            "bucket_start": T0 + timedelta(minutes=step_minutes * i),
            "avg": float(v),
            "samples": 30,
        }
        for i, v in enumerate(values)
    ]


def test_flat_rss_has_no_leak():
    buckets = _rss_buckets([1000.0] * 24)
    slopes = rss_leak_slopes(buckets, window_hours=2.0, min_points=6)
    assert slopes["node-1/ant-node-1"]["growth_percent_per_hour"] == pytest.approx(0.0)


def test_rising_rss_is_reported_as_percent_of_its_own_mean():
    # Normalised so one threshold works across services of different sizes.
    buckets = _rss_buckets([1000.0 + 100.0 * i for i in range(24)])
    slopes = rss_leak_slopes(buckets, window_hours=2.0, min_points=6)
    assert slopes["node-1/ant-node-1"]["growth_percent_per_hour"] > 0


def test_leak_gate_uses_a_central_statistic_not_a_tail():
    """Symmetric fit noise must not fail the gate.

    Fitting a line through a couple of hours of jittery RSS gives a spread of
    slopes symmetric about zero. Measured across four recorded runs (~990
    services each): p5 of -40 to -60 %/h against p95 of +37 to +60 %/h, median
    within +-2.5, and ~50% of services "growing". A tail percentile would fail
    every one of those clean runs.
    """
    config = load_gate_config()
    # 100 services spread symmetrically from -50 to +50 %/h, median ~0.
    noisy = list(range(-50, 50))
    growth = percentile([float(v) for v in noisy], config.memory_leak.service_percentile / 100.0)
    assert abs(growth) < config.memory_leak.max_growth_percent_per_hour


def test_tail_asymmetry_is_balanced_under_symmetric_noise():
    from saorsa_deploy.gate_comparison import tail_asymmetry

    symmetric = [float(v) for v in range(-50, 50)]
    result = tail_asymmetry(symmetric, 25.0)
    assert result["ratio"] == pytest.approx(1.0, rel=0.1)


def test_tail_asymmetry_flags_a_one_sided_subset():
    from saorsa_deploy.gate_comparison import tail_asymmetry

    # A subset leaking: many services well above +25, none below -25.
    one_sided = [0.0] * 80 + [100.0] * 20
    result = tail_asymmetry(one_sided, 25.0)
    assert result["above_percent"] == pytest.approx(20.0)
    assert result["below_percent"] == 0.0
    # None, not infinity: with no noise reference the ratio says nothing.
    assert result["ratio"] is None


def test_leak_check_skips_services_with_too_few_points():
    buckets = _rss_buckets([1000.0, 1100.0, 1200.0])
    assert rss_leak_slopes(buckets, window_hours=2.0, min_points=6) == {}


def test_leak_check_ignores_client_hosts():
    buckets = _rss_buckets([1000.0 + 500.0 * i for i in range(24)], host="dl-downloader-1")
    assert rss_leak_slopes(buckets, window_hours=2.0, min_points=6) == {}


def test_leak_window_is_computed_per_host_not_fleet_wide():
    """A clock-skewed host must not erase every other host's leak check.

    The staging profile skews node clocks by up to +-3h in a per-host random
    direction, and Telegraf timestamps by the host's own clock. A fleet-wide
    `max(bucket_start)` is whatever the most forward-skewed host thinks the
    time is, so a "final 2h" measured back from it misses everyone else. On
    STG-01 run 470 that found 15 services where 997 had reported.
    """
    normal = _rss_buckets([1000.0] * 24, host="node-normal")
    # Same 24 buckets, but this host's clock runs three hours fast.
    skewed = [
        {**b, "host": "node-skewed", "bucket_start": b["bucket_start"] + timedelta(hours=3)}
        for b in _rss_buckets([1000.0] * 24, host="node-skewed")
    ]
    slopes = rss_leak_slopes(normal + skewed, window_hours=2.0, min_points=6)
    # Both hosts assessed. A fleet-wide cutoff would have kept only the skewed
    # one, because every one of the normal host's buckets predates it.
    assert "node-normal/ant-node-1" in slopes
    assert "node-skewed/ant-node-1" in slopes


def test_leak_window_only_covers_the_final_hours():
    # 8 hours of buckets, only the last 2 should be fitted.
    buckets = _rss_buckets([1000.0] * 96)
    slopes = rss_leak_slopes(buckets, window_hours=2.0, min_points=6)
    assert slopes["node-1/ant-node-1"]["points"] <= 25


# ---------------------------------------------------------------------------
# The noise rule
# ---------------------------------------------------------------------------


def test_below_history_minimum_the_percentage_threshold_stands_alone(config):
    verdict, noise = apply_noise_rule(15.0, 10.0, [1.0, 2.0], config)
    assert verdict == FAIL
    assert noise["criterion"] == "percentage only"


def test_within_threshold_passes(config):
    verdict, _ = apply_noise_rule(5.0, 10.0, None, config)
    assert verdict == PASS


def test_exceeding_both_criteria_fails(config):
    # Tight history -> a small sigma band, so a 50% delta clears both.
    history = [100.0, 101.0, 99.0, 100.5, 99.5]
    verdict, noise = apply_noise_rule(50.0, 10.0, history, config)
    assert verdict == FAIL
    assert noise["exceeds_percent"] and noise["exceeds_sigma"]


def test_exceeding_only_one_criterion_is_gray(config):
    # Noisy history -> a wide sigma band the 12% delta does not clear, but it
    # does clear the 10% percentage threshold.
    history = [50.0, 150.0, 60.0, 140.0, 100.0]
    verdict, noise = apply_noise_rule(12.0, 10.0, history, config)
    assert verdict == GRAY
    assert noise["exceeds_percent"] and not noise["exceeds_sigma"]


def test_history_without_variance_falls_back_to_percentage(config):
    # Zero variance across five runs is a degenerate history, not evidence that
    # every change is significant.
    verdict, noise = apply_noise_rule(5.0, 10.0, [100.0] * 5, config)
    assert verdict == PASS
    assert noise["reason"] == "history has no variance"


def test_absent_delta_is_not_assessed(config):
    verdict, _ = apply_noise_rule(None, 10.0, None, config)
    assert verdict == NOT_ASSESSED


# ---------------------------------------------------------------------------
# Comparability invariants
# ---------------------------------------------------------------------------


def _run(deployment_id=1, name="STG-01", label="full-run", **kwargs):
    return RunMetrics(deployment_id=deployment_id, name=name, window_label=label, **kwargs)


def test_mismatched_window_labels_refuse(config):
    problems = check_comparability(
        _run(label="steady-state"), _run(deployment_id=2, label="full-run"), config
    )
    assert any("window labels differ" in p for p in problems)


def test_mismatched_derived_source_refuses(config):
    # capture is exact; buckets is approximate. Comparing them measures the
    # method, not the release.
    candidate = _run(derived={"gb_uploaded": {"value": 1.0, "source": "capture"}})
    baseline = _run(deployment_id=2, derived={"gb_uploaded": {"value": 1.0, "source": "buckets"}})
    problems = check_comparability(candidate, baseline, config)
    assert any("sources differ" in p for p in problems)


def test_materially_different_window_lengths_refuse(config):
    # Replication continues after an upload completes, so a short window
    # truncates the disk numerator while counting the full denominator.
    candidate = _run(window_start=T0, window_end=T0 + timedelta(hours=3))
    baseline = _run(deployment_id=2, window_start=T0, window_end=T0 + timedelta(hours=10))
    problems = check_comparability(candidate, baseline, config)
    assert any("window lengths differ" in p for p in problems)


def test_similar_window_lengths_are_accepted(config):
    candidate = _run(window_start=T0, window_end=T0 + timedelta(hours=3))
    baseline = _run(
        deployment_id=2, window_start=T0, window_end=T0 + timedelta(hours=3, minutes=20)
    )
    assert check_comparability(candidate, baseline, config) == []


def test_activity_coverage_mismatch_warns_rather_than_refusing(config):
    """A mostly-idle window inflates every per-GB ratio.

    The window-LENGTH check cannot see this: the length can be perfectly
    ordinary while most of it is idle. Measured on run 471 -- a ~10h recorded
    window around a ~4h measurement -- the disk ratio came out at 14.86 GB/GB
    against 9.1-10.1 for three runs with normal coverage.
    """
    busy = _full_run(1, "STG-01", 20.0, 1e9)
    idle = _full_run(2, "STG-00", 20.0, 1e9)
    # Same window, but the baseline's operations all finish in the first
    # few minutes, leaving the rest of its window idle.
    idle.upload_records = [op(minutes=i % 5) for i in range(100)]
    idle.download_records = idle.upload_records

    verdict = compare_runs(busy, idle, config)
    assert any("Activity coverage differs" in w for w in verdict["warnings"])
    # A warning, not a refusal: four runs is too little history to block a
    # release on, and the coverage figures are in the report either way.
    assert verdict["overall"] in (PASS, GRAY, "SOFT FAIL", "HARD FAIL")
    assert verdict["candidate"]["activity_coverage"] is not None


def test_matched_coverage_produces_no_coverage_warning(config):
    verdict = compare_runs(
        _full_run(1, "STG-01", 20.0, 1e9), _full_run(2, "STG-00", 20.0, 1e9), config
    )
    assert not any("Activity coverage" in w for w in verdict["warnings"])


def test_full_run_window_warns_about_phase_composition(config):
    """Neither guard inspects what the windows CONTAIN.

    A full-run window is the span of observed metrics, not the run's shape: the
    deliberate node clock skew drags a host's first points up to 3h before T0,
    and any post-measurement phase is swept in too. On STG-01 #474 vs #470 both
    guards passed (length 1.13x, coverage 1.12x) while the candidate window
    carried 3h05m of pre-run data and a mixed-version hour the baseline never
    ran.
    """
    verdict = compare_runs(
        _full_run(1, "STG-01", 20.0, 1e9), _full_run(2, "STG-00", 20.0, 1e9), config
    )
    assert any("full-run" in w for w in verdict["warnings"])


def test_a_labelled_phase_window_does_not_warn(config):
    candidate = _full_run(1, "STG-01", 20.0, 1e9)
    baseline = _full_run(2, "STG-00", 20.0, 1e9)
    for run in (candidate, baseline):
        run.window_label = "steady-state"
        for entry in run.derived.values():
            entry["source"] = "buckets"
    verdict = compare_runs(candidate, baseline, config)
    assert verdict["warnings"] == []


def test_compare_runs_raises_on_incomparable_runs(config):
    with pytest.raises(ComparisonError):
        compare_runs(_run(label="steady-state"), _run(deployment_id=2, label="full-run"), config)


# ---------------------------------------------------------------------------
# End-to-end verdict assembly
# ---------------------------------------------------------------------------


def _full_run(deployment_id, name, cpu, ratio, ok=100, fail=0, duration=1000):
    # Spread across the 4h window so activity coverage is realistic; a run whose
    # operations all share one timestamp has a zero-length activity span.
    total = ok + fail
    step = 240 / max(total, 1)
    records = [op(duration_ms=duration, minutes=int(i * step)) for i in range(ok)] + [
        op(status="failure", minutes=int((ok + i) * step)) for i in range(fail)
    ]
    return RunMetrics(
        deployment_id=deployment_id,
        name=name,
        window_label="full-run",
        window_start=T0,
        window_end=T0 + timedelta(hours=4),
        upload_records=records,
        download_records=records,
        host_metrics=[{"host": "node-1", "cpu_percent": {"avg": cpu, "samples": 100}}],
        rss_buckets=_rss_buckets([1000.0] * 24),
        derived={
            "outbound_bytes_per_gb_traffic": {"value": ratio, "source": "capture"},
            "disk_bytes_per_gb_stored": {"value": ratio, "source": "capture"},
        },
    )


def test_identical_runs_pass_every_gate(config):
    verdict = compare_runs(
        _full_run(1, "STG-01", 20.0, 1e9), _full_run(2, "STG-00", 20.0, 1e9), config
    )
    assert verdict["overall"] == PASS
    assert verdict["counts"][FAIL] == 0


def test_cpu_regression_fails_the_soft_gate(config):
    verdict = compare_runs(
        _full_run(1, "STG-01", 40.0, 1e9), _full_run(2, "STG-00", 20.0, 1e9), config
    )
    cpu = next(g for g in verdict["gates"] if g["gate"] == "Node CPU")
    assert cpu["verdict"] == FAIL
    assert cpu["delta_percent"] == pytest.approx(100.0)
    assert verdict["overall"] == "SOFT FAIL"


def test_success_floor_breach_is_a_hard_fail(config):
    verdict = compare_runs(
        _full_run(1, "STG-01", 20.0, 1e9, ok=90, fail=10),
        _full_run(2, "STG-00", 20.0, 1e9),
        config,
    )
    assert verdict["overall"] == "HARD FAIL"
    floor = next(g for g in verdict["gates"] if g["gate"] == "Upload success floor")
    assert floor["verdict"] == FAIL
    assert floor["kind"] == "hard"


def test_missing_derived_metric_is_not_assessed_never_zero(config):
    candidate = _full_run(1, "STG-01", 20.0, 1e9)
    candidate.derived.pop("disk_bytes_per_gb_stored")
    baseline = _full_run(2, "STG-00", 20.0, 1e9)
    verdict = compare_runs(candidate, baseline, config)
    disk = next(g for g in verdict["gates"] if g["gate"] == "Disk bytes per GB stored")
    assert disk["verdict"] == NOT_ASSESSED
    assert disk["candidate"] is None
    # Crucially not a FAIL: a zero would have tripped the threshold.
    assert verdict["overall"] == PASS


# ---------------------------------------------------------------------------
# Determinism -- the whole point of the tool
# ---------------------------------------------------------------------------


def test_same_inputs_produce_a_byte_identical_report(config):
    candidate = _full_run(1, "STG-01", 25.0, 1.2e9)
    baseline = _full_run(2, "STG-00", 20.0, 1e9)
    first = render_report(compare_runs(candidate, baseline, config))
    second = render_report(compare_runs(candidate, baseline, config))
    assert first == second


def test_report_does_not_embed_a_generation_time_by_default(config):
    report = render_report(
        compare_runs(_full_run(1, "STG-01", 20.0, 1e9), _full_run(2, "STG-00", 20.0, 1e9), config)
    )
    assert "Computed at" not in report


def test_report_names_the_threshold_config_version(config):
    report = render_report(
        compare_runs(_full_run(1, "STG-01", 20.0, 1e9), _full_run(2, "STG-00", 20.0, 1e9), config)
    )
    assert f"Threshold config version: {config.version}" in report


def test_report_flags_a_mean_standing_in_for_a_median(config):
    # The spec names node CPU *median*; a mean silently standing in for it is
    # exactly the substitution nobody notices.
    report = render_report(
        compare_runs(_full_run(1, "STG-01", 20.0, 1e9), _full_run(2, "STG-00", 20.0, 1e9), config)
    )
    assert "[mean]" in report


# ---------------------------------------------------------------------------
# Data integrity (V2-757)
# ---------------------------------------------------------------------------


def _integrity_gate(verdict):
    return next(g for g in verdict["gates"] if g["gate"] == DATA_INTEGRITY_GATE)


def _run_with_verifications(deployment_id, name, verify_statuses, failures=0):
    records = [op(minutes=i, verify_status=v) for i, v in enumerate(verify_statuses)] + [
        op(status="failure", minutes=len(verify_statuses) + i) for i in range(failures)
    ]
    return RunMetrics(
        deployment_id=deployment_id,
        name=name,
        window_label="full-run",
        window_start=T0,
        window_end=T0 + timedelta(hours=4),
        upload_records=[op(minutes=i) for i in range(len(verify_statuses))],
        download_records=records,
    )


def test_verification_counts_denominator_excludes_unverified():
    # Only ok + mismatch. An `unavailable` download was never checked against
    # anything and a NULL was never checkable at all -- counting either as a
    # pass would report an unverified run as a verified one.
    counts = verification_counts(
        [
            op(verify_status="ok"),
            op(verify_status="ok"),
            op(verify_status="mismatch"),
            op(verify_status="unavailable"),
            op(),
            op(status="failure"),
        ]
    )
    assert counts["ok"] == 2
    assert counts["mismatch"] == 1
    assert counts["unavailable"] == 1
    assert counts["verified"] == 3
    # Coverage is over SUCCESSFUL downloads: five succeeded, three were checked.
    assert counts["successful_downloads"] == 5
    assert counts["coverage"] == pytest.approx(3 / 5)


def test_verification_coverage_of_a_run_with_no_downloads_is_none():
    assert verification_counts([])["coverage"] is None


def test_data_integrity_passes_on_clean_verifications(config):
    verdict = compare_runs(
        _run_with_verifications(1, "STG-01", ["ok"] * 10),
        _run_with_verifications(2, "STG-00", ["ok"] * 10),
        config,
    )
    gate = _integrity_gate(verdict)
    assert gate["kind"] == "hard"
    assert gate["verdict"] == PASS
    assert gate["candidate"] == 0
    assert "10 ok / 0 mismatch / 0 unavailable" in gate["detail"]


def test_a_single_mismatch_hard_fails(config):
    # Zero tolerance, and hard: there is no fraction of corrupt content a
    # release may ship.
    verdict = compare_runs(
        _run_with_verifications(1, "STG-01", ["ok"] * 99 + ["mismatch"]),
        _run_with_verifications(2, "STG-00", ["ok"] * 100),
        config,
    )
    assert _integrity_gate(verdict)["verdict"] == FAIL
    assert verdict["overall"] == "HARD FAIL"


def test_a_mismatch_does_not_touch_the_availability_floors(config):
    # Availability and integrity are separate concerns: a download that
    # returns the wrong bytes did not fail to download. Folding the two would
    # let a run with corrupt content pass on a high enough success rate.
    verdict = compare_runs(
        _run_with_verifications(1, "STG-01", ["ok"] * 99 + ["mismatch"]),
        _run_with_verifications(2, "STG-00", ["ok"] * 100),
        config,
    )
    floor = next(g for g in verdict["gates"] if g["gate"].startswith("Download success floor"))
    assert floor["verdict"] == PASS
    assert floor["candidate"] == 100.0


def test_no_verified_downloads_is_not_assessed_never_a_pass(config):
    # A run whose uploaders predate content verification publishes no hashes,
    # so every download reads `unavailable`. That is an absence of evidence,
    # and must never read as evidence of absence.
    verdict = compare_runs(
        _run_with_verifications(1, "STG-01", ["unavailable"] * 10),
        _run_with_verifications(2, "STG-00", ["unavailable"] * 10),
        config,
    )
    gate = _integrity_gate(verdict)
    assert gate["verdict"] == NOT_ASSESSED
    assert "no verified downloads recorded" in gate["detail"]
    assert verdict["overall"] == PASS


def test_a_run_with_no_downloads_at_all_is_not_assessed(config):
    verdict = compare_runs(
        _run_with_verifications(1, "STG-01", []),
        _run_with_verifications(2, "STG-00", []),
        config,
    )
    assert _integrity_gate(verdict)["verdict"] == NOT_ASSESSED


def test_failed_downloads_are_excluded_from_coverage_but_not_hidden(config):
    verdict = compare_runs(
        _run_with_verifications(1, "STG-01", ["ok"] * 8, failures=2),
        _run_with_verifications(2, "STG-00", ["ok"] * 8, failures=2),
        config,
    )
    # Eight successful downloads, all eight verified: the two failures had no
    # bytes to check and are not in the denominator.
    assert "coverage 100.0% of 8 successful download(s)" in _integrity_gate(verdict)["detail"]


def test_data_integrity_survives_new_baseline(config):
    # Candidate-only, like the success floors: a mismatch is wrong in absolute
    # terms, so there is nothing a baseline would contribute.
    verdict = compare_runs(
        _run_with_verifications(1, "STG-01", ["ok"] * 9 + ["mismatch"]),
        None,
        config,
        new_baseline=True,
    )
    gate = _integrity_gate(verdict)
    assert gate["verdict"] == FAIL
    assert gate["single_run"] is True
    assert verdict["overall"] == "HARD FAIL"


def test_the_integrity_gate_is_rendered_in_the_hard_gate_table(config):
    report = render_report(
        compare_runs(
            _run_with_verifications(1, "STG-01", ["ok"] * 9 + ["mismatch"]),
            _run_with_verifications(2, "STG-00", ["ok"] * 10),
            config,
        )
    )
    assert DATA_INTEGRITY_GATE in report
    # A count, so no percent suffix: "> 0 %" would read as a percentage of
    # something.
    assert "> 0 |" in report
