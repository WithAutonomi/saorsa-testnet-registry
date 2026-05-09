"""Verify `deployments record-results` dispatches prod-uploaders to the
per-file ``deployment_prod_upload_results`` upsert path.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from saorsa_db.db import Base
from saorsa_db.models import ProdUploadStatus
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from saorsa_deploy.cmd.record_results import cmd_record_results


@pytest.fixture()
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session), session
    session.close()
    engine.dispose()


def _influx_point(**overrides):
    base = {
        "_time": datetime.fromisoformat("2026-04-26T10:30:00+00:00"),
        "host": "prodhost-01",
        "region": "lon1",
        "status": 0,
        "total_ms": 12345,
        "upload_secs": 3.5,
        "file_size": 1048576,
        "start_ns": 1776700000000000000,
        "chunks_stored": 42,
        "chunks_failed": 0,
        "total_chunks": 42,
        "address": "addr-abc",
        "file_name": "file.bin",
        "source_url": "https://example.com/file.bin",
        "reason": "",
    }
    base.update(overrides)
    return base


def _args(name="PROD-UP-01"):
    return SimpleNamespace(
        name=name,
        monitoring_name=None,
        influx_url="https://influx.example.com",
        influx_token="tok",
        influx_org="ant",
        influx_bucket="testnet-metrics",
        window="-30d",
    )


def _run_cmd(repo, points, args=None):
    """Patch repo + Influx fetch and invoke the unified record command."""
    with (
        patch(
            "saorsa_deploy.cmd.record_results.get_repository",
            return_value=repo,
        ),
        patch(
            "saorsa_deploy.cmd.record_results.fetch_prod_upload_points",
            return_value=points,
        ),
    ):
        cmd_record_results(args or _args())


def test_records_one_row_per_point(repo):
    r, _ = repo
    deployment = r.create(
        name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"}
    )

    points = [
        _influx_point(),
        _influx_point(
            status=1,
            source_url="https://example.com/partial.bin",
            file_name="partial.bin",
            chunks_failed=2,
            chunks_stored=40,
            reason="wave store failed after retries",
        ),
        _influx_point(
            status=2,
            source_url="https://example.com/missing.bin",
            file_name="missing.bin",
            address="",
            chunks_stored=0,
            chunks_failed=0,
            total_chunks=0,
            reason="HTTP 404",
        ),
    ]

    _run_cmd(r, points)

    rows = r.list_prod_upload_results_for_deployment(deployment.id)
    assert len(rows) == 3
    by_url = {row.source_url: row for row in rows}
    assert by_url["https://example.com/file.bin"].status == ProdUploadStatus.SUCCESS
    assert by_url["https://example.com/file.bin"].reason is None
    assert (
        by_url["https://example.com/partial.bin"].status
        == ProdUploadStatus.PARTIAL_UPLOAD
    )
    assert (
        by_url["https://example.com/partial.bin"].reason
        == "wave store failed after retries"
    )
    assert (
        by_url["https://example.com/missing.bin"].status
        == ProdUploadStatus.DOWNLOAD_FAILED
    )
    assert by_url["https://example.com/missing.bin"].reason == "HTTP 404"


def test_re_run_updates_existing_row(repo):
    r, _ = repo
    deployment = r.create(
        name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"}
    )

    _run_cmd(r, [_influx_point(status=1, chunks_failed=2, chunks_stored=40)])

    rows = r.list_prod_upload_results_for_deployment(deployment.id)
    assert len(rows) == 1
    assert rows[0].chunks_failed == 2

    _run_cmd(r, [_influx_point(status=0, chunks_stored=42, chunks_failed=0)])

    rows = r.list_prod_upload_results_for_deployment(deployment.id)
    assert len(rows) == 1
    assert rows[0].status == ProdUploadStatus.SUCCESS
    assert rows[0].chunks_failed == 0
    assert rows[0].chunks_stored == 42


def test_skips_points_without_source_url(repo):
    r, _ = repo
    r.create(name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"})

    _run_cmd(r, [_influx_point(source_url="")])

    deployment = r.get_by_name("PROD-UP-01")
    rows = r.list_prod_upload_results_for_deployment(deployment.id)
    assert rows == []


def test_uses_start_ns_for_started_at(repo):
    r, _ = repo
    r.create(name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"})

    _run_cmd(r, [_influx_point(start_ns=1776700000000000000)])

    deployment = r.get_by_name("PROD-UP-01")
    rows = r.list_prod_upload_results_for_deployment(deployment.id)
    expected = datetime.fromtimestamp(
        1776700000000000000 / 1_000_000_000, tz=timezone.utc
    )
    actual = rows[0].started_at
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=timezone.utc)
    assert actual == expected


def test_falls_back_to_time_when_start_ns_missing(repo):
    r, _ = repo
    r.create(name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"})

    pt = _influx_point(start_ns=None)
    _run_cmd(r, [pt])

    deployment = r.get_by_name("PROD-UP-01")
    rows = r.list_prod_upload_results_for_deployment(deployment.id)
    actual = rows[0].started_at
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=timezone.utc)
    assert actual == pt["_time"]


def test_rejects_prod_local_uploads(repo):
    r, _ = repo
    r.create(name="LOCAL-UP-01", deploy_args={"kind": "prod-local-uploads"})
    with patch(
        "saorsa_deploy.cmd.record_results.get_repository",
        return_value=r,
    ):
        # Should return without raising or calling Influx — local-uploads
        # writes its results directly to Postgres at run time.
        cmd_record_results(_args(name="LOCAL-UP-01"))
