"""Bulk cataloguing must produce exactly what the per-file path produces.

The per-file path holds a separate conversation with the database for every
file — two SELECTs, two INSERTs and a COMMIT — which measured at ~163ms per
file against the managed Postgres while the actual work (reading and hashing)
took 1ms. Batching removes the waiting, and the only thing that matters is that
it removes nothing else: the duplicate-digest linking in particular has to keep
working, including for two copies of one file arriving in the same batch, where
the pre-flight query cannot see the first one because it is not committed yet.
"""

import argparse

import pytest
from saorsa_db.db import Base
from saorsa_db.models.prod_file_pool import PoolFileKind
from saorsa_db.repositories import SqlAlchemyProdFilePoolRepository
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from saorsa_deploy.cmd import prod_pool

MIB = 1024 * 1024


def _repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return SqlAlchemyProdFilePoolRepository(Session(engine))


@pytest.fixture
def repo():
    r = _repo()
    yield r
    r.session.close()


def _entry(locator, name, size=5 * MIB, digests=None, tags=None, kind=None):
    return {
        "kind": kind or PoolFileKind.LOCAL,
        "locator": locator,
        "name": name,
        "size_bytes": size,
        "digests": digests,
        "tags": tags,
    }


def _snapshot(repo):
    """Catalogue state in a comparable form."""
    out = {}
    for f in repo.list_pool_files(active=None):
        out[f.locator] = {
            "name": f.name,
            "size": f.size_bytes,
            "tags": tuple(f.tags),
            "digests": tuple(sorted((d.algo, d.value) for d in f.digests)),
            "duplicate_of_locator": (
                repo.get_pool_file(f.duplicate_of).locator if f.duplicate_of else None
            ),
        }
    return out


class TestEquivalenceWithPerFilePath:
    """The two paths must agree. This is the whole safety argument."""

    ENTRIES = [
        _entry("/data/a.bin", "a.bin", digests={"sha1": "AAA", "sha256": "111"}, tags=["corpus"]),
        _entry("/data/b.bin", "b.bin", digests={"sha1": "BBB"}, tags=["corpus"]),
        # Same content as a.bin, arriving in the same batch.
        _entry("/data/a-copy.bin", "a-copy.bin", digests={"sha1": "aaa"}, tags=["corpus"]),
        _entry("/data/c.bin", "c.bin", size=50 * MIB, tags=["corpus", "video"]),
    ]

    def test_bulk_matches_one_at_a_time(self):
        per_file = _repo()
        for e in self.ENTRIES:
            per_file.add_pool_file(
                e["kind"],
                e["locator"],
                e["name"],
                size_bytes=e["size_bytes"],
                digests=e["digests"],
                tags=e["tags"],
            )
        bulk = _repo()
        bulk.add_pool_files_bulk(list(self.ENTRIES))

        assert _snapshot(bulk) == _snapshot(per_file)
        per_file.session.close()
        bulk.session.close()

    def test_small_batches_match_one_large_batch(self):
        one = _repo()
        one.add_pool_files_bulk(list(self.ENTRIES), batch_size=100)
        many = _repo()
        many.add_pool_files_bulk(list(self.ENTRIES), batch_size=1)
        assert _snapshot(many) == _snapshot(one)
        one.session.close()
        many.session.close()


