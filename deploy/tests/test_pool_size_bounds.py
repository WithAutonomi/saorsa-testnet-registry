"""Byte bounds narrowing a size-class pool request.

A class is sometimes too coarse to express what a run needs. The canary is the
case that forced this: it measures chunk loss, the chunk floor is 4, and a 14 KB
file therefore buys the same minimum observation as a 5 MB one while costing the
same single pool entry. "``tiny``, but at least 5 MB" is not a class.

The bounds *narrow* a request rather than replacing it — the counts still say
how many of each class to take — so the shape of a selection (37 tiny / 233
small / 112 medium) is preserved.
"""

import pytest
from saorsa_db.db import Base
from saorsa_db.models.prod_file_pool import ClaimSource, PoolFileKind
from saorsa_db.repositories import (
    InsufficientPoolInventory,
    SqlAlchemyProdFilePoolRepository,
)
from saorsa_db.size_class import (
    SizeClass,
    bounded_byte_range,
    describe_bounds,
    parse_size,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy.pool_selection import (
    PoolSelectionError,
    describe_request,
    parse_size_bounds,
    validate_bounds,
)

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB


@pytest.fixture
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield SqlAlchemyProdFilePoolRepository(session)


def _seed(repo, size_bytes, count, prefix):
    for i in range(count):
        repo.add_pool_file(
            PoolFileKind.URL,
            f"https://example.org/{prefix}{i}",
            f"{prefix}{i}.bin",
            size_bytes=size_bytes,
        )


class TestParseSize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("5MB", 5 * MIB),
            ("5mb", 5 * MIB),
            ("5 MB", 5 * MIB),
            ("5MiB", 5 * MIB),
            ("1.5GB", int(1.5 * GIB)),
            ("512KB", 512 * KIB),
            ("1048576", 1048576),
            ("900B", 900),
            (5242880, 5242880),
        ],
    )
    def test_accepted_forms(self, raw, expected):
        assert parse_size(raw) == expected

    def test_units_are_binary_matching_how_sizes_are_displayed(self):
        # A bound pasted back from displayed output must select the file it was
        # read from, so MB here is MiB — not 10**6.
        assert parse_size("1MB") == 1048576

    @pytest.mark.parametrize("raw", ["", "big", "5PB", "5 gigs", "-1", "-5MB", None, True])
    def test_rejected_forms(self, raw):
        # No silent fallback: a bound parsing to zero would widen the selection
        # instead of narrowing it, invisibly, until files had been spent.
        with pytest.raises(ValueError):
            parse_size(raw)


class TestBoundedByteRange:
    def test_min_narrows_the_floor(self):
        assert bounded_byte_range(SizeClass.TINY, 5 * MIB, None) == (5 * MIB, 10 * MIB)

    def test_max_is_exclusive_so_bounds_can_name_a_class_exactly(self):
        assert bounded_byte_range(SizeClass.SMALL, 10 * MIB, 100 * MIB) == bounded_byte_range(
            SizeClass.SMALL
        )

    def test_a_bound_wider_than_the_class_is_inert(self):
        assert bounded_byte_range(SizeClass.SMALL, 1 * MIB, 1 * GIB) == (
            10 * MIB,
            100 * MIB,
        )

    def test_min_can_bound_the_unbounded_top_class(self):
        assert bounded_byte_range(SizeClass.XLARGE, 6 * GIB, None) == (6 * GIB, None)

    def test_empty_intersection_is_reported_not_silently_empty(self):
        assert bounded_byte_range(SizeClass.MEDIUM, None, 5 * MIB) is None


class TestParseSizeBounds:
    def test_absent_bounds(self):
        assert parse_size_bounds(None, None) == (None, None)

    def test_blank_strings_are_absent(self):
        assert parse_size_bounds("", "  ") == (None, None)

    def test_inverted_pair_is_refused(self):
        with pytest.raises(PoolSelectionError, match="must be below"):
            parse_size_bounds("100MB", "10MB")

    def test_equal_pair_is_refused_because_max_is_exclusive(self):
        with pytest.raises(PoolSelectionError, match="must be below"):
            parse_size_bounds("10MB", "10MB")

    def test_zero_max_is_refused(self):
        with pytest.raises(PoolSelectionError, match="selects nothing"):
            parse_size_bounds(None, "0")

    def test_unparseable_bound_names_which_one(self):
        with pytest.raises(PoolSelectionError, match="invalid max_size"):
            parse_size_bounds("5MB", "later")


class TestValidateBounds:
    def test_bounds_excluding_a_requested_class_are_refused_up_front(self):
        # Not left to surface as a shortfall of "0 available", which reads as an
        # empty pool and sends the operator cataloguing files that would not
        # have been selected either.
        with pytest.raises(PoolSelectionError, match="exclude the 'small' class"):
            validate_bounds({SizeClass.SMALL: 3}, None, 5 * MIB)

    def test_a_class_the_request_does_not_ask_for_is_not_checked(self):
        validate_bounds({SizeClass.TINY: 3}, None, 5 * MIB)

    def test_unbounded_request_passes(self):
        validate_bounds({SizeClass.SMALL: 3}, None, None)


