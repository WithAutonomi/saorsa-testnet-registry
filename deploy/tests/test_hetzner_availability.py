from unittest.mock import patch

import pytest

from saorsa_deploy import hetzner_availability

SERVER_TYPES_PAGE_1 = {
    "server_types": [
        {"id": 109, "name": "cpx22"},
        {"id": 45, "name": "cpx41"},
    ],
    "meta": {"pagination": {"next_page": None}},
}


DATACENTERS_FSN = {"location": {"name": "fsn1"}, "server_types": {"available": [45, 109]}}
DATACENTERS_NBG = {"location": {"name": "nbg1"}, "server_types": {"available": [109]}}
DATACENTERS_HEL = {"location": {"name": "hel1"}, "server_types": {"available": [45, 109]}}
DATACENTERS_SIN = {"location": {"name": "sin"}, "server_types": {"available": [45]}}


def _mock_fetch_with(map_):
    """Return a fake _fetch function that returns canned responses by path prefix."""

    def fake(path, token):
        for key, value in map_.items():
            if path.startswith(key):
                return value
        raise AssertionError(f"unexpected path: {path}")

    return fake


class TestFetchServerTypeId:
    def test_returns_id_for_known_type(self):
        with patch.object(
            hetzner_availability,
            "_fetch",
            side_effect=_mock_fetch_with({"/server_types": SERVER_TYPES_PAGE_1}),
        ):
            assert hetzner_availability.fetch_server_type_id("cpx41", "t") == 45

    def test_raises_for_unknown_type(self):
        with patch.object(
            hetzner_availability,
            "_fetch",
            side_effect=_mock_fetch_with({"/server_types": SERVER_TYPES_PAGE_1}),
        ):
            with pytest.raises(ValueError, match="not found"):
                hetzner_availability.fetch_server_type_id("bogus", "t")


class TestResolveHetznerRegions:
    def _patch(self, datacenters):
        return patch.object(
            hetzner_availability,
            "_fetch",
            side_effect=_mock_fetch_with(
                {
                    "/server_types": SERVER_TYPES_PAGE_1,
                    "/datacenters": {"datacenters": datacenters},
                }
            ),
        )

    def test_happy_path_uses_preferred_regions_in_order(self):
        with self._patch([DATACENTERS_FSN, DATACENTERS_NBG, DATACENTERS_HEL]):
            resolved, notes = hetzner_availability.resolve_hetzner_regions(
                preferred=["fsn1", "nbg1", "hel1"],
                server_type="cpx41",
                count=2,
                token="t",
                fallback_pool=["fsn1", "nbg1", "hel1", "sin"],
            )
        # nbg1 doesn't have cpx41 → skipped; fsn1 + hel1 picked in order.
        assert resolved == ["fsn1", "hel1"]
        # hel1 WAS in the preferred region list (position 2), so no fallback note.
        # But preferred[:count] = ["fsn1", "nbg1"] and hel1 is not in that slice,
        # so a fallback note is expected.
        assert any("fallback" in n and "hel1" in n for n in notes)

    def test_falls_back_to_pool_regions_when_preferred_insufficient(self):
        # Make the first N preferred regions fail, force fallback to sin.
        with self._patch([DATACENTERS_NBG, DATACENTERS_SIN]):
            resolved, notes = hetzner_availability.resolve_hetzner_regions(
                preferred=["fsn1", "hel1"],
                server_type="cpx41",
                count=1,
                token="t",
                fallback_pool=["fsn1", "nbg1", "hel1", "sin"],
            )
        assert resolved == ["sin"]
        assert notes and "sin" in notes[0]

    def test_raises_when_insufficient_capacity(self):
        with self._patch([DATACENTERS_NBG]):  # only nbg1 available, no cpx41
            with pytest.raises(RuntimeError, match="only 0 region"):
                hetzner_availability.resolve_hetzner_regions(
                    preferred=["fsn1", "nbg1"],
                    server_type="cpx41",
                    count=1,
                    token="t",
                    fallback_pool=["fsn1", "nbg1", "hel1", "sin"],
                )

    def test_returns_empty_when_count_zero(self):
        # Must not make any API calls — patch _fetch to fail if called.
        with patch.object(
            hetzner_availability,
            "_fetch",
            side_effect=AssertionError("should not be called"),
        ):
            resolved, notes = hetzner_availability.resolve_hetzner_regions(
                preferred=["fsn1"],
                server_type="cpx41",
                count=0,
                token="t",
            )
        assert resolved == []
        assert notes == []


class TestFetchServerTypes:
    def test_returns_all_types_from_single_page(self):
        with patch.object(
            hetzner_availability,
            "_fetch",
            side_effect=_mock_fetch_with({"/server_types": SERVER_TYPES_PAGE_1}),
        ):
            result = hetzner_availability.fetch_server_types("t")
        assert [st["name"] for st in result] == ["cpx22", "cpx41"]

    def test_paginates_until_exhausted(self):
        page_1 = {
            "server_types": [{"id": 1, "name": "a"}],
            "meta": {"pagination": {"next_page": 2}},
        }
        page_2 = {
            "server_types": [{"id": 2, "name": "b"}],
            "meta": {"pagination": {"next_page": None}},
        }

        call_log = []

        def fake(path, token):
            call_log.append(path)
            return page_2 if "page=2" in path else page_1

        with patch.object(hetzner_availability, "_fetch", side_effect=fake):
            result = hetzner_availability.fetch_server_types("t")
        assert [st["name"] for st in result] == ["a", "b"]
        assert len(call_log) == 2


class TestFetchAvailabilityMap:
    def test_unions_across_datacenters_in_same_location(self):
        dcs = [
            {"location": {"name": "fsn1"}, "server_types": {"available": [1, 2]}},
            {"location": {"name": "fsn1"}, "server_types": {"available": [3]}},
        ]
        with patch.object(
            hetzner_availability,
            "_fetch",
            side_effect=_mock_fetch_with({"/datacenters": {"datacenters": dcs}}),
        ):
            avail = hetzner_availability.fetch_availability_map("t")
        assert avail["fsn1"] == {1, 2, 3}
