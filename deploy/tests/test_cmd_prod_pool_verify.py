"""Tests for `prod pool verify`.

The distinction this command must get right is "will not tell me its size"
versus "is gone". A URL that publishes no Content-Length is still perfectly
usable; retiring it because of that would quietly shrink the pool. A URL that
404s is not usable, and leaving it active means discovering the fact mid-deploy.
"""

import argparse

import pytest
from saorsa_db.db import Base
from saorsa_db.models.prod_file_pool import PoolFileKind
from saorsa_db.repositories import SqlAlchemyProdFilePoolRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy.cmd import prod_pool

MIB = 1024 * 1024


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


def _args(**kwargs):
    base = dict(kind=None, limit=None, rehash=True)
    base.update(kwargs)
    return argparse.Namespace(**base)


def _flat(text):
    return " ".join(text.split())


class TestUrlEntries:
    def test_unchanged_url_is_ok(self, wired, monkeypatch, capsys):
        wired.add_pool_file(PoolFileKind.URL, "https://example.org/a", "a", size_bytes=10 * MIB)
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: 10 * MIB)
        prod_pool.cmd_prod_pool_verify(_args())
        out = _flat(capsys.readouterr().out)
        assert "ok 1" in out
        assert "retired" not in out

    def test_changed_size_is_updated(self, wired, monkeypatch, capsys):
        pool_file, _ = wired.add_pool_file(
            PoolFileKind.URL, "https://example.org/a", "a", size_bytes=10 * MIB
        )
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: 11 * MIB)
        prod_pool.cmd_prod_pool_verify(_args())
        assert "size updated" in _flat(capsys.readouterr().out)
        assert wired.get_pool_file(pool_file.id).size_bytes == 11 * MIB

    def test_a_size_change_that_moves_class_is_called_out(self, wired, monkeypatch, capsys):
        # Moving between classes changes which requests can ever draw the file,
        # so it is worth more than a silent update.
        wired.add_pool_file(PoolFileKind.URL, "https://example.org/a", "a", size_bytes=9 * MIB)
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: 50 * MIB)
        prod_pool.cmd_prod_pool_verify(_args())
        out = _flat(capsys.readouterr().out)
        assert "now small, was tiny" in out

    def test_unreachable_url_is_retired(self, wired, monkeypatch, capsys):
        pool_file, _ = wired.add_pool_file(
            PoolFileKind.URL, "https://example.org/gone", "gone", size_bytes=MIB
        )
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: None)
        monkeypatch.setattr(prod_pool, "_url_reachable", lambda url: False)
        prod_pool.cmd_prod_pool_verify(_args())
        assert "retired" in _flat(capsys.readouterr().out)
        assert wired.get_pool_file(pool_file.id).active is False

    def test_reachable_url_without_content_length_is_kept(self, wired, monkeypatch, capsys):
        # The distinction that matters: silent about its size, but alive and
        # usable. Retiring it would shrink the pool for no reason.
        pool_file, _ = wired.add_pool_file(
            PoolFileKind.URL,
            "https://example.org/stream",
            "stream",
            size_bytes=None,
        )
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: None)
        monkeypatch.setattr(prod_pool, "_url_reachable", lambda url: True)
        prod_pool.cmd_prod_pool_verify(_args())
        assert wired.get_pool_file(pool_file.id).active is True
        assert "retired" not in _flat(capsys.readouterr().out)


class TestLocalEntries:
    def test_missing_file_is_retired(self, wired, tmp_path, capsys):
        pool_file, _ = wired.add_pool_file(
            PoolFileKind.LOCAL, str(tmp_path / "gone.bin"), "gone.bin", size_bytes=MIB
        )
        prod_pool.cmd_prod_pool_verify(_args())
        assert "missing" in _flat(capsys.readouterr().out)
        assert wired.get_pool_file(pool_file.id).active is False

    def test_resized_local_file_is_rehashed(self, wired, tmp_path):
        target = tmp_path / "a.bin"
        target.write_bytes(b"a" * 10)
        pool_file, _ = wired.add_pool_file(
            PoolFileKind.LOCAL,
            str(target),
            "a.bin",
            size_bytes=999,
            digests={"sha1": "stale"},
        )
        prod_pool.cmd_prod_pool_verify(_args())
        refreshed = wired.get_pool_file(pool_file.id)
        assert refreshed.size_bytes == 10
        # The stale sha1 is kept (one value per algorithm wins), but sha256 is
        # now present from the rehash.
        assert "sha256" in {d.algo for d in refreshed.digests}

    def test_no_rehash_skips_hashing(self, wired, tmp_path):
        target = tmp_path / "a.bin"
        target.write_bytes(b"a" * 10)
        pool_file, _ = wired.add_pool_file(PoolFileKind.LOCAL, str(target), "a.bin", size_bytes=999)
        prod_pool.cmd_prod_pool_verify(_args(rehash=False))
        assert wired.get_pool_file(pool_file.id).digests == []


class TestResidualRisk:
    def test_undigested_entries_are_reported(self, wired, monkeypatch, capsys):
        # Not hidden: these cannot be recognised as the same content as anything
        # else, so a URL and a local copy of one file could both be handed out.
        wired.add_pool_file(PoolFileKind.URL, "https://example.org/a", "a", size_bytes=MIB)
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: MIB)
        prod_pool.cmd_prod_pool_verify(_args())
        out = _flat(capsys.readouterr().out)
        assert "cannot be deduplicated against another entry before upload" in out
        assert "datamap address catches these after the fact" in out

    def test_digested_entries_are_not_flagged(self, wired, monkeypatch, capsys):
        wired.add_pool_file(
            PoolFileKind.URL,
            "https://example.org/a",
            "a",
            size_bytes=MIB,
            digests={"sha1": "aaa"},
        )
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: MIB)
        prod_pool.cmd_prod_pool_verify(_args())
        assert "no digest" not in _flat(capsys.readouterr().out)


class TestScoping:
    def test_empty_pool_says_so(self, wired, capsys):
        prod_pool.cmd_prod_pool_verify(_args())
        assert "Nothing to verify" in capsys.readouterr().out

    def test_kind_filter_applies(self, wired, tmp_path, monkeypatch, capsys):
        wired.add_pool_file(PoolFileKind.URL, "https://example.org/a", "a", size_bytes=MIB)
        wired.add_pool_file(PoolFileKind.LOCAL, str(tmp_path / "b.bin"), "b.bin", size_bytes=MIB)
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: MIB)
        prod_pool.cmd_prod_pool_verify(_args(kind="url"))
        out = _flat(capsys.readouterr().out)
        assert "Checking 1 entry" in out
