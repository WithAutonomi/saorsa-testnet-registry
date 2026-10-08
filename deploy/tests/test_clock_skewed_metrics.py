"""Clock-skewed hosts must not vanish from Influx-backed capture.

`provisioning/clock_skew.py` jumps a VM's *system* clock, so a forward-skewed
host's Telegraf writes every point hours ahead of real time. Flux defaults a
range's `stop` to `now()`, which silently discarded exactly those points — a
host could hold hundreds of samples in the bucket and contribute nothing.

Found on DEV-03 (2026-07-25): a +3h node VM contributed 0 of 27 hosts to
`record-analysis` while holding 460 `memory_rss` points.
"""

from datetime import datetime, timedelta, timezone

import pytest

from saorsa_deploy import influx, influx_canary
from saorsa_deploy.influx import (
    FLUX_FAR_FUTURE,
    aggregate_host_metrics,
    detect_clock_offsets,
    fetch_host_metric_points,
    fetch_operation_points,
    fetch_prod_upload_points,
)

UTC = timezone.utc


def _capture(monkeypatch, module=influx, attr="_stream_flux"):
    """Capture the Flux text each host-metric query sends.

    Patches `_stream_flux`, not `_run_flux`: the host-metric path streams its
    response line by line so a multi-GB CSV is never materialised, so the fake
    returns an empty iterable of lines rather than an empty body string.
    """
    seen = []

    def _fake(url, token, org, flux, timeout=30):
        seen.append(flux)
        return iter(())

    monkeypatch.setattr(module, attr, _fake)
    return seen


# ---------------------------------------------------------------------------
# Every query path carries an explicit stop
# ---------------------------------------------------------------------------


def test_host_metric_queries_all_bound_the_future(monkeypatch):
    seen = _capture(monkeypatch)
    fetch_host_metric_points(url="http://x", token="t", org="ant", bucket="b")
    # cpu, mem, net, disk, diskio, procstat, ant_node_memory. The count is
    # asserted so a query added later cannot quietly skip the explicit stop
    # bound.
    assert len(seen) == 7
    for flux in seen:
        assert f"stop: {FLUX_FAR_FUTURE}" in flux


def test_file_op_query_bounds_the_future(monkeypatch):
    """Testnet manifests skew *client* clocks explicitly, so this path matters too."""
    seen = []

    class _Resp:
        text = ""

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=None, decode_unicode=False):
            return iter(())

    monkeypatch.setattr(
        influx.requests,
        "post",
        lambda *a, **k: (seen.append(k.get("data")), _Resp())[1],
    )
    fetch_operation_points(url="http://x", token="t", org="ant", bucket="b")
    assert f"stop: {FLUX_FAR_FUTURE}" in seen[0]


def test_prod_upload_query_bounds_the_future(monkeypatch):
    seen = []

    class _Resp:
        text = ""

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=None, decode_unicode=False):
            return iter(())

    monkeypatch.setattr(
        influx.requests,
        "post",
        lambda *a, **k: (seen.append(k.get("data")), _Resp())[1],
    )
    fetch_prod_upload_points(url="http://x", token="t", org="ant", bucket="b")
    assert f"stop: {FLUX_FAR_FUTURE}" in seen[0]


def test_canary_queries_bound_the_future(monkeypatch):
    seen = _capture(monkeypatch, module=influx_canary, attr="_query")
    for fn in (
        influx_canary.fetch_canary_upload_points,
        influx_canary.fetch_canary_chunk_points,
        influx_canary.fetch_canary_probe_points,
    ):
        fn(url="http://x", token="t", org="ant", bucket="b", deployment_name="D")
    assert len(seen) == 3
    for flux in seen:
        assert f"stop: {FLUX_FAR_FUTURE}" in flux


def test_no_bare_range_survives_in_the_query_modules():
    """A regression guard: a bare `range(start: …)` is the bug's whole shape."""
    for module_path in (
        "deploy/saorsa_deploy/influx.py",
        "deploy/saorsa_deploy/influx_canary.py",
    ):
        with open(module_path) as handle:
            body = handle.read()
        assert "range(start: {start})" not in body, module_path


# ---------------------------------------------------------------------------
# A forward-skewed host survives aggregation
# ---------------------------------------------------------------------------


