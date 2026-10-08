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
    monkeypatch.setattr("saorsa_deploy.registry.get_repository", lambda: deployment_repo)
    return pool_repo, deployment_repo, session


def _args(dry_run=False, catalogue_missing=False, deployment=None, tag=None):
    return argparse.Namespace(
        dry_run=dry_run,
        catalogue_missing=catalogue_missing,
        deployment=deployment,
        tag=tag,
    )


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


class TestAttribution:
    """Which run a file came from, when two kinds share one table.

    `deployment_prod_upload_results` is written by remote `prod-uploaders` runs
    AND by `prod-local-uploads`, which writes its rows inline. The table carries
    no hint of which. Measured on the live registry: 444 of 583 rows are local
    uploads, so assuming the remote kind mislabels the majority of them.
    """

    def _deployment(self, session, name, kind):
        deployment = Deployment(name=name, deploy_args={"kind": kind})
        session.add(deployment)
        session.commit()
        return deployment

    def test_local_uploads_are_not_labelled_prod_uploaders(self, wired):
        pool_repo, _, session = wired
        pool_repo.add_pool_file(PoolFileKind.LOCAL, "/data/a.bin", "a.bin", size_bytes=50 * MIB)
        deployment = self._deployment(session, "PROD-LOCAL-UL-01", "prod-local-uploads")
        _upload_result(session, deployment, "a.bin")

        prod_pool.cmd_prod_pool_import_history(_args())

        claim = pool_repo.list_pool_claims()[0]
        assert claim.source is ClaimSource.PROD_LOCAL_UPLOADS
        assert claim.deployment_name == "PROD-LOCAL-UL-01"
        assert claim.deployment_id == deployment.id

    def test_remote_uploads_keep_their_kind(self, wired):
        pool_repo, _, session = wired
        pool_repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/b.bin", "b.bin", size_bytes=50 * MIB
        )
        deployment = self._deployment(session, "PROD-UL-05", "prod-uploaders")
        _upload_result(session, deployment, "b.bin")

        prod_pool.cmd_prod_pool_import_history(_args())
        assert pool_repo.list_pool_claims()[0].source is ClaimSource.PROD_UPLOADERS

    def test_canary_is_settled_by_its_table_not_deploy_args(self, wired):
        # prod_canary_uploads can only have come from a canary, so the source
        # must not depend on deploy_args being populated.
        pool_repo, _, session = wired
        pool_repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/c.mkv", "c.mkv", size_bytes=50 * MIB
        )
        deployment = Deployment(name="PROD-CANARY-03", deploy_args={})
        session.add(deployment)
        session.commit()
        _canary_upload(session, deployment, "c.mkv")

        prod_pool.cmd_prod_pool_import_history(_args())
        assert pool_repo.list_pool_claims()[0].source is ClaimSource.PROD_CANARY

    def test_a_rerun_corrects_a_previous_mislabelling(self, wired, capsys):
        # This command is a sync, not a one-shot: re-running it should repair
        # what an earlier version got wrong.
        from saorsa_db.models.prod_file_pool import ClaimStatus

        pool_repo, _, session = wired
        pool_file, _ = pool_repo.add_pool_file(
            PoolFileKind.LOCAL, "/data/a.bin", "a.bin", size_bytes=50 * MIB
        )
        deployment = self._deployment(session, "PROD-LOCAL-UL-01", "prod-local-uploads")
        _upload_result(session, deployment, "a.bin")
        # Simulate what the buggy version left behind.
        pool_repo.seed_uploaded_claim(
            pool_file.id,
            source=ClaimSource.PROD_UPLOADERS,
            status=ClaimStatus.UPLOADED,
            note="imported from recorded upload history",
        )

        prod_pool.cmd_prod_pool_import_history(_args())

        claim = pool_repo.list_pool_claims()[0]
        assert claim.source is ClaimSource.PROD_LOCAL_UPLOADS
        assert claim.deployment_name == "PROD-LOCAL-UL-01"
        assert "corrected" in _flat(capsys.readouterr().out)

    def test_a_real_deployment_claim_is_never_reattributed(self, wired):
        # Only claims this command created carry that note. A claim taken by an
        # actual run must not be rewritten by an import.
        from saorsa_db.models.prod_file_pool import ClaimStatus

        pool_repo, _, session = wired
        pool_file, _ = pool_repo.add_pool_file(
            PoolFileKind.LOCAL, "/data/a.bin", "a.bin", size_bytes=50 * MIB
        )
        deployment = self._deployment(session, "PROD-LOCAL-UL-01", "prod-local-uploads")
        _upload_result(session, deployment, "a.bin")
        pool_repo.seed_uploaded_claim(
            pool_file.id,
            source=ClaimSource.PROD_UPLOADERS,
            status=ClaimStatus.UPLOADED,
            deployment_name="SOME-REAL-RUN",
            note="claimed during the run",
        )

        prod_pool.cmd_prod_pool_import_history(_args())

        claim = pool_repo.list_pool_claims()[0]
        assert claim.source is ClaimSource.PROD_UPLOADERS
        assert claim.deployment_name == "SOME-REAL-RUN"


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