class TestDuplicateLinking:
    def test_two_copies_in_one_batch_are_linked(self, repo):
        # The pre-flight digest query cannot see the first copy, because it is
        # not committed when the second is processed.
        repo.add_pool_files_bulk(
            [
                _entry("/data/first.bin", "first.bin", digests={"sha1": "aaa"}),
                _entry("/data/second.bin", "second.bin", digests={"sha1": "aaa"}),
            ]
        )
        rows = {f.name: f for f in repo.list_pool_files()}
        assert rows["second.bin"].duplicate_of == rows["first.bin"].id

    def test_links_to_a_file_from_an_earlier_batch(self, repo):
        repo.add_pool_files_bulk([_entry("/data/first.bin", "first.bin", digests={"sha1": "aaa"})])
        repo.add_pool_files_bulk(
            [_entry("/data/second.bin", "second.bin", digests={"sha1": "aaa"})]
        )
        rows = {f.name: f for f in repo.list_pool_files()}
        assert rows["second.bin"].duplicate_of == rows["first.bin"].id

    def test_links_stay_flat(self, repo):
        repo.add_pool_files_bulk(
            [
                _entry("/data/1.bin", "1.bin", digests={"sha1": "aaa"}),
                _entry("/data/2.bin", "2.bin", digests={"sha1": "aaa"}),
                _entry("/data/3.bin", "3.bin", digests={"sha1": "aaa"}),
            ]
        )
        rows = {f.name: f for f in repo.list_pool_files()}
        # Never a chain: availability resolves with one COALESCE.
        assert rows["2.bin"].duplicate_of == rows["1.bin"].id
        assert rows["3.bin"].duplicate_of == rows["1.bin"].id

    def test_reported_links_are_returned(self, repo):
        result = repo.add_pool_files_bulk(
            [
                _entry("/data/1.bin", "1.bin", digests={"sha1": "aaa"}),
                _entry("/data/2.bin", "2.bin", digests={"sha1": "aaa"}),
            ]
        )
        assert [name for name, _ in result["linked"]] == ["2.bin"]


class TestReRuns:
    def test_already_catalogued_files_are_skipped(self, repo):
        entries = [_entry("/data/a.bin", "a.bin")]
        repo.add_pool_files_bulk(entries)
        result = repo.add_pool_files_bulk(entries)
        assert result["skipped"] == 1
        assert result["created"] == []
        assert len(repo.list_pool_files()) == 1

    def test_a_rerun_merges_new_tags(self, repo):
        # Re-running a section with an extra tag must still take effect, exactly
        # as the per-file path did.
        repo.add_pool_files_bulk([_entry("/data/a.bin", "a.bin", tags=["corpus"])])
        repo.add_pool_files_bulk([_entry("/data/a.bin", "a.bin", tags=["corpus", "photos"])])
        assert repo.list_pool_files()[0].tags == ["corpus", "photos"]

    def test_a_rerun_fills_in_a_previously_unknown_size(self, repo):
        repo.add_pool_files_bulk([_entry("/data/a.bin", "a.bin", size=None)])
        repo.add_pool_files_bulk([_entry("/data/a.bin", "a.bin", size=7 * MIB)])
        assert repo.list_pool_files()[0].size_bytes == 7 * MIB


class TestRoundTrips:
    """Statement count must not grow with the number of files.

    This is the whole point of the method, and it is not something a timing
    assertion can check reliably. An early version of this code looked batched
    and still issued 252 statements for 50 files — the inserts were batched but
    a per-row refresh and a lazy relationship load were not.
    """

    def _statements(self, entries, **kwargs):
        repo = _repo()
        seen = []

        @event.listens_for(repo.session.bind, "before_cursor_execute")
        def _cap(conn, cursor, statement, params, context, executemany):
            seen.append(statement)

        repo.add_pool_files_bulk(entries, **kwargs)
        repo.session.close()
        return len(seen)

    def _entries(self, n):
        return [
            _entry(f"/data/{i}.bin", f"{i}.bin", digests={"sha1": f"{i:040d}"}, tags=["corpus"])
            for i in range(n)
        ]

    def test_flat_in_file_count(self):
        small = self._statements(self._entries(10))
        large = self._statements(self._entries(400))
        # 40x the files, same number of round-trips.
        assert small == large, f"{small} for 10 files, {large} for 400"

    def test_scales_with_batches_not_files(self):
        one_batch = self._statements(self._entries(100), batch_size=500)
        two_batches = self._statements(self._entries(600), batch_size=500)
        assert two_batches == one_batch * 2

    def test_duplicates_do_not_cost_a_round_trip_each(self):
        """The gap that let a real bug through.

        The flat-count tests above give every file a unique digest, so no
        duplicate links are written and the per-link UPDATE path is never
        exercised. A real archive directory produced 207 links and 5.3s of
        round-trips writing them one at a time.
        """
        unique = self._statements(
            [
                _entry(f"/data/u{i}.bin", f"u{i}.bin", digests={"sha1": f"{i:040d}"})
                for i in range(100)
            ]
        )
        # Same file count, but every one after the first is a duplicate.
        duplicated = self._statements(
            [_entry(f"/data/d{i}.bin", f"d{i}.bin", digests={"sha1": "a" * 40}) for i in range(100)]
        )
        assert duplicated <= unique + 2, (
            f"{duplicated} statements with 99 duplicate links vs {unique} with none"
        )

    def test_returned_records_can_be_read_without_touching_the_database(self):
        """The other gap, and the one that caused the visible hang.

        session.commit() expires every ORM instance it manages, so returning
        them meant the caller's summary line re-fetched each row — two
        statements each, ~46 seconds for 1156 files, all after the progress bar
        had closed.
        """
        repo = _repo()
        result = repo.add_pool_files_bulk(self._entries(40))

        seen = []

        @event.listens_for(repo.session.bind, "before_cursor_execute")
        def _cap(conn, cursor, statement, params, context, executemany):
            seen.append(statement)

        for record in result["created"]:
            _ = (record.id, record.name, record.size_bytes, record.kind, record.duplicate_of)
        repo.session.close()

        assert seen == [], f"reading {len(result['created'])} records cost {len(seen)}"

    def test_a_rerun_is_also_flat(self):
        # An interrupted import gets re-run over files that are already
        # catalogued; skipping them must not cost a round-trip each.
        entries = self._entries(200)
        repo = _repo()
        repo.add_pool_files_bulk(entries)
        seen = []

        @event.listens_for(repo.session.bind, "before_cursor_execute")
        def _cap(conn, cursor, statement, params, context, executemany):
            seen.append(statement)

        repo.add_pool_files_bulk(entries)
        repo.session.close()
        assert len(seen) < 15, f"{len(seen)} statements to skip 200 known files"


