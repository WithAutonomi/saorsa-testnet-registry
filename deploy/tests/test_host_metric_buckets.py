"""Bucketed host metrics (V2-753) and per-service node RSS (V2-754).

The load-bearing property under test is that a window re-derived from buckets
reproduces what the whole-run rollup would have said, so a run can be sliced
after its InfluxDB is destroyed.
"""

from datetime import datetime, timedelta, timezone

import pytest
from saorsa_db.db import Base
from saorsa_db.models.host_metric_bucket import DeploymentHostMetricBucket
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import saorsa_deploy.cmd.record_analysis as record_analysis_mod
from saorsa_deploy.influx import (
    NODE_STORAGE_ROLE,
    aggregate_host_metrics,
    bucket_host_metric_samples,
    normalise_host_metric_points,
    peak_rss_service,
)

UTC = timezone.utc


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _cpu_point(host: str, ts: str, idle: float) -> dict:
    return {
        "_time": _ts(ts),
        "host": host,
        "_measurement": "cpu",
        "cpu": "cpu-total",
        "_field": "usage_idle",
        "_value": str(idle),
    }


def _mem_point(host: str, ts: str, used_percent: float) -> dict:
    return {
        "_time": _ts(ts),
        "host": host,
        "_measurement": "mem",
        "_field": "used_percent",
        "_value": str(used_percent),
    }


def _net_point(host: str, ts: str, interface: str, field: str, value: int) -> dict:
    return {
        "_time": _ts(ts),
        "host": host,
        "_measurement": "net",
        "interface": interface,
        "_field": field,
        "_value": str(value),
    }


def _diskio_point(
    host: str,
    ts: str,
    device: str,
    field: str,
    value: int,
    volume_role: str | None = None,
) -> dict:
    point = {
        "_time": _ts(ts),
        "host": host,
        "_measurement": "diskio",
        "name": device,
        "_field": field,
        "_value": str(value),
    }
    if volume_role is not None:
        point["volume_role"] = volume_role
    return point


def _procstat_point(host: str, ts: str, service: str, rss: int) -> dict:
    return {
        "_time": _ts(ts),
        "host": host,
        "_measurement": "procstat",
        "_field": "memory_rss",
        "node_service": service,
        "_value": str(rss),
    }


# ---------------------------------------------------------------------------
# Normalise stage: the delta must be computed before any windowing
# ---------------------------------------------------------------------------


def test_normalise_emits_gauges_unchanged():
    normalised = normalise_host_metric_points(
        {"cpu": [_cpu_point("h1", "2026-07-01T00:00:00", 90.0)]}
    )
    assert [(s["metric"], s["value"]) for s in normalised.samples] == [("cpu_percent", 10.0)]


def test_normalise_timestamps_a_rate_at_the_end_of_its_interval():
    """A rate describes the interval that just closed, not the one about to open.

    Dating it to the interval start would place the sample before the data that
    produced it existed — and would put it in the wrong bucket at a boundary.
    """
    normalised = normalise_host_metric_points(
        {
            "net": [
                _net_point("h1", "2026-07-01T00:00:00", "eth0", "bytes_recv", 0),
                _net_point("h1", "2026-07-01T00:00:10", "eth0", "bytes_recv", 100),
            ]
        }
    )
    assert len(normalised.samples) == 1
    assert normalised.samples[0]["ts"] == _ts("2026-07-01T00:00:10")
    assert normalised.samples[0]["value"] == 10.0


def test_normalise_drops_counter_resets_across_the_split():
    """A reboot resets the counters; the negative delta must not survive."""
    normalised = normalise_host_metric_points(
        {
            "diskio": [
                _diskio_point("h1", "2026-07-01T00:00:00", "sda", "read_bytes", 5000),
                _diskio_point("h1", "2026-07-01T00:00:10", "sda", "read_bytes", 10),
                _diskio_point("h1", "2026-07-01T00:00:20", "sda", "read_bytes", 110),
            ]
        }
    )
    values = [s["value"] for s in normalised.samples]
    assert values == [10.0]


