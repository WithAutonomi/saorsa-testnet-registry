"""Tests for claims closing themselves when `record-results` runs.

The gap this fills is the one that motivated the teardown guard: a remote
upload's results only reach Postgres when `record-results` is run, so between
the run finishing and that command, every claim sits open. Closing them here is
what keeps the open-claims list meaningful — if it never emptied, the operator
would learn to ignore it.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from saorsa_db.db import Base
from saorsa_db.models.deployment import Deployment
from saorsa_db.models.prod_canary import (
    DeploymentProdCanaryUpload,
    ProdCanaryInitialVerifyStatus,
)
from saorsa_db.models.prod_file_pool import (
    ClaimSource,
    ClaimStatus,
    PoolFileKind,
    ProdFilePoolClaim,
)
from saorsa_db.models.prod_upload_result import (
    DeploymentProdUploadResult,
    ProdUploadStatus,
)
from saorsa_db.repositories import (
    SqlAlchemyDeploymentRepository,
    SqlAlchemyProdFilePoolRepository,
)
from saorsa_db.size_class import SizeClass
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy.cmd.record_results import _resolve_pool_claims

MIB = 1024 * 1024
NOW = datetime(2026, 7, 30, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    pool_repo = SqlAlchemyProdFilePoolRepository(session)
    deployment_repo = SqlAlchemyDeploymentRepository(session)
    monkeypatch.setattr("saorsa_deploy.registry.get_file_pool_repository", lambda: pool_repo)
    deployment = Deployment(name="PROD-UL-05", deploy_args={})
    session.add(deployment)
    session.commit()
    yield pool_repo, deployment_repo, session, deployment
    session.close()


def _flat(text):
    return " ".join(text.split())


def _pooled_claim(pool_repo, deployment, name="a.bin", source=ClaimSource.PROD_UPLOADERS):
    pool_file, _ = pool_repo.add_pool_file(
        PoolFileKind.URL, f"https://example.org/{name}", name, size_bytes=50 * MIB
    )
    claim = pool_repo.claim_pool_files(
        PoolFileKind.URL,
        {SizeClass.SMALL: 1},
        source,
        deployment_id=deployment.id,
        deployment_name=deployment.name,
    )[0]
    return pool_file, claim


class TestProdUploaders:
    def test_claim_is_closed_with_address_and_costs(self, env, capsys):
        from rich.console import Console

        pool_repo, deployment_repo, session, deployment = env
        pool_file, claim = _pooled_claim(pool_repo, deployment)
        session.add(
            DeploymentProdUploadResult(
                deployment_id=deployment.id,
                client_hostname="host-1",
                region="lon1",
                source_url="https://example.org/a.bin",
                file_name="a.bin",
                file_size_bytes=50 * MIB,
                status=ProdUploadStatus.SUCCESS,
                address="a7f3c2",
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

        _resolve_pool_claims(deployment_repo, deployment, "prod-uploaders", Console())

        resolved = pool_repo.list_pool_claims()[0]
        assert resolved.status is ClaimStatus.UPLOADED
        assert resolved.address == "a7f3c2"
        assert resolved.total_chunks == 77
        assert resolved.storage_cost_atto == Decimal("419203847182734")

    def test_unmatched_claim_stays_open_and_is_reported(self, env, capsys):
        # A handed-out file with no recorded upload is exactly the state the
        # operator needs to see, not have assumed away.
        from rich.console import Console

        pool_repo, deployment_repo, session, deployment = env
        _pooled_claim(pool_repo, deployment)

        _resolve_pool_claims(deployment_repo, deployment, "prod-uploaders", Console())

        assert pool_repo.list_pool_claims()[0].status is ClaimStatus.CLAIMED
        out = _flat(capsys.readouterr().out)
        assert "1 file-pool claim(s) have no matching upload result" in out
        assert "prod pool claims" in out

    def test_partial_upload_is_recorded_as_partial_not_left_open(self, env, capsys):
        # Leaving it open reported "no matching upload result", which reads as
        # "the run never touched this file" — the opposite of what happened.
        from rich.console import Console

        pool_repo, deployment_repo, session, deployment = env
        pool_file, _ = _pooled_claim(pool_repo, deployment)
        session.add(
            DeploymentProdUploadResult(
                deployment_id=deployment.id,
                client_hostname="host-1",
                region="lon1",
                source_url="https://example.org/a.bin",
                file_name="a.bin",
                file_size_bytes=50 * MIB,
                status=ProdUploadStatus.PARTIAL_UPLOAD,
                address="a7f3c2",
                chunks_stored=342,
                chunks_failed=58,
                total_chunks=400,
                started_at=NOW,
                finished_at=NOW,
            )
        )
        session.commit()

        _resolve_pool_claims(deployment_repo, deployment, "prod-uploaders", Console())

        claim = pool_repo.list_pool_claims()[0]
        assert claim.status is ClaimStatus.PARTIAL
        # How far it got is kept, not just that it did not finish.
        assert claim.chunks_stored == 342
        assert claim.total_chunks == 400
        out = " ".join(capsys.readouterr().out.split())
        assert "uploaded only partially" in out
        assert "excluded from canary selection" in out

    def test_partial_returns_the_file_to_the_pool(self, env):
        from rich.console import Console
        from saorsa_db.repositories import POOL_STATUS_AVAILABLE

        pool_repo, deployment_repo, session, deployment = env
        pool_file, _ = _pooled_claim(pool_repo, deployment)
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
        _resolve_pool_claims(deployment_repo, deployment, "prod-uploaders", Console())
        available = pool_repo.list_pool_files(status=POOL_STATUS_AVAILABLE)
        assert pool_file.id in {f.id for f in available}

    def test_a_success_beats_a_partial_for_the_same_file(self, env):
        # Two attempts, one of which finished: the file IS on the network.
        from rich.console import Console

        pool_repo, deployment_repo, session, deployment = env
        _pooled_claim(pool_repo, deployment)
        for status, host in (
            (ProdUploadStatus.PARTIAL_UPLOAD, "host-1"),
            (ProdUploadStatus.SUCCESS, "host-2"),
        ):
            session.add(
                DeploymentProdUploadResult(
                    deployment_id=deployment.id,
                    client_hostname=host,
                    region="lon1",
                    source_url="https://example.org/a.bin",
                    file_name="a.bin",
                    file_size_bytes=50 * MIB,
                    status=status,
                    address="a7f3c2",
                    started_at=NOW,
                    finished_at=NOW,
                )
            )
        session.commit()

        _resolve_pool_claims(deployment_repo, deployment, "prod-uploaders", Console())
        assert pool_repo.list_pool_claims()[0].status is ClaimStatus.UPLOADED

    def test_rerunning_is_idempotent_and_can_revise(self, env):
        # record-results is re-runnable and a later pass may carry revised
        # costs, so resolution upserts rather than inserting once.
        from rich.console import Console

        pool_repo, deployment_repo, session, deployment = env
        _pooled_claim(pool_repo, deployment)
        row = DeploymentProdUploadResult(
            deployment_id=deployment.id,
            client_hostname="host-1",
            region="lon1",
            source_url="https://example.org/a.bin",
            file_name="a.bin",
            file_size_bytes=50 * MIB,
            status=ProdUploadStatus.SUCCESS,
            address="a7f3c2",
            chunks_stored=70,
            total_chunks=77,
            started_at=NOW,
            finished_at=NOW,
        )
        session.add(row)
        session.commit()

        _resolve_pool_claims(deployment_repo, deployment, "prod-uploaders", Console())
        row.chunks_stored = 77
        session.commit()
        _resolve_pool_claims(deployment_repo, deployment, "prod-uploaders", Console())

        claims = pool_repo.list_pool_claims()
        assert len(claims) == 1
        assert claims[0].chunks_stored == 77


class TestProdCanary:
    def test_reads_the_canary_table_with_its_own_column_names(self, env):
        # address/top_address and total_chunks/num_chunks describe the same
        # things under different names, which is why each kind needs its own
        # reader rather than one generic path.
        import uuid

        from rich.console import Console

        pool_repo, deployment_repo, session, deployment = env
        _pooled_claim(pool_repo, deployment, source=ClaimSource.PROD_CANARY)
        session.add(
            DeploymentProdCanaryUpload(
                canary_id=uuid.uuid4(),
                deployment_id=deployment.id,
                source_url="https://example.org/a.bin",
                file_name="a.bin",
                file_size_bytes=50 * MIB,
                top_address="c4n4ry",
                num_chunks=42,
                uploaded_at=NOW,
                initial_verify_status=ProdCanaryInitialVerifyStatus.OK,
                storage_cost_atto=Decimal("999"),
            )
        )
        session.commit()

        _resolve_pool_claims(deployment_repo, deployment, "prod-canary", Console())

        resolved = pool_repo.list_pool_claims()[0]
        assert resolved.status is ClaimStatus.UPLOADED
        assert resolved.address == "c4n4ry"
        assert resolved.total_chunks == 42
        assert resolved.storage_cost_atto == Decimal("999")

    def test_an_already_uploaded_claim_is_not_read_again(self, env, monkeypatch):
        """A canary's upload rows never change, so re-resolving a spent claim
        buys nothing — and it cost a read and a commit per claim on every
        daily publisher run, which is the cost this skip removes."""
        import uuid

        from rich.console import Console

        pool_repo, deployment_repo, session, deployment = env
        _pooled_claim(pool_repo, deployment, source=ClaimSource.PROD_CANARY)
        session.add(
            DeploymentProdCanaryUpload(
                canary_id=uuid.uuid4(),
                deployment_id=deployment.id,
                source_url="https://example.org/a.bin",
                file_name="a.bin",
                file_size_bytes=50 * MIB,
                top_address="c4n4ry",
                num_chunks=42,
                uploaded_at=NOW,
                initial_verify_status=ProdCanaryInitialVerifyStatus.OK,
            )
        )
        session.commit()
        _resolve_pool_claims(deployment_repo, deployment, "prod-canary", Console())
        assert pool_repo.list_pool_claims()[0].status is ClaimStatus.UPLOADED

        resolved_again = []
        monkeypatch.setattr(
            pool_repo,
            "resolve_pool_claim",
            lambda *a, **k: resolved_again.append(a),
        )
        _resolve_pool_claims(deployment_repo, deployment, "prod-canary", Console())
        assert resolved_again == []

    def test_an_open_canary_claim_is_still_resolved_on_a_later_run(self, env):
        """The skip is on terminal claims only: an open one still closes."""
        import uuid

        from rich.console import Console

        pool_repo, deployment_repo, session, deployment = env
        _pooled_claim(pool_repo, deployment, source=ClaimSource.PROD_CANARY)
        _resolve_pool_claims(deployment_repo, deployment, "prod-canary", Console())
        assert pool_repo.list_pool_claims()[0].status is ClaimStatus.CLAIMED

        session.add(
            DeploymentProdCanaryUpload(
                canary_id=uuid.uuid4(),
                deployment_id=deployment.id,
                source_url="https://example.org/a.bin",
                file_name="a.bin",
                file_size_bytes=50 * MIB,
                top_address="c4n4ry",
                num_chunks=42,
                uploaded_at=NOW,
                initial_verify_status=ProdCanaryInitialVerifyStatus.OK,
            )
        )
        session.commit()
        _resolve_pool_claims(deployment_repo, deployment, "prod-canary", Console())
        assert pool_repo.list_pool_claims()[0].status is ClaimStatus.UPLOADED


class TestScoping:
    def test_other_kinds_are_left_alone(self, env):
        from rich.console import Console

        pool_repo, deployment_repo, session, deployment = env
        _pooled_claim(pool_repo, deployment)
        _resolve_pool_claims(deployment_repo, deployment, "prod-downloaders", Console())
        assert pool_repo.list_pool_claims()[0].status is ClaimStatus.CLAIMED

    def test_no_claims_is_a_noop(self, env, capsys):
        from rich.console import Console

        _, deployment_repo, _, deployment = env
        _resolve_pool_claims(deployment_repo, deployment, "prod-uploaders", Console())
        assert capsys.readouterr().out == ""

    def test_no_pool_repository_is_a_noop(self, env, monkeypatch):
        from rich.console import Console

        pool_repo, deployment_repo, _, deployment = env
        _pooled_claim(pool_repo, deployment)
        monkeypatch.setattr("saorsa_deploy.registry.get_file_pool_repository", lambda: None)
        _resolve_pool_claims(deployment_repo, deployment, "prod-uploaders", Console())
        assert pool_repo.list_pool_claims()[0].status is ClaimStatus.CLAIMED


class TestPercentEncodedNames:
    """V2-1058: the two sides spell the file name differently.

    An uploader records the raw percent-encoded URL basename
    (``NBC%20Atlanta1%2083.avi``); the pool catalogues the decoded name
    (``NBC Atlanta1 83.avi``). Matching on the name alone therefore missed
    every file containing a space or ``#`` — 195 of 378 claims on
    PROD-CANARY-04, and one per run on STG-UL-01 — leaving files that were on
    the network and paid for reported as "no matching upload result".

    The locator is the same string on both sides, so it is matched on first.
    """

    def _claim_for(self, pool_repo, deployment, *, locator, name):
        pool_repo.add_pool_file(PoolFileKind.URL, locator, name, size_bytes=50 * MIB)
        return pool_repo.claim_pool_files(
            PoolFileKind.URL,
            {SizeClass.SMALL: 1},
            ClaimSource.PROD_UPLOADERS,
            deployment_id=deployment.id,
            deployment_name=deployment.name,
        )[0]

    def _upload_row(self, deployment, *, locator, file_name):
        return DeploymentProdUploadResult(
            deployment_id=deployment.id,
            client_hostname="host-1",
            region="lon1",
            source_url=locator,
            file_name=file_name,
            file_size_bytes=50 * MIB,
            status=ProdUploadStatus.SUCCESS,
            address="a7f3c2",
            chunks_stored=63,
            chunks_failed=0,
            total_chunks=63,
            started_at=NOW,
            finished_at=NOW,
        )

    @pytest.mark.parametrize(
        "encoded_name, decoded_name",
        [
            ("NBC%20Atlanta1%2083.avi", "NBC Atlanta1 83.avi"),
            ("track%20%2312.mp3", "track #12.mp3"),
        ],
    )
    def test_a_space_or_hash_in_the_name_still_resolves(
        self, env, capsys, encoded_name, decoded_name
    ):
        from rich.console import Console

        pool_repo, deployment_repo, session, deployment = env
        locator = f"https://archive.org/download/item/{encoded_name}"
        self._claim_for(pool_repo, deployment, locator=locator, name=decoded_name)
        # The uploader records the encoded basename, never the pool's name.
        session.add(self._upload_row(deployment, locator=locator, file_name=encoded_name))
        session.commit()

        _resolve_pool_claims(deployment_repo, deployment, "prod-uploaders", Console())

        claim = pool_repo.list_pool_claims()[0]
        assert claim.status is ClaimStatus.UPLOADED
        assert claim.address == "a7f3c2"
        assert claim.total_chunks == 63
        assert "no matching upload result" not in _flat(capsys.readouterr().out)

    def test_a_genuinely_absent_upload_is_still_left_open(self, env, capsys):
        # The locator match must not become a way of guessing: a claim whose
        # file was never uploaded stays open, which is the whole point of the
        # open-claims list.
        from rich.console import Console

        pool_repo, deployment_repo, session, deployment = env
        self._claim_for(
            pool_repo,
            deployment,
            locator="https://archive.org/download/item/NBC%20Atlanta1%2083.avi",
            name="NBC Atlanta1 83.avi",
        )
        session.add(
            self._upload_row(
                deployment,
                locator="https://archive.org/download/item/something%20else.avi",
                file_name="something%20else.avi",
            )
        )
        session.commit()

        _resolve_pool_claims(deployment_repo, deployment, "prod-uploaders", Console())

        assert pool_repo.list_pool_claims()[0].status is ClaimStatus.CLAIMED
        assert "no matching upload result" in _flat(capsys.readouterr().out)

    def test_the_locator_actually_fetched_wins_over_the_canonical_row(self, env):
        # A claim attaches to the canonical file, but a duplicate group may be
        # reachable two ways and the run fetched exactly one of them. The
        # upload records the URL it used, so that is what must match.
        from rich.console import Console

        pool_repo, deployment_repo, session, deployment = env
        canonical = "https://archive.org/download/item/NBC%20Atlanta1%2083.avi"
        fetched = "https://mirror.example.org/NBC%20Atlanta1%2083.avi"
        claim = self._claim_for(
            pool_repo, deployment, locator=canonical, name="NBC Atlanta1 83.avi"
        )
        session.get(ProdFilePoolClaim, claim.id).locator_used = fetched
        session.add(
            self._upload_row(deployment, locator=fetched, file_name="NBC%20Atlanta1%2083.avi")
        )
        session.commit()

        _resolve_pool_claims(deployment_repo, deployment, "prod-uploaders", Console())
        assert pool_repo.list_pool_claims()[0].status is ClaimStatus.UPLOADED
