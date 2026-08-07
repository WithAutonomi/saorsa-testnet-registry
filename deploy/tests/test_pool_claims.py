"""Claim and selection tests for the production upload file pool.

Two behaviours here are the ones that would hurt if wrong. Selection must be
all-or-nothing — a canary asked for 350 files and quietly given 200 produces a
smaller experiment than the one designed, while having already burned the 200.
And a claim must never be resolved or released in a way that loses track of
whether the content reached the network.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from saorsa_db.db import Base
from saorsa_db.models.deployment import Deployment, DeploymentStatus
from saorsa_db.models.prod_file_pool import (
    ClaimSource,
    ClaimStatus,
    PoolFileKind,
    ProdFilePoolClaim,
)
from saorsa_db.repositories import (
    POOL_STATUS_AVAILABLE,
    InsufficientPoolInventory,
    SqlAlchemyProdFilePoolRepository,
)
from saorsa_db.size_class import SizeClass
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

MIB = 1024 * 1024
GIB = 1024 * MIB
NOW = datetime(2026, 7, 30, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield SqlAlchemyProdFilePoolRepository(session)


def _naive(value):
    """Drop tzinfo for comparison — see the note in the resolution test."""
    return value.replace(tzinfo=None) if value is not None else None


def _seed(repo, kind, size_bytes, count, prefix="f"):
    made = []
    for i in range(count):
        pool_file, _ = repo.add_pool_file(
            kind,
            f"https://example.org/{prefix}{i}"
            if kind is PoolFileKind.URL
            else f"/data/{prefix}{i}",
            f"{prefix}{i}.bin",
            size_bytes=size_bytes,
        )
        made.append(pool_file)
    return made


class TestClaiming:
    def test_claims_the_requested_counts_per_class(self, repo):
        _seed(repo, PoolFileKind.URL, 5 * MIB, 4, "tiny")
        _seed(repo, PoolFileKind.URL, 50 * MIB, 4, "small")
        claims = repo.claim_pool_files(
            PoolFileKind.URL,
            {SizeClass.TINY: 2, SizeClass.SMALL: 3},
            ClaimSource.PROD_UPLOADERS,
            when=NOW,
        )
        assert len(claims) == 5
        assert all(c.status is ClaimStatus.CLAIMED for c in claims)

    def test_claimed_files_leave_the_available_set(self, repo):
        _seed(repo, PoolFileKind.URL, 5 * MIB, 3, "t")
        repo.claim_pool_files(
            PoolFileKind.URL, {SizeClass.TINY: 2}, ClaimSource.MANUAL, when=NOW
        )
        remaining = repo.list_pool_files(status=POOL_STATUS_AVAILABLE)
        assert len(remaining) == 1

    def test_records_which_locator_was_used(self, repo):
        _seed(repo, PoolFileKind.URL, 5 * MIB, 1, "t")
        claim = repo.claim_pool_files(
            PoolFileKind.URL, {SizeClass.TINY: 1}, ClaimSource.MANUAL, when=NOW
        )[0]
        assert claim.locator_used == "https://example.org/t0"

    def test_kind_is_respected(self, repo):
        # A remote uploader cannot be handed a path on the operator's machine.
        _seed(repo, PoolFileKind.LOCAL, 5 * MIB, 5, "local")
        with pytest.raises(InsufficientPoolInventory):
            repo.claim_pool_files(
                PoolFileKind.URL, {SizeClass.TINY: 1}, ClaimSource.PROD_UPLOADERS
            )

    def test_zero_counts_are_ignored(self, repo):
        _seed(repo, PoolFileKind.URL, 5 * MIB, 1, "t")
        claims = repo.claim_pool_files(
            PoolFileKind.URL,
            {SizeClass.TINY: 1, SizeClass.XLARGE: 0},
            ClaimSource.MANUAL,
        )
        assert len(claims) == 1


class TestAllOrNothing:
    def test_shortfall_raises_with_per_class_detail(self, repo):
        _seed(repo, PoolFileKind.URL, 2 * GIB, 1, "lg")
        with pytest.raises(InsufficientPoolInventory) as exc:
            repo.claim_pool_files(
                PoolFileKind.URL, {SizeClass.LARGE: 2}, ClaimSource.PROD_CANARY
            )
        assert exc.value.shortfall == {
            "large": {"requested": 2, "available": 1}
        }
        assert "large 2 requested / 1 available" in str(exc.value)

    def test_nothing_is_claimed_when_a_later_class_falls_short(self, repo):
        # The case that would silently burn inventory: plenty of tiny files,
        # not enough large ones. The tiny files must survive the failed request.
        _seed(repo, PoolFileKind.URL, 5 * MIB, 10, "t")
        _seed(repo, PoolFileKind.URL, 2 * GIB, 1, "lg")
        with pytest.raises(InsufficientPoolInventory):
            repo.claim_pool_files(
                PoolFileKind.URL,
                {SizeClass.TINY: 5, SizeClass.LARGE: 2},
                ClaimSource.PROD_CANARY,
            )
        assert len(repo.list_pool_files(status=POOL_STATUS_AVAILABLE)) == 11
        assert repo.list_pool_claims() == []

    def test_empty_pool_reports_zero_available(self, repo):
        with pytest.raises(InsufficientPoolInventory) as exc:
            repo.claim_pool_files(
                PoolFileKind.URL, {SizeClass.MEDIUM: 3}, ClaimSource.MANUAL
            )
        assert exc.value.shortfall["medium"]["available"] == 0


class TestConcurrency:
    """The database is the arbiter, not the candidate query.

    Selection reads a window of available files and then inserts claims. Between
    those two steps another run can take one, so the insert is where the truth
    is settled — the partial unique index refuses, and the loser moves on to the
    next candidate rather than failing the whole request.
    """

    def test_a_candidate_taken_in_between_is_skipped(self, repo, monkeypatch):
        files = _seed(repo, PoolFileKind.URL, 5 * MIB, 3, "t")
        stolen = files[0]

        real_candidates = repo._candidates

        def stale_window(kind, size_class, needed, exclude_partial=False, **kwargs):
            candidates = real_candidates(kind, size_class, needed)
            # Simulate the race: another run claims the first candidate after
            # this window was read but before we insert.
            if repo.open_claim_for_file(stolen.id) is None:
                repo.session.add(
                    ProdFilePoolClaim(
                        file_id=stolen.id,
                        source=ClaimSource.MANUAL,
                        status=ClaimStatus.CLAIMED,
                        claimed_at=NOW,
                    )
                )
                repo.session.commit()
            # Return the now-stale window, stolen entry first.
            return [stolen] + [c for c in candidates if c.id != stolen.id]

        monkeypatch.setattr(repo, "_candidates", stale_window)

        claims = repo.claim_pool_files(
            PoolFileKind.URL, {SizeClass.TINY: 2}, ClaimSource.PROD_UPLOADERS
        )
        assert len(claims) == 2
        # The contested file is not double-claimed: exactly one claim exists on
        # it, and it is not one of ours.
        assert stolen.id not in {c.file_id for c in claims}
        assert len(repo.list_pool_claims(file_id=stolen.id)) == 1

    def test_losing_too_many_races_fails_without_claiming(self, repo, monkeypatch):
        _seed(repo, PoolFileKind.URL, 5 * MIB, 2, "t")

        def everything_taken(kind, size_class, needed, exclude_partial=False, **kwargs):
            candidates = repo.list_pool_files(
                kind=kind, size_class=size_class, active=True
            )
            for candidate in candidates:
                if repo.open_claim_for_file(candidate.id) is None:
                    repo.session.add(
                        ProdFilePoolClaim(
                            file_id=candidate.id,
                            source=ClaimSource.MANUAL,
                            status=ClaimStatus.CLAIMED,
                            claimed_at=NOW,
                        )
                    )
            repo.session.commit()
            return candidates

        monkeypatch.setattr(repo, "_candidates", everything_taken)

        with pytest.raises(InsufficientPoolInventory):
            repo.claim_pool_files(
                PoolFileKind.URL, {SizeClass.TINY: 2}, ClaimSource.PROD_UPLOADERS
            )
        # Only the simulated rival's claims exist; the failed request added none.
        assert all(
            c.source is ClaimSource.MANUAL for c in repo.list_pool_claims()
        )


class TestDuplicatesAreClaimedOnce:
    def test_claim_attaches_to_canonical_content(self, repo):
        local, _ = repo.add_pool_file(
            PoolFileKind.LOCAL,
            "/data/film.mkv",
            "film.mkv",
            size_bytes=500 * MIB,
            digests={"sha1": "aaa"},
        )
        mirror, _ = repo.add_pool_file(
            PoolFileKind.URL,
            "https://archive.org/download/i/film.mkv",
            "film.mkv",
            size_bytes=500 * MIB,
            digests={"sha1": "aaa"},
        )
        claim = repo.claim_pool_files(
            PoolFileKind.URL, {SizeClass.MEDIUM: 1}, ClaimSource.PROD_UPLOADERS
        )[0]
        # Claimed through the URL, recorded against the local canonical row —
        # the content, not the route to it.
        assert claim.file_id == local.id
        assert claim.locator_used == mirror.locator
        # And the local pool now has nothing left to give.
        with pytest.raises(InsufficientPoolInventory):
            repo.claim_pool_files(
                PoolFileKind.LOCAL, {SizeClass.MEDIUM: 1}, ClaimSource.MANUAL
            )


class TestResolution:
    def _one_claim(self, repo):
        _seed(repo, PoolFileKind.URL, 5 * MIB, 1, "t")
        return repo.claim_pool_files(
            PoolFileKind.URL, {SizeClass.TINY: 1}, ClaimSource.PROD_UPLOADERS, when=NOW
        )[0]

    def test_resolving_records_the_network_result(self, repo):
        claim = self._one_claim(repo)
        resolved = repo.resolve_pool_claim(
            claim.id,
            address="a7f3c2",
            chunks_stored=77,
            total_chunks=77,
            chunks_failed=0,
            storage_cost_atto=Decimal("419203847182734"),
            when=NOW + timedelta(minutes=3),
        )
        assert resolved.status is ClaimStatus.UPLOADED
        assert resolved.address == "a7f3c2"
        assert resolved.chunks_stored == 77
        # Compared without tzinfo: SQLite has no native timezone type, so a
        # DateTime(timezone=True) column reads back naive here. The instant is
        # correct, and Postgres preserves the offset.
        assert _naive(resolved.resolved_at) == _naive(NOW + timedelta(minutes=3))

    def test_resolution_is_an_upsert(self, repo):
        # record-results is idempotent and re-runnable; a second pass may carry
        # revised costs and must not create a second claim or a second file.
        claim = self._one_claim(repo)
        repo.resolve_pool_claim(claim.id, address="a7f3c2", chunks_stored=70)
        repo.resolve_pool_claim(claim.id, chunks_stored=77)
        assert len(repo.list_pool_claims()) == 1
        again = repo.list_pool_claims()[0]
        assert again.chunks_stored == 77
        # A field absent from the later pass keeps what the earlier one learned.
        assert again.address == "a7f3c2"

    def test_uploaded_file_cannot_be_reclaimed(self, repo):
        claim = self._one_claim(repo)
        repo.resolve_pool_claim(claim.id, address="a7f3c2")
        with pytest.raises(InsufficientPoolInventory):
            repo.claim_pool_files(
                PoolFileKind.URL, {SizeClass.TINY: 1}, ClaimSource.MANUAL
            )

    def test_released_claim_returns_the_file(self, repo):
        claim = self._one_claim(repo)
        repo.release_pool_claim(claim.id, note="run aborted")
        again = repo.claim_pool_files(
            PoolFileKind.URL, {SizeClass.TINY: 1}, ClaimSource.MANUAL
        )
        assert len(again) == 1

    def test_uploaded_claim_cannot_be_released(self, repo):
        # Releasing it would hand a file that is on the network back to the pool.
        claim = self._one_claim(repo)
        repo.resolve_pool_claim(claim.id, address="a7f3c2")
        with pytest.raises(ValueError, match="already uploaded"):
            repo.release_pool_claim(claim.id)

    def test_released_claim_cannot_be_resolved(self, repo):
        claim = self._one_claim(repo)
        repo.release_pool_claim(claim.id)
        with pytest.raises(ValueError, match="released"):
            repo.resolve_pool_claim(claim.id, address="a7f3c2")


class TestClaimListing:
    def test_filters_by_status_source_and_label(self, repo):
        _seed(repo, PoolFileKind.URL, 5 * MIB, 3, "t")
        manual = repo.claim_pool_files(
            PoolFileKind.URL,
            {SizeClass.TINY: 1},
            ClaimSource.MANUAL,
            label="desktop-app",
            when=NOW,
        )[0]
        repo.claim_pool_files(
            PoolFileKind.URL,
            {SizeClass.TINY: 1},
            ClaimSource.PROD_UPLOADERS,
            when=NOW,
        )
        assert [c.id for c in repo.list_pool_claims(label="desktop-app")] == [manual.id]
        assert len(repo.list_pool_claims(source=ClaimSource.PROD_UPLOADERS)) == 1
        assert len(repo.list_pool_claims(status=ClaimStatus.CLAIMED)) == 2

    def test_stale_filter_uses_claim_age(self, repo):
        _seed(repo, PoolFileKind.URL, 5 * MIB, 2, "t")
        old = repo.claim_pool_files(
            PoolFileKind.URL,
            {SizeClass.TINY: 1},
            ClaimSource.MANUAL,
            when=NOW - timedelta(days=19),
        )[0]
        repo.claim_pool_files(
            PoolFileKind.URL, {SizeClass.TINY: 1}, ClaimSource.MANUAL, when=NOW
        )
        stale = repo.list_pool_claims(stale_before=NOW - timedelta(days=7))
        assert [c.id for c in stale] == [old.id]

    def test_open_claim_for_file_follows_duplicates(self, repo):
        local, _ = repo.add_pool_file(
            PoolFileKind.LOCAL, "/data/a", "a", size_bytes=5 * MIB,
            digests={"sha1": "aaa"},
        )
        mirror, _ = repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/a", "a", size_bytes=5 * MIB,
            digests={"sha1": "aaa"},
        )
        claim = repo.claim_pool_files(
            PoolFileKind.LOCAL, {SizeClass.TINY: 1}, ClaimSource.MANUAL
        )[0]
        # Asking about either row must find the one claim on the content.
        assert repo.open_claim_for_file(local.id).id == claim.id
        assert repo.open_claim_for_file(mirror.id).id == claim.id


class TestUnresolvableClaims:
    def _deployment(self, repo, name, status):
        deployment = Deployment(name=name, deploy_args={})
        deployment.status = status
        repo.session.add(deployment)
        repo.session.commit()
        return deployment

    def test_destroyed_deployment_makes_a_claim_unresolvable(self, repo):
        # The deployment's InfluxDB went with it, so record-results can never
        # close this claim. It needs the operator, and saying so is the
        # difference between a wedged claim and a known one.
        destroyed = self._deployment(repo, "PROD-UL-05", DeploymentStatus.DESTROYED)
        running = self._deployment(repo, "PROD-UL-06", DeploymentStatus.RUNNING)
        _seed(repo, PoolFileKind.URL, 5 * MIB, 2, "t")
        doomed = repo.claim_pool_files(
            PoolFileKind.URL,
            {SizeClass.TINY: 1},
            ClaimSource.PROD_UPLOADERS,
            deployment_id=destroyed.id,
            deployment_name=destroyed.name,
        )[0]
        repo.claim_pool_files(
            PoolFileKind.URL,
            {SizeClass.TINY: 1},
            ClaimSource.PROD_UPLOADERS,
            deployment_id=running.id,
            deployment_name=running.name,
        )
        rows = repo.list_pool_claims(unresolvable=True)
        assert [c.id for c in rows] == [doomed.id]

    def test_manual_claims_are_never_unresolvable(self, repo):
        # A manual claim always had to be resolved by the operator, so it is not
        # "stuck" in the sense this filter means.
        _seed(repo, PoolFileKind.URL, 5 * MIB, 1, "t")
        repo.claim_pool_files(
            PoolFileKind.URL, {SizeClass.TINY: 1}, ClaimSource.MANUAL
        )
        assert repo.list_pool_claims(unresolvable=True) == []


class TestDeploymentBackfill:
    def test_deployment_is_attached_after_the_row_exists(self, repo):
        # Selection runs before any infrastructure, and record_deployment_created
        # always inserts a fresh row — so the id cannot be known at claim time.
        _seed(repo, PoolFileKind.URL, 5 * MIB, 2, "t")
        claims = repo.claim_pool_files(
            PoolFileKind.URL, {SizeClass.TINY: 2}, ClaimSource.PROD_UPLOADERS
        )
        assert all(c.deployment_id is None for c in claims)

        deployment = Deployment(name="PROD-UL-07", deploy_args={})
        repo.session.add(deployment)
        repo.session.commit()
        repo.attach_deployment_to_claims(
            [c.id for c in claims], deployment.id, "PROD-UL-07"
        )
        rows = repo.list_pool_claims(deployment_id=deployment.id)
        assert len(rows) == 2
        assert {c.deployment_name for c in rows} == {"PROD-UL-07"}

    def test_empty_backfill_is_a_noop(self, repo):
        repo.attach_deployment_to_claims([], 1, "X")