class TestCatalogueMissing:
    """Uploads that happened before the pool existed.

    A file uploaded by a hand-written URL list is on the network but unknown to
    the pool, so a later bulk-add of the same source would hand it out again.
    The registry already holds its URL, name, byte size and datamap address, so
    the entry can be created and marked spent without touching the network.
    """

    def _deployment(self, session, name, kind):
        deployment = Deployment(name=name, deploy_args={"kind": kind})
        session.add(deployment)
        session.commit()
        return deployment

    def test_creates_the_entry_and_marks_it_spent(self, wired):
        pool_repo, _, session = wired
        deployment = self._deployment(session, "PROD-CANARY-03", "prod-canary")
        _canary_upload(session, deployment, "clip.avi")

        prod_pool.cmd_prod_pool_import_history(_args(catalogue_missing=True, tag=["nsia-911"]))

        rows = pool_repo.list_pool_files(active=None)
        assert len(rows) == 1
        entry = rows[0]
        assert entry.kind is PoolFileKind.URL
        assert entry.locator == "https://archive.org/download/i/clip.avi"
        assert entry.size_bytes == 50 * MIB
        assert entry.tags == ["nsia-911"]

        claim = pool_repo.open_claim_for_file(entry.id)
        assert claim.status is ClaimStatus.UPLOADED
        assert claim.source is ClaimSource.PROD_CANARY
        # The address is the point: a canary report names the file when its
        # chunks go missing, and that needs the datamap carried across.
        assert claim.address == "caddr1"
        assert claim.deployment_name == "PROD-CANARY-03"

    def test_catalogued_files_cannot_be_handed_out(self, wired):
        from saorsa_db.repositories import InsufficientPoolInventory
        from saorsa_db.size_class import SizeClass

        pool_repo, _, session = wired
        deployment = self._deployment(session, "PROD-CANARY-03", "prod-canary")
        _canary_upload(session, deployment, "clip.avi")
        prod_pool.cmd_prod_pool_import_history(_args(catalogue_missing=True))

        with pytest.raises(InsufficientPoolInventory):
            pool_repo.claim_pool_files(
                PoolFileKind.URL, {SizeClass.SMALL: 1}, ClaimSource.PROD_CANARY
            )

    def test_without_the_flag_nothing_is_created(self, wired, capsys):
        pool_repo, _, session = wired
        deployment = self._deployment(session, "PROD-CANARY-03", "prod-canary")
        _canary_upload(session, deployment, "clip.avi")

        prod_pool.cmd_prod_pool_import_history(_args())

        assert pool_repo.list_pool_files(active=None) == []
        assert "unmatched" in _flat(capsys.readouterr().out)

    def test_dry_run_creates_nothing(self, wired, capsys):
        pool_repo, _, session = wired
        deployment = self._deployment(session, "PROD-CANARY-03", "prod-canary")
        _canary_upload(session, deployment, "clip.avi")

        prod_pool.cmd_prod_pool_import_history(_args(catalogue_missing=True, dry_run=True))
        assert pool_repo.list_pool_files(active=None) == []
        assert "catalogued" in _flat(capsys.readouterr().out)

    def test_deployment_scopes_the_import(self, wired, capsys):
        pool_repo, _, session = wired
        wanted = self._deployment(session, "PROD-CANARY-03", "prod-canary")
        other = self._deployment(session, "PROD-CANARY-02", "prod-canary")
        _canary_upload(session, wanted, "keep.avi")
        _canary_upload(session, other, "skip.avi", address="other")

        prod_pool.cmd_prod_pool_import_history(
            _args(catalogue_missing=True, deployment="PROD-CANARY-03")
        )

        names = {f.name for f in pool_repo.list_pool_files(active=None)}
        assert names == {"keep.avi"}

    def test_unknown_deployment_is_rejected(self, wired, capsys):
        pool_repo, _, session = wired
        deployment = self._deployment(session, "PROD-CANARY-03", "prod-canary")
        _canary_upload(session, deployment, "clip.avi")

        with pytest.raises(SystemExit):
            prod_pool.cmd_prod_pool_import_history(_args(catalogue_missing=True, deployment="NOPE"))
        assert "no deployment named" in _flat(capsys.readouterr().out)

    def test_a_local_upload_becomes_a_local_entry(self, wired):
        # prod-local-uploads records an absolute path as its source_url, so the
        # kind must follow the locator rather than being assumed.
        pool_repo, _, session = wired
        deployment = self._deployment(session, "PROD-LOCAL-UL-01", "prod-local-uploads")
        session.add(
            DeploymentProdUploadResult(
                deployment_id=deployment.id,
                client_hostname="h",
                region="r",
                source_url="/mnt/data/movie.wmv",
                file_name="movie.wmv",
                file_size_bytes=50 * MIB,
                status=ProdUploadStatus.SUCCESS,
                address="a",
                started_at=NOW,
                finished_at=NOW,
            )
        )
        session.commit()

        prod_pool.cmd_prod_pool_import_history(_args(catalogue_missing=True))
        entry = pool_repo.list_pool_files(active=None)[0]
        assert entry.kind is PoolFileKind.LOCAL

    def test_rerunning_does_not_duplicate(self, wired):
        pool_repo, _, session = wired
        deployment = self._deployment(session, "PROD-CANARY-03", "prod-canary")
        _canary_upload(session, deployment, "clip.avi")

        prod_pool.cmd_prod_pool_import_history(_args(catalogue_missing=True))
        prod_pool.cmd_prod_pool_import_history(_args(catalogue_missing=True))

        assert len(pool_repo.list_pool_files(active=None)) == 1
        assert len(pool_repo.list_pool_claims()) == 1