def _cpu(host: str, ts: datetime, idle: float) -> dict:
    return {
        "_time": ts,
        "host": host,
        "_measurement": "cpu",
        "cpu": "cpu-total",
        "_field": "usage_idle",
        "_value": str(idle),
    }


def _rss(host: str, ts: datetime, service: str, value: int) -> dict:
    return {
        "_time": ts,
        "host": host,
        "_measurement": "procstat",
        "_field": "memory_rss",
        "node_service": service,
        "_value": str(value),
    }


def test_a_forward_skewed_host_is_aggregated_like_any_other():
    """The DEV-03 failure, reproduced: +3h host alongside normal ones."""
    base = datetime(2026, 7, 25, 6, 0, tzinfo=UTC)
    skew = timedelta(hours=3)
    points = {
        "cpu": [
            _cpu("normal", base, 90.0),
            _cpu("normal", base + timedelta(seconds=10), 80.0),
            _cpu("skewed", base + skew, 70.0),
            _cpu("skewed", base + skew + timedelta(seconds=10), 60.0),
        ],
        "procstat": [
            _rss("skewed", base + skew, "ant-node-1", 1_000),
        ],
    }
    hosts = {a["host"]: a for a in aggregate_host_metrics(points)}
    assert set(hosts) == {"normal", "skewed"}
    assert hosts["skewed"]["cpu_percent"]["samples"] == 2
    assert hosts["skewed"]["rss_by_service"]["ant-node-1"]["samples"] == 1
    # Its timestamps stay displaced -- that is the truth about the host, and is
    # what the clock-offset warning exists to surface.
    assert hosts["skewed"]["metrics_started_at"] > hosts["normal"]["metrics_ended_at"]


# ---------------------------------------------------------------------------
# Offset detection
# ---------------------------------------------------------------------------


def _agg(host: str, ended: datetime) -> dict:
    return {"host": host, "metrics_started_at": ended, "metrics_ended_at": ended}


def test_detect_clock_offsets_flags_a_forward_skewed_host():
    base = datetime(2026, 7, 25, 6, 0, tzinfo=UTC)
    aggregates = [
        _agg("a", base),
        _agg("b", base + timedelta(seconds=10)),
        _agg("c", base + timedelta(seconds=20)),
        _agg("skewed", base + timedelta(hours=3)),
    ]
    offsets = detect_clock_offsets(aggregates)
    assert len(offsets) == 1
    host, delta = offsets[0]
    assert host == "skewed"
    assert delta == pytest.approx(3 * 3600, abs=30)


def test_detect_clock_offsets_flags_a_host_that_stopped_early():
    base = datetime(2026, 7, 25, 6, 0, tzinfo=UTC)
    aggregates = [
        _agg("a", base),
        _agg("b", base),
        _agg("c", base),
        _agg("stopped", base - timedelta(hours=2)),
    ]
    offsets = detect_clock_offsets(aggregates)
    assert [h for h, _ in offsets] == ["stopped"]
    assert offsets[0][1] < 0


def test_detect_clock_offsets_ignores_ordinary_scrape_jitter():
    base = datetime(2026, 7, 25, 6, 0, tzinfo=UTC)
    aggregates = [_agg(f"h{i}", base + timedelta(seconds=10 * i)) for i in range(6)]
    assert detect_clock_offsets(aggregates) == []


def test_detect_clock_offsets_uses_the_median_not_the_extremes():
    """A majority of skewed hosts must not make the healthy ones look wrong."""
    base = datetime(2026, 7, 25, 6, 0, tzinfo=UTC)
    aggregates = [
        _agg("skew1", base + timedelta(hours=3)),
        _agg("skew2", base + timedelta(hours=3)),
        _agg("skew3", base + timedelta(hours=3)),
        _agg("normal", base),
    ]
    offsets = detect_clock_offsets(aggregates)
    assert [h for h, _ in offsets] == ["normal"]


def test_detect_clock_offsets_needs_at_least_two_hosts():
    base = datetime(2026, 7, 25, 6, 0, tzinfo=UTC)
    assert detect_clock_offsets([_agg("only", base)]) == []
    assert detect_clock_offsets([]) == []
