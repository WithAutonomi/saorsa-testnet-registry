"""`deployments derive-metrics` — re-deriving a window after teardown (V2-758).

Exercised against a real (in-memory) repository rather than a fake, because the
point of the command is that everything it needs already lives in Postgres: the
bucket rows, their endpoints, and the per-op records. The InfluxDB it came from
is assumed gone.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from saorsa_db.db import Base
from saorsa_db.models.derived_metric import (
    METRIC_DISK_BYTES_PER_GB_STORED,
    METRIC_GB_UPLOADED,
    METRIC_NODE_OUTBOUND_BYTES_TOTAL,
    METRIC_NODE_STORAGE_GROWTH_BYTES,
    METRIC_OUTBOUND_BYTES_PER_GB_UPLOADED,
    SOURCE_BUCKETS,
    WINDOW_FULL_RUN,
)
from saorsa_db.models.host_metric_bucket import (
    METRIC_BYTES_OUT_PER_SEC,
    METRIC_DISK_USED_BYTES,
)
from saorsa_db.models.op_record import OpRecordStatus
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import saorsa_deploy.cmd.derive_metrics as derive_mod
from saorsa_deploy.derived_metrics import BYTES_PER_GB, infer_bucket_secs

UTC = timezone.utc
NODE_MOUNT = "/mnt/ant-node-storage"
NODE_STORAGE = "node-storage"
BUCKET_SECS = 300


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@pytest.fixture()
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session)
    session.close()
    engine.dispose()


def _bucket(metric, start, avg, **extra):
    row = {
        "host": "h1",
        "metric": metric,
        "bucket_start": _ts(start),
        "avg": avg,
        "min": avg,
        "max": avg,
        "samples": 30,
        "device": None,
        "volume_role": None,
        "service": None,
    }
    row.update(extra)
    return row


def _seed(repo, *, uploads=((1, "2026-07-01T00:10:00"),)):
    """One host, four 5-minute buckets of egress and a growing volume."""
    deployment = repo.create(name="DEV-03", deploy_args={})
    buckets = []
    for index, minute in enumerate(("00:00", "00:05", "00:10", "00:15")):
        start = f"2026-07-01T{minute}:00"
        # 1 MB/s throughout: 300 s per bucket => 300 MB per bucket.
        buckets.append(
            _bucket(METRIC_BYTES_OUT_PER_SEC, start, 1024 * 1024)
        )
        buckets.append(
            _bucket(
                METRIC_DISK_USED_BYTES,
                start,
                index * BYTES_PER_GB,
                device=NODE_MOUNT,
                volume_role=NODE_STORAGE,
            )
        )
    repo.replace_host_metric_buckets(deployment.id, buckets)

    for index, (gb, finished) in enumerate(uploads):
        repo.upsert_upload_record(
            deployment_id=deployment.id,
            client_hostname="client-1",
            region="lon1",
            file_size_bytes=gb * BYTES_PER_GB,
            status=OpRecordStatus.SUCCESS,
            start_ns=index + 1,
            influx_time=_ts(finished),
            started_at=_ts(finished),
            finished_at=_ts(finished),
            duration_ms=1000,
        )
    return deployment


def _args(**overrides):
    base = dict(
        name="DEV-03",
        window_start=None,
        window_end=None,
        label="steady-state",
        bucket_secs=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _run(monkeypatch, repo, args=None):
    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.setattr(derive_mod, "get_repository", lambda: repo)
    derive_mod.cmd_derive_metrics(args or _args())


def _stored(repo, deployment, label="steady-state"):
    return {
        row.metric: row
        for row in repo.list_derived_metrics_for_deployment(
            deployment.id, window_label=label
        )
    }


def test_derives_the_whole_span_by_default(monkeypatch, repo):
    deployment = _seed(repo)
    _run(monkeypatch, repo)
    stored = _stored(repo, deployment)
    # Four buckets at 1 MB/s x 300 s each.
    assert stored[METRIC_NODE_OUTBOUND_BYTES_TOTAL].value == 4 * 300 * 1024 * 1024
    assert stored[METRIC_GB_UPLOADED].value == 1.0


def test_growth_comes_from_the_bucket_endpoints(monkeypatch, repo):
    deployment = _seed(repo)
    _run(monkeypatch, repo)
    # 0 GB -> 3 GB across the four buckets.
    assert _stored(repo, deployment)[METRIC_NODE_STORAGE_GROWTH_BYTES].value == (
        3 * BYTES_PER_GB
    )


def test_a_sub_window_measures_less_than_the_whole_run(monkeypatch, repo):
    deployment = _seed(repo)
    _run(
        monkeypatch,
        repo,
        _args(
            window_start="2026-07-01T00:05:00",
            window_end="2026-07-01T00:15:00",
        ),
    )
    stored = _stored(repo, deployment)
    # Two buckets, not four; growth 1 GB -> 2 GB.
    assert stored[METRIC_NODE_OUTBOUND_BYTES_TOTAL].value == 2 * 300 * 1024 * 1024
    assert stored[METRIC_NODE_STORAGE_GROWTH_BYTES].value == BYTES_PER_GB


def test_the_denominator_is_windowed_too(monkeypatch, repo):
    # An upload that finished before the window must not inflate the ratio for
    # a window it contributed no traffic to.
    deployment = _seed(
        repo,
        uploads=((1, "2026-07-01T00:01:00"), (2, "2026-07-01T00:07:00")),
    )
    _run(
        monkeypatch,
        repo,
        _args(
            window_start="2026-07-01T00:05:00",
            window_end="2026-07-01T00:15:00",
        ),
    )
    assert _stored(repo, deployment)[METRIC_GB_UPLOADED].value == 2.0


def test_rows_are_marked_as_re_derived_from_buckets(monkeypatch, repo):
    deployment = _seed(repo)
    _run(monkeypatch, repo)
    row = _stored(repo, deployment)[METRIC_OUTBOUND_BYTES_PER_GB_UPLOADED]
    # Integrated from averages, not summed from raw counters: a consumer must
    # be able to tell, or it will compare an approximation to an exact value.
    assert row.source == SOURCE_BUCKETS
    assert row.window_label == "steady-state"


def test_ratio_reconciles_with_its_own_components(monkeypatch, repo):
    deployment = _seed(repo)
    _run(monkeypatch, repo)
    stored = _stored(repo, deployment)
    ratio = stored[METRIC_DISK_BYTES_PER_GB_STORED]
    assert ratio.numerator == stored[METRIC_NODE_STORAGE_GROWTH_BYTES].value
    assert ratio.denominator == stored[METRIC_GB_UPLOADED].value
    assert ratio.value == pytest.approx(ratio.numerator / ratio.denominator)


def test_rerunning_the_same_label_replaces_rather_than_accumulates(
    monkeypatch, repo
):
    deployment = _seed(repo)
    _run(monkeypatch, repo)
    _run(monkeypatch, repo)
    rows = repo.list_derived_metrics_for_deployment(
        deployment.id, window_label="steady-state"
    )
    assert len(rows) == len({row.metric for row in rows})


def test_two_windows_coexist(monkeypatch, repo):
    deployment = _seed(repo)
    _run(monkeypatch, repo, _args(label="warm-up",
                                  window_end="2026-07-01T00:10:00"))
    _run(monkeypatch, repo, _args(label="steady-state",
                                  window_start="2026-07-01T00:10:00"))
    warm = _stored(repo, deployment, "warm-up")
    steady = _stored(repo, deployment, "steady-state")
    assert warm[METRIC_NODE_OUTBOUND_BYTES_TOTAL].value == 2 * 300 * 1024 * 1024
    assert steady[METRIC_NODE_OUTBOUND_BYTES_TOTAL].value == 2 * 300 * 1024 * 1024


def test_refuses_to_overwrite_the_full_run_label(monkeypatch, repo):
    _seed(repo)
    with pytest.raises(SystemExit):
        _run(monkeypatch, repo, _args(label=WINDOW_FULL_RUN))


def test_refuses_a_deployment_with_no_buckets(monkeypatch, repo):
    repo.create(name="DEV-03", deploy_args={})
    with pytest.raises(SystemExit):
        _run(monkeypatch, repo)


def test_refuses_an_inverted_window(monkeypatch, repo):
    _seed(repo)
    with pytest.raises(SystemExit):
        _run(
            monkeypatch,
            repo,
            _args(
                window_start="2026-07-01T00:15:00",
                window_end="2026-07-01T00:05:00",
            ),
        )


def test_refuses_a_window_containing_no_buckets(monkeypatch, repo):
    _seed(repo)
    with pytest.raises(SystemExit):
        _run(
            monkeypatch,
            repo,
            _args(
                window_start="2026-07-02T00:00:00",
                window_end="2026-07-02T01:00:00",
            ),
        )


# ---------------------------------------------------------------------------
# Bucket width inference
# ---------------------------------------------------------------------------


def test_infers_the_width_from_consecutive_buckets():
    rows = [
        {"bucket_start": _ts(f"2026-07-01T00:{minute:02d}:00")}
        for minute in (0, 5, 10, 15)
    ]
    assert infer_bucket_secs(rows) == 300


def test_a_collection_gap_does_not_move_the_inferred_width():
    # One oversized interval where scrapes were missed must not be mistaken for
    # the bucket width, which would scale every byte total by that factor.
    rows = [
        {"bucket_start": _ts(t)}
        for t in (
            "2026-07-01T00:00:00",
            "2026-07-01T00:05:00",
            "2026-07-01T00:10:00",
            "2026-07-01T01:00:00",
        )
    ]
    assert infer_bucket_secs(rows) == 300


def test_falls_back_to_the_default_with_a_single_bucket():
    assert infer_bucket_secs([{"bucket_start": _ts("2026-07-01T00:00:00")}]) == 300


def test_an_explicit_width_overrides_inference(monkeypatch, repo):
    deployment = _seed(repo)
    _run(monkeypatch, repo, _args(bucket_secs=60))
    assert _stored(repo, deployment)[METRIC_NODE_OUTBOUND_BYTES_TOTAL].value == (
        4 * 60 * 1024 * 1024
    )
