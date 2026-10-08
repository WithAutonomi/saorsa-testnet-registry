"""Tests for `prod pool add`.

Two behaviours are worth guarding. A URL whose size cannot be resolved must not
slip into the pool looking usable — it is invisible to every size-class request,
so a pool that appears full can still fail to satisfy a canary. And a digest
inferred from the wrong place would be worse than no digest at all: it would
link two unrelated files and silently retire one.
"""

import argparse
import hashlib
from pathlib import Path

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
    base = dict(
        url=None,
        urls_file=None,
        dir=None,
        path=None,
        tag=None,
        provenance=None,
        allow_unknown_size=False,
        hash=True,
        materialise=None,
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


class TestAddUrl:
    def test_adds_a_sized_url(self, wired, monkeypatch):
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: 42 * MIB)
        prod_pool.cmd_prod_pool_add(_args(url=["https://example.org/a.bin"]))
        rows = wired.list_pool_files()
        assert len(rows) == 1
        assert rows[0].kind is PoolFileKind.URL
        assert rows[0].size_bytes == 42 * MIB
        assert rows[0].name == "a.bin"

    def test_unsizeable_url_is_refused_by_default(self, wired, monkeypatch):
        # Silently adding it would leave an entry that no size request can ever
        # draw, in a pool that reports itself as larger than it usefully is.
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: None)
        prod_pool.cmd_prod_pool_add(_args(url=["https://example.org/stream"]))
        assert wired.list_pool_files() == []

    def test_unsizeable_url_can_be_forced(self, wired, monkeypatch):
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: None)
        prod_pool.cmd_prod_pool_add(
            _args(url=["https://example.org/stream"], allow_unknown_size=True)
        )
        rows = wired.list_pool_files()
        assert len(rows) == 1
        assert rows[0].size_bytes is None

    def test_readding_a_known_url_does_not_duplicate(self, wired, monkeypatch):
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: MIB)
        for _ in range(2):
            prod_pool.cmd_prod_pool_add(_args(url=["https://example.org/a.bin"]))
        assert len(wired.list_pool_files()) == 1

    def test_reads_a_urls_file_ignoring_comments(self, wired, monkeypatch, tmp_path):
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: MIB)
        listing = tmp_path / "urls.txt"
        listing.write_text("# a comment\nhttps://example.org/a.bin\n\nhttps://example.org/b.bin\n")
        prod_pool.cmd_prod_pool_add(_args(urls_file=str(listing)))
        assert {f.name for f in wired.list_pool_files()} == {"a.bin", "b.bin"}

    def test_tags_and_provenance_are_recorded(self, wired, monkeypatch):
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: MIB)
        prod_pool.cmd_prod_pool_add(
            _args(
                url=["https://example.org/a.bin"],
                tag=["video", "corpus"],
                provenance="internet-archive:item",
            )
        )
        row = wired.list_pool_files()[0]
        assert set(row.tags) == {"video", "corpus"}
        assert row.provenance == "internet-archive:item"


