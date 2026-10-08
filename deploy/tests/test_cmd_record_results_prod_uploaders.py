"""Verify `deployments record-results` dispatches prod-uploaders to the
per-file ``deployment_prod_upload_results`` upsert path.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from saorsa_db.db import Base
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