def test_normalise_registers_a_device_that_produced_no_rate():
    """One point yields no delta, but the device exists and must be reported."""
    normalised = normalise_host_metric_points(
        {"diskio": [_diskio_point("h1", "2026-07-01T00:00:00", "sdb", "read_bytes", 1)]}
    )
    assert normalised.samples == []
    assert normalised.devices == {"h1": {"sdb": None}}


def test_normalise_keeps_the_node_storage_role_on_each_sample():
    normalised = normalise_host_metric_points(
        {
            "diskio": [
                _diskio_point(
                    "h1",
                    "2026-07-01T00:00:00",
                    "sda",
                    "read_bytes",
                    0,
                    volume_role=NODE_STORAGE_ROLE,
                ),
                _diskio_point(
                    "h1",
                    "2026-07-01T00:00:10",
                    "sda",
                    "read_bytes",
                    100,
                    volume_role=NODE_STORAGE_ROLE,
                ),
            ]
        }
    )
    assert normalised.samples[0]["volume_role"] == NODE_STORAGE_ROLE
    assert normalised.samples[0]["device"] == "sda"


# ---------------------------------------------------------------------------
# Bucketing stage
# ---------------------------------------------------------------------------


def test_buckets_floor_to_the_bucket_boundary():
    points = {
        "cpu": [
            _cpu_point("h1", "2026-07-01T00:00:05", 90.0),
            _cpu_point("h1", "2026-07-01T00:04:55", 80.0),
            _cpu_point("h1", "2026-07-01T00:05:05", 70.0),
        ]
    }
    rows = bucket_host_metric_samples(normalise_host_metric_points(points), bucket_secs=300)
    assert [(r["bucket_start"], r["samples"]) for r in rows] == [
        (_ts("2026-07-01T00:00:00"), 2),
        (_ts("2026-07-01T00:05:00"), 1),
    ]


def test_buckets_carry_their_own_min_and_max():
    """A spike inside a bucket survives as that bucket's max.

    This is why a wider bucket does not blunt a comparison: extremes compose
    through MIN(min)/MAX(max) regardless of width.
    """
    points = {
        "cpu": [
            _cpu_point("h1", "2026-07-01T00:00:00", 95.0),
            _cpu_point("h1", "2026-07-01T00:01:00", 10.0),
            _cpu_point("h1", "2026-07-01T00:02:00", 95.0),
        ]
    }
    rows = bucket_host_metric_samples(normalise_host_metric_points(points), bucket_secs=300)
    assert len(rows) == 1
    assert rows[0]["min"] == pytest.approx(5.0)
    assert rows[0]["max"] == pytest.approx(90.0)


def test_buckets_restrict_disk_to_the_node_storage_device():
    """Untagged devices are root-disk partitions duplicating the whole disk."""
    points = {
        "diskio": [
            _diskio_point(
                "h1",
                "2026-07-01T00:00:00",
                "sda",
                "read_bytes",
                0,
                volume_role=NODE_STORAGE_ROLE,
            ),
            _diskio_point(
                "h1",
                "2026-07-01T00:00:10",
                "sda",
                "read_bytes",
                100,
                volume_role=NODE_STORAGE_ROLE,
            ),
            _diskio_point("h1", "2026-07-01T00:00:00", "vda1", "read_bytes", 0),
            _diskio_point("h1", "2026-07-01T00:00:10", "vda1", "read_bytes", 100),
        ]
    }
    normalised = normalise_host_metric_points(points)
    rows = bucket_host_metric_samples(normalised, bucket_secs=300)
    assert {r["device"] for r in rows} == {"sda"}

    # The whole-run rollup still keeps every device.
    rollup = aggregate_host_metrics(points)
    assert set(rollup[0]["disk_io_by_device"]) == {"sda", "vda1"}