class TestCommandIntegration:
    def _args(self, directory, **kwargs):
        base = dict(
            url=None,
            urls_file=None,
            dir=str(directory),
            path=None,
            tag=["corpus"],
            provenance=None,
            allow_unknown_size=False,
            hash=True,
            materialise=None,
            from_listing=None,
            size=None,
            limit=None,
            dry_run=False,
            originals_only=False,
            verbose=False,
        )
        base.update(kwargs)
        return argparse.Namespace(**base)

    def test_directory_is_catalogued_with_hashes(self, repo, tmp_path, monkeypatch):
        import hashlib

        monkeypatch.setattr(prod_pool, "get_file_pool_repository", lambda: repo)
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.bin").write_bytes(b"a" * 100)
        (tmp_path / "sub" / "b.bin").write_bytes(b"b" * 200)

        prod_pool.cmd_prod_pool_add(self._args(tmp_path))

        rows = {f.name: f for f in repo.list_pool_files()}
        assert set(rows) == {"a.bin", "b.bin"}
        digests = {d.algo: d.value for d in rows["a.bin"].digests}
        assert digests["sha1"] == hashlib.sha1(b"a" * 100).hexdigest()
        assert rows["a.bin"].tags == ["corpus"]

    def test_an_unreadable_file_does_not_lose_the_batch(self, repo, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(prod_pool, "get_file_pool_repository", lambda: repo)
        for name in ("a.bin", "bad.bin", "c.bin"):
            (tmp_path / name).write_bytes(b"x" * 10)

        real_hash = prod_pool.hash_local_file

        def flaky(path):
            if path.name == "bad.bin":
                raise OSError("permission denied")
            return real_hash(path)

        monkeypatch.setattr(prod_pool, "hash_local_file", flaky)
        prod_pool.cmd_prod_pool_add(self._args(tmp_path))

        # The other two are catalogued; one bad file in thousands must not cost
        # the batch.
        assert {f.name for f in repo.list_pool_files()} == {"a.bin", "c.bin"}
        assert "permission denied" in " ".join(capsys.readouterr().out.split())

    def test_no_hash_still_catalogues(self, repo, tmp_path, monkeypatch):
        monkeypatch.setattr(prod_pool, "get_file_pool_repository", lambda: repo)
        (tmp_path / "a.bin").write_bytes(b"a" * 10)
        prod_pool.cmd_prod_pool_add(self._args(tmp_path, hash=False))
        assert repo.list_pool_files()[0].digests == []