class TestDescription:
    def test_request_carries_its_bounds(self):
        rendered = describe_request({SizeClass.TINY: 4}, 5 * MIB, None)
        assert rendered == "tiny 4 (≥ 5.00 MB)"

    def test_unbounded_request_renders_as_before(self):
        assert describe_request({SizeClass.TINY: 4}) == "tiny 4"

    def test_both_bounds(self):
        assert describe_bounds(10 * MIB, 100 * MIB) == "10.00 MB – 100.00 MB"


class TestClaimingWithBounds:
    def test_min_size_keeps_the_bottom_of_a_class_out(self, repo):
        _seed(repo, 14 * KIB, 5, "sliver")
        _seed(repo, 6 * MIB, 3, "chunky")

        claims = repo.claim_pool_files(
            PoolFileKind.URL,
            {SizeClass.TINY: 3},
            ClaimSource.PROD_CANARY,
            min_size_bytes=5 * MIB,
        )

        assert len(claims) == 3
        sizes = {repo.get_pool_file(c.file_id).size_bytes for c in claims}
        assert sizes == {6 * MIB}

    def test_shortfall_counts_only_what_the_bounds_allow(self, repo):
        # 20 files in the class, but only 2 above the bound.
        _seed(repo, 1 * MIB, 18, "sliver")
        _seed(repo, 6 * MIB, 2, "chunky")

        with pytest.raises(InsufficientPoolInventory) as excinfo:
            repo.claim_pool_files(
                PoolFileKind.URL,
                {SizeClass.TINY: 5},
                ClaimSource.PROD_CANARY,
                min_size_bytes=5 * MIB,
            )

        assert excinfo.value.shortfall["tiny"]["available"] == 2
        # Without the bounds in the message, "2 available" reads as a whole-class
        # figure and sends the operator looking for files the bounds exclude.
        assert "5.00 MB" in str(excinfo.value)

    def test_nothing_is_claimed_when_the_bounded_request_falls_short(self, repo):
        _seed(repo, 1 * MIB, 4, "sliver")
        _seed(repo, 6 * MIB, 1, "chunky")

        with pytest.raises(InsufficientPoolInventory):
            repo.claim_pool_files(
                PoolFileKind.URL,
                {SizeClass.TINY: 3},
                ClaimSource.PROD_CANARY,
                min_size_bytes=5 * MIB,
            )
        assert repo.list_pool_claims() == []

    def test_max_size_is_exclusive_at_the_boundary(self, repo):
        _seed(repo, 10 * MIB, 2, "onboundary")
        _seed(repo, 9 * MIB, 2, "under")

        claims = repo.claim_pool_files(
            PoolFileKind.URL,
            {SizeClass.TINY: 2},
            ClaimSource.PROD_UPLOADERS,
            max_size_bytes=10 * MIB,
        )
        sizes = {repo.get_pool_file(c.file_id).size_bytes for c in claims}
        assert sizes == {9 * MIB}

    def test_counts_per_class_are_unchanged_by_bounds(self, repo):
        _seed(repo, 6 * MIB, 4, "tinyok")
        _seed(repo, 20 * MIB, 4, "smallok")

        claims = repo.claim_pool_files(
            PoolFileKind.URL,
            {SizeClass.TINY: 2, SizeClass.SMALL: 3},
            ClaimSource.PROD_CANARY,
            min_size_bytes=5 * MIB,
        )

        sizes = [repo.get_pool_file(c.file_id).size_bytes for c in claims]
        assert sorted(sizes) == [6 * MIB, 6 * MIB, 20 * MIB, 20 * MIB, 20 * MIB]

    def test_unknown_size_entries_can_never_satisfy_a_bound(self, repo):
        repo.add_pool_file(PoolFileKind.URL, "https://example.org/mystery", "mystery.bin")
        _seed(repo, 6 * MIB, 1, "chunky")

        with pytest.raises(InsufficientPoolInventory) as excinfo:
            repo.claim_pool_files(
                PoolFileKind.URL,
                {SizeClass.TINY: 2},
                ClaimSource.PROD_CANARY,
                min_size_bytes=5 * MIB,
            )
        assert excinfo.value.shortfall["tiny"]["available"] == 1


class TestListingWithBounds:
    def test_bounds_apply_without_a_class(self, repo):
        _seed(repo, 1 * MIB, 2, "sliver")
        _seed(repo, 50 * MIB, 3, "small")
        _seed(repo, 2 * GIB, 1, "large")

        rows = repo.list_pool_files(min_size_bytes=5 * MIB, max_size_bytes=1 * GIB)
        assert {r.size_bytes for r in rows} == {50 * MIB}

    def test_bounds_intersect_with_a_class(self, repo):
        _seed(repo, 1 * MIB, 2, "sliver")
        _seed(repo, 6 * MIB, 2, "chunky")

        rows = repo.list_pool_files(size_class=SizeClass.TINY, min_size_bytes=5 * MIB)
        assert {r.size_bytes for r in rows} == {6 * MIB}

    def test_a_class_the_bounds_exclude_returns_nothing(self, repo):
        _seed(repo, 50 * MIB, 3, "small")
        assert repo.list_pool_files(size_class=SizeClass.SMALL, max_size_bytes=5 * MIB) == []
