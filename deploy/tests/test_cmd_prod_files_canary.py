"""`prod files` must show canary uploads too.

Canary uploads land in `prod_canary_uploads`, not
`deployment_prod_upload_results`. Reading only the latter meant this view — the
one an operator consults to answer "has this file been uploaded?" — was blind
to 1006 of the 1544 files on production as at 2026-07-30, and confidently
printed a complete-looking list.
"""

import argparse
import uuid
from datetime import datetime, timezone

import pytest
from saorsa_db.db import Base
from saorsa_db.models.deployment import Deployment
from saorsa_db.models.prod_canary import (
    DeploymentProdCanaryUpload,
    ProdCanaryInitialVerifyStatus,
)
from saorsa_db.models.prod_upload_result import (
    DeploymentProdUploadResult,
    ProdUploadStatus,
)
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy.cmd import prod_files

MIB = 1024 * 1024
NOW = datetime(2026, 7, 30, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    repo = SqlAlchemyDeploymentRepository(session)
    monkeypatch.setattr(prod_files, "get_repository", lambda: repo)
    yield repo, session
    session.close()


def _deployment(session, name):
    deployment = Deployment(name=name, deploy_args={"kind": "prod-uploaders"})
    session.add(deployment)
    session.commit()
    return deployment


def _upload(session, deployment, name, status=ProdUploadStatus.SUCCESS):
    session.add(
        DeploymentProdUploadResult(
            deployment_id=deployment.id,
            client_hostname="host-1",
            region="lon1",
            source_url=f"https://example.org/{name}",
            file_name=name,
            file_size_bytes=50 * MIB,
            status=status,
            address=f"addr-{name}",
            started_at=NOW,
            finished_at=NOW,
        )
    )
    session.commit()


def _canary_upload(session, deployment, name):
    session.add(
        DeploymentProdCanaryUpload(
            canary_id=uuid.uuid4(),
            deployment_id=deployment.id,
            source_url=f"https://archive.org/download/i/{name}",
            file_name=name,
            file_size_bytes=80 * MIB,
            top_address=f"canary-addr-{name}",
            num_chunks=42,
            uploaded_at=NOW,
            initial_verify_status=ProdCanaryInitialVerifyStatus.OK,
        )
    )
    session.commit()


def _flat(text):
    return " ".join(text.split())


class TestCanaryCoverage:
    def test_canary_files_are_listed(self, env, capsys):
        repo, session = env
        canary = _deployment(session, "PROD-CANARY-03")
        _canary_upload(session, canary, "film.mkv")

        prod_files.cmd_prod_files(argparse.Namespace(failed=False))

        out = _flat(capsys.readouterr().out)
        assert "film.mkv" in out
        assert "canary-addr-film.mkv" in out
        assert "PROD-CANARY-03" in out

    def test_both_tables_appear_together(self, env, capsys):
        repo, session = env
        uploaders = _deployment(session, "PROD-UL-05")
        canary = _deployment(session, "PROD-CANARY-03")
        _upload(session, uploaders, "a.bin")
        _canary_upload(session, canary, "b.mkv")

        prod_files.cmd_prod_files(argparse.Namespace(failed=False))

        out = _flat(capsys.readouterr().out)
        assert "a.bin" in out
        assert "b.mkv" in out

    def test_canary_size_and_date_render(self, env, capsys):
        repo, session = env
        canary = _deployment(session, "PROD-CANARY-03")
        _canary_upload(session, canary, "film.mkv")

        prod_files.cmd_prod_files(argparse.Namespace(failed=False))
        out = _flat(capsys.readouterr().out)
        assert "80.00 MB" in out
        assert "2026-07-30" in out

    def test_failed_view_excludes_canary(self, env, capsys):
        # A canary that failed to upload simply has no row, so there is no
        # failure state of the same shape to show.
        repo, session = env
        uploaders = _deployment(session, "PROD-UL-05")
        canary = _deployment(session, "PROD-CANARY-03")
        _upload(session, uploaders, "bad.bin", ProdUploadStatus.PARTIAL_UPLOAD)
        _canary_upload(session, canary, "film.mkv")

        prod_files.cmd_prod_files(argparse.Namespace(failed=True))
        out = _flat(capsys.readouterr().out)
        assert "bad.bin" in out
        assert "film.mkv" not in out

    def test_empty_registry_is_still_handled(self, env, capsys):
        prod_files.cmd_prod_files(argparse.Namespace(failed=False))
        assert "No successful prod uploads recorded" in _flat(capsys.readouterr().out)
