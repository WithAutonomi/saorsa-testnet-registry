"""Catalogue-side tests for the production upload file pool repository.

The behaviour worth pinning here is duplicate linking. An Internet Archive URL
and a local copy of the same file are two rows describing one piece of content,
and uploading either puts that content on the network — so the pool must treat
them as one. These tests assert the link is formed on a shared digest, that it
stays flat, and that availability follows the content rather than the row.
"""

from datetime import datetime, timezone

import pytest
from saorsa_db.db import Base
from saorsa_db.models.prod_file_pool import (
    ClaimSource,
    ClaimStatus,
    PoolFileKind,
    ProdFilePoolClaim,
)
from saorsa_db.repositories import (
    POOL_STATUS_AVAILABLE,
    POOL_STATUS_CLAIMED,
    POOL_STATUS_UPLOADED,
    SqlAlchemyProdFilePoolRepository,
    locator_digest,
)
from saorsa_db.size_class import SizeClass
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

MIB = 1024 * 1024
GIB = 1024 * MIB
NOW = datetime(2026, 7, 30, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield SqlAlchemyProdFilePoolRepository(session)


def _claim(repo, file_id, status=ClaimStatus.CLAIMED):
    claim = ProdFilePoolClaim(
        file_id=file_id,
        source=ClaimSource.MANUAL,
        status=status,
        claimed_at=NOW,
    )
    repo.session.add(claim)
    repo.session.commit()
    return claim


class TestAddPoolFile:
    def test_adds_a_url_entry(self, repo):
        pool_file, created = repo.add_pool_file(
            PoolFileKind.URL,
            "https://archive.org/download/x/a.bin",
            "a.bin",
            size_bytes=42 * MIB,
        )
        assert created is True
        assert pool_file.locator_sha256 == locator_digest(pool_file.locator)
        assert pool_file.active is True

    def test_readding_a_known_locator_is_a_noop(self, repo):
        first, created_first = repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/a.bin", "a.bin", size_bytes=1
        )
        second, created_second = repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/a.bin", "a.bin", size_bytes=1
        )
        assert created_first is True
        assert created_second is False
        assert first.id == second.id

    def test_readding_fills_in_a_size_learned_later(self, repo):
        # An entry added with --allow-unknown-size becomes usable once a later
        # pass resolves its size; it must not be duplicated to get there.
        pool_file, _ = repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/s", "s", size_bytes=None
        )
        again, created = repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/s", "s", size_bytes=5 * MIB
        )
        assert created is False
        assert again.id == pool_file.id
        assert again.size_bytes == 5 * MIB

    def test_readding_does_not_overwrite_a_known_size(self, repo):
        pool_file, _ = repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/s", "s", size_bytes=5 * MIB
        )
        again, _ = repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/s", "s", size_bytes=9 * MIB
        )
        assert again.size_bytes == 5 * MIB


class TestDuplicateLinking:
    def test_shared_digest_links_url_to_local(self, repo):
        local, _ = repo.add_pool_file(
            PoolFileKind.LOCAL,
            "/data/corpus/film.mkv",
            "film.mkv",
            size_bytes=800 * MIB,
            digests={"sha1": "ABC123", "sha256": "def456"},
        )
        # The Internet Archive publishes md5+sha1 in item metadata, so the URL
        # arrives carrying sha1 without anything being downloaded.
        mirror, created = repo.add_pool_file(
            PoolFileKind.URL,
            "https://archive.org/download/item/film.mkv",
            "film.mkv",
            size_bytes=800 * MIB,
            digests={"md5": "999", "sha1": "abc123"},
        )
        assert created is True
        assert mirror.duplicate_of == local.id

    def test_digest_matching_is_case_insensitive(self, repo):
        repo.add_pool_file(
            PoolFileKind.LOCAL, "/data/a", "a", digests={"sha1": "AbC"}
        )
        mirror, _ = repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/a", "a", digests={"SHA1": "aBc"}
        )
        assert mirror.duplicate_of is not None

    def test_no_shared_algorithm_means_no_link(self, repo):
        # The honest residual: a URL published with only md5 and a local file
        # hashed only with sha256 cannot be recognised as the same content.
        repo.add_pool_file(
            PoolFileKind.LOCAL, "/data/a", "a", digests={"sha256": "aaa"}
        )
        mirror, _ = repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/a", "a", digests={"md5": "bbb"}
        )
        assert mirror.duplicate_of is None

    def test_links_stay_flat(self, repo):
        # A duplicate must point at the canonical row, never at another
        # duplicate — availability resolves with a single COALESCE, so a chain
        # would silently leave the far end available.
        original, _ = repo.add_pool_file(
            PoolFileKind.LOCAL, "/data/a", "a", digests={"sha1": "aaa"}
        )
        second, _ = repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/1", "a", digests={"sha1": "aaa"}
        )
        third, _ = repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/2", "a", digests={"sha1": "aaa"}
        )
        assert second.duplicate_of == original.id
        assert third.duplicate_of == original.id
        assert repo.canonical_pool_file_id(third.id) == original.id