def test_buckets_can_keep_every_device_when_asked():
    points = {
        "diskio": [
            _diskio_point("h1", "2026-07-01T00:00:00", "vda1", "read_bytes", 0),
            _diskio_point("h1", "2026-07-01T00:00:10", "vda1", "read_bytes", 100),
        ]
    }
    rows = bucket_host_metric_samples(
        normalise_host_metric_points(points),
        bucket_secs=300,
        node_storage_only=False,
    )
    assert {r["device"] for r in rows} == {"vda1"}


def test_buckets_keep_rss_per_service():
    points = {
        "procstat": [
            _procstat_point("h1", "2026-07-01T00:00:00", "ant-node-1", 100),
            _procstat_point("h1", "2026-07-01T00:00:10", "ant-node-1", 200),
            _procstat_point("h1", "2026-07-01T00:00:00", "ant-node-2", 900),
        ]
    }
    rows = bucket_host_metric_samples(normalise_host_metric_points(points), bucket_secs=300)
    by_service = {r["service"]: r for r in rows}
    assert set(by_service) == {"ant-node-1", "ant-node-2"}
    assert by_service["ant-node-1"]["avg"] == pytest.approx(150.0)
    assert by_service["ant-node-2"]["avg"] == pytest.approx(900.0)


def test_buckets_reject_a_non_positive_width():
    with pytest.raises(ValueError):
        bucket_host_metric_samples(normalise_host_metric_points({}), bucket_secs=0)


# ---------------------------------------------------------------------------
# RSS in the whole-run rollup (V2-754)
# ---------------------------------------------------------------------------


def test_aggregate_reports_rss_per_service_without_pooling():
    """A 15-service mean would dilute one leaking service fifteenfold."""
    points = {
        "procstat": [
            _procstat_point("h1", "2026-07-01T00:00:00", "ant-node-1", 1_000),
            _procstat_point("h1", "2026-07-01T00:00:10", "ant-node-1", 3_000),
            _procstat_point("h1", "2026-07-01T00:00:00", "ant-node-2", 500),
        ]
    }
    entry = aggregate_host_metrics(points)[0]
    assert entry["rss_by_service"]["ant-node-1"] == {
        "avg": 2_000.0,
        "min": 1_000.0,
        "max": 3_000.0,
        # Exact whole-run percentiles, captured because the release gates name
        # node RSS p50 and avg/min/max cannot yield it.
        "p50": 2_000.0,
        "p95": 2_900.0,
        "samples": 2,
    }
    assert entry["rss_by_service"]["ant-node-2"]["avg"] == 500.0


def test_aggregate_ignores_procstat_points_without_a_service_tag():
    points = {
        "procstat": [
            {
                "_time": _ts("2026-07-01T00:00:00"),
                "host": "h1",
                "_measurement": "procstat",
                "_field": "memory_rss",
                "_value": "123",
            }
        ],
        "cpu": [_cpu_point("h1", "2026-07-01T00:00:00", 90.0)],
    }
    entry = aggregate_host_metrics(points)[0]
    assert entry["rss_by_service"] == {}


def test_aggregate_absent_rss_yields_an_empty_service_map():
    entry = aggregate_host_metrics({"cpu": [_cpu_point("h1", "2026-07-01T00:00:00", 90.0)]})[0]
    assert entry["rss_by_service"] == {}


def test_peak_rss_service_picks_the_largest_by_max():
    entry = {
        "rss_by_service": {
            "ant-node-1": {"avg": 10.0, "min": 5.0, "max": 20.0, "samples": 2},
            "ant-node-2": {"avg": 15.0, "min": 5.0, "max": 90.0, "samples": 2},
        }
    }
    assert peak_rss_service(entry)[0] == "ant-node-2"


def test_peak_rss_service_is_none_for_a_host_with_no_services():
    assert peak_rss_service({"rss_by_service": {}}) is None
    assert peak_rss_service({}) is None


# ---------------------------------------------------------------------------
# Repository: storage and windowed re-derivation
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session), session
    session.close()
    engine.dispose()


