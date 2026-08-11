"""Tests for the public-collection adapters.

The Internet Archive adapter is what makes the pool sustainable: a canary run
burns ~350 real files, and IA's metadata API gives name, size, MD5 and SHA1 for
every file in an item without downloading anything. The filtering matters as
much as the fetching — an item's derivatives and bookkeeping files are
artefacts of the archive, not user content, and would pad the pool with things
nobody wants uploaded to production.
"""

import argparse

import pytest
from saorsa_db.db import Base
from saorsa_db.models.prod_file_pool import PoolFileKind
from saorsa_db.repositories import SqlAlchemyProdFilePoolRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy import pool_sources
from saorsa_deploy.cmd import prod_pool

MIB = 1024 * 1024


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise pool_sources.requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


# Modelled on a real item (BigBuckBunny_124). Note the media is marked
# "derivative" while the only "original" entries are metadata and a torrent —
# which is why `source` is not usable as a content filter.
ITEM_PAYLOAD = {
    "files": [
        {
            "name": "Content/film.mkv",
            "source": "derivative",
            "format": "Matroska",
            "size": "838860800",
            "md5": "AAA111",
            "sha1": "BBB222",
        },
        {
            "name": "notes.txt",
            "source": "original",
            "format": "Text",
            "size": "1024",
            "md5": "ccc333",
            "sha1": "ddd444",
        },
        # Archive artefacts, excluded by format or by name.
        {
            "name": "film.torrent",
            "source": "original",
            "format": "BitTorrent",
            "size": "51283",
        },
        {
            "name": "item.thumbs/frame_000001.jpg",
            "source": "derivative",
            "format": "Thumbnail",
            "size": "10377",
        },
        {"name": "item_meta.xml", "source": "original", "format": "Metadata", "size": "512"},
        {"name": "item_files.xml", "source": "original", "format": "Metadata", "size": "256"},
        {"name": "__ia_thumb.jpg", "source": "original", "format": "Item Tile", "size": "64"},
    ]
}


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


