"""Normalised per-GB metrics (V2-758).

The properties under test are the ones that make a value comparable against a
baseline: the numerator is an exact byte total rather than an integrated
average, the denominator counts only successful ops that finished inside the
window, and a metric that cannot be measured is omitted rather than
fabricated.
"""
from datetime import datetime, timezone

from saorsa_db.models.derived_metric import (
    METRIC_DISK_BYTES_PER_GB_STORED,
    METRIC_GB_DOWNLOADED,
    METRIC_GB_UPLOADED,
    METRIC_NODE_OUTBOUND_BYTES_TOTAL,
    METRIC_NODE_STORAGE_GROWTH_BYTES,
    METRIC_OUTBOUND_BYTES_PER_GB_TRAFFIC,
    METRIC_OUTBOUND_BYTES_PER_GB_UPLOADED,
    SOURCE_BUCKETS,
    SOURCE_CAPTURE,
    WINDOW_FULL_RUN,
)

from saorsa_deploy.derived_metrics import (
    BYTES_PER_GB,
    build_metric_rows,
    compute_from_points,
    sum_net_counter_deltas,
    sum_node_storage_growth,
    sum_node_storage_growth_from_endpoints,
    sum_outbound_from_buckets,
    sum_traffic_gb,
)
from saorsa_deploy.influx import NODE_STORAGE_ROLE

UTC = timezone.utc
NODE_MOUNT = "/mnt/ant-node-storage"


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _net(host: str, ts: str, interface: str, field: str, value: int) -> dict:
    return {
        "_time": _ts(ts),
        "host": host,
        "interface": interface,
        "_field": field,
        "_value": str(value),
    }


def _disk(host: str, ts: str, used: int, role: str | None = NODE_STORAGE_ROLE) -> dict:
    point = {
        "_time": _ts(ts),
        "host": host,
        "path": NODE_MOUNT,
        "_field": "used",
        "_value": str(used),
    }
    if role is not None:
        point["volume_role"] = role
    return point


class _Record:
    """Stand-in for a DeploymentUploadRecord / DeploymentDownloadRecord row."""

    def __init__(self, size, finished, status="success"):
        self.file_size_bytes = size
        self.finished_at = _ts(finished) if isinstance(finished, str) else finished
        self.status = status


# ---------------------------------------------------------------------------
# Numerator: outbound bytes
# ---------------------------------------------------------------------------


def test_outbound_sums_counter_deltas():
    points = [
        _net("h1", "2026-07-01T00:00:00", "eth0", "bytes_sent", 1_000),
        _net("h1", "2026-07-01T00:00:10", "eth0", "bytes_sent", 3_000),
        _net("h1", "2026-07-01T00:00:20", "eth0", "bytes_sent", 6_000),
    ]
    assert sum_net_counter_deltas(points, "bytes_sent")["total"] == 5_000


def test_outbound_sums_a_hosts_interfaces_rather_than_averaging_them():
    # This is why the raw deltas are summed instead of integrating the stored
    # rate stats: those pool interfaces by AVERAGING, so a two-NIC host's total
    # would come out halved.
    points = [
        _net("h1", "2026-07-01T00:00:00", "eth0", "bytes_sent", 0),
        _net("h1", "2026-07-01T00:00:10", "eth0", "bytes_sent", 1_000),
        _net("h1", "2026-07-01T00:00:00", "eth1", "bytes_sent", 0),
        _net("h1", "2026-07-01T00:00:10", "eth1", "bytes_sent", 3_000),
    ]
    assert sum_net_counter_deltas(points, "bytes_sent")["total"] == 4_000


def test_outbound_ignores_loopback():
    points = [
        _net("h1", "2026-07-01T00:00:00", "lo", "bytes_sent", 0),
        _net("h1", "2026-07-01T00:00:10", "lo", "bytes_sent", 9_999),
    ]
    assert sum_net_counter_deltas(points, "bytes_sent")["total"] == 0


def test_outbound_drops_counter_resets():
    # A VM reboot restarts the counter; the negative step is not traffic.
    points = [
        _net("h1", "2026-07-01T00:00:00", "eth0", "bytes_sent", 8_000),
        _net("h1", "2026-07-01T00:00:10", "eth0", "bytes_sent", 100),
        _net("h1", "2026-07-01T00:00:20", "eth0", "bytes_sent", 400),
    ]
    assert sum_net_counter_deltas(points, "bytes_sent")["total"] == 300