def _bucket(
    host: str,
    metric: str,
    start: str,
    avg: float,
    minimum: float,
    maximum: float,
    samples: int,
    **extra,
) -> dict:
    row = {
        "host": host,
        "metric": metric,
        "bucket_start": _ts(start),
        "avg": avg,
        "min": minimum,
        "max": maximum,
        "samples": samples,
        "device": None,
        "volume_role": None,
        "service": None,
    }
    row.update(extra)
    return row


def test_replace_host_metric_buckets_inserts_then_replaces(repo):
    r, session = repo
    deployment = r.create(name="DEV-TEST", deploy_args={})

    written = r.replace_host_metric_buckets(
        deployment.id,
        [_bucket("h1", "cpu_percent", "2026-07-01T00:00:00", 10, 5, 15, 3)],
    )
    assert written == 1
    assert r.count_host_metric_buckets(deployment.id) == 1

    # A re-run re-states the whole stored window rather than appending.
    r.replace_host_metric_buckets(
        deployment.id,
        [
            _bucket("h1", "cpu_percent", "2026-07-01T00:05:00", 20, 20, 20, 1),
            _bucket("h1", "cpu_percent", "2026-07-01T00:10:00", 30, 30, 30, 1),
        ],
    )
    assert r.count_host_metric_buckets(deployment.id) == 2
    remaining = session.query(DeploymentHostMetricBucket).all()
    assert {row.bucket_start.replace(tzinfo=UTC) for row in remaining} == {
        _ts("2026-07-01T00:05:00"),
        _ts("2026-07-01T00:10:00"),
    }


def test_replace_host_metric_buckets_only_touches_its_own_deployment(repo):
    r, _ = repo
    keep = r.create(name="KEEP", deploy_args={})
    replace = r.create(name="REPLACE", deploy_args={})
    r.replace_host_metric_buckets(
        keep.id, [_bucket("h1", "cpu_percent", "2026-07-01T00:00:00", 1, 1, 1, 1)]
    )
    r.replace_host_metric_buckets(
        replace.id, [_bucket("h1", "cpu_percent", "2026-07-01T00:00:00", 2, 2, 2, 1)]
    )
    r.replace_host_metric_buckets(replace.id, [])
    assert r.count_host_metric_buckets(keep.id) == 1
    assert r.count_host_metric_buckets(replace.id) == 0


def test_window_average_is_sample_weighted_not_a_mean_of_means(repo):
    """The correctness requirement buckets exist to satisfy.

    Buckets hold unequal sample counts wherever a scrape was missed or a run
    started mid-bucket, so averaging the per-bucket averages is simply wrong.
    """
    r, _ = repo
    deployment = r.create(name="DEV-TEST", deploy_args={})
    r.replace_host_metric_buckets(
        deployment.id,
        [
            # 3 samples averaging 20 (10, 20, 30)
            _bucket("h1", "cpu_percent", "2026-07-01T00:00:00", 20, 10, 30, 3),
            # 1 sample of 100
            _bucket("h1", "cpu_percent", "2026-07-01T00:05:00", 100, 100, 100, 1),
        ],
    )
    stats = r.get_host_metric_window(deployment.id)[0]["cpu_percent"]

    # True mean of (10, 20, 30, 100).
    assert stats["avg"] == pytest.approx(40.0)
    # A mean of means would say 60 -- half again as large.
    assert stats["avg"] != pytest.approx(60.0)
    assert stats["min"] == pytest.approx(10.0)
    assert stats["max"] == pytest.approx(100.0)
    assert stats["samples"] == 4


def test_window_is_half_open_on_bucket_start(repo):
    r, _ = repo
    deployment = r.create(name="DEV-TEST", deploy_args={})
    r.replace_host_metric_buckets(
        deployment.id,
        [
            _bucket("h1", "cpu_percent", "2026-07-01T00:00:00", 10, 10, 10, 1),
            _bucket("h1", "cpu_percent", "2026-07-01T00:05:00", 20, 20, 20, 1),
            _bucket("h1", "cpu_percent", "2026-07-01T00:10:00", 30, 30, 30, 1),
        ],
    )
    stats = r.get_host_metric_window(
        deployment.id,
        start=_ts("2026-07-01T00:05:00"),
        end=_ts("2026-07-01T00:10:00"),
    )[0]["cpu_percent"]
    assert stats["samples"] == 1
    assert stats["avg"] == pytest.approx(20.0)