def _add_args(**kwargs):
    base = dict(
        url=None, urls_file=None, dir=None, path=None, tag=None, provenance=None,
        allow_unknown_size=False, hash=True, materialise=None,
        from_listing=None, size=None, limit=None, dry_run=False,
        originals_only=False, path_prefix=None,
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


def _flat(text):
    return " ".join(text.split())


class TestSpecParsing:
    def test_parses_kind_and_identifier(self):
        assert pool_sources.parse_source_spec("internet-archive:my-item") == (
            "internet-archive",
            "my-item",
        )

    def test_identifier_may_contain_colons(self):
        kind, identifier = pool_sources.parse_source_spec("internet-archive:a:b")
        assert identifier == "a:b"

    def test_missing_colon_is_rejected(self):
        with pytest.raises(ValueError, match="Expected kind:identifier"):
            pool_sources.parse_source_spec("internet-archive")

    def test_unknown_kind_lists_valid_ones(self):
        with pytest.raises(ValueError, match="internet-archive"):
            pool_sources.parse_source_spec("wikimedia:cats")

    def test_empty_identifier_is_rejected(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            pool_sources.parse_source_spec("internet-archive:")


class TestInternetArchive:
    def test_returns_content_with_size_and_digests(self, monkeypatch):
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response(ITEM_PAYLOAD)
        )
        candidates = pool_sources.fetch_internet_archive_item("my-item")
        names = {c.name for c in candidates}
        assert names == {"film.mkv", "notes.txt"}

        film = next(c for c in candidates if c.name == "film.mkv")
        assert film.size_bytes == 838860800
        # Lower-cased on the way in, so a lookup cannot miss on case alone.
        assert film.digests == {"md5": "aaa111", "sha1": "bbb222"}
        # The URL keeps the item-relative path; the name is just the basename.
        assert film.url == "https://archive.org/download/my-item/Content/film.mkv"
        assert film.provenance == "internet-archive:my-item"

    def test_source_is_not_used_as_a_content_filter(self, monkeypatch):
        # Measured on the real BigBuckBunny_124 item: the media is marked
        # "derivative" and the only "originals" are metadata plus a torrent.
        # Filtering to originals discarded 332 MB, 62 MB and 47 MB of real
        # content and kept a 50 KB torrent.
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response(ITEM_PAYLOAD)
        )
        names = {c.name for c in pool_sources.fetch_internet_archive_item("my-item")}
        assert "film.mkv" in names

    def test_originals_only_is_available_but_opt_in(self, monkeypatch):
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response(ITEM_PAYLOAD)
        )
        names = {
            c.name
            for c in pool_sources.fetch_internet_archive_item(
                "my-item", originals_only=True
            )
        }
        assert names == {"notes.txt"}

    def test_artefact_formats_are_excluded(self, monkeypatch):
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response(ITEM_PAYLOAD)
        )
        names = {c.name for c in pool_sources.fetch_internet_archive_item("my-item")}
        assert "film.torrent" not in names
        assert "frame_000001.jpg" not in names

    def test_bookkeeping_files_are_excluded(self, monkeypatch):
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response(ITEM_PAYLOAD)
        )
        names = {c.name for c in pool_sources.fetch_internet_archive_item("my-item")}
        assert not names & {"item_meta.xml", "item_files.xml", "__ia_thumb.jpg"}

    def test_unknown_item_is_reported_clearly(self, monkeypatch):
        # IA answers an unknown identifier with 200 and an empty body, so this
        # is the only signal that the item does not exist.
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response({})
        )
        with pytest.raises(pool_sources.SourceError, match="check the identifier"):
            pool_sources.fetch_internet_archive_item("nope")

    def test_non_json_response_is_reported(self, monkeypatch):
        class _Bad:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                raise ValueError("not json")

        monkeypatch.setattr(pool_sources.requests, "get", lambda url, **kw: _Bad())
        with pytest.raises(pool_sources.SourceError, match="did not return JSON"):
            pool_sources.fetch_internet_archive_item("x")

    def test_unparseable_size_becomes_unknown(self, monkeypatch):
        monkeypatch.setattr(
            pool_sources.requests,
            "get",
            lambda url, **kw: _Response(
                {"files": [{"name": "a.bin", "format": "Binary", "size": "huge"}]}
            ),
        )
        candidate = pool_sources.fetch_internet_archive_item("x")[0]
        assert candidate.size_bytes is None


