"""Tests for `prod pool import-history`.

The load-bearing behaviour is that it reads BOTH result tables. Canary uploads
live in `prod_canary_uploads`, not `deployment_prod_upload_results`, and they
are the larger share of what production actually holds — 1006 against 538 as at
2026-07-30. An import that read only the latter would leave the pool willing to
re-issue two thirds of the files already stored.
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from saorsa_db.db import Base
from saorsa_db.models.deployment import Deployment
from saorsa_db.models.prod_canary import (
    DeploymentProdCanaryUpload,
    ProdCanaryInitialVerifyStatus,
)
from saorsa_db.models.prod_file_pool import ClaimSource, ClaimStatus, PoolFileKind
from saorsa_db.models.prod_upload_result import (
    DeploymentProdUploadResult,
    ProdUploadStatus,
)
from saorsa_db.repositories import (
    SqlAlchemyDeploymentRepository,
    SqlAlchemyProdFilePoolRepository,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy.cmd import prod_pool

MIB = 1024 * 1024
NOW = datetime(2026, 7, 30, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def repos():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield (
            SqlAlchemyProdFilePoolRepository(session),
            SqlAlchemyDeploymentRepository(session),
            session,
        )


@pytest.fixture
def wired(repos, monkeypatch):
    pool_repo, deployment_repo, session = repos
    monkeypatch.setattr(prod_pool, "get_file_pool_repository", lambda: pool_repo)
    monkeypatch.setattr(
        "saorsa_deploy.registry.get_repository", lambda: deployment_repo
    )
    return pool_repo, deployment_repo, session


def _args(dry_run=False):
    return argparse.Namespace(dry_run=dry_run)


def _flat(text):
    return " ".join(text.split())


def _deployment(session, name):
    deployment = Deployment(name=name, deploy_args={})
    session.add(deployment)
    session.commit()
    return deployment


def _upload_result(session, deployment, file_name, size=50 * MIB, address="addr1"):
    session.add(
        DeploymentProdUploadResult(
            deployment_id=deployment.id,
            client_hostname="host-1",
            region="lon1",
            source_url=f"https://example.org/{file_name}",
            file_name=file_name,
            file_size_bytes=size,
            status=ProdUploadStatus.SUCCESS,
            address=address,
            chunks_stored=77,
            chunks_failed=0,
            total_chunks=77,
            storage_cost_atto=Decimal("419203847182734"),
            gas_cost_wei=Decimal("1310000000000"),
            started_at=NOW,
            finished_at=NOW,
        )
    )
    session.commit()


def _canary_upload(session, deployment, file_name, size=50 * MIB, address="caddr1"):
    import uuid

    session.add(
        DeploymentProdCanaryUpload(
            canary_id=uuid.uuid4(),
            deployment_id=deployment.id,
            source_url=f"https://archive.org/download/i/{file_name}",
            file_name=file_name,
            file_size_bytes=size,
            top_address=address,
            num_chunks=42,
            uploaded_at=NOW,
            initial_verify_status=ProdCanaryInitialVerifyStatus.OK,
        )
    )
    session.commit()


class TestBothTablesAreRead:
    def test_imports_from_prod_uploaders_results(self, wired, capsys):
        pool_repo, _, session = wired
        pool_file, _ = pool_repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/a.bin", "a.bin", size_bytes=50 * MIB
        )
        _upload_result(session, _deployment(session, "PROD-UL-05"), "a.bin")

        prod_pool.cmd_prod_pool_import_history(_args())

        claim = pool_repo.open_claim_for_file(pool_file.id)
        assert claim is not None
        assert claim.status is ClaimStatus.UPLOADED
        assert claim.source is ClaimSource.PROD_UPLOADERS
        assert claim.address == "addr1"

    def test_imports_from_canary_uploads(self, wired):
        # The half a results-table-only import would miss entirely.
        pool_repo, _, session = wired
        pool_file, _ = pool_repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/c.mkv", "c.mkv", size_bytes=50 * MIB
        )
        _canary_upload(session, _deployment(session, "PROD-CANARY-03"), "c.mkv")

        prod_pool.cmd_prod_pool_import_history(_args())

        claim = pool_repo.open_claim_for_file(pool_file.id)
        assert claim is not None
        assert claim.source is ClaimSource.PROD_CANARY
        assert claim.address == "caddr1"
        assert claim.total_chunks == 42

    def test_imported_files_cannot_be_handed_out(self, wired):
        from saorsa_db.repositories import InsufficientPoolInventory
        from saorsa_db.size_class import SizeClass

        pool_repo, _, session = wired
        pool_repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/a.bin", "a.bin", size_bytes=50 * MIB
        )
        _canary_upload(session, _deployment(session, "PROD-CANARY-03"), "a.bin")
        prod_pool.cmd_prod_pool_import_history(_args())

        with pytest.raises(InsufficientPoolInventory):
            pool_repo.claim_pool_files(
                PoolFileKind.URL, {SizeClass.SMALL: 1}, ClaimSource.PROD_UPLOADERS
            )


class TestIdempotence:
    def test_rerunning_imports_nothing_new(self, wired, capsys):
        pool_repo, _, session = wired
        pool_repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/a.bin", "a.bin", size_bytes=50 * MIB
        )
        _upload_result(session, _deployment(session, "PROD-UL-05"), "a.bin")

        prod_pool.cmd_prod_pool_import_history(_args())
        capsys.readouterr()
        prod_pool.cmd_prod_pool_import_history(_args())

        out = _flat(capsys.readouterr().out)
        assert "imported 0" in out
        assert "already 1" in out
        assert len(pool_repo.list_pool_claims()) == 1

    def test_dry_run_writes_nothing(self, wired, capsys):
        pool_repo, _, session = wired
        pool_repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/a.bin", "a.bin", size_bytes=50 * MIB
        )
        _upload_result(session, _deployment(session, "PROD-UL-05"), "a.bin")

        prod_pool.cmd_prod_pool_import_history(_args(dry_run=True))

        out = _flat(capsys.readouterr().out)
        assert "imported 1" in out
        assert "Dry run — nothing was written" in out
        assert pool_repo.list_pool_claims() == []


class TestMatching:
    def test_history_not_in_the_pool_is_reported_as_expected(self, wired, capsys):
        pool_repo, _, session = wired
        _upload_result(session, _deployment(session, "PROD-UL-05"), "never-pooled.bin")

        prod_pool.cmd_prod_pool_import_history(_args())

        out = _flat(capsys.readouterr().out)
        assert "unmatched 1" in out
        # Said plainly, so the number is not read as a coverage failure.
        assert "most recorded uploads predate the pool" in out

    def test_size_mismatch_is_flagged_but_still_marked_spent(self, wired, capsys):
        # A name match with a different size is suspicious, but re-uploading
        # content already on the network is worse than losing a pool entry,
        # and `pool release` undoes a wrong call.
        pool_repo, _, session = wired
        pool_file, _ = pool_repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/a.bin", "a.bin", size_bytes=99 * MIB
        )
        _upload_result(session, _deployment(session, "PROD-UL-05"), "a.bin", size=50 * MIB)

        prod_pool.cmd_prod_pool_import_history(_args())

        out = _flat(capsys.readouterr().out)
        assert "size differs" in out
        assert "pool release" in out
        assert pool_repo.open_claim_for_file(pool_file.id) is not None

    def test_failed_uploads_are_not_imported(self, wired):
        # A partial upload did not put the whole file on the network, so the
        # pool entry is still usable.
        pool_repo, _, session = wired
        pool_file, _ = pool_repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/a.bin", "a.bin", size_bytes=50 * MIB
        )
        deployment = _deployment(session, "PROD-UL-05")
        session.add(
            DeploymentProdUploadResult(
                deployment_id=deployment.id,
                client_hostname="host-1",
                region="lon1",
                source_url="https://example.org/a.bin",
                file_name="a.bin",
                file_size_bytes=50 * MIB,
                status=ProdUploadStatus.PARTIAL_UPLOAD,
                started_at=NOW,
                finished_at=NOW,
            )
        )
        session.commit()

        prod_pool.cmd_prod_pool_import_history(_args())
        assert pool_repo.open_claim_for_file(pool_file.id) is None

    def test_nothing_recorded_says_so(self, wired, capsys):
        prod_pool.cmd_prod_pool_import_history(_args())
        assert "No recorded production uploads" in capsys.readouterr().out