def test_outbound_is_attributed_per_host():
    points = [
        _net("h1", "2026-07-01T00:00:00", "eth0", "bytes_sent", 0),
        _net("h1", "2026-07-01T00:00:10", "eth0", "bytes_sent", 1_000),
        _net("h2", "2026-07-01T00:00:00", "eth0", "bytes_sent", 0),
        _net("h2", "2026-07-01T00:00:10", "eth0", "bytes_sent", 2_000),
    ]
    result = sum_net_counter_deltas(points, "bytes_sent")
    assert result["by_host"] == {"h1": 1_000, "h2": 2_000}
    assert result["hosts"] == 2


def test_outbound_window_counts_a_delta_at_the_end_of_its_interval():
    # The interval 00:00:00->00:00:10 is attributed to 00:00:10, matching where
    # normalise_host_metric_points timestamps a rate sample.
    points = [
        _net("h1", "2026-07-01T00:00:00", "eth0", "bytes_sent", 0),
        _net("h1", "2026-07-01T00:00:10", "eth0", "bytes_sent", 1_000),
        _net("h1", "2026-07-01T00:00:20", "eth0", "bytes_sent", 5_000),
    ]
    windowed = sum_net_counter_deltas(
        points, "bytes_sent",
        start=_ts("2026-07-01T00:00:15"),
        end=_ts("2026-07-01T00:00:25"),
    )
    assert windowed["total"] == 4_000


def test_outbound_from_buckets_integrates_the_average():
    rows = [
        {"host": "h1", "metric": "bytes_out_per_sec", "avg": 10.0},
        {"host": "h1", "metric": "bytes_out_per_sec", "avg": 20.0},
        {"host": "h1", "metric": "bytes_in_per_sec", "avg": 99.0},
    ]
    result = sum_outbound_from_buckets(rows, 300, "bytes_out_per_sec")
    assert result["total"] == 9_000


# ---------------------------------------------------------------------------
# Numerator: storage growth
# ---------------------------------------------------------------------------


def test_growth_is_summed_across_node_hosts():
    points = [
        _disk("h1", "2026-07-01T00:00:00", 1_000),
        _disk("h1", "2026-07-01T01:00:00", 4_000),
        _disk("h2", "2026-07-01T00:00:00", 500),
        _disk("h2", "2026-07-01T01:00:00", 2_500),
    ]
    result = sum_node_storage_growth(points)
    assert result["total"] == 5_000
    assert result["hosts"] == 2


def test_growth_ignores_mountpoints_not_tagged_node_storage():
    # The root filesystem's usage is dominated by the OS, not by stored chunks.
    points = [
        _disk("h1", "2026-07-01T00:00:00", 1_000, role=None),
        _disk("h1", "2026-07-01T01:00:00", 9_000, role=None),
    ]
    result = sum_node_storage_growth(points)
    assert result["total"] == 0
    assert result["hosts"] == 0
    assert result["hosts_without_series"] == 1


def test_growth_reports_the_peak_separately_from_the_net_figure():
    # A run that filled the volume and was then pruned has not consumed the
    # peak, but the peak is still worth surfacing.
    points = [
        _disk("h1", "2026-07-01T00:00:00", 1_000),
        _disk("h1", "2026-07-01T00:30:00", 9_000),
        _disk("h1", "2026-07-01T01:00:00", 2_000),
    ]
    result = sum_node_storage_growth(points)
    assert result["total"] == 1_000
    assert result["peak_growth"] == 8_000


def test_growth_from_bucket_endpoints():
    endpoints = [
        {
            "host": "h1",
            "device": NODE_MOUNT,
            "volume_role": NODE_STORAGE_ROLE,
            "service": None,
            "first": 1_000.0,
            "last": 4_000.0,
        },
        {
            "host": "h2",
            "device": "/",
            "volume_role": None,
            "service": None,
            "first": 1.0,
            "last": 9_999.0,
        },
    ]
    result = sum_node_storage_growth_from_endpoints(endpoints)
    assert result["total"] == 3_000
    assert result["hosts"] == 1
    # An aggregate over buckets cannot tell a pruned run from a monotonic one.
    assert result["peak_growth"] is None


# ---------------------------------------------------------------------------
# Denominator: user traffic
# ---------------------------------------------------------------------------


def test_traffic_counts_successes_only():
    records = [
        _Record(BYTES_PER_GB, "2026-07-01T00:30:00"),
        _Record(BYTES_PER_GB, "2026-07-01T00:40:00", status="failure"),
    ]
    result = sum_traffic_gb(records)
    assert result["gb"] == 1.0
    assert result["ops"] == 1
    assert result["failed_ops"] == 1
    assert result["failed_bytes"] == BYTES_PER_GB


