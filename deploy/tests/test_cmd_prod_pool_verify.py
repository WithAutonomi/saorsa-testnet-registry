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


@pytest.fixture(autouse=True)
def wide_console(monkeypatch):
    """Keep rich from wrapping long locators, which assertions read back."""
    from rich.console import Console as RichConsole

    monkeypatch.setattr(prod_pool, "Console", lambda: RichConsole(width=300))


def _args(**kwargs):
    base = dict(kind=None, limit=None, rehash=True, dry_run=False, force=False)
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


@pytest.fixture
def writes(repo, monkeypatch):
    """Record every write the command makes, so "nothing was written" is testable.

    Asserting on the resulting row state is not enough for a dry run: a row
    that was never going to change looks identical either way.
    """
    recorded = {"active": [], "size": [], "digests": []}
    original_active = repo.set_pool_file_active
    original_size = repo.update_pool_file_size
    original_digests = repo.add_pool_file_digests

    def active(file_id, value):
        recorded["active"].append((file_id, value))
        return original_active(file_id, value)

    def size(file_id, value):
        recorded["size"].append((file_id, value))
        return original_size(file_id, value)

    def digests(file_id, value):
        recorded["digests"].append((file_id, value))
        return original_digests(file_id, value)

    monkeypatch.setattr(repo, "set_pool_file_active", active)
    monkeypatch.setattr(repo, "update_pool_file_size", size)
    monkeypatch.setattr(repo, "add_pool_file_digests", digests)
    return recorded


class TestDryRun:
    def test_dry_run_writes_nothing(self, wired, writes, tmp_path, monkeypatch, capsys):
        # Both branches reach "gone"; neither is allowed to touch the database.
        local, _ = wired.add_pool_file(
            PoolFileKind.LOCAL, str(tmp_path / "gone.bin"), "gone.bin", size_bytes=MIB
        )
        url, _ = wired.add_pool_file(
            PoolFileKind.URL, "https://example.org/gone", "gone", size_bytes=MIB
        )
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: None)
        monkeypatch.setattr(prod_pool, "_url_reachable", lambda url: False)
        prod_pool.cmd_prod_pool_verify(_args(dry_run=True))
        out = _flat(capsys.readouterr().out)
        assert "gone 2" in out
        assert "would be retired" in out
        assert "Dry run — nothing was written." in out
        assert writes["active"] == []
        assert wired.get_pool_file(local.id).active is True
        assert wired.get_pool_file(url.id).active is True

    def test_dry_run_tallies_match_a_real_run(self, wired, tmp_path, monkeypatch, capsys):
        wired.add_pool_file(
            PoolFileKind.LOCAL, str(tmp_path / "gone.bin"), "gone.bin", size_bytes=MIB
        )
        wired.add_pool_file(PoolFileKind.URL, "https://example.org/a", "a", size_bytes=10 * MIB)
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: 10 * MIB)
        prod_pool.cmd_prod_pool_verify(_args(dry_run=True))
        dry = _flat(capsys.readouterr().out)
        prod_pool.cmd_prod_pool_verify(_args())
        real = _flat(capsys.readouterr().out)
        for tally in ("ok 1", "gone 1"):
            assert tally in dry
            assert tally in real

    def test_dry_run_suppresses_size_and_digest_writes(self, wired, writes, tmp_path, capsys):
        target = tmp_path / "a.bin"
        target.write_bytes(b"a" * 10)
        pool_file, _ = wired.add_pool_file(PoolFileKind.LOCAL, str(target), "a.bin", size_bytes=999)
        prod_pool.cmd_prod_pool_verify(_args(dry_run=True))
        assert "size would be updated" in _flat(capsys.readouterr().out)
        assert writes["size"] == []
        assert writes["digests"] == []
        refreshed = wired.get_pool_file(pool_file.id)
        assert refreshed.size_bytes == 999
        assert refreshed.digests == []


class TestAbsentDirectory:
    def test_an_unmounted_volume_retires_nothing(self, wired, writes, tmp_path, capsys):
        # The 2026-10-22 incident: /mnt/… was not mounted, and every local
        # entry in the catalogue was retired on one failed is_file().
        volume = tmp_path / "volume"
        rows = [
            wired.add_pool_file(
                PoolFileKind.LOCAL, str(volume / f"{i}.JPG"), f"{i}.JPG", size_bytes=MIB
            )[0]
            for i in range(1, 4)
        ]
        assert not volume.exists()
        prod_pool.cmd_prod_pool_verify(_args())
        out = _flat(capsys.readouterr().out)
        assert "unreachable 3" in out
        assert "gone" not in out
        assert writes["active"] == []
        assert all(wired.get_pool_file(row.id).active is True for row in rows)
        # Named once per directory, not once per entry: 1728 identical lines
        # are what hid this the first time.
        assert out.count(str(volume)) == 1
        assert "3 entries" in out
        assert "not mounted" in out

    def test_a_deleted_file_is_still_retired(self, wired, writes, tmp_path, capsys):
        # The behaviour that has to survive the fix: the parent is present, so
        # the file really is gone.
        pool_file, _ = wired.add_pool_file(
            PoolFileKind.LOCAL, str(tmp_path / "gone.bin"), "gone.bin", size_bytes=MIB
        )
        prod_pool.cmd_prod_pool_verify(_args())
        assert "missing" in _flat(capsys.readouterr().out)
        assert writes["active"] == [(pool_file.id, False)]
        assert wired.get_pool_file(pool_file.id).active is False

    def test_deleted_and_unreachable_in_one_run(self, wired, writes, tmp_path, capsys):
        deleted, _ = wired.add_pool_file(
            PoolFileKind.LOCAL, str(tmp_path / "gone.bin"), "gone.bin", size_bytes=MIB
        )
        absent, _ = wired.add_pool_file(
            PoolFileKind.LOCAL,
            str(tmp_path / "volume" / "a.JPG"),
            "a.JPG",
            size_bytes=MIB,
        )
        prod_pool.cmd_prod_pool_verify(_args())
        out = _flat(capsys.readouterr().out)
        assert "gone 1" in out
        assert "unreachable 1" in out
        assert writes["active"] == [(deleted.id, False)]
        assert wired.get_pool_file(deleted.id).active is False
        assert wired.get_pool_file(absent.id).active is True

    def test_url_rows_keep_the_two_signal_rule(self, wired, writes, monkeypatch, capsys):
        # A URL has no parent directory to test, so the new outcome must not
        # reach it: unanswered and unreachable is still a retirement.
        pool_file, _ = wired.add_pool_file(
            PoolFileKind.URL, "https://example.org/gone", "gone", size_bytes=MIB
        )
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: None)
        monkeypatch.setattr(prod_pool, "_url_reachable", lambda url: False)
        prod_pool.cmd_prod_pool_verify(_args())
        out = _flat(capsys.readouterr().out)
        assert "gone 1" in out
        assert "unreachable" not in out
        assert writes["active"] == [(pool_file.id, False)]