def test_window_keeps_disk_devices_separate(repo):
    r, _ = repo
    deployment = r.create(name="DEV-TEST", deploy_args={})
    r.replace_host_metric_buckets(
        deployment.id,
        [
            _bucket(
                "h1",
                "read_bytes_per_sec",
                "2026-07-01T00:00:00",
                10,
                10,
                10,
                1,
                device="sda",
                volume_role=NODE_STORAGE_ROLE,
            ),
            _bucket(
                "h1",
                "read_bytes_per_sec",
                "2026-07-01T00:00:00",
                90,
                90,
                90,
                1,
                device="vda",
            ),
        ],
    )
    devices = r.get_host_metric_window(deployment.id)[0]["disk_io_by_device"]
    assert devices["sda"]["volume_role"] == NODE_STORAGE_ROLE
    assert devices["sda"]["read_bytes_per_sec"]["avg"] == pytest.approx(10.0)
    assert devices["vda"]["volume_role"] is None
    assert devices["vda"]["read_bytes_per_sec"]["avg"] == pytest.approx(90.0)


def test_window_keeps_rss_per_service(repo):
    r, _ = repo
    deployment = r.create(name="DEV-TEST", deploy_args={})
    r.replace_host_metric_buckets(
        deployment.id,
        [
            _bucket(
                "h1",
                "rss_bytes",
                "2026-07-01T00:00:00",
                100,
                100,
                100,
                1,
                service="ant-node-1",
            ),
            _bucket(
                "h1",
                "rss_bytes",
                "2026-07-01T00:00:00",
                900,
                900,
                900,
                1,
                service="ant-node-2",
            ),
        ],
    )
    services = r.get_host_metric_window(deployment.id)[0]["rss_by_service"]
    assert services["ant-node-1"]["avg"] == pytest.approx(100.0)
    assert services["ant-node-2"]["avg"] == pytest.approx(900.0)


def test_window_returns_nothing_for_a_deployment_without_buckets(repo):
    """Historical runs have no buckets; consumers must degrade to the rollup."""
    r, _ = repo
    deployment = r.create(name="OLD", deploy_args={})
    assert r.get_host_metric_window(deployment.id) == []
    assert r.get_host_metric_bucket_span(deployment.id) is None


def test_bucket_span_reports_first_and_last(repo):
    r, _ = repo
    deployment = r.create(name="DEV-TEST", deploy_args={})
    r.replace_host_metric_buckets(
        deployment.id,
        [
            _bucket("h1", "cpu_percent", "2026-07-01T00:00:00", 10, 10, 10, 1),
            _bucket("h1", "cpu_percent", "2026-07-01T02:00:00", 20, 20, 20, 1),
        ],
    )
    first, last = r.get_host_metric_bucket_span(deployment.id)
    assert first.replace(tzinfo=UTC) == _ts("2026-07-01T00:00:00")
    assert last.replace(tzinfo=UTC) == _ts("2026-07-01T02:00:00")


# ---------------------------------------------------------------------------
# The acceptance criterion: full window from buckets == the rollup
# ---------------------------------------------------------------------------


