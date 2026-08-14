"""The verification table and the work queue that replaces per-VM state (V2-837).

Runs against in-memory SQLite, like the other repository tests.
"""

from datetime import datetime, timedelta, timezone

import pytest
from saorsa_db.db import Base
from saorsa_db.models import (
    SELF_RESOLVING_SOURCES,
    ClaimSource,
    ProdUploadStatus,
    ProdUploadVerifyStatus,
)
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    return SqlAlchemyDeploymentRepository(session)


@pytest.fixture
def deployment(repo):
    return repo.create(
        name="PROD-UL-DAILY-01",
        deploy_args={"kind": "prod-continuous-uploader"},
    )


def _upload(repo, deployment, address, *, sha256="a" * 64, status=None, offset=0):
    return repo.upsert_prod_upload_result(
        deployment_id=deployment.id,
        client_hostname="up-1",
        region="fsn1",
        source_url=f"https://example.org/{address}.bin",
        file_name=f"{address}.bin",
        file_size_bytes=1024,
        status=status or ProdUploadStatus.SUCCESS,
        address=address,
        chunks_stored=4,
        chunks_failed=0,
        total_chunks=4,
        duration_ms=100,
        upload_secs=0.1,
        started_at=NOW + timedelta(minutes=offset),
        finished_at=NOW + timedelta(minutes=offset),
        sha256=sha256,
    )


def _verify(repo, deployment, address, host, status=ProdUploadVerifyStatus.OK, **kw):
    return repo.upsert_prod_upload_verification(
        deployment_id=deployment.id,
        client_hostname=host,
        region=kw.pop("region", "lon1"),
        address=address,
        file_name=f"{address}.bin",
        status=status,
        started_at=NOW,
        finished_at=NOW,
        **kw,
    )


class TestTheClaimSource:
    def test_the_new_source_resolves_without_the_operator(self):
        """It resolves inline on the VM rather than via record-results, but
        either way the operator never closes these by hand."""
        assert ClaimSource.PROD_CONTINUOUS_UPLOADER in SELF_RESOLVING_SOURCES


class TestTheUploadHash:
    def test_the_hash_is_stored(self, repo, deployment):
        row = _upload(repo, deployment, "addr1", sha256="b" * 64)
        assert row.sha256 == "b" * 64

    def test_a_re_record_without_a_hash_does_not_erase_one(self, repo, deployment):
        """Losing it would strand that address as permanently unverifiable."""
        _upload(repo, deployment, "addr1", sha256="b" * 64)
        again = repo.upsert_prod_upload_result(
            deployment_id=deployment.id,
            client_hostname="up-1",
            region="fsn1",
            source_url="https://example.org/addr1.bin",
            file_name="addr1.bin",
            file_size_bytes=1024,
            status=ProdUploadStatus.SUCCESS,
            address="addr1",
            chunks_stored=4,
            chunks_failed=0,
            total_chunks=4,
            duration_ms=100,
            upload_secs=0.1,
            started_at=NOW,
            finished_at=NOW,
            sha256=None,
        )
        assert again.sha256 == "b" * 64