class TestAddFromListing:
    def test_catalogues_without_probing_or_downloading(
        self, wired, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response(ITEM_PAYLOAD)
        )

        def _no_probe(url):
            raise AssertionError("bulk acquisition must not probe each URL")

        monkeypatch.setattr(prod_pool, "probe_url_size", _no_probe)
        monkeypatch.setattr(
            prod_pool, "_download", lambda *a, **kw: pytest.fail("must not download")
        )

        prod_pool.cmd_prod_pool_add(
            _add_args(from_listing="internet-archive:my-item")
        )

        rows = wired.list_pool_files()
        assert {r.name for r in rows} == {"film.mkv", "notes.txt"}
        film = next(r for r in rows if r.name == "film.mkv")
        assert film.size_bytes == 838860800
        assert {d.algo for d in film.digests} == {"md5", "sha1"}
        assert film.provenance == "internet-archive:my-item"

    def test_size_filter_applies(self, wired, monkeypatch, capsys):
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response(ITEM_PAYLOAD)
        )
        prod_pool.cmd_prod_pool_add(
            _add_args(from_listing="internet-archive:my-item", size="medium")
        )
        assert {r.name for r in wired.list_pool_files()} == {"film.mkv"}
        assert "did not match --size medium" in _flat(capsys.readouterr().out)

    def test_min_size_expresses_what_a_class_cannot(self, wired, monkeypatch, capsys):
        # Both files here are catalogued by the unbounded run; a class filter
        # can only say "tiny" (which takes the 1 KB notes) or "medium" (which
        # takes the 800 MB film). "at least 5 MB" needs the byte bound.
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response(ITEM_PAYLOAD)
        )
        prod_pool.cmd_prod_pool_add(
            _add_args(from_listing="internet-archive:my-item", min_size="5MB")
        )
        assert {r.name for r in wired.list_pool_files()} == {"film.mkv"}
        assert "did not match ≥ 5.00 MB" in _flat(capsys.readouterr().out)

    def test_max_size_applies(self, wired, monkeypatch):
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response(ITEM_PAYLOAD)
        )
        prod_pool.cmd_prod_pool_add(
            _add_args(from_listing="internet-archive:my-item", max_size="5MB")
        )
        assert {r.name for r in wired.list_pool_files()} == {"notes.txt"}

    def test_bounds_combine_with_a_class(self, wired, monkeypatch, capsys):
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response(ITEM_PAYLOAD)
        )
        prod_pool.cmd_prod_pool_add(
            _add_args(
                from_listing="internet-archive:my-item", size="tiny", min_size="5MB"
            )
        )
        assert wired.list_pool_files() == []
        assert "did not match --size tiny and ≥ 5.00 MB" in _flat(
            capsys.readouterr().out
        )

    def test_inverted_bounds_are_refused(self, wired, monkeypatch, capsys):
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response(ITEM_PAYLOAD)
        )
        with pytest.raises(SystemExit):
            prod_pool.cmd_prod_pool_add(
                _add_args(
                    from_listing="internet-archive:my-item",
                    min_size="100MB",
                    max_size="10MB",
                )
            )
        assert "must be below" in _flat(capsys.readouterr().out)

    def test_limit_applies(self, wired, monkeypatch):
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response(ITEM_PAYLOAD)
        )
        prod_pool.cmd_prod_pool_add(
            _add_args(from_listing="internet-archive:my-item", limit=1)
        )
        assert len(wired.list_pool_files()) == 1

    def test_dry_run_writes_nothing(self, wired, monkeypatch, capsys):
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response(ITEM_PAYLOAD)
        )
        prod_pool.cmd_prod_pool_add(
            _add_args(from_listing="internet-archive:my-item", dry_run=True)
        )
        assert wired.list_pool_files() == []
        assert "Dry run" in _flat(capsys.readouterr().out)

    def test_rerunning_skips_what_is_already_catalogued(self, wired, monkeypatch, capsys):
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response(ITEM_PAYLOAD)
        )
        prod_pool.cmd_prod_pool_add(_add_args(from_listing="internet-archive:my-item"))
        capsys.readouterr()
        prod_pool.cmd_prod_pool_add(_add_args(from_listing="internet-archive:my-item"))
        out = _flat(capsys.readouterr().out)
        assert "skipped 2 already in the pool" in out
        assert len(wired.list_pool_files()) == 2

    def test_ia_entry_links_to_an_existing_local_copy(self, wired, monkeypatch, capsys):
        # The whole reason for reading IA's checksums: a local file hashed with
        # SHA1 and an IA URL carrying SHA1 are recognised as one piece of
        # content, so the pool cannot hand out both.
        local, _ = wired.add_pool_file(
            PoolFileKind.LOCAL,
            "/data/corpus/film.mkv",
            "film.mkv",
            size_bytes=838860800,
            digests={"sha1": "bbb222"},
        )
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response(ITEM_PAYLOAD)
        )
        prod_pool.cmd_prod_pool_add(_add_args(from_listing="internet-archive:my-item"))

        mirror = next(
            r for r in wired.list_pool_files() if r.kind is PoolFileKind.URL
            and r.name == "film.mkv"
        )
        assert mirror.duplicate_of == local.id
        assert "claiming either spends both" in _flat(capsys.readouterr().out)

    def test_bad_source_spec_exits_cleanly(self, wired, capsys):
        with pytest.raises(SystemExit):
            prod_pool.cmd_prod_pool_add(_add_args(from_listing="nonsense"))
        assert "Expected kind:identifier" in _flat(capsys.readouterr().out)

    def test_from_listing_is_exclusive_with_other_sources(self, wired):
        with pytest.raises(SystemExit):
            prod_pool.cmd_prod_pool_add(
                _add_args(
                    from_listing="internet-archive:x", url=["https://example.org/a"]
                )
            )


