"""Tests for the `saorsa-deploy releases ...` commands.

These cover what the commands refuse as much as what they record: a promotion
with no ship record behind it, a rolled-past release with no reason, a
malformed manifest. Recording any of those would make `releases current`
answer confidently and wrongly.
"""

import argparse
import json
from datetime import datetime, timedelta, timezone

import pytest
from saorsa_db.db import Base
from saorsa_db.models.release import ReleaseOutcome
from saorsa_db.models.release_candidate import ReleaseCandidate
from saorsa_db.repositories import SqlAlchemyReleaseRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy.cmd.releases import (
    current_cmd,
    ls_cmd,
    promote_cmd,
    rolled_past_cmd,
    ship_cmd,
    show_cmd,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def repo(session):
    return SqlAlchemyReleaseRepository(session)


@pytest.fixture
def wired(repo, monkeypatch):
    """Each command module imports the accessor into its own namespace, so the
    patch target is the command module, not `_shared`."""
    for module in (
        ship_cmd,
        promote_cmd,
        rolled_past_cmd,
        current_cmd,
        ls_cmd,
        show_cmd,
    ):
        monkeypatch.setattr(module, "require_release_repository", lambda: repo)
    return repo


def _ship_args(**kwargs):
    base = dict(
        binary_target="ant-node",
        version="0.17.1",
        rc_branch="rc-2026.8.2",
        beta_tag="v0.17.1-beta.1",
        beta_tag_sha="a" * 40,
        shipped_at="2026-08-18",
        planned_promotion="2026-08-24",
        rc_version=None,
        notes=None,
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


def _seed_ship(repo, **kwargs):
    base = dict(
        binary_target="ant-node",
        version="0.17.1",
        rc_branch="rc-2026.8.2",
        beta_tag="v0.17.1-beta.1",
        shipped_to_beta_at=NOW,
        planned_promotion_at=NOW + timedelta(days=6),
    )
    base.update(kwargs)
    return repo.ship(**base)


class TestShip:
    def test_records_the_ship(self, wired, capsys):
        ship_cmd.cmd_releases_ship(_ship_args())

        out = capsys.readouterr().out
        assert "Shipped to beta" in out
        release = wired.get_by_version("ant-node", "0.17.1")
        assert release.outcome is ReleaseOutcome.SHIPPED_TO_BETA
        assert release.beta_tag == "v0.17.1-beta.1"

    def test_takes_a_bare_date_as_midnight_utc(self, wired):
        ship_cmd.cmd_releases_ship(_ship_args())
        release = wired.get_by_version("ant-node", "0.17.1")
        assert release.planned_promotion_at.date().isoformat() == "2026-08-24"

    def test_rejects_an_unparseable_timestamp(self, wired, capsys):
        with pytest.raises(SystemExit) as exc:
            ship_cmd.cmd_releases_ship(_ship_args(shipped_at="last tuesday"))
        assert exc.value.code == 2
        assert "not a valid ISO-8601" in capsys.readouterr().err

    def test_links_the_rc_row_by_natural_key(self, wired, session, capsys):
        rc = ReleaseCandidate(
            binary_target="ant-node",
            binary_version="0.17.1-rc.1",
            rc_branch="rc-2026.8.2",
        )
        session.add(rc)
        session.commit()

        ship_cmd.cmd_releases_ship(_ship_args(rc_version="0.17.1-rc.1"))

        release = wired.get_by_version("ant-node", "0.17.1")
        assert release.release_candidate_id == rc.id
        assert "Release candidate" in capsys.readouterr().out

    def test_a_missing_rc_row_warns_but_still_records(self, wired, capsys):
        ship_cmd.cmd_releases_ship(_ship_args(rc_version="0.17.1-rc.9"))

        out = capsys.readouterr().out
        assert "No release-candidate row found" in out
        release = wired.get_by_version("ant-node", "0.17.1")
        assert release is not None
        assert release.release_candidate_id is None

    def test_refuses_to_reship_a_terminal_release(self, wired, capsys):
        release = _seed_ship(wired)
        wired.promote(release.id)

        with pytest.raises(SystemExit) as exc:
            ship_cmd.cmd_releases_ship(_ship_args())
        assert exc.value.code == 2
        assert "terminal" in capsys.readouterr().err


def _promote_args(tmp_path, payload, allow_missing_ship=False):
    path = tmp_path / "promote.json"
    path.write_text(json.dumps(payload))
    return argparse.Namespace(from_file=str(path), allow_missing_ship=allow_missing_ship)


class TestPromote:
    def _payload(self, **kwargs):
        base = dict(
            binary_target="ant-node",
            version="0.17.1",
            final_tag="v0.17.1",
            final_tag_sha="b" * 40,
            promoted_at="2026-08-24T09:00:00Z",
            crates=[
                {
                    "crate_name": "ant-cli",
                    "version": "0.3.3",
                    "repo_slug": "WithAutonomi/ant-client",
                    "tag": "ant-cli-v0.3.3",
                    "tag_sha": "c" * 40,
                }
            ],
        )
        base.update(kwargs)
        return base

    def test_promotes_and_records_the_manifest(self, wired, tmp_path, capsys):
        _seed_ship(wired)
        promote_cmd.cmd_releases_promote(_promote_args(tmp_path, self._payload()))

        out = capsys.readouterr().out
        assert "Promoted to stable" in out
        assert "1 crates recorded" in out

        release = wired.get_by_version("ant-node", "0.17.1")
        assert release.outcome is ReleaseOutcome.PROMOTED
        assert release.final_tag == "v0.17.1"
        assert [c.crate_name for c in release.crates] == ["ant-cli"]

    def test_refuses_a_promotion_with_no_ship_record(self, wired, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            promote_cmd.cmd_releases_promote(_promote_args(tmp_path, self._payload()))
        assert exc.value.code == 1
        assert "has no ship record" in capsys.readouterr().err

    def test_refuses_a_rolled_past_release(self, wired, tmp_path, capsys):
        release = _seed_ship(wired)
        wired.mark_rolled_past(release.id, reason="soak failed")

        with pytest.raises(SystemExit) as exc:
            promote_cmd.cmd_releases_promote(_promote_args(tmp_path, self._payload()))
        assert exc.value.code == 2
        assert "rolled past" in capsys.readouterr().err

    def test_rejects_a_payload_missing_the_version(self, wired, tmp_path, capsys):
        payload = self._payload()
        del payload["version"]

        with pytest.raises(SystemExit) as exc:
            promote_cmd.cmd_releases_promote(_promote_args(tmp_path, payload))
        assert exc.value.code == 2
        assert "missing version" in capsys.readouterr().err

    def test_rejects_a_crate_missing_its_repo(self, wired, tmp_path, capsys):
        _seed_ship(wired)
        payload = self._payload(crates=[{"crate_name": "evmlib", "version": "0.9.1"}])

        with pytest.raises(SystemExit) as exc:
            promote_cmd.cmd_releases_promote(_promote_args(tmp_path, payload))
        assert exc.value.code == 2
        assert "repo_slug" in capsys.readouterr().err

    def test_allow_missing_ship_records_a_promotion_with_no_soak(self, wired, tmp_path, capsys):
        """Releases promoted under the pre-beta structure never soaked. The
        row must say that, not claim a soak of unknown length."""
        payload = self._payload(rc_branch="rc-2026.8.2")
        promote_cmd.cmd_releases_promote(_promote_args(tmp_path, payload, allow_missing_ship=True))

        out = capsys.readouterr().out
        assert "no beta soak" in out
        release = wired.get_by_version("ant-node", "0.17.1")
        assert release.outcome is ReleaseOutcome.PROMOTED
        assert release.shipped_to_beta_at is None
        assert release.rc_branch == "rc-2026.8.2"

    def test_allow_missing_ship_needs_the_rc_branch(self, wired, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            promote_cmd.cmd_releases_promote(
                _promote_args(tmp_path, self._payload(), allow_missing_ship=True)
            )
        assert exc.value.code == 2
        assert "needs 'rc_branch'" in capsys.readouterr().err

    def test_allow_missing_ship_still_promotes_an_existing_ship_normally(self, wired, tmp_path):
        _seed_ship(wired)
        promote_cmd.cmd_releases_promote(
            _promote_args(
                tmp_path,
                self._payload(rc_branch="rc-2026.8.2"),
                allow_missing_ship=True,
            )
        )

        release = wired.get_by_version("ant-node", "0.17.1")
        assert release.outcome is ReleaseOutcome.PROMOTED
        # The real soak is preserved, not overwritten by the no-soak path.
        assert release.shipped_to_beta_at is not None

    def test_rejects_malformed_json(self, wired, tmp_path, capsys):
        path = tmp_path / "bad.json"
        path.write_text("{not json")

        with pytest.raises(SystemExit) as exc:
            promote_cmd.cmd_releases_promote(
                argparse.Namespace(from_file=str(path), allow_missing_ship=False)
            )
        assert exc.value.code == 2
        assert "not valid JSON" in capsys.readouterr().err


class TestRolledPast:
    def _args(self, **kwargs):
        base = dict(
            id=None,
            binary_target="ant-node",
            version="0.17.1",
            reason="NO-PROMOTE: chunk loss regression",
        )
        base.update(kwargs)
        return argparse.Namespace(**base)

    def test_records_the_outcome_and_reason(self, wired, capsys):
        _seed_ship(wired)
        rolled_past_cmd.cmd_releases_rolled_past(self._args())

        assert "Rolled past" in capsys.readouterr().out
        release = wired.get_by_version("ant-node", "0.17.1")
        assert release.outcome is ReleaseOutcome.ROLLED_PAST
        assert "chunk loss regression" in release.notes

    def test_requires_an_identifier(self, wired, capsys):
        with pytest.raises(SystemExit) as exc:
            rolled_past_cmd.cmd_releases_rolled_past(self._args(binary_target=None, version=None))
        assert exc.value.code == 2
        assert "provide --id" in capsys.readouterr().err

    def test_reports_an_unknown_release(self, wired, capsys):
        with pytest.raises(SystemExit) as exc:
            rolled_past_cmd.cmd_releases_rolled_past(self._args())
        assert exc.value.code == 1
        assert "no release found" in capsys.readouterr().err

    def test_refuses_a_promoted_release(self, wired, capsys):
        release = _seed_ship(wired)
        wired.promote(release.id)

        with pytest.raises(SystemExit) as exc:
            rolled_past_cmd.cmd_releases_rolled_past(self._args())
        assert exc.value.code == 2
        assert "on stable" in capsys.readouterr().err


class TestCurrent:
    def test_reports_both_channels(self, wired, capsys):
        stable = _seed_ship(wired, version="0.17.0")
        wired.promote(stable.id, final_tag="v0.17.0", promoted_at=NOW)
        _seed_ship(wired, version="0.17.1")

        current_cmd.cmd_releases_current(argparse.Namespace(binary_target=None))

        out = capsys.readouterr().out
        assert "stable" in out and "0.17.0" in out
        assert "beta" in out and "0.17.1" in out

    def test_reports_the_widened_jump_window(self, wired, capsys):
        stable = _seed_ship(wired, version="0.17.0")
        wired.promote(stable.id, promoted_at=NOW - timedelta(days=20))
        rolled = _seed_ship(wired, version="0.17.1", shipped_to_beta_at=NOW - timedelta(days=8))
        wired.mark_rolled_past(rolled.id, reason="failed soak")
        _seed_ship(wired, version="0.17.2")

        current_cmd.cmd_releases_current(argparse.Namespace(binary_target=None))

        out = capsys.readouterr().out
        assert "rolled past" in out
        assert "N+2" in out

    def test_says_so_when_nothing_is_recorded(self, wired, capsys):
        current_cmd.cmd_releases_current(argparse.Namespace(binary_target=None))
        assert "No releases recorded" in capsys.readouterr().out


class TestLsAndShow:
    def test_ls_lists_releases_with_their_manifest(self, wired, capsys):
        release = _seed_ship(wired)
        wired.set_crates(
            release.id,
            [
                {
                    "crate_name": "ant-cli",
                    "version": "0.3.3",
                    "repo_slug": "WithAutonomi/ant-client",
                }
            ],
        )

        ls_cmd.cmd_releases_ls(argparse.Namespace(binary_target=None, outcome=None, limit=None))

        out = capsys.readouterr().out
        assert "0.17.1" in out
        assert "ant-cli" in out

    def test_ls_filters_by_outcome(self, wired, capsys):
        promoted = _seed_ship(wired, version="0.17.0")
        wired.promote(promoted.id)
        _seed_ship(wired, version="0.17.1")

        ls_cmd.cmd_releases_ls(
            argparse.Namespace(binary_target=None, outcome="promoted", limit=None)
        )

        out = capsys.readouterr().out
        assert "0.17.0" in out
        assert "0.17.1" not in out

    def test_ls_says_so_when_empty(self, wired, capsys):
        ls_cmd.cmd_releases_ls(argparse.Namespace(binary_target=None, outcome=None, limit=None))
        assert "No releases recorded" in capsys.readouterr().out

    def test_show_renders_the_release(self, wired, capsys):
        _seed_ship(wired)
        show_cmd.cmd_releases_show(
            argparse.Namespace(id=None, binary_target="ant-node", version="0.17.1")
        )

        out = capsys.readouterr().out
        assert "shipped_to_beta" in out
        assert "v0.17.1-beta.1" in out
        assert "No release manifest recorded" in out

    def test_show_requires_an_identifier(self, wired, capsys):
        with pytest.raises(SystemExit) as exc:
            show_cmd.cmd_releases_show(
                argparse.Namespace(id=None, binary_target=None, version=None)
            )
        assert exc.value.code == 2
        assert "provide --id" in capsys.readouterr().err

    def test_show_reports_an_unknown_release(self, wired, capsys):
        with pytest.raises(SystemExit) as exc:
            show_cmd.cmd_releases_show(
                argparse.Namespace(id=4242, binary_target=None, version=None)
            )
        assert exc.value.code == 1
        assert "no release found" in capsys.readouterr().err