class TestMassRetirementGuard:
    def _local_rows(self, repo, tmp_path, total, doomed):
        """`total` rows in an existing directory, the first `doomed` of them absent."""
        present = tmp_path / "present"
        present.mkdir()
        rows = []
        for index in range(total):
            target = present / f"{index}.bin"
            if index >= doomed:
                target.write_bytes(b"a")
            rows.append(
                repo.add_pool_file(
                    PoolFileKind.LOCAL, str(target), target.name, size_bytes=1 if index else 1
                )[0]
            )
        return rows

    def test_a_bulk_retirement_is_refused(self, wired, writes, tmp_path, capsys):
        rows = self._local_rows(wired, tmp_path, total=20, doomed=15)
        with pytest.raises(SystemExit) as exit_info:
            prod_pool.cmd_prod_pool_verify(_args())
        assert exit_info.value.code == 2
        out = _flat(capsys.readouterr().out)
        assert "Refusing to retire 15 of the 20 entries scanned" in out
        assert "source or mount outage" in out
        # The first ten are named, and the remainder counted.
        for row in rows[:10]:
            assert f"#{row.id}" in out
        assert "… and 5 more" in out
        assert writes["active"] == []
        assert all(wired.get_pool_file(row.id).active is True for row in rows[:15])

    def test_force_retires_anyway(self, wired, writes, tmp_path, capsys):
        rows = self._local_rows(wired, tmp_path, total=20, doomed=15)
        prod_pool.cmd_prod_pool_verify(_args(force=True))
        out = _flat(capsys.readouterr().out)
        assert "--force was passed" in out
        assert len(writes["active"]) == 15
        assert all(wired.get_pool_file(row.id).active is False for row in rows[:15])

    def test_a_small_proportion_is_not_guarded(self, wired, writes, tmp_path, capsys):
        rows = self._local_rows(wired, tmp_path, total=20, doomed=2)
        prod_pool.cmd_prod_pool_verify(_args())
        out = _flat(capsys.readouterr().out)
        assert "Refusing to retire" not in out
        assert len(writes["active"]) == 2
        assert all(wired.get_pool_file(row.id).active is False for row in rows[:2])

    def test_a_small_run_is_not_guarded(self, wired, writes, tmp_path, capsys):
        # 1 of 3 is over the proportion but under the ten-entry floor, so a
        # --limit run that legitimately retires one is unaffected.
        rows = self._local_rows(wired, tmp_path, total=3, doomed=1)
        prod_pool.cmd_prod_pool_verify(_args())
        assert "Refusing to retire" not in _flat(capsys.readouterr().out)
        assert writes["active"] == [(rows[0].id, False)]

    def test_the_guard_counts_what_was_scanned(self, wired, writes, tmp_path, capsys):
        # 15 doomed out of 215 catalogued is 7%, but --limit 20 scans only
        # the 20, of which 15 are doomed. The proportion is of the scan.
        self._local_rows(wired, tmp_path, total=15, doomed=15)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        for index in range(200):
            target = elsewhere / f"live-{index}.bin"
            target.write_bytes(b"a")
            wired.add_pool_file(PoolFileKind.LOCAL, str(target), target.name, size_bytes=1)
        with pytest.raises(SystemExit) as exit_info:
            prod_pool.cmd_prod_pool_verify(_args(limit=20))
        assert exit_info.value.code == 2
        assert "15 of the 20 entries scanned" in _flat(capsys.readouterr().out)
        assert writes["active"] == []

    def test_dry_run_reports_the_guard_and_exits_zero(self, wired, writes, tmp_path, capsys):
        # A dry run is a report: it says what the guard would do and returns
        # normally, rather than making a preview exit non-zero.
        self._local_rows(wired, tmp_path, total=20, doomed=15)
        prod_pool.cmd_prod_pool_verify(_args(dry_run=True))
        out = _flat(capsys.readouterr().out)
        assert "Would refuse to retire 15 of the 20 entries scanned" in out
        assert "Dry run — nothing was written." in out
        assert writes["active"] == []