class TestPathPrefix:
    """A large item is catalogued a directory at a time.

    IA file names are full item-internal paths, so an item holding several
    releases can be narrowed to one. Without this the only options are taking
    the whole item or deleting what you did not want afterwards.
    """

    PAYLOAD = {
        "files": [
            {"name": "Release_03/Release 3/Photographer 1/a.jpg",
             "format": "JPEG", "size": "14515", "md5": "aa", "sha1": "bb"},
            {"name": "Release_03/Release 3/Photographer 2/b.jpg",
             "format": "JPEG", "size": "25975", "md5": "cc", "sha1": "dd"},
            {"name": "Release_04/other.jpg", "format": "JPEG", "size": "999"},
            {"name": "Release_03/NIST FOIA Terms of Use.txt",
             "format": "Text", "size": "512"},
            {"name": "item_torrent.txt", "format": "BitTorrentContents",
             "size": "300"},
        ]
    }

    def _resp(self, monkeypatch):
        monkeypatch.setattr(
            pool_sources.requests, "get", lambda url, **kw: _Response(self.PAYLOAD)
        )

    def test_narrows_to_the_directory(self, monkeypatch):
        self._resp(monkeypatch)
        names = {
            c.name
            for c in pool_sources.fetch_internet_archive_item(
                "item", path_prefix="Release_03/Release 3/"
            )
        }
        assert names == {"a.jpg", "b.jpg"}

    def test_accepts_a_percent_encoded_prefix(self, monkeypatch):
        # So it can be pasted straight from a browser URL.
        self._resp(monkeypatch)
        names = {
            c.name
            for c in pool_sources.fetch_internet_archive_item(
                "item", path_prefix="Release_03/Release%203/"
            )
        }
        assert names == {"a.jpg", "b.jpg"}

    def test_without_a_prefix_the_whole_item_is_offered(self, monkeypatch):
        self._resp(monkeypatch)
        names = {c.name for c in pool_sources.fetch_internet_archive_item("item")}
        # The torrent-contents listing is an archive artefact and stays excluded
        # either way; the Terms-of-Use text is real content and is not.
        assert "other.jpg" in names
        assert "item_torrent.txt" not in names
        assert "NIST FOIA Terms of Use.txt" in names

    def test_urls_encode_spaces_in_the_path(self, monkeypatch):
        # An item-internal path routinely contains spaces; an unencoded URL
        # 404s when the uploader VM fetches it.
        self._resp(monkeypatch)
        c = next(
            c for c in pool_sources.fetch_internet_archive_item("item")
            if c.name == "a.jpg"
        )
        assert " " not in c.url
        assert c.url.endswith("Release_03/Release%203/Photographer%201/a.jpg")