def test_traffic_accepts_an_enum_status():
    class _Status:
        value = "success"

    result = sum_traffic_gb([_Record(BYTES_PER_GB, "2026-07-01T00:30:00", _Status())])
    assert result["gb"] == 1.0


def test_traffic_window_is_half_open_on_finished_at():
    records = [
        _Record(BYTES_PER_GB, "2026-07-01T00:00:00"),
        _Record(BYTES_PER_GB, "2026-07-01T01:00:00"),
        _Record(BYTES_PER_GB, "2026-07-01T02:00:00"),
    ]
    result = sum_traffic_gb(
        records,
        start=_ts("2026-07-01T01:00:00"),
        end=_ts("2026-07-01T02:00:00"),
    )
    assert result["ops"] == 1


def test_traffic_skips_ops_that_never_finished():
    assert sum_traffic_gb([_Record(BYTES_PER_GB, None)])["ops"] == 0


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _rows(**overrides):
    args = {
        "outbound": {"total": 10 * BYTES_PER_GB, "by_host": {"h1": 10 * BYTES_PER_GB}, "hosts": 1},
        "inbound": {"total": BYTES_PER_GB, "by_host": {}, "hosts": 1},
        "storage_growth": {
            "total": 7 * BYTES_PER_GB,
            "by_host": {"h1": 7 * BYTES_PER_GB},
            "hosts": 1,
            "hosts_without_series": 0,
            "peak_growth": 7 * BYTES_PER_GB,
        },
        "uploaded": {
            "bytes": 2 * BYTES_PER_GB, "gb": 2.0, "ops": 2,
            "failed_bytes": 0, "failed_ops": 0,
        },
        "downloaded": {
            "bytes": 3 * BYTES_PER_GB, "gb": 3.0, "ops": 3,
            "failed_bytes": 0, "failed_ops": 0,
        },
        "window_start": _ts("2026-07-01T00:00:00"),
        "window_end": _ts("2026-07-01T04:00:00"),
        "window_label": WINDOW_FULL_RUN,
        "source": SOURCE_CAPTURE,
    }
    args.update(overrides)
    return {row["metric"]: row for row in build_metric_rows(**args)}


def test_ratios_divide_the_numerator_by_gb():
    rows = _rows()
    assert rows[METRIC_OUTBOUND_BYTES_PER_GB_UPLOADED]["value"] == 5 * BYTES_PER_GB
    assert rows[METRIC_OUTBOUND_BYTES_PER_GB_TRAFFIC]["value"] == 2 * BYTES_PER_GB
    assert rows[METRIC_DISK_BYTES_PER_GB_STORED]["value"] == 3.5 * BYTES_PER_GB


def test_ratios_keep_their_numerator_and_denominator():
    # A ratio alone cannot be sanity-checked, and a later gate may want to
    # renormalise once the source InfluxDB is long gone.
    row = _rows()[METRIC_OUTBOUND_BYTES_PER_GB_UPLOADED]
    assert row["numerator"] == 10 * BYTES_PER_GB
    assert row["denominator"] == 2.0


def test_components_are_persisted_in_their_own_right():
    rows = _rows()
    assert rows[METRIC_NODE_OUTBOUND_BYTES_TOTAL]["value"] == 10 * BYTES_PER_GB
    assert rows[METRIC_GB_UPLOADED]["value"] == 2.0
    assert rows[METRIC_GB_DOWNLOADED]["value"] == 3.0
    assert rows[METRIC_NODE_STORAGE_GROWTH_BYTES]["value"] == 7 * BYTES_PER_GB


def test_a_run_that_uploaded_nothing_omits_the_ratios():
    # Publishing 0 (or dividing by zero) would put a fabricated value into a
    # baseline comparison. Absent means "not measured".
    rows = _rows(
        uploaded={
            "bytes": 0, "gb": 0.0, "ops": 0, "failed_bytes": 0, "failed_ops": 0,
        },
        downloaded={
            "bytes": 0, "gb": 0.0, "ops": 0, "failed_bytes": 0, "failed_ops": 0,
        },
    )
    assert METRIC_OUTBOUND_BYTES_PER_GB_UPLOADED not in rows
    assert METRIC_OUTBOUND_BYTES_PER_GB_TRAFFIC not in rows
    assert METRIC_DISK_BYTES_PER_GB_STORED not in rows
    # The components are still recorded: knowing the run moved bytes and stored
    # nothing is itself the finding.
    assert rows[METRIC_NODE_OUTBOUND_BYTES_TOTAL]["value"] == 10 * BYTES_PER_GB