class TestTheWorkQueue:
    def test_an_unverified_upload_is_queued_for_every_host(self, repo, deployment):
        _upload(repo, deployment, "addr1")
        assert len(repo.list_unverified_uploads_for_host(deployment.id, "ver-1")) == 1
        assert len(repo.list_unverified_uploads_for_host(deployment.id, "ver-2")) == 1

    def test_verifying_clears_it_for_that_host_only(self, repo, deployment):
        """The regions do not divide the work — each must check everything."""
        _upload(repo, deployment, "addr1")
        _verify(repo, deployment, "addr1", "ver-1")
        assert repo.list_unverified_uploads_for_host(deployment.id, "ver-1") == []
        assert len(repo.list_unverified_uploads_for_host(deployment.id, "ver-2")) == 1

    def test_failed_and_partial_uploads_are_not_queued(self, repo, deployment):
        """There is nothing to fetch for an upload that did not complete."""
        _upload(repo, deployment, "ok1")
        _upload(
            repo, deployment, "bad1",
            status=ProdUploadStatus.PARTIAL_UPLOAD, offset=1,
        )
        _upload(
            repo, deployment, "bad2",
            status=ProdUploadStatus.DOWNLOAD_FAILED, offset=2,
        )
        queued = repo.list_unverified_uploads_for_host(deployment.id, "ver-1")
        assert [u.address for u in queued] == ["ok1"]

    def test_the_queue_is_oldest_first(self, repo, deployment):
        """A backlog is worked in the order the files were written."""
        _upload(repo, deployment, "second", offset=10)
        _upload(repo, deployment, "first", offset=0)
        queued = repo.list_unverified_uploads_for_host(deployment.id, "ver-1")
        assert [u.address for u in queued] == ["first", "second"]

    def test_a_limit_bounds_a_cycle(self, repo, deployment):
        for index in range(5):
            _upload(repo, deployment, f"addr{index}", offset=index)
        assert (
            len(repo.list_unverified_uploads_for_host(deployment.id, "ver-1", limit=2))
            == 2
        )

    def test_a_replaced_vm_with_the_same_hostname_resumes(self, repo, deployment):
        """The queue is server-side, so nothing is lost with the VM's disk."""
        _upload(repo, deployment, "addr1")
        _upload(repo, deployment, "addr2", offset=1)
        _verify(repo, deployment, "addr1", "ver-1")
        # A brand-new VM, same name, no local state at all.
        remaining = repo.list_unverified_uploads_for_host(deployment.id, "ver-1")
        assert [u.address for u in remaining] == ["addr2"]


class TestRecordingAVerification:
    def test_it_is_idempotent_per_host_and_address(self, repo, deployment):
        _verify(repo, deployment, "addr1", "ver-1")
        _verify(repo, deployment, "addr1", "ver-1")
        rows = repo.list_prod_upload_verifications_for_deployment(deployment.id)
        assert len(rows) == 1

    def test_each_host_records_its_own_verdict(self, repo, deployment):
        _verify(repo, deployment, "addr1", "ver-1", region="lon1")
        _verify(repo, deployment, "addr1", "ver-2", region="GRA9")
        rows = repo.list_prod_upload_verifications_for_deployment(deployment.id)
        assert {r.client_hostname for r in rows} == {"ver-1", "ver-2"}

    def test_filtering_by_host(self, repo, deployment):
        _verify(repo, deployment, "addr1", "ver-1")
        _verify(repo, deployment, "addr2", "ver-2")
        rows = repo.list_prod_upload_verifications_for_deployment(
            deployment.id, client_hostname="ver-2"
        )
        assert [r.address for r in rows] == ["addr2"]

    def test_a_mismatch_keeps_both_hashes_as_evidence(self, repo, deployment):
        row = _verify(
            repo, deployment, "addr1", "ver-1",
            status=ProdUploadVerifyStatus.DIGEST_MISMATCH,
            expected_sha256="a" * 64,
            actual_sha256="c" * 64,
        )
        assert row.expected_sha256 == "a" * 64
        assert row.actual_sha256 == "c" * 64

    def test_a_mismatch_is_never_overwritten_by_a_later_success(
        self, repo, deployment
    ):
        """Bytes that came back wrong once are a permanent fact.

        A later OK means the network healed, not that the mismatch never
        happened — and this is the most serious thing the test can detect, so
        the evidence must not be silently replaced.
        """
        _verify(
            repo, deployment, "addr1", "ver-1",
            status=ProdUploadVerifyStatus.DIGEST_MISMATCH,
            expected_sha256="a" * 64,
            actual_sha256="c" * 64,
        )
        _verify(repo, deployment, "addr1", "ver-1", status=ProdUploadVerifyStatus.OK)
        rows = repo.list_prod_upload_verifications_for_deployment(deployment.id)
        assert rows[0].status is ProdUploadVerifyStatus.DIGEST_MISMATCH
        assert "re-check returned ok" in rows[0].reason

    def test_an_ordinary_failure_can_be_revised(self, repo, deployment):
        """Only a mismatch is sticky; a transient failure is not."""
        _verify(
            repo, deployment, "addr1", "ver-1",
            status=ProdUploadVerifyStatus.DOWNLOAD_FAILED,
        )
        _verify(repo, deployment, "addr1", "ver-1", status=ProdUploadVerifyStatus.OK)
        rows = repo.list_prod_upload_verifications_for_deployment(deployment.id)
        assert rows[0].status is ProdUploadVerifyStatus.OK
