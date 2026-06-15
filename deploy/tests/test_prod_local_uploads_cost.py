"""Verify prod-local-uploads captures per-file ANT + ETH gas cost.

local-uploads writes per-file rows straight into ``deployment_prod_upload_results``
(no Influx), so cost has to be extracted from ant's JSON at upload time and
passed to the upsert. Display reuses the prod-uploaders renderer in
``print-recorded-results``, already covered elsewhere.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console
from saorsa_db.db import Base
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from saorsa_deploy.cmd import prod_local_uploads


@pytest.fixture()
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session)
    session.close()
    engine.dispose()


class TestCostToDecimal:
    def test_parses_uint256_string(self):
        assert prod_local_uploads._cost_to_decimal(
            "123456789000000000"
        ) == Decimal("123456789000000000")

    def test_empty_and_none_are_null(self):
        assert prod_local_uploads._cost_to_decimal("") is None
        assert prod_local_uploads._cost_to_decimal(None) is None

    def test_unparseable_is_null(self):
        assert prod_local_uploads._cost_to_decimal("not-a-number") is None


def _process(repo, tmp_path, ant_json):
    deployment = repo.create(
        name="LOCAL-UP-01", deploy_args={"kind": "prod-local-uploads"}
    )
    f = tmp_path / "file.bin"
    f.write_bytes(b"x")
    with patch.object(
        prod_local_uploads, "_run_ant", return_value=(0, json.dumps(ant_json))
    ):
        prod_local_uploads._process_one_upload(
            repo=repo,
            deployment_id=deployment.id,
            host="localhost",
            region="local",
            source_url="file:///file.bin",
            file_name="file.bin",
            file_path=f,
            file_size_bytes=1,
            ant_path=Path("/usr/bin/ant"),
            bootstrap_args=[],
            secret_key="sk",
            payment_type=None,
            verbose=1,
            console=Console(quiet=True),
            started_at=datetime.now(timezone.utc),
            start_ns=0,
            log_dir=tmp_path,
            idx=1,
        )
    return repo.list_prod_upload_results_for_deployment(deployment.id)


def test_cost_persisted_from_ant_json(repo, tmp_path):
    rows = _process(
        repo,
        tmp_path,
        {
            "chunks_stored": 5, "chunks_failed": 0, "total_chunks": 5,
            "address": "addr", "elapsed_secs": 1.0,
            "storage_cost_atto": "123456789000000000",
            "gas_cost_wei": "42000000000000",
        },
    )
    assert len(rows) == 1
    assert rows[0].storage_cost_atto == Decimal("123456789000000000")
    assert rows[0].gas_cost_wei == Decimal("42000000000000")


def test_cost_null_when_ant_omits(repo, tmp_path):
    rows = _process(
        repo,
        tmp_path,
        {
            "chunks_stored": 5, "chunks_failed": 0, "total_chunks": 5,
            "address": "addr", "elapsed_secs": 1.0,
        },
    )
    assert len(rows) == 1
    assert rows[0].storage_cost_atto is None
    assert rows[0].gas_cost_wei is None
