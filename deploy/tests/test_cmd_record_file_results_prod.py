"""Verify record-file-results works for prod-continuous-downloader deployment rows.

The existing testnet flow keys on ``deployment.name`` and the record command is
generic over deployment kind — this test just pins that contract down so a
future refactor can't accidentally couple record-file-results to testnet rows.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from saorsa_db.db import Base
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from saorsa_deploy.cmd.record_file_results import cmd_record_file_results


@pytest.fixture()
def repo_and_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    repo = SqlAlchemyDeploymentRepository(session)
    yield repo, session
    session.close()
    engine.dispose()


def _download_point(host: str, ts_iso: str, start_ns: int, total_ms: int, status: int = 0):
    return {
        "_time": datetime.fromisoformat(ts_iso).astimezone(timezone.utc),
        "host": host,
        "_measurement": "download",
        "file_size": 4 * 1024 * 1024,
        "start_ns": start_ns,
        "status": status,
        "total_ms": total_ms,
    }


def _args(name: str):
    return SimpleNamespace(
        name=name,
        influx_url="http://fake-influx.local:8086",
        influx_token="fake-token",
        influx_org="ant",
        influx_bucket="testnet-metrics",
        monitoring_name=None,
        window="-1h",
    )


def test_record_file_results_upserts_download_rows_for_prod_deployment(repo_and_session):
    repo, session = repo_and_session
    deployment = repo.create(
        name="PROD-DL-01",
        deploy_args={
            "kind": "prod-continuous-downloader",
            "prod_bootstraps_url": "https://example.test/peers.toml",
            "downloader_specs": ["lon1", "nyc1"],
        },
    )

    canned_points = [
        _download_point("PROD-DL-01-lon1-1", "2026-04-24T10:00:00+00:00", 1_000, 150),
        _download_point("PROD-DL-01-lon1-1", "2026-04-24T10:01:00+00:00", 2_000, 250),
        _download_point(
            "PROD-DL-01-nyc1-1", "2026-04-24T10:02:00+00:00", 3_000, 500, status=1
        ),
    ]

    with (
        patch(
            "saorsa_deploy.cmd.record_file_results.get_repository", return_value=repo
        ),
        patch(
            "saorsa_deploy.cmd.record_file_results.fetch_operation_points",
            return_value=canned_points,
        ),
    ):
        cmd_record_file_results(_args("PROD-DL-01"))

    rows = repo.list_file_results_for_deployment(deployment.id)
    assert len(rows) == 2  # one per host, both download
    by_host = {r.client_hostname: r for r in rows}

    lon1 = by_host["PROD-DL-01-lon1-1"]
    assert lon1.op_type.value == "download"
    assert lon1.success_count == 2
    assert lon1.failure_count == 0
    assert lon1.avg_duration_ms == 200  # (150 + 250) // 2
    assert lon1.min_duration_ms == 150
    assert lon1.max_duration_ms == 250

    nyc1 = by_host["PROD-DL-01-nyc1-1"]
    assert nyc1.success_count == 0
    assert nyc1.failure_count == 1


def test_record_file_results_no_ops_no_rows_no_crash(repo_and_session):
    repo, _ = repo_and_session
    deployment = repo.create(
        name="PROD-DL-EMPTY",
        deploy_args={"kind": "prod-continuous-downloader"},
    )

    with (
        patch(
            "saorsa_deploy.cmd.record_file_results.get_repository", return_value=repo
        ),
        patch(
            "saorsa_deploy.cmd.record_file_results.fetch_operation_points",
            return_value=[],
        ),
    ):
        cmd_record_file_results(_args("PROD-DL-EMPTY"))

    assert repo.list_file_results_for_deployment(deployment.id) == []
