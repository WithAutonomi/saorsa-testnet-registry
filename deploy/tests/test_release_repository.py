"""Tests for the release channel-state repository.

The lifecycle is the whole point of this table, so most of these tests are
about what it *refuses*. A promoted release that quietly stopped being
promoted, or a dead train that came back to life without a recorded reason,
would make every later "what is on stable" answer untrustworthy.
"""

from datetime import datetime, timedelta, timezone

import pytest
from saorsa_db.db import Base
from saorsa_db.models.release import ReleaseOutcome
from saorsa_db.models.release_candidate import ReleaseCandidate
from saorsa_db.repositories import SqlAlchemyReleaseRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _utc(value):
    """SQLite stores DateTime(timezone=True) without the offset; Postgres keeps
    it. Normalise so these tests assert on the instant, not on the backend."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def repo(session):
    return SqlAlchemyReleaseRepository(session)


def _ship(repo, target="ant-node", version="0.17.1", **kwargs):
    base = dict(
        binary_target=target,
        version=version,
        rc_branch="rc-2026.8.2",
        beta_tag=f"v{version}-beta.1",
        beta_tag_sha="a" * 40,
        shipped_to_beta_at=NOW,
        planned_promotion_at=NOW + timedelta(days=5),
    )
    base.update(kwargs)
    return repo.ship(**base)


class TestShip:
    def test_records_a_beta_ship(self, repo):
        release = _ship(repo)

        assert release.outcome is ReleaseOutcome.SHIPPED_TO_BETA
        assert release.version == "0.17.1"
        assert release.beta_tag == "v0.17.1-beta.1"
        assert _utc(release.shipped_to_beta_at) == NOW
        assert release.promoted_at is None

    def test_defaults_shipped_at_to_now(self, repo):
        release = _ship(repo, shipped_to_beta_at=None)
        assert release.shipped_to_beta_at is not None

    def test_is_idempotent_on_binary_target_and_version(self, repo):
        first = _ship(repo)
        second = _ship(repo, beta_tag="v0.17.1-beta.2")

        assert second.id == first.id
        assert second.beta_tag == "v0.17.1-beta.2"
        assert len(repo.list()) == 1

    def test_a_respin_does_not_clear_fields_it_does_not_carry(self, repo):
        _ship(repo)
        again = repo.ship(
            binary_target="ant-node", version="0.17.1", rc_branch="rc-2026.8.2"
        )
        # The beta tag was recorded by the first ship and not re-supplied.
        assert again.beta_tag == "v0.17.1-beta.1"

    def test_same_version_on_a_different_binary_is_a_different_release(self, repo):
        _ship(repo, target="ant-node", version="0.17.1")
        _ship(repo, target="ant", version="0.17.1")
        assert len(repo.list()) == 2

    def test_refuses_to_reship_a_promoted_release(self, repo):
        release = _ship(repo)
        repo.promote(release.id)

        with pytest.raises(ValueError, match="terminal"):
            _ship(repo)

    def test_refuses_to_reship_a_rolled_past_release(self, repo):
        release = _ship(repo)
        repo.mark_rolled_past(release.id, reason="panics under load")

        with pytest.raises(ValueError, match="terminal"):
            _ship(repo)

    def test_links_the_release_candidate_row(self, repo, session):
        rc = ReleaseCandidate(
            binary_target="ant-node",
            binary_version="0.17.1-rc.1",
            rc_branch="rc-2026.8.2",
        )
        session.add(rc)
        session.commit()

        release = _ship(repo, release_candidate_id=rc.id)
        assert release.release_candidate_id == rc.id


class TestPromote:
    def test_promotes_a_shipped_release(self, repo):
        release = _ship(repo)
        promoted_at = NOW + timedelta(days=5)

        promoted = repo.promote(
            release.id,
            final_tag="v0.17.1",
            final_tag_sha="b" * 40,
            promoted_at=promoted_at,
        )

        assert promoted.outcome is ReleaseOutcome.PROMOTED
        assert promoted.final_tag == "v0.17.1"
        assert _utc(promoted.promoted_at) == promoted_at
        # The ship record survives the promotion — the soak is history worth keeping.
        assert promoted.beta_tag == "v0.17.1-beta.1"
        assert _utc(promoted.shipped_to_beta_at) == NOW

    def test_refuses_to_promote_a_rolled_past_release(self, repo):
        release = _ship(repo)
        repo.mark_rolled_past(release.id, reason="chunk loss regression")

        with pytest.raises(ValueError, match="rolled past"):
            repo.promote(release.id)

    def test_refuses_an_unknown_release(self, repo):
        with pytest.raises(ValueError, match="not found"):
            repo.promote(9999)


class TestRolledPast:
    def test_records_the_reason(self, repo):
        release = _ship(repo)
        rolled = repo.mark_rolled_past(release.id, reason="NO-PROMOTE: panics")

        assert rolled.outcome is ReleaseOutcome.ROLLED_PAST
        assert "NO-PROMOTE: panics" in rolled.notes

    def test_appends_to_existing_notes(self, repo):
        release = _ship(repo, notes="shipped late")
        rolled = repo.mark_rolled_past(release.id, reason="soak failed")

        assert "shipped late" in rolled.notes
        assert "soak failed" in rolled.notes

    def test_requires_a_reason(self, repo):
        release = _ship(repo)
        with pytest.raises(ValueError, match="record why"):
            repo.mark_rolled_past(release.id, reason="   ")

    def test_refuses_a_promoted_release(self, repo):
        release = _ship(repo)
        repo.promote(release.id)

        with pytest.raises(ValueError, match="on stable"):
            repo.mark_rolled_past(release.id, reason="mistake")


class TestCreatePromoted:
    def test_records_a_promotion_that_never_soaked(self, repo):
        release = repo.create_promoted(
            binary_target="ant-node",
            version="0.17.1",
            rc_branch="rc-2026.8.2",
            final_tag="v0.17.1",
            promoted_at=NOW,
        )

        assert release.outcome is ReleaseOutcome.PROMOTED
        # NULL, not a fabricated soak — these predate the beta structure.
        assert release.shipped_to_beta_at is None
        assert release.beta_tag is None

    def test_refuses_to_shadow_an_existing_release(self, repo):
        _ship(repo)
        with pytest.raises(ValueError, match="already recorded"):
            repo.create_promoted(
                binary_target="ant-node",
                version="0.17.1",
                rc_branch="rc-2026.8.2",
            )

    def test_current_reports_it_as_stable_with_no_beta(self, repo):
        repo.create_promoted(
            binary_target="ant-node",
            version="0.17.1",
            rc_branch="rc-2026.8.2",
            promoted_at=NOW,
        )

        state = repo.current()[0]
        assert state.stable.version == "0.17.1"
        assert state.beta is None
        assert state.soak_seconds is None
        assert state.soak_elapsed is None


class TestManifest:
    def test_records_the_crate_manifest(self, repo):
        release = _ship(repo)
        repo.set_crates(
            release.id,
            [
                {
                    "crate_name": "ant-cli",
                    "version": "0.3.3",
                    "repo_slug": "WithAutonomi/ant-client",
                    "tag": "ant-cli-v0.3.3",
                    "tag_sha": "c" * 40,
                },
                {
                    "crate_name": "ant-core",
                    "version": "0.6.0",
                    "repo_slug": "WithAutonomi/ant-client",
                },
            ],
        )

        crates = repo.get(release.id).crates
        assert [c.crate_name for c in crates] == ["ant-cli", "ant-core"]
        # ant-core is bumped with every release and tagged with none of them.
        assert crates[1].tag is None

    def test_replaces_rather_than_appends(self, repo):
        release = _ship(repo)
        repo.set_crates(
            release.id,
            [{"crate_name": "evmlib", "version": "0.9.0", "repo_slug": "x/y"}],
        )
        repo.set_crates(
            release.id,
            [{"crate_name": "evmlib", "version": "0.9.1", "repo_slug": "x/y"}],
        )

        crates = repo.get(release.id).crates
        assert len(crates) == 1
        assert crates[0].version == "0.9.1"

    def test_rejects_an_incomplete_crate(self, repo):
        release = _ship(repo)
        with pytest.raises(ValueError, match="repo_slug"):
            repo.set_crates(
                release.id, [{"crate_name": "evmlib", "version": "0.9.1"}]
            )

    def test_deleting_a_release_takes_its_manifest(self, repo, session):
        release = _ship(repo)
        repo.set_crates(
            release.id,
            [{"crate_name": "evmlib", "version": "0.9.1", "repo_slug": "x/y"}],
        )
        repo.delete(release.id)

        from saorsa_db.models.release import ReleaseCrate

        assert session.query(ReleaseCrate).count() == 0


class TestList:
    def test_filters_by_target_and_outcome(self, repo):
        node = _ship(repo, target="ant-node", version="0.17.1")
        _ship(repo, target="ant", version="0.3.3")
        repo.promote(node.id)

        assert len(repo.list()) == 2
        assert len(repo.list(binary_target="ant-node")) == 1
        assert len(repo.list(outcome=ReleaseOutcome.PROMOTED)) == 1
        assert len(repo.list(outcome=ReleaseOutcome.SHIPPED_TO_BETA)) == 1

    def test_applies_the_limit(self, repo):
        _ship(repo, version="0.17.1")
        _ship(repo, version="0.17.2")
        assert len(repo.list(limit=1)) == 1


class TestCurrent:
    def test_reports_stable_and_beta_per_target(self, repo):
        old = _ship(repo, version="0.17.0", shipped_to_beta_at=NOW - timedelta(days=7))
        repo.promote(old.id, final_tag="v0.17.0", promoted_at=NOW - timedelta(days=2))
        _ship(repo, version="0.17.1")

        states = repo.current()
        assert len(states) == 1
        state = states[0]
        assert state.binary_target == "ant-node"
        assert state.stable.version == "0.17.0"
        assert state.beta.version == "0.17.1"
        assert state.rolled_past == []

    def test_counts_only_betas_rolled_past_since_the_current_stable(self, repo):
        # An old rolled-past beta, then a promotion, then another rolled-past.
        ancient = _ship(repo, version="0.16.0", shipped_to_beta_at=NOW - timedelta(days=30))
        repo.mark_rolled_past(ancient.id, reason="old failure")

        stable = _ship(repo, version="0.17.0", shipped_to_beta_at=NOW - timedelta(days=20))
        repo.promote(stable.id, promoted_at=NOW - timedelta(days=15))

        recent = _ship(repo, version="0.17.1", shipped_to_beta_at=NOW - timedelta(days=8))
        repo.mark_rolled_past(recent.id, reason="chunk loss")

        state = repo.current()[0]
        assert state.stable.version == "0.17.0"
        # The pre-promotion failure is history, not part of the current jump window.
        assert [r.version for r in state.rolled_past] == ["0.17.1"]

    def test_soak_elapsed_is_none_when_no_deadline_was_recorded(self, repo):
        _ship(repo, planned_promotion_at=None)
        state = repo.current()[0]
        # Deliberately None, not False: "no deadline recorded" is a different
        # answer from "the deadline has not arrived".
        assert state.soak_elapsed is None
        assert state.soak_seconds is not None

    def test_soak_elapsed_flips_once_the_planned_date_passes(self, repo):
        _ship(repo, planned_promotion_at=NOW - timedelta(days=1))
        assert repo.current()[0].soak_elapsed is True

        repo.delete(repo.current()[0].beta.id)
        _ship(repo, planned_promotion_at=datetime.now(timezone.utc) + timedelta(days=3))
        assert repo.current()[0].soak_elapsed is False

    def test_reports_nothing_on_an_empty_registry(self, repo):
        assert repo.current() == []

    def test_a_target_with_no_promotion_has_no_stable(self, repo):
        _ship(repo)
        state = repo.current()[0]
        assert state.stable is None
        assert state.beta.version == "0.17.1"

    def test_filters_by_binary_target(self, repo):
        _ship(repo, target="ant-node", version="0.17.1")
        _ship(repo, target="ant", version="0.3.3")

        assert len(repo.current()) == 2
        assert len(repo.current(binary_target="ant")) == 1