class TestAddLocal:
    def test_adds_a_directory_recursively_with_hashes(self, wired, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.bin").write_bytes(b"a" * 100)
        (tmp_path / "sub" / "b.bin").write_bytes(b"b" * 200)

        prod_pool.cmd_prod_pool_add(_args(dir=str(tmp_path)))

        rows = wired.list_pool_files()
        assert {r.name for r in rows} == {"a.bin", "b.bin"}
        assert all(r.kind is PoolFileKind.LOCAL for r in rows)
        by_name = {r.name: r for r in rows}
        assert by_name["a.bin"].size_bytes == 100
        digests = {d.algo: d.value for d in by_name["a.bin"].digests}
        assert digests["sha1"] == hashlib.sha1(b"a" * 100).hexdigest()
        assert digests["sha256"] == hashlib.sha256(b"a" * 100).hexdigest()

    def test_no_hash_skips_digests(self, wired, tmp_path):
        (tmp_path / "a.bin").write_bytes(b"a" * 10)
        prod_pool.cmd_prod_pool_add(_args(dir=str(tmp_path), hash=False))
        assert wired.list_pool_files()[0].digests == []

    def test_locator_is_absolute(self, wired, tmp_path):
        (tmp_path / "a.bin").write_bytes(b"a")
        prod_pool.cmd_prod_pool_add(_args(path=str(tmp_path / "a.bin")))
        assert Path(wired.list_pool_files()[0].locator).is_absolute()


class TestCrossKindDuplicates:
    def test_local_file_and_url_sharing_sha1_are_linked(self, wired, tmp_path, monkeypatch):
        payload = b"x" * 500
        local = tmp_path / "film.mkv"
        local.write_bytes(payload)
        prod_pool.cmd_prod_pool_add(_args(path=str(local)))

        # The Internet Archive path supplies sha1 from item metadata without
        # downloading; simulate that by passing the digest straight through.
        monkeypatch.setattr(prod_pool, "probe_url_size", lambda url: len(payload))
        report = prod_pool._AddReport()
        prod_pool._add_url(
            wired,
            prod_pool.Console(),
            report,
            "https://archive.org/download/i/film.mkv",
            tags=None,
            provenance=None,
            allow_unknown_size=False,
            digests={"sha1": hashlib.sha1(payload).hexdigest()},
        )
        rows = wired.list_pool_files()
        url_row = next(r for r in rows if r.kind is PoolFileKind.URL)
        local_row = next(r for r in rows if r.kind is PoolFileKind.LOCAL)
        assert url_row.duplicate_of == local_row.id
        assert report.linked_duplicates


class TestInputValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"url": ["https://example.org/a"], "path": "/tmp/x"},
            {"dir": "/tmp", "urls_file": "/tmp/u"},
        ],
    )
    def test_exactly_one_source_is_required(self, wired, kwargs):
        with pytest.raises(SystemExit):
            prod_pool.cmd_prod_pool_add(_args(**kwargs))

    def test_missing_database_exits(self, monkeypatch):
        # The pool IS the never-reuse mechanism, so a missing database must
        # refuse loudly rather than degrade to a no-op the way the old
        # name-based duplicate check did.
        monkeypatch.setattr(prod_pool, "get_file_pool_repository", lambda: None)
        with pytest.raises(SystemExit):
            prod_pool.cmd_prod_pool_add(_args(url=["https://example.org/a"]))


class TestProbeUrlSize:
    def test_falls_back_to_get_when_head_is_refused(self, monkeypatch):
        # Internet Archive node servers answer HEAD with a redirect or a 405
        # while answering GET perfectly well.
        calls = []

        class _Response:
            def __init__(self, status, length=None):
                self.status_code = status
                self.headers = {"Content-Length": length} if length else {}

            def close(self):
                pass

        def fake_request(method, url, **kwargs):
            calls.append(method)
            if method == "head":
                return _Response(405)
            return _Response(200, "12345")

        monkeypatch.setattr(prod_pool.requests, "request", fake_request)
        assert prod_pool.probe_url_size("https://archive.org/download/i/f") == 12345
        assert calls == ["head", "get"]

    def test_returns_none_when_no_length_is_published(self, monkeypatch):
        class _Response:
            status_code = 200
            headers: dict = {}

            def close(self):
                pass

        monkeypatch.setattr(prod_pool.requests, "request", lambda method, url, **kw: _Response())
        assert prod_pool.probe_url_size("https://example.org/stream") is None

    def test_never_infers_a_digest_from_etag(self, monkeypatch):
        # An ETag is an MD5 for some objects and something else for multipart
        # uploads. A wrong digest links unrelated files and silently retires
        # one of them, which is worse than having no digest.
        class _Response:
            status_code = 200
            headers = {
                "Content-Length": "10",
                "ETag": '"d41d8cd98f00b204e9800998ecf8427e"',
            }

            def close(self):
                pass

        monkeypatch.setattr(prod_pool.requests, "request", lambda method, url, **kw: _Response())
        assert prod_pool.probe_url_size("https://example.org/a") == 10


class TestMaterialise:
    def test_downloads_and_catalogues_as_local(self, wired, tmp_path, monkeypatch):
        payload = b"z" * 321

        def fake_download(url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(payload)
            return len(payload)

        monkeypatch.setattr(prod_pool, "_download", fake_download)
        prod_pool.cmd_prod_pool_add(
            _args(
                url=["https://example.org/film.mkv"],
                materialise=str(tmp_path / "corpus"),
            )
        )
        rows = wired.list_pool_files()
        assert len(rows) == 1
        # One piece of content, one entry — there is no URL row that could be
        # handed out separately.
        assert rows[0].kind is PoolFileKind.LOCAL
        assert rows[0].size_bytes == len(payload)
        assert rows[0].provenance == "https://example.org/film.mkv"
        assert {d.algo for d in rows[0].digests} == {"sha1", "sha256"}
