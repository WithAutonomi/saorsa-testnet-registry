"""Tests for the manual checkout flow — the desktop-application path.

This is the flow with no safety net: nothing in a GUI application can write to
this database, so the checkout itself is what spends the file, and the operator
is the only thing that can resolve it afterwards. The behaviours guarded here
are the ones that decide whether they can find that file again weeks later.
"""

import argparse
from datetime import datetime, timedelta, timezone

import pytest
from saorsa_db.db import Base
from saorsa_db.models.deployment import Deployment, DeploymentStatus
from saorsa_db.models.prod_file_pool import ClaimSource, ClaimStatus, PoolFileKind
from saorsa_db.repositories import SqlAlchemyProdFilePoolRepository
from saorsa_db.size_class import SizeClass
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy.cmd import prod_pool

MIB = 1024 * 1024
NOW = datetime(2026, 7, 30, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield SqlAlchemyProdFilePoolRepository(session)


@pytest.fixture
def wired(repo, monkeypatch):
    monkeypatch.setattr(prod_pool, "get_file_pool_repository", lambda: repo)
    return repo


def _flat(text: str) -> str:
    """Collapse whitespace in captured output.

    Rich wraps to the console width, so a phrase asserted on can arrive split
    across lines. Flattening keeps the assertions about *what was said* rather
    than about where the terminal happened to break it.
    """
    return " ".join(text.split())


def _checkout_args(**kwargs):
    base = dict(size="small", count=1, kind="local", consumer=None, reuse_open=False)
    base.update(kwargs)
    return argparse.Namespace(**base)


def _claims_args(**kwargs):
    base = dict(
        consumer=None,
        source=None,
        status=None,
        stale=None,
        include_deployments=False,
        unresolvable=False,
        all=False,
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


def _seed_local(repo, count=3, size=50 * MIB):
    made = []
    for i in range(count):
        pool_file, _ = repo.add_pool_file(
            PoolFileKind.LOCAL, f"/data/corpus/f{i}.tar", f"f{i}.tar", size_bytes=size
        )
        made.append(pool_file)
    return made


class TestCheckout:
    def test_claims_and_prints_the_path(self, wired, capsys):
        _seed_local(wired)
        prod_pool.cmd_prod_pool_checkout(_checkout_args(consumer="desktop-app"))
        out = capsys.readouterr().out
        assert "Claimed 1 file(s) for 'desktop-app'" in out
        # Which file is not asserted: selection shuffles within its candidate
        # window, so any of the seeded paths is a correct answer. What matters
        # is that the operator is told a concrete path they can open.
        assert any(f"/data/corpus/f{i}.tar" in out for i in range(3))
        assert "unavailable to every other consumer" in _flat(out)
        # The resolution hint is the whole point — an operator who never sees it
        # has no idea the file is now spent pending their action.
        assert "mark-uploaded" in out and "release" in out

    def test_defaults_to_local_and_manual_label(self, wired):
        _seed_local(wired)
        prod_pool.cmd_prod_pool_checkout(_checkout_args())
        claim = wired.list_pool_claims()[0]
        assert claim.label == "manual"
        assert claim.source is ClaimSource.MANUAL

    def test_checkout_spends_the_file(self, wired):
        _seed_local(wired, count=1)
        prod_pool.cmd_prod_pool_checkout(_checkout_args())
        assert wired.list_pool_files(status="available") == []

    def test_existing_claims_are_surfaced_but_do_not_block(self, wired, capsys):
        # Checking out several deliberately is normal, so this warns rather than
        # refusing — but a forgotten pile must not accumulate invisibly.
        _seed_local(wired, count=3)
        prod_pool.cmd_prod_pool_checkout(_checkout_args(consumer="desktop-app"))
        capsys.readouterr()
        prod_pool.cmd_prod_pool_checkout(_checkout_args(consumer="desktop-app"))
        out = _flat(capsys.readouterr().out)
        assert "has 1 unresolved claim(s)" in out
        assert "Claimed 1 file(s)" in out
        assert len(wired.list_pool_claims()) == 2

    def test_reuse_open_returns_the_existing_claim(self, wired, capsys):
        _seed_local(wired, count=3)
        prod_pool.cmd_prod_pool_checkout(_checkout_args(consumer="desktop-app"))
        capsys.readouterr()
        prod_pool.cmd_prod_pool_checkout(_checkout_args(consumer="desktop-app", reuse_open=True))
        out = capsys.readouterr().out
        assert "Reusing the open claim(s)" in out
        # No new inventory burned.
        assert len(wired.list_pool_claims()) == 1

    def test_reuse_open_with_nothing_held_says_so(self, wired, capsys):
        _seed_local(wired)
        prod_pool.cmd_prod_pool_checkout(_checkout_args(consumer="desktop-app", reuse_open=True))
        assert "no open claims to reuse" in capsys.readouterr().out

    def test_empty_inventory_exits_with_guidance(self, wired, capsys):
        with pytest.raises(SystemExit):
            prod_pool.cmd_prod_pool_checkout(_checkout_args())
        out = _flat(capsys.readouterr().out)
        assert "cannot satisfy" in out
        assert "prod pool add" in out

    def test_url_kind_can_be_requested(self, wired):
        wired.add_pool_file(PoolFileKind.URL, "https://example.org/a", "a", size_bytes=50 * MIB)
        prod_pool.cmd_prod_pool_checkout(_checkout_args(kind="url"))
        assert len(wired.list_pool_claims()) == 1


class TestClaimsView:
    def test_lists_open_claims_by_default(self, wired, capsys):
        _seed_local(wired)
        prod_pool.cmd_prod_pool_checkout(_checkout_args(consumer="desktop-app"))
        capsys.readouterr()
        prod_pool.cmd_prod_pool_claims(_claims_args())
        out = _flat(capsys.readouterr().out)
        assert "desktop-app" in out
        assert "need you" in out

    def test_deployment_claims_are_marked_as_self_resolving(self, wired, capsys):
        _seed_local(wired)
        wired.claim_pool_files(
            PoolFileKind.LOCAL,
            {SizeClass.SMALL: 1},
            ClaimSource.PROD_UPLOADERS,
            deployment_name="PROD-UL-05",
            when=NOW,
        )
        prod_pool.cmd_prod_pool_claims(_claims_args())
        out = _flat(capsys.readouterr().out)
        assert "will resolve on the next" in out
        assert "record-results" in out

    def test_stale_excludes_pending_deployment_claims(self, wired, capsys):
        # A deployment-backed claim awaiting a recording run is pending, not
        # forgotten. Listing it beside genuinely stale checkouts is how an
        # operator learns to ignore the list.
        _seed_local(wired, count=2)
        wired.claim_pool_files(
            PoolFileKind.LOCAL,
            {SizeClass.SMALL: 1},
            ClaimSource.PROD_UPLOADERS,
            deployment_name="PROD-UL-05",
            when=NOW - timedelta(days=30),
        )
        wired.claim_pool_files(
            PoolFileKind.LOCAL,
            {SizeClass.SMALL: 1},
            ClaimSource.MANUAL,
            label="desktop-app",
            when=NOW - timedelta(days=30),
        )
        prod_pool.cmd_prod_pool_claims(_claims_args(stale="7d"))
        out = capsys.readouterr().out
        assert "desktop-app" in out
        assert "PROD-UL-05" not in out

    def test_stale_can_include_deployment_claims(self, wired, capsys):
        _seed_local(wired, count=2)
        wired.claim_pool_files(
            PoolFileKind.LOCAL,
            {SizeClass.SMALL: 1},
            ClaimSource.PROD_UPLOADERS,
            deployment_name="PROD-UL-05",
            when=NOW - timedelta(days=30),
        )
        prod_pool.cmd_prod_pool_claims(_claims_args(stale="7d", include_deployments=True))
        assert "PROD-UL-05" in capsys.readouterr().out

    def test_unresolvable_explains_why(self, wired, capsys):
        deployment = Deployment(name="PROD-UL-05", deploy_args={})
        deployment.status = DeploymentStatus.DESTROYED
        wired.session.add(deployment)
        wired.session.commit()
        _seed_local(wired)
        wired.claim_pool_files(
            PoolFileKind.LOCAL,
            {SizeClass.SMALL: 1},
            ClaimSource.PROD_UPLOADERS,
            deployment_id=deployment.id,
            deployment_name="PROD-UL-05",
        )
        prod_pool.cmd_prod_pool_claims(_claims_args(unresolvable=True))
        assert "cannot resolve themselves" in _flat(capsys.readouterr().out)

    def test_no_claims_is_a_clean_message(self, wired, capsys):
        prod_pool.cmd_prod_pool_claims(_claims_args())
        assert "No matching claims" in capsys.readouterr().out

    def test_filters_by_consumer(self, wired, capsys):
        _seed_local(wired, count=2)
        prod_pool.cmd_prod_pool_checkout(_checkout_args(consumer="desktop-app"))
        prod_pool.cmd_prod_pool_checkout(_checkout_args(consumer="phone"))
        capsys.readouterr()
        prod_pool.cmd_prod_pool_claims(_claims_args(consumer="phone"))
        out = capsys.readouterr().out
        assert "phone" in out
        assert "desktop-app" not in out


class TestResolve:
    def test_mark_uploaded_records_the_address(self, wired, capsys):
        files = _seed_local(wired, count=1)
        prod_pool.cmd_prod_pool_checkout(_checkout_args(consumer="desktop-app"))
        capsys.readouterr()
        prod_pool.cmd_prod_pool_mark_uploaded(
            argparse.Namespace(
                file_id=files[0].id,
                address="a7f3c2",
                chunks_stored=10,
                chunks_failed=0,
                total_chunks=10,
                storage_cost_atto="12345",
                gas_cost_wei="678",
                note=None,
            )
        )
        assert "Recorded upload" in capsys.readouterr().out
        claim = wired.list_pool_claims()[0]
        assert claim.status is ClaimStatus.UPLOADED
        assert claim.address == "a7f3c2"

    def test_mark_uploaded_reports_duplicates_also_spent(self, wired, capsys):
        local, _ = wired.add_pool_file(
            PoolFileKind.LOCAL,
            "/data/f",
            "f",
            size_bytes=50 * MIB,
            digests={"sha1": "aaa"},
        )
        mirror, _ = wired.add_pool_file(
            PoolFileKind.URL,
            "https://example.org/f",
            "f",
            size_bytes=50 * MIB,
            digests={"sha1": "aaa"},
        )
        prod_pool.cmd_prod_pool_checkout(_checkout_args())
        capsys.readouterr()
        prod_pool.cmd_prod_pool_mark_uploaded(
            argparse.Namespace(
                file_id=local.id,
                address="a7f3c2",
                chunks_stored=None,
                chunks_failed=None,
                total_chunks=None,
                storage_cost_atto=None,
                gas_cost_wei=None,
                note=None,
            )
        )
        out = _flat(capsys.readouterr().out)
        assert f"#{mirror.id}" in out
        assert "same content" in out

    def test_non_numeric_cost_is_rejected(self, wired, capsys):
        files = _seed_local(wired, count=1)
        prod_pool.cmd_prod_pool_checkout(_checkout_args())
        with pytest.raises(SystemExit):
            prod_pool.cmd_prod_pool_mark_uploaded(
                argparse.Namespace(
                    file_id=files[0].id,
                    address="a",
                    chunks_stored=None,
                    chunks_failed=None,
                    total_chunks=None,
                    storage_cost_atto="0.004 ANT",
                    gas_cost_wei=None,
                    note=None,
                )
            )
        assert "atto/wei, not ANT/ETH" in _flat(capsys.readouterr().out)

    def test_release_returns_the_file(self, wired, capsys):
        files = _seed_local(wired, count=1)
        prod_pool.cmd_prod_pool_checkout(_checkout_args())
        capsys.readouterr()
        prod_pool.cmd_prod_pool_release(
            argparse.Namespace(file_ids=[files[0].id], note="never happened")
        )
        assert "available again" in capsys.readouterr().out
        assert len(wired.list_pool_files(status="available")) == 1

    def test_resolving_a_file_with_no_open_claim_exits(self, wired, capsys):
        files = _seed_local(wired, count=1)
        with pytest.raises(SystemExit):
            prod_pool.cmd_prod_pool_release(argparse.Namespace(file_ids=[files[0].id], note=None))
        assert "has no open claim" in _flat(capsys.readouterr().out)

    def test_releasing_an_uploaded_file_is_refused(self, wired, capsys):
        files = _seed_local(wired, count=1)
        prod_pool.cmd_prod_pool_checkout(_checkout_args())
        prod_pool.cmd_prod_pool_mark_uploaded(
            argparse.Namespace(
                file_id=files[0].id,
                address="a",
                chunks_stored=None,
                chunks_failed=None,
                total_chunks=None,
                storage_cost_atto=None,
                gas_cost_wei=None,
                note=None,
            )
        )
        capsys.readouterr()
        # It is on the network; returning it to the pool would hand it out again.
        with pytest.raises(SystemExit):
            prod_pool.cmd_prod_pool_release(argparse.Namespace(file_ids=[files[0].id], note=None))


class TestHistory:
    def test_shows_the_full_timeline(self, wired, capsys):
        files = _seed_local(wired, count=1)
        prod_pool.cmd_prod_pool_checkout(_checkout_args(consumer="desktop-app"))
        prod_pool.cmd_prod_pool_release(argparse.Namespace(file_ids=[files[0].id], note="aborted"))
        prod_pool.cmd_prod_pool_checkout(_checkout_args(consumer="phone"))
        capsys.readouterr()
        prod_pool.cmd_prod_pool_history(argparse.Namespace(file_id=files[0].id))
        out = capsys.readouterr().out
        assert "desktop-app" in out
        assert "phone" in out
        assert "released" in out
        assert "aborted" in out

    def test_never_handed_out_says_so(self, wired, capsys):
        files = _seed_local(wired, count=1)
        prod_pool.cmd_prod_pool_history(argparse.Namespace(file_id=files[0].id))
        assert "never handed out" in capsys.readouterr().out

    def test_duplicate_points_at_canonical_history(self, wired, capsys):
        wired.add_pool_file(
            PoolFileKind.LOCAL,
            "/data/f",
            "f",
            size_bytes=MIB,
            digests={"sha1": "aaa"},
        )
        mirror, _ = wired.add_pool_file(
            PoolFileKind.URL,
            "https://example.org/f",
            "f",
            size_bytes=MIB,
            digests={"sha1": "aaa"},
        )
        prod_pool.cmd_prod_pool_history(argparse.Namespace(file_id=mirror.id))
        assert "canonical entry for this content" in _flat(capsys.readouterr().out)

    def test_unknown_file_exits(self, wired):
        with pytest.raises(SystemExit):
            prod_pool.cmd_prod_pool_history(argparse.Namespace(file_id=999))


class TestAgeFormatting:
    def test_renders_minutes_hours_and_days(self):
        assert prod_pool._format_age(NOW - timedelta(minutes=30), NOW) == "30m"
        assert prod_pool._format_age(NOW - timedelta(hours=6), NOW) == "6h"
        assert prod_pool._format_age(NOW - timedelta(days=19), NOW) == "19d"

    def test_naive_timestamps_are_treated_as_utc(self):
        # SQLite reads DateTime(timezone=True) back naive; age arithmetic has to
        # work on both backends.
        naive = (NOW - timedelta(hours=2)).replace(tzinfo=None)
        assert prod_pool._format_age(naive, NOW) == "2h"