class TestAvailabilityFollowsContent:
    def test_claiming_the_local_copy_spends_the_mirror(self, repo):
        local, _ = repo.add_pool_file(
            PoolFileKind.LOCAL,
            "/data/film.mkv",
            "film.mkv",
            size_bytes=800 * MIB,
            digests={"sha1": "aaa"},
        )
        mirror, _ = repo.add_pool_file(
            PoolFileKind.URL,
            "https://archive.org/download/i/film.mkv",
            "film.mkv",
            size_bytes=800 * MIB,
            digests={"sha1": "aaa"},
        )
        _claim(repo, local.id, ClaimStatus.UPLOADED)

        available = repo.list_pool_files(status=POOL_STATUS_AVAILABLE)
        assert local.id not in {f.id for f in available}
        # The whole point: the URL is spent too, because the content is on the
        # network regardless of which locator delivered it.
        assert mirror.id not in {f.id for f in available}

    def test_released_claim_returns_the_content(self, repo):
        pool_file, _ = repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/a", "a", size_bytes=MIB
        )
        _claim(repo, pool_file.id, ClaimStatus.RELEASED)
        available = repo.list_pool_files(status=POOL_STATUS_AVAILABLE)
        assert pool_file.id in {f.id for f in available}


class TestListFiltering:
    def _seed(self, repo):
        sizes = {
            "tiny": 5 * MIB,
            "small": 50 * MIB,
            "medium": 500 * MIB,
            "large": 2 * GIB,
        }
        made = {}
        for label, size in sizes.items():
            made[label], _ = repo.add_pool_file(
                PoolFileKind.URL,
                f"https://example.org/{label}",
                f"{label}.bin",
                size_bytes=size,
                tags=["corpus"] if label == "tiny" else ["video"],
            )
        made["local"], _ = repo.add_pool_file(
            PoolFileKind.LOCAL, "/data/x", "x", size_bytes=5 * MIB
        )
        return made

    def test_filters_by_size_class(self, repo):
        made = self._seed(repo)
        rows = repo.list_pool_files(kind=PoolFileKind.URL, size_class=SizeClass.MEDIUM)
        assert [r.id for r in rows] == [made["medium"].id]

    def test_filters_by_kind(self, repo):
        made = self._seed(repo)
        rows = repo.list_pool_files(kind=PoolFileKind.LOCAL)
        assert [r.id for r in rows] == [made["local"].id]

    def test_filters_by_tag(self, repo):
        made = self._seed(repo)
        rows = repo.list_pool_files(tags=["corpus"])
        assert [r.id for r in rows] == [made["tiny"].id]

    def test_unknown_size_is_excluded_from_every_class(self, repo):
        repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/stream", "stream", size_bytes=None
        )
        for size_class in SizeClass:
            assert repo.list_pool_files(size_class=size_class) == []

    def test_inactive_entries_are_hidden_by_default(self, repo):
        made = self._seed(repo)
        repo.set_pool_file_active(made["tiny"].id, False)
        rows = repo.list_pool_files(kind=PoolFileKind.URL)
        assert made["tiny"].id not in {r.id for r in rows}
        assert made["tiny"].id in {
            r.id for r in repo.list_pool_files(kind=PoolFileKind.URL, active=None)
        }

    def test_status_filters_are_distinct(self, repo):
        made = self._seed(repo)
        _claim(repo, made["tiny"].id, ClaimStatus.CLAIMED)
        _claim(repo, made["small"].id, ClaimStatus.UPLOADED)

        claimed = {f.id for f in repo.list_pool_files(status=POOL_STATUS_CLAIMED)}
        uploaded = {f.id for f in repo.list_pool_files(status=POOL_STATUS_UPLOADED)}
        available = {f.id for f in repo.list_pool_files(status=POOL_STATUS_AVAILABLE)}

        # A checkout awaiting resolution must never be reported as on the
        # network, and neither must appear as available.
        assert claimed == {made["tiny"].id}
        assert uploaded == {made["small"].id}
        assert not (claimed | uploaded) & available

    def test_unknown_status_is_rejected(self, repo):
        with pytest.raises(ValueError, match="unknown pool status"):
            repo.list_pool_files(status="spent")

    def test_limit_applies(self, repo):
        self._seed(repo)
        assert len(repo.list_pool_files(limit=2)) == 2


class TestStats:
    def test_counts_split_by_kind_and_class(self, repo):
        repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/a", "a", size_bytes=50 * MIB
        )
        taken, _ = repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/b", "b", size_bytes=50 * MIB
        )
        _claim(repo, taken.id, ClaimStatus.UPLOADED)
        repo.add_pool_file(
            PoolFileKind.LOCAL, "/data/c", "c", size_bytes=5 * MIB
        )

        stats = repo.pool_stats()
        assert stats["url"]["by_class"]["small"] == {"available": 1, "total": 2}
        assert stats["local"]["by_class"]["tiny"] == {"available": 1, "total": 1}
        # A remote uploader cannot use the local entry, which is why the two
        # inventories are never summed.
        assert stats["url"]["by_class"]["tiny"] == {"available": 0, "total": 0}

    def test_unknown_size_counted_separately(self, repo):
        repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/s", "s", size_bytes=None
        )
        stats = repo.pool_stats()
        assert stats["url"]["unknown_size"] == 1
        assert all(
            entry["total"] == 0 for entry in stats["url"]["by_class"].values()
        )


class TestDigests:
    def test_adding_digests_is_idempotent_per_algorithm(self, repo):
        pool_file, _ = repo.add_pool_file(
            PoolFileKind.LOCAL, "/data/a", "a", digests={"sha1": "aaa"}
        )
        repo.add_pool_file_digests(pool_file.id, {"sha1": "bbb", "sha256": "ccc"})
        repo.session.refresh(pool_file)
        by_algo = {d.algo: d.value for d in pool_file.digests}
        # The first value for an algorithm wins; a second must not conflict with
        # the unique constraint nor silently replace what we already trusted.
        assert by_algo == {"sha1": "aaa", "sha256": "ccc"}