class TestDuplicateBasenames:
    """Distinct files sharing one basename get a distinguishing ancestor.

    Measured on ``NIST_9-11_Release_20``: eleven DVD rips, eleven distinct
    MD5s, every one of them named ``VTS_01_1.VOB`` under its own
    ``42A00<nn> - FBI#<n>/VIDEO_TS/`` directory. The pool keys on the locator
    and the digest so those catalogue correctly, but ``name`` is what
    ``import-history`` matches recorded uploads against and what the legacy
    ``urls=`` duplicate preflight compares — neither can tell eleven identical
    names apart.
    """

    PAYLOAD = {
        "files": [
            {"name": "Rel/42A0059 - FBI#1/VIDEO_TS/VTS_01_1.VOB",
             "format": "MPEG2", "size": "127619645", "md5": "a1"},
            {"name": "Rel/42A0060 - FBI#2/VIDEO_TS/VTS_01_1.VOB",
             "format": "MPEG2", "size": "189531648", "md5": "a2"},
            {"name": "Rel/42A0061 - FBI#3/VIDEO_TS/VTS_01_1.VOB",
             "format": "MPEG2", "size": "1073203200", "md5": "a3"},
            {"name": "Rel/42A0059 - FBI#1/VIDEO_TS/VIDEO_TS.IFO",
             "format": "Unknown", "size": "12288", "md5": "b1"},
            {"name": "Rel/42A0060 - FBI#2/VIDEO_TS/VIDEO_TS.IFO",
             "format": "Unknown", "size": "12288", "md5": "b2"},
            {"name": "Rel/NIST FOIA Terms of Use.txt",
             "format": "Text", "size": "1424", "md5": "c1"},
        ]
    }

    def _fetch(self, monkeypatch, payload=None):
        monkeypatch.setattr(
            pool_sources.requests,
            "get",
            lambda url, **kw: _Response(payload or self.PAYLOAD),
        )
        return pool_sources.fetch_internet_archive_item("item")

    def test_colliding_names_become_unique(self, monkeypatch):
        candidates = self._fetch(monkeypatch)
        names = [c.name for c in candidates]
        assert len(set(names)) == len(names)

    def test_the_nearest_discriminating_ancestor_is_used(self, monkeypatch):
        # VIDEO_TS is the immediate parent of all three and discriminates
        # nothing, so it is skipped in favour of the directory that does.
        names = {c.name for c in self._fetch(monkeypatch)}
        assert "42A0061 - FBI#3 - VTS_01_1.VOB" in names
        assert "VIDEO_TS - VTS_01_1.VOB" not in names

    def test_a_unique_name_is_left_alone(self, monkeypatch):
        # Renaming unconditionally would change the name of every entry already
        # catalogued from a listing, breaking the very matching this protects.
        names = {c.name for c in self._fetch(monkeypatch)}
        assert "NIST FOIA Terms of Use.txt" in names

    def test_each_colliding_group_is_resolved_independently(self, monkeypatch):
        names = {c.name for c in self._fetch(monkeypatch)}
        assert "42A0059 - FBI#1 - VIDEO_TS.IFO" in names
        assert "42A0060 - FBI#2 - VIDEO_TS.IFO" in names

    def test_the_full_path_is_kept_for_reference(self, monkeypatch):
        c = next(
            c for c in self._fetch(monkeypatch)
            if c.name == "42A0061 - FBI#3 - VTS_01_1.VOB"
        )
        assert c.path == "Rel/42A0061 - FBI#3/VIDEO_TS/VTS_01_1.VOB"
        # The URL is built from the path, so it is unaffected by the rename.
        assert c.url.endswith("VIDEO_TS/VTS_01_1.VOB")

    def test_the_full_path_is_the_fallback_when_an_ancestor_would_collide(
        self, monkeypatch
    ):
        # The ancestor rename would produce "x - f.bin", which the third file
        # already holds — so taking it would trade one collision for another.
        payload = {
            "files": [
                {"name": "d/x/f.bin", "format": "Bin", "size": "10", "md5": "1"},
                {"name": "d/y/f.bin", "format": "Bin", "size": "20", "md5": "2"},
                {"name": "d/x - f.bin", "format": "Bin", "size": "30", "md5": "3"},
            ]
        }
        names = [c.name for c in self._fetch(monkeypatch, payload)]
        assert len(set(names)) == len(names)
        assert "d - x - f.bin" in names
        assert "x - f.bin" in names  # the pre-existing name is untouched

    def test_a_file_with_no_ancestor_keeps_its_bare_name(self, monkeypatch):
        # Nothing to prepend for the root-level one; the other still gets its
        # directory, which is enough to separate them.
        payload = {
            "files": [
                {"name": "f.bin", "format": "Bin", "size": "10", "md5": "1"},
                {"name": "d/f.bin", "format": "Bin", "size": "20", "md5": "2"},
            ]
        }
        names = [c.name for c in self._fetch(monkeypatch, payload)]
        assert names == ["f.bin", "d - f.bin"]