class TestPercentEncodedNames:
    """V2-1058, the second call site.

    The uploader records the raw percent-encoded URL basename while the pool
    holds the decoded name, so a name-only match misses every file with a
    space or `#`. Here the miss was worse than a stuck claim: under
    --catalogue-missing it made the command catalogue a *second* pool entry
    for content the pool already held, splitting one file's history in two and
    leaving the duplicate available for a canary to draw.
    """

    def test_an_encoded_name_matches_the_existing_entry_by_locator(self, wired, capsys):
        pool_repo, _, session = wired
        locator = "https://example.org/NBC%20Atlanta1%2083.avi"
        pool_file, _ = pool_repo.add_pool_file(
            PoolFileKind.URL, locator, "NBC Atlanta1 83.avi", size_bytes=50 * MIB
        )
        deployment = _deployment(session, "STG-UL-01")
        _upload_result(session, deployment, "NBC%20Atlanta1%2083.avi")

        prod_pool.cmd_prod_pool_import_history(_args())

        claims = pool_repo.list_pool_claims()
        assert len(claims) == 1
        assert claims[0].file_id == pool_file.id
        assert claims[0].status is ClaimStatus.UPLOADED
        out = _flat(capsys.readouterr().out)
        assert "imported 1" in out
        assert "unmatched 0" in out

    def test_catalogue_missing_does_not_duplicate_an_encoded_name(self, wired, capsys):
        # The regression that matters: one file must not become two rows.
        pool_repo, _, session = wired
        locator = "https://example.org/NBC%20Atlanta1%2083.avi"
        pool_repo.add_pool_file(
            PoolFileKind.URL, locator, "NBC Atlanta1 83.avi", size_bytes=50 * MIB
        )
        deployment = _deployment(session, "STG-UL-01")
        _upload_result(session, deployment, "NBC%20Atlanta1%2083.avi")

        prod_pool.cmd_prod_pool_import_history(_args(catalogue_missing=True))

        assert len(pool_repo.list_pool_files(active=None)) == 1
        assert "catalogued" not in _flat(capsys.readouterr().out)