def _round_trip_points() -> dict:
    """Irregularly-spaced points spanning several buckets, all four families."""
    cpu, mem, net, diskio, procstat = [], [], [], [], []
    base = _ts("2026-07-01T00:00:00")
    counters = {"recv": 0, "sent": 0, "read": 0, "write": 0}
    for i in range(37):
        # Deliberately uneven: gaps mean buckets hold unequal sample counts,
        # which is exactly when a naive mean-of-means diverges.
        offset = timedelta(seconds=i * 20 + (i % 3) * 7)
        stamp = (base + offset).isoformat()
        cpu.append(_cpu_point("h1", stamp, 90.0 - (i % 5)))
        mem.append(_mem_point("h1", stamp, 40.0 + (i % 7)))
        counters["recv"] += 1000 + i * 13
        counters["sent"] += 500 + i * 7
        counters["read"] += 2000 + i * 3
        counters["write"] += 100 + i
        net.append(_net_point("h1", stamp, "eth0", "bytes_recv", counters["recv"]))
        net.append(_net_point("h1", stamp, "eth0", "bytes_sent", counters["sent"]))
        diskio.append(
            _diskio_point(
                "h1",
                stamp,
                "sda",
                "read_bytes",
                counters["read"],
                volume_role=NODE_STORAGE_ROLE,
            )
        )
        diskio.append(
            _diskio_point(
                "h1",
                stamp,
                "sda",
                "write_bytes",
                counters["write"],
                volume_role=NODE_STORAGE_ROLE,
            )
        )
        procstat.append(_procstat_point("h1", stamp, "ant-node-1", 1_000_000 + i * 4_096))
        procstat.append(_procstat_point("h1", stamp, "ant-node-2", 2_000_000 - i * 512))
    return {
        "cpu": cpu,
        "mem": mem,
        "net": net,
        "diskio": diskio,
        "procstat": procstat,
    }


@pytest.mark.parametrize("bucket_secs", [60, 300, 900])
def test_full_window_from_buckets_matches_the_rollup(repo, bucket_secs):
    """V2-753's acceptance criterion, at three bucket widths.

    That the width does not change the answer is the whole argument for
    choosing a coarse default: re-aggregation is exact at any width, so width
    trades only window-edge precision, never accuracy.
    """
    r, _ = repo
    deployment = r.create(name="DEV-TEST", deploy_args={})
    points = _round_trip_points()

    rollup = aggregate_host_metrics(points)[0]
    normalised = normalise_host_metric_points(points)
    r.replace_host_metric_buckets(
        deployment.id, bucket_host_metric_samples(normalised, bucket_secs=bucket_secs)
    )
    derived = r.get_host_metric_window(deployment.id)[0]

    for metric in (
        "cpu_percent",
        "mem_percent",
        "bytes_in_per_sec",
        "bytes_out_per_sec",
    ):
        for stat in ("avg", "min", "max", "samples"):
            assert derived[metric][stat] == pytest.approx(rollup[metric][stat]), (
                f"{metric}.{stat} at {bucket_secs}s buckets"
            )

    for stat in ("avg", "min", "max", "samples"):
        for series in ("read_bytes_per_sec", "write_bytes_per_sec"):
            assert derived["disk_io_by_device"]["sda"][series][stat] == pytest.approx(
                rollup["disk_io_by_device"]["sda"][series][stat]
            ), f"sda.{series}.{stat} at {bucket_secs}s buckets"

    for service in ("ant-node-1", "ant-node-2"):
        for stat in ("avg", "min", "max", "samples"):
            assert derived["rss_by_service"][service][stat] == pytest.approx(
                rollup["rss_by_service"][service][stat]
            ), f"{service}.{stat} at {bucket_secs}s buckets"


def test_a_sub_window_differs_from_the_full_run(repo):
    """The point of the feature: a narrower slice is a different answer."""
    r, _ = repo
    deployment = r.create(name="DEV-TEST", deploy_args={})
    points = _round_trip_points()
    r.replace_host_metric_buckets(
        deployment.id,
        bucket_host_metric_samples(normalise_host_metric_points(points), bucket_secs=60),
    )
    full = r.get_host_metric_window(deployment.id)[0]["cpu_percent"]
    tail = r.get_host_metric_window(deployment.id, start=_ts("2026-07-01T00:08:00"))[0][
        "cpu_percent"
    ]
    assert tail["samples"] < full["samples"]
    assert tail["samples"] > 0


# ---------------------------------------------------------------------------
# Command wiring
# ---------------------------------------------------------------------------