def test_downloads_alone_still_produce_the_traffic_ratio():
    rows = _rows(
        uploaded={
            "bytes": 0, "gb": 0.0, "ops": 0, "failed_bytes": 0, "failed_ops": 0,
        }
    )
    assert METRIC_OUTBOUND_BYTES_PER_GB_UPLOADED not in rows
    assert rows[METRIC_OUTBOUND_BYTES_PER_GB_TRAFFIC]["denominator"] == 3.0


def test_no_node_storage_series_omits_the_disk_metrics():
    # Every run recorded before occupancy capture existed, and every client-only
    # deployment. Reporting a growth of zero would read as "nothing was stored".
    rows = _rows(
        storage_growth={
            "total": 0, "by_host": {}, "hosts": 0,
            "hosts_without_series": 4, "peak_growth": 0,
        }
    )
    assert METRIC_NODE_STORAGE_GROWTH_BYTES not in rows
    assert METRIC_DISK_BYTES_PER_GB_STORED not in rows


def test_every_row_carries_the_window_and_its_source():
    for row in _rows(source=SOURCE_BUCKETS).values():
        assert row["window_label"] == WINDOW_FULL_RUN
        assert row["window_start"] == _ts("2026-07-01T00:00:00")
        assert row["window_end"] == _ts("2026-07-01T04:00:00")
        # An exact whole-run capture and an approximate bucket re-derivation
        # must never be mistaken for one another.
        assert row["source"] == SOURCE_BUCKETS


# ---------------------------------------------------------------------------
# End to end over raw points
# ---------------------------------------------------------------------------


def test_compute_from_points_produces_the_expected_ratio():
    points = {
        "net": [
            _net("h1", "2026-07-01T00:00:00", "eth0", "bytes_sent", 0),
            _net("h1", "2026-07-01T01:00:00", "eth0", "bytes_sent", 4 * BYTES_PER_GB),
        ],
        "disk": [
            _disk("h1", "2026-07-01T00:00:00", 0),
            _disk("h1", "2026-07-01T01:00:00", 2 * BYTES_PER_GB),
        ],
    }
    rows = {
        row["metric"]: row
        for row in compute_from_points(
            points_by_measurement=points,
            upload_records=[_Record(BYTES_PER_GB, "2026-07-01T00:30:00")],
            download_records=[],
            window_start=_ts("2026-07-01T00:00:00"),
            window_end=_ts("2026-07-01T01:00:00"),
            window_label=WINDOW_FULL_RUN,
            source=SOURCE_CAPTURE,
        )
    }
    # 4 GB out for 1 GB uploaded, 2 GB of disk for 1 GB stored.
    assert rows[METRIC_OUTBOUND_BYTES_PER_GB_UPLOADED]["value"] == 4 * BYTES_PER_GB
    assert rows[METRIC_DISK_BYTES_PER_GB_STORED]["value"] == 2 * BYTES_PER_GB


class _FakeRepo:
    def __init__(self, uploads=(), downloads=()):
        self.uploads = list(uploads)
        self.downloads = list(downloads)
        self.derived = []
        self.host_result = None
        self.buckets = None

    def get_active_by_name(self, name):
        return type("D", (), {"id": 1, "name": name})()

    def get_by_name(self, name):
        return self.get_active_by_name(name)

    def upsert_host_result(self, **kwargs):
        self.host_result = kwargs

    def replace_host_metric_buckets(self, deployment_id, buckets):
        self.buckets = buckets
        return len(buckets)

    def list_upload_records_for_deployment(self, deployment_id):
        return self.uploads

    def list_download_records_for_deployment(self, deployment_id):
        return self.downloads

    def upsert_derived_metric(self, **kwargs):
        self.derived.append(kwargs)


