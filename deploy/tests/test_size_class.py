"""Tests for the upload-pool size classification.

The boundary cases are the point of this suite: the classes are half-open, so
exactly 10 MiB must be ``small`` and not ``tiny``. An off-by-one here would
misroute selection silently — a manifest asking for "tiny" would quietly get a
100 MB file, and nothing downstream would notice.
"""

import pytest
from saorsa_db.size_class import (
    SizeClass,
    byte_range,
    classify,
    describe,
    format_size,
    ordered,
    parse,
)

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB


class TestClassify:
    @pytest.mark.parametrize(
        "size_bytes,expected",
        [
            (0, SizeClass.TINY),
            (1, SizeClass.TINY),
            (10 * MIB - 1, SizeClass.TINY),
            (10 * MIB, SizeClass.SMALL),
            (100 * MIB - 1, SizeClass.SMALL),
            (100 * MIB, SizeClass.MEDIUM),
            (GIB - 1, SizeClass.MEDIUM),
            (GIB, SizeClass.LARGE),
            (5 * GIB - 1, SizeClass.LARGE),
            (5 * GIB, SizeClass.XLARGE),
            (500 * GIB, SizeClass.XLARGE),
        ],
    )
    def test_boundaries_are_half_open(self, size_bytes, expected):
        assert classify(size_bytes) is expected

    def test_negative_size_is_rejected(self):
        # A negative size means a failed stat or a misparsed HEAD upstream.
        # Bucketing it as tiny would hide the real bug.
        with pytest.raises(ValueError, match="negative"):
            classify(-1)

    def test_every_class_is_reachable(self):
        reached = {classify(n) for n in (0, 50 * MIB, 500 * MIB, 2 * GIB, 9 * GIB)}
        assert reached == set(ordered())

    def test_measured_canary_sizes_classify_as_observed(self):
        # Real sizes taken from prod_canary_uploads on 2026-07-30. The measured
        # distribution was small+medium heavy with nothing in large/xlarge; if a
        # boundary moves, this is what changes underneath the pool sizing.
        assert classify(4_639_426) is SizeClass.TINY
        assert classify(6_981_520) is SizeClass.TINY
        assert classify(323_586_541) is SizeClass.MEDIUM
        assert classify(832_643_940) is SizeClass.MEDIUM
        # 997.4 MiB — reads as "1.05 GB" in decimal but is under 1 GiB, so it
        # is medium. This is why the canary distribution measured zero `large`
        # files despite several approaching a gigabyte.
        assert classify(1_045_869_721) is SizeClass.MEDIUM
        assert classify(2 * GIB) is SizeClass.LARGE


class TestByteRange:
    def test_range_round_trips_through_classify(self):
        for size_class in ordered():
            lower, upper = byte_range(size_class)
            assert classify(lower) is size_class
            if upper is not None:
                assert classify(upper - 1) is size_class
                assert classify(upper) is not size_class

    def test_ranges_are_contiguous_and_start_at_zero(self):
        # A gap would make some byte count unclassifiable; an overlap would let
        # one file satisfy two different class requests.
        previous_upper = 0
        for size_class in ordered():
            lower, upper = byte_range(size_class)
            assert lower == previous_upper
            previous_upper = upper if upper is not None else previous_upper
        assert byte_range(ordered()[-1])[1] is None

    def test_unknown_class_is_rejected(self):
        with pytest.raises(ValueError, match="unknown size class"):
            byte_range("small")  # a bare string is not a SizeClass


class TestParse:
    @pytest.mark.parametrize("token", ["tiny", "TINY", "  Small  ", "medium"])
    def test_accepts_case_and_whitespace_variants(self, token):
        assert parse(token) is parse(token.strip().lower())

    def test_maps_to_the_right_member(self):
        assert parse("xlarge") is SizeClass.XLARGE

    def test_unknown_name_lists_the_valid_ones(self):
        with pytest.raises(ValueError) as exc:
            parse("huge")
        message = str(exc.value)
        assert "huge" in message
        for size_class in ordered():
            assert size_class.value in message

    def test_non_string_is_rejected(self):
        with pytest.raises(ValueError, match="must be a string"):
            parse(10 * MIB)


class TestFormatting:
    def test_format_size_uses_binary_units(self):
        assert format_size(0) == "0 B"
        assert format_size(512) == "512 B"
        assert format_size(10 * MIB) == "10.00 MB"
        assert format_size(GIB) == "1.00 GB"

    def test_format_size_handles_unknown(self):
        assert format_size(None) == "<unknown>"

    def test_describe_bounds_each_class(self):
        assert describe(SizeClass.TINY) == "0 B – 10.00 MB"
        assert describe(SizeClass.XLARGE) == "5.00 GB+"
