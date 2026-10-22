"""Tests for `prod pool reactivate`.

`verify` reads only active rows, so a retirement is invisible to every later
run — remounting the volume does not bring the entries back with it, and before
this command the only route back was SQL against the production registry. The
property that keeps an undo safe is that it re-checks each entry itself: a dead
link stays retired however it is selected.
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


@pytest.fixture(autouse=True)
def wide_console(monkeypatch):
    """Keep rich from wrapping long locators, which assertions read back."""
    from rich.console import Console as RichConsole

    monkeypatch.setattr(prod_pool, "Console", lambda: RichConsole(width=300))


@pytest.fixture
def writes(repo, monkeypatch):
    """Record every `active` write, so "nothing was written" is testable."""
    recorded: list[tuple[int, bool]] = []
    original = repo.set_pool_file_active

    def record(file_id, value):
        recorded.append((file_id, value))
        return original(file_id, value)

    monkeypatch.setattr(repo, "set_pool_file_active", record)
    return recorded


def _args(**kwargs):
    base = dict(kind=None, ids=None, all=False, dry_run=False, verbose=False)
    base.update(kwargs)
    return argparse.Namespace(**base)


def _flat(text):
    return " ".join(text.split())


def _retired_local(repo, path, size_bytes=MIB):
    pool_file, _ = repo.add_pool_file(
        PoolFileKind.LOCAL, str(path), path.name, size_bytes=size_bytes
    )
    repo.set_pool_file_active(pool_file.id, False)
    return pool_file


def _retired_url(repo, url, name):
    pool_file, _ = repo.add_pool_file(PoolFileKind.URL, url, name, size_bytes=MIB)
    repo.set_pool_file_active(pool_file.id, False)
    return pool_file


class TestLocalEntries:
    def test_a_file_that_is_back_is_reactivated(self, wired, writes, tmp_path, capsys):
        target = tmp_path / "a.bin"
        target.write_bytes(b"a")
        pool_file = _retired_local(wired, target)
        writes.clear()
        prod_pool.cmd_prod_pool_reactivate(_args(all=True))
        out = _flat(capsys.readouterr().out)
        assert "reactivated 1" in out
        assert writes == [(pool_file.id, True)]
        assert wired.get_pool_file(pool_file.id).active is True

    def test_a_file_still_absent_is_left_inactive(self, wired, writes, tmp_path, capsys):
        pool_file = _retired_local(wired, tmp_path / "gone.bin")
        writes.clear()
        prod_pool.cmd_prod_pool_reactivate(_args(all=True))
        out = _flat(capsys.readouterr().out)
        assert "still missing 1" in out
        assert f"#{pool_file.id} gone.bin" in out
        assert writes == []
        assert wired.get_pool_file(pool_file.id).active is False

    def test_the_output_says_claims_were_never_touched(self, wired, tmp_path, capsys):
        # The state this restores is only the flag, so a file that was already
        # spent stays spent — which is what makes the undo safe to offer.
        target = tmp_path / "a.bin"
        target.write_bytes(b"a")
        _retired_local(wired, target)
        prod_pool.cmd_prod_pool_reactivate(_args(all=True))
        out = _flat(capsys.readouterr().out)
        assert "claims were never touched" in out
        assert "already spent stays spent" in out


class TestUrlEntries:
    def test_a_reachable_url_is_reactivated(self, wired, writes, monkeypatch, capsys):
        pool_file = _retired_url(wired, "https://example.org/a", "a")
        writes.clear()
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: MIB)
        prod_pool.cmd_prod_pool_reactivate(_args(all=True))
        assert "reactivated 1" in _flat(capsys.readouterr().out)
        assert writes == [(pool_file.id, True)]

    def test_a_url_that_answers_without_a_size_is_reactivated(
        self, wired, writes, monkeypatch, capsys
    ):
        # The same two signals `verify` retires on, read the other way round:
        # either one answering is enough to call the entry real.
        pool_file = _retired_url(wired, "https://example.org/stream", "stream")
        writes.clear()
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: None)
        monkeypatch.setattr(prod_pool, "_url_reachable", lambda url: True)
        prod_pool.cmd_prod_pool_reactivate(_args(all=True))
        assert writes == [(pool_file.id, True)]

    def test_a_dead_url_stays_retired(self, wired, writes, monkeypatch, capsys):
        pool_file = _retired_url(wired, "https://example.org/gone", "gone")
        writes.clear()
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: None)
        monkeypatch.setattr(prod_pool, "_url_reachable", lambda url: False)
        prod_pool.cmd_prod_pool_reactivate(_args(all=True))
        assert "still missing 1" in _flat(capsys.readouterr().out)
        assert writes == []
        assert wired.get_pool_file(pool_file.id).active is False


class TestSelection:
    def test_id_touches_only_those_entries(self, wired, writes, tmp_path, capsys):
        first = tmp_path / "first.bin"
        first.write_bytes(b"a")
        second = tmp_path / "second.bin"
        second.write_bytes(b"a")
        wanted = _retired_local(wired, first)
        other = _retired_local(wired, second)
        writes.clear()
        prod_pool.cmd_prod_pool_reactivate(_args(ids=[wanted.id]))
        assert writes == [(wanted.id, True)]
        assert wired.get_pool_file(other.id).active is False

    def test_kind_scopes_to_one_inventory(self, wired, writes, tmp_path, monkeypatch, capsys):
        target = tmp_path / "a.bin"
        target.write_bytes(b"a")
        local = _retired_local(wired, target)
        url = _retired_url(wired, "https://example.org/a", "a")
        writes.clear()
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: MIB)
        prod_pool.cmd_prod_pool_reactivate(_args(kind="local"))
        assert writes == [(local.id, True)]
        assert wired.get_pool_file(url.id).active is False

    def test_an_unknown_id_is_named(self, wired, writes, capsys):
        prod_pool.cmd_prod_pool_reactivate(_args(ids=[999]))
        assert "No pool entry with id #999" in _flat(capsys.readouterr().out)
        assert writes == []

    def test_a_bare_reactivate_refuses(self, wired, writes, capsys):
        # A whole-catalogue resurrection is not something to do by accident.
        with pytest.raises(SystemExit) as exit_info:
            prod_pool.cmd_prod_pool_reactivate(_args())
        assert exit_info.value.code != 0
        assert "Nothing selected" in _flat(capsys.readouterr().out)
        assert writes == []

    def test_an_already_active_entry_is_not_written_again(self, wired, writes, tmp_path, capsys):
        target = tmp_path / "a.bin"
        target.write_bytes(b"a")
        pool_file, _ = wired.add_pool_file(PoolFileKind.LOCAL, str(target), "a.bin", size_bytes=MIB)
        prod_pool.cmd_prod_pool_reactivate(_args(ids=[pool_file.id]))
        out = _flat(capsys.readouterr().out)
        assert "Nothing inactive to reactivate" in out
        assert "1 already active" in out
        assert writes == []

    def test_nothing_inactive_reports_zero(self, wired, capsys):
        prod_pool.cmd_prod_pool_reactivate(_args(kind="local"))
        assert "Nothing inactive to reactivate" in _flat(capsys.readouterr().out)


class TestDryRun:
    def test_dry_run_writes_nothing(self, wired, writes, tmp_path, capsys):
        target = tmp_path / "a.bin"
        target.write_bytes(b"a")
        pool_file = _retired_local(wired, target)
        writes.clear()
        prod_pool.cmd_prod_pool_reactivate(_args(all=True, dry_run=True))
        out = _flat(capsys.readouterr().out)
        assert "reactivated 1" in out
        assert "Dry run — nothing was written." in out
        assert writes == []
        assert wired.get_pool_file(pool_file.id).active is False