def _cmd_args(**overrides):
    from types import SimpleNamespace

    base = dict(
        name="DEV-03",
        monitoring_name=None,
        influx_url="http://x",
        influx_token="t",
        influx_org="ant",
        influx_bucket="testnet-metrics",
        window="0",
        bucket_secs=300,
        summary=None,
        derived=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _run_record_analysis(monkeypatch, repo, args=None):
    import saorsa_deploy.cmd.record_analysis as mod

    points = {
        "cpu": [
            {
                "_time": _ts("2026-07-01T00:00:00"),
                "host": "h1",
                "cpu": "cpu-total",
                "_field": "usage_idle",
                "_value": "90",
            },
            {
                "_time": _ts("2026-07-01T01:00:00"),
                "host": "h1",
                "cpu": "cpu-total",
                "_field": "usage_idle",
                "_value": "80",
            },
        ],
        "net": [
            _net("h1", "2026-07-01T00:00:00", "eth0", "bytes_sent", 0),
            _net("h1", "2026-07-01T01:00:00", "eth0", "bytes_sent", 4 * BYTES_PER_GB),
        ],
        "disk": [
            _disk("h1", "2026-07-01T00:00:00", 0),
            _disk("h1", "2026-07-01T01:00:00", 2 * BYTES_PER_GB),
        ],
    }
    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.setattr(mod, "get_repository", lambda: repo)
    monkeypatch.setattr(mod, "iter_host_metric_points", lambda **kwargs: points)
    mod.cmd_record_analysis(args or _cmd_args())


def test_record_analysis_persists_derived_metrics(monkeypatch):
    repo = _FakeRepo(uploads=[_Record(BYTES_PER_GB, "2026-07-01T00:30:00")])
    _run_record_analysis(monkeypatch, repo)
    stored = {row["metric"]: row for row in repo.derived}
    assert stored[METRIC_OUTBOUND_BYTES_PER_GB_UPLOADED]["value"] == 4 * BYTES_PER_GB
    assert stored[METRIC_DISK_BYTES_PER_GB_STORED]["value"] == 2 * BYTES_PER_GB
    assert stored[METRIC_GB_UPLOADED]["value"] == 1.0


def test_record_analysis_stamps_the_window_from_the_metric_span(monkeypatch):
    repo = _FakeRepo(uploads=[_Record(BYTES_PER_GB, "2026-07-01T00:30:00")])
    _run_record_analysis(monkeypatch, repo)
    row = repo.derived[0]
    assert row["window_label"] == WINDOW_FULL_RUN
    assert row["source"] == SOURCE_CAPTURE
    assert row["window_start"] == _ts("2026-07-01T00:00:00")
    assert row["window_end"] == _ts("2026-07-01T01:00:00")


def test_record_analysis_no_derived_skips_the_computation(monkeypatch):
    repo = _FakeRepo(uploads=[_Record(BYTES_PER_GB, "2026-07-01T00:30:00")])
    _run_record_analysis(monkeypatch, repo, _cmd_args(derived=False))
    assert repo.derived == []
    # The host-metric capture is unaffected.
    assert repo.host_result is not None


def test_record_analysis_without_op_records_still_stores_components(monkeypatch, capsys):
    # record-results has not been run yet. The ratios have no denominator and
    # are omitted, but the components measured this pass are still worth
    # keeping -- and the operator is told what to run.
    repo = _FakeRepo()
    _run_record_analysis(monkeypatch, repo)
    stored = {row["metric"] for row in repo.derived}
    assert METRIC_NODE_OUTBOUND_BYTES_TOTAL in stored
    assert METRIC_OUTBOUND_BYTES_PER_GB_UPLOADED not in stored
    assert "record-results" in capsys.readouterr().out


def test_compute_from_points_applies_the_window_to_both_sides():
    points = {
        "net": [
            _net("h1", "2026-07-01T00:00:00", "eth0", "bytes_sent", 0),
            _net("h1", "2026-07-01T01:00:00", "eth0", "bytes_sent", 4 * BYTES_PER_GB),
            _net("h1", "2026-07-01T02:00:00", "eth0", "bytes_sent", 8 * BYTES_PER_GB),
        ],
        "disk": [],
    }
    rows = {
        row["metric"]: row
        for row in compute_from_points(
            points_by_measurement=points,
            upload_records=[
                _Record(BYTES_PER_GB, "2026-07-01T00:30:00"),
                _Record(BYTES_PER_GB, "2026-07-01T01:30:00"),
            ],
            download_records=[],
            window_start=_ts("2026-07-01T01:00:00"),
            window_end=_ts("2026-07-01T02:00:00"),
            window_label="steady-state",
            source=SOURCE_CAPTURE,
            start=_ts("2026-07-01T01:00:01"),
            end=_ts("2026-07-01T02:00:01"),
        )
    }
    # Only the second hour's 4 GB and the second upload count.
    assert rows[METRIC_NODE_OUTBOUND_BYTES_TOTAL]["value"] == 4 * BYTES_PER_GB
    assert rows[METRIC_GB_UPLOADED]["value"] == 1.0