class _FakeRepo:
    def __init__(self, deployment_id: int = 1):
        self.deployment_id = deployment_id
        self.host_result = None
        self.buckets = None
        self.derived = []

    def get_active_by_name(self, name):
        return type("D", (), {"id": self.deployment_id, "name": name})()

    def get_by_name(self, name):
        return self.get_active_by_name(name)

    def upsert_host_result(self, **kwargs):
        self.host_result = kwargs
        return None

    def replace_host_metric_buckets(self, deployment_id, buckets):
        self.buckets = buckets
        return len(buckets)

    def list_upload_records_for_deployment(self, deployment_id):
        return []

    def list_download_records_for_deployment(self, deployment_id):
        return []

    def upsert_derived_metric(self, **kwargs):
        self.derived.append(kwargs)
        return None


def _args(**overrides):
    from types import SimpleNamespace

    base = dict(
        name="DEV-01",
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


def _drive(monkeypatch, args, points):
    fake = _FakeRepo()
    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.setattr(record_analysis_mod, "get_repository", lambda: fake)
    monkeypatch.setattr(
        record_analysis_mod,
        "iter_host_metric_points",
        lambda **kwargs: points,
    )
    record_analysis_mod.cmd_record_analysis(args)
    return fake


def test_record_analysis_writes_buckets_by_default(monkeypatch):
    fake = _drive(monkeypatch, _args(), _round_trip_points())
    assert fake.buckets
    assert {row["metric"] for row in fake.buckets} >= {
        "cpu_percent",
        "mem_percent",
        "rss_bytes",
    }


def test_record_analysis_bucket_secs_zero_skips_bucket_capture(monkeypatch):
    """The rollup is still written -- only the bucket rows are skipped."""
    fake = _drive(monkeypatch, _args(bucket_secs=0), _round_trip_points())
    assert fake.buckets is None
    assert fake.host_result is not None


def test_record_analysis_persists_rss_in_the_rollup(monkeypatch):
    fake = _drive(monkeypatch, _args(), _round_trip_points())
    entry = fake.host_result["host_metrics"][0]
    assert set(entry["rss_by_service"]) == {"ant-node-1", "ant-node-2"}
    assert entry["rss_by_service"]["ant-node-1"]["samples"] == 37


# ---------------------------------------------------------------------------
# Prune
# ---------------------------------------------------------------------------


def test_prune_deletes_old_buckets_but_never_the_rollup(repo):
    r, _ = repo
    deployment = r.create(name="DEV-TEST", deploy_args={})
    r.upsert_host_result(
        deployment_id=deployment.id,
        host_metrics=[{"host": "h1"}],
        metrics_started_at=_ts("2026-01-01T00:00:00"),
        metrics_ended_at=_ts("2026-01-01T01:00:00"),
        summary="kept",
    )
    r.replace_host_metric_buckets(
        deployment.id,
        [
            _bucket("h1", "cpu_percent", "2026-01-01T00:00:00", 10, 10, 10, 1),
            _bucket("h1", "cpu_percent", "2026-07-01T00:00:00", 20, 20, 20, 1),
        ],
    )

    deleted = r.delete_host_metric_buckets_before(cutoff=_ts("2026-06-01T00:00:00"))
    assert deleted == 1
    assert r.count_host_metric_buckets(deployment.id) == 1

    # The whole-run analysis survives: a pruned deployment degrades to exactly
    # the view it would have had before bucketing existed.
    result = r.get_host_result_for_deployment(deployment.id)
    assert result is not None
    assert result.summary == "kept"


def test_prune_can_be_scoped_to_one_deployment(repo):
    r, _ = repo
    a = r.create(name="A", deploy_args={})
    b = r.create(name="B", deploy_args={})
    old = [_bucket("h1", "cpu_percent", "2026-01-01T00:00:00", 1, 1, 1, 1)]
    r.replace_host_metric_buckets(a.id, old)
    r.replace_host_metric_buckets(b.id, old)

    r.delete_host_metric_buckets_before(cutoff=_ts("2026-06-01T00:00:00"), deployment_id=a.id)
    assert r.count_host_metric_buckets(a.id) == 0
    assert r.count_host_metric_buckets(b.id) == 1


def test_summarize_reports_only_what_a_prune_would_remove(repo):
    r, _ = repo
    deployment = r.create(name="DEV-TEST", deploy_args={})
    r.replace_host_metric_buckets(
        deployment.id,
        [
            _bucket("h1", "cpu_percent", "2026-01-01T00:00:00", 10, 10, 10, 1),
            _bucket("h1", "cpu_percent", "2026-07-01T00:00:00", 20, 20, 20, 1),
        ],
    )
    rows = r.summarize_host_metric_buckets(cutoff=_ts("2026-06-01T00:00:00"))
    assert len(rows) == 1
    assert rows[0]["name"] == "DEV-TEST"
    assert rows[0]["rows"] == 1


# ---------------------------------------------------------------------------
# Percentile capture (V2-721)
#
# The release gates name node CPU *median* and node RSS *p50*, which
# avg/min/max cannot yield at any bucket width. Captured at extraction time
# because once a run's InfluxDB dies with its monitoring stack there is
# nothing left to recompute from, and no past run can be backfilled.
# ---------------------------------------------------------------------------


def test_bucket_rows_carry_percentiles():
    from saorsa_deploy.influx import (
        bucket_host_metric_samples,
        normalise_host_metric_points,
    )

    points = {
        "cpu": [
            _cpu_point("h1", f"2026-07-01T00:00:{s:02d}", float(v))
            for s, v in zip(range(0, 50, 10), [10, 20, 30, 40, 50])
        ]
    }
    rows = bucket_host_metric_samples(normalise_host_metric_points(points), bucket_secs=300)
    # `_cpu_point` takes idle, and cpu_percent is 100 - idle, so the series is
    # 90, 80, 70, 60, 50.
    row = next(r for r in rows if r["metric"] == "cpu_percent")
    assert row["p50"] == 70.0
    assert row["p95"] == pytest.approx(88.0)
    # avg/min/max unchanged -- percentiles are additive to what was stored.
    assert row["avg"] == 70.0
    assert row["min"] == 50.0
    assert row["max"] == 90.0


def test_percentile_of_a_single_sample_is_that_sample():
    from saorsa_deploy.influx import _stats

    assert _stats([42.0])["p50"] == 42.0
    assert _stats([42.0])["p95"] == 42.0


def test_percentiles_of_an_empty_series_are_none_not_zero():
    from saorsa_deploy.influx import _stats

    stats = _stats([])
    assert stats["p50"] is None
    assert stats["p95"] is None


def test_normalised_samples_are_stored_columnar_not_as_dicts():
    """Samples must cost ~16 bytes each, not ~305 as retained dicts.

    Regression guard for the second half of the `record-analysis` OOM: the flat
    `list[dict]` of samples was ~5.1 GB on a 990-node run. They are now two
    `array('d')` columns per series, rebuilt into dicts only while a consumer
    iterates.
    """
    import tracemalloc
    from datetime import datetime, timezone

    from saorsa_deploy.influx import normalise_host_metric_points

    n = 60_000
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    points = {
        "procstat": [
            {
                "_time": base.replace(second=i % 60, minute=(i // 60) % 60),
                "host": f"host{i % 20}",
                "node_service": f"ant-node-{i % 15}",
                "_field": "memory_rss",
                "_value": str(1000 + i),
            }
            for i in range(n)
        ]
    }

    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    normalised = normalise_host_metric_points(points)
    after = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()

    assert len(normalised.samples) == n
    per_sample = (after - before) / n
    assert per_sample < 80, f"{per_sample:.0f} B/sample retained; expected ~16"

    # The view must still yield ordinary sample dicts to its consumers.
    first = next(iter(normalised.samples))
    assert set(first) == {"host", "metric", "device", "volume_role", "service", "ts", "value"}
    assert first["ts"].tzinfo is not None
