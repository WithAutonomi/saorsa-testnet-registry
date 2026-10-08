"""Tests for `prod pool list`, `show` and `stats`.

The distinction these views must never blur is claimed-versus-uploaded. A file
handed to the desktop application three weeks ago and never resolved is not on
the network, and a view that renders it as uploaded would make the pool lie
about what production holds.
"""

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from saorsa_db.db import Base
from saorsa_db.models.deployment import Deployment
from saorsa_db.models.prod_file_pool import ClaimSource, PoolFileKind
from saorsa_db.repositories import SqlAlchemyProdFilePoolRepository
from saorsa_db.size_class import SizeClass
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy.cmd import prod_pool

MIB = 1024 * 1024
GIB = 1024 * MIB
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


def _list_args(**kwargs):
    base = dict(
        status=None,
        kind=None,
        size=None,
        tag=None,
        since=None,
        limit=None,
        all=False,
        min_size=None,
        max_size=None,
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


def _seed(repo):
    made = {}
    made["tiny"], _ = repo.add_pool_file(
        PoolFileKind.URL, "https://example.org/t", "t.bin", size_bytes=5 * MIB
    )
    made["small"], _ = repo.add_pool_file(
        PoolFileKind.URL, "https://example.org/s", "s.bin", size_bytes=50 * MIB
    )
    made["local"], _ = repo.add_pool_file(
        PoolFileKind.LOCAL, "/data/l.bin", "l.bin", size_bytes=5 * MIB
    )
    return made


class TestListSizeBounds:
    """`list` takes the same bounds a manifest does, so inventory can be
    checked against the exact selection a run will make."""

    def test_min_size_narrows(self, wired, capsys):
        made = _seed(wired)
        prod_pool.cmd_prod_pool_list(_list_args(min_size="10MB"))
        out = capsys.readouterr().out
        assert f"#{made['small'].id}" in out
        assert f"#{made['tiny'].id}" not in out

    def test_max_size_is_exclusive(self, wired, capsys):
        made = _seed(wired)
        prod_pool.cmd_prod_pool_list(_list_args(max_size="50MB"))
        out = capsys.readouterr().out
        assert f"#{made['small'].id}" not in out
        assert f"#{made['tiny'].id}" in out

    def test_bounds_intersect_with_a_class(self, wired, capsys):
        _seed(wired)
        prod_pool.cmd_prod_pool_list(_list_args(size="tiny", min_size="10MB"))
        assert "No pool files match" in capsys.readouterr().out

    def test_unparseable_bound_reports_itself(self, wired, capsys):
        _seed(wired)
        with pytest.raises(SystemExit):
            prod_pool.cmd_prod_pool_list(_list_args(min_size="lots"))
        assert "could not parse size" in capsys.readouterr().out


class TestList:
    def test_renders_state_per_file(self, wired, capsys):
        made = _seed(wired)
        wired.claim_pool_files(
            PoolFileKind.URL,
            {SizeClass.TINY: 1},
            ClaimSource.MANUAL,
            label="desktop-app",
            when=NOW,
        )
        prod_pool.cmd_prod_pool_list(_list_args())
        out = capsys.readouterr().out
        assert "claimed" in out
        assert "available" in out
        assert "desktop-app" in out
        assert f"#{made['small'].id}" in out

    def test_status_uploaded_shows_the_address(self, wired, capsys):
        _seed(wired)
        claim = wired.claim_pool_files(
            PoolFileKind.URL, {SizeClass.TINY: 1}, ClaimSource.PROD_UPLOADERS
        )[0]
        wired.resolve_pool_claim(claim.id, address="a7f3c29c2")
        prod_pool.cmd_prod_pool_list(_list_args(status="uploaded"))
        out = capsys.readouterr().out
        assert "a7f3c29c2" in out

    def test_claimed_is_not_reported_as_uploaded(self, wired, capsys):
        # The distinction that matters most: an unresolved checkout is not
        # evidence that anything reached the network.
        _seed(wired)
        wired.claim_pool_files(PoolFileKind.URL, {SizeClass.TINY: 1}, ClaimSource.MANUAL)
        prod_pool.cmd_prod_pool_list(_list_args(status="uploaded"))
        assert "No pool files match" in capsys.readouterr().out

    def test_filters_by_kind_and_size(self, wired, capsys):
        made = _seed(wired)
        prod_pool.cmd_prod_pool_list(_list_args(kind="local"))
        out = capsys.readouterr().out
        assert f"#{made['local'].id}" in out
        assert f"#{made['tiny'].id}" not in out

    def test_unsized_entries_are_flagged_loudly(self, wired, capsys):
        wired.add_pool_file(
            PoolFileKind.URL, "https://example.org/stream", "stream", size_bytes=None
        )
        prod_pool.cmd_prod_pool_list(_list_args())
        out = capsys.readouterr().out
        assert "INVISIBLE to size-class selection" in out

    def test_retired_entries_hidden_unless_all(self, wired, capsys):
        made = _seed(wired)
        wired.set_pool_file_active(made["tiny"].id, False)
        prod_pool.cmd_prod_pool_list(_list_args())
        assert f"#{made['tiny'].id}" not in capsys.readouterr().out
        prod_pool.cmd_prod_pool_list(_list_args(all=True))
        assert f"#{made['tiny'].id}" in capsys.readouterr().out

    def test_bad_size_class_exits_cleanly(self, wired):
        with pytest.raises(SystemExit):
            prod_pool.cmd_prod_pool_list(_list_args(size="huge"))


class TestSince:
    def test_accepts_relative_durations(self):
        parsed = prod_pool._parse_since("7d")
        assert parsed is not None
        assert parsed < datetime.now(timezone.utc)
        assert parsed > datetime.now(timezone.utc) - timedelta(days=8)

    def test_accepts_iso_dates_and_assumes_utc(self):
        parsed = prod_pool._parse_since("2026-07-01")
        assert parsed.tzinfo is not None
        assert parsed.year == 2026 and parsed.month == 7

    def test_none_passes_through(self):
        assert prod_pool._parse_since(None) is None


class TestShow:
    def test_shows_upload_result_with_costs(self, wired, capsys):
        _seed(wired)
        deployment = Deployment(name="PROD-UL-05", deploy_args={})
        wired.session.add(deployment)
        wired.session.commit()
        claim = wired.claim_pool_files(
            PoolFileKind.URL,
            {SizeClass.TINY: 1},
            ClaimSource.PROD_UPLOADERS,
            deployment_id=deployment.id,
            deployment_name="PROD-UL-05",
        )[0]
        wired.resolve_pool_claim(
            claim.id,
            address="a7f3c2",
            chunks_stored=77,
            total_chunks=77,
            chunks_failed=0,
            storage_cost_atto=Decimal("419203847182734"),
            gas_cost_wei=Decimal("1310000000000"),
        )
        prod_pool.cmd_prod_pool_show(argparse.Namespace(file_id=claim.file_id, address=None))
        out = capsys.readouterr().out
        assert "a7f3c2" in out
        assert "77 stored / 77 total" in out
        # Both the human rendering and the stored integer, because the former
        # is lossy by design.
        assert "419203847182734 atto" in out
        assert "ANT" in out and "ETH" in out
        # Names are reused, so the run is only identifiable with its id.
        assert "PROD-UL-05" in out and "id=" in out

    def test_reverse_lookup_by_address(self, wired, capsys):
        _seed(wired)
        claim = wired.claim_pool_files(
            PoolFileKind.URL, {SizeClass.TINY: 1}, ClaimSource.PROD_UPLOADERS
        )[0]
        wired.resolve_pool_claim(claim.id, address="deadbeef")
        prod_pool.cmd_prod_pool_show(argparse.Namespace(file_id=None, address="deadbeef"))
        assert "t.bin" in capsys.readouterr().out

    def test_unknown_address_is_reported_not_crashed(self, wired, capsys):
        prod_pool.cmd_prod_pool_show(argparse.Namespace(file_id=None, address="nope"))
        assert "No pooled file recorded" in capsys.readouterr().out

    def test_missing_file_exits(self, wired):
        with pytest.raises(SystemExit):
            prod_pool.cmd_prod_pool_show(argparse.Namespace(file_id=999, address=None))

    def test_duplicate_link_is_surfaced(self, wired, capsys):
        original, _ = wired.add_pool_file(
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
        prod_pool.cmd_prod_pool_show(argparse.Namespace(file_id=mirror.id, address=None))
        out = capsys.readouterr().out
        assert f"duplicate of #{original.id}" in out
        assert "claiming either spends both" in out


class TestAttoRendering:
    def test_converts_by_ten_to_the_eighteen(self):
        assert prod_pool._atto_to_ant(Decimal("1000000000000000000")) == "1"
        assert prod_pool._atto_to_ant(Decimal("500000000000000000")) == "0.5"


class TestStats:
    def test_splits_by_kind_and_never_sums_them(self, wired, capsys):
        _seed(wired)
        prod_pool.cmd_prod_pool_stats(argparse.Namespace())
        out = capsys.readouterr().out
        assert "URL avail/total" in out
        assert "LOCAL avail/total" in out
        assert "serve prod uploaders and canaries" in out

    def test_reports_unsized_entries_separately(self, wired, capsys):
        wired.add_pool_file(PoolFileKind.URL, "https://example.org/s", "s", size_bytes=None)
        prod_pool.cmd_prod_pool_stats(argparse.Namespace())
        out = capsys.readouterr().out
        assert "cannot satisfy any size request" in out
