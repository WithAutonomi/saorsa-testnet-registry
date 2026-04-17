from unittest.mock import patch

import pytest

from saorsa_deploy import vultr_availability

PLANS_PAGE = {
    "plans": [
        {
            "id": "vc2-4c-8gb",
            "vcpu_count": 4,
            "ram": 8192,
            "disk": 160,
            "monthly_cost": 40.0,
            "locations": ["ewr", "ord", "lhr"],
        },
        {
            "id": "vc2-8c-32gb",
            "vcpu_count": 8,
            "ram": 32768,
            "disk": 640,
            "monthly_cost": 160.0,
            "locations": ["lhr", "fra", "ams"],
        },
        {
            "id": "vhp-8c-16gb-amd",
            "vcpu_count": 8,
            "ram": 16384,
            "disk": 350,
            "monthly_cost": 96.0,
            "locations": ["ewr", "lhr", "fra"],
        },
    ],
    "meta": {"links": {"next": ""}},
}


def _mock_fetch_with(map_):
    def fake(path, token):
        for key, value in map_.items():
            if path.startswith(key):
                return value
        raise AssertionError(f"unexpected path: {path}")

    return fake


class TestFetchPlans:
    def test_returns_flat_list(self):
        with patch.object(
            vultr_availability, "_fetch",
            side_effect=_mock_fetch_with({"/plans": PLANS_PAGE}),
        ):
            plans = vultr_availability.fetch_plans("t")
        assert [p["id"] for p in plans] == [
            "vc2-4c-8gb", "vc2-8c-32gb", "vhp-8c-16gb-amd"
        ]


class TestFetchPlanAvailabilityMap:
    def test_inverts_plan_locations(self):
        with patch.object(
            vultr_availability, "_fetch",
            side_effect=_mock_fetch_with({"/plans": PLANS_PAGE}),
        ):
            avail = vultr_availability.fetch_plan_availability_map("t")
        assert avail["ewr"] == {"vc2-4c-8gb", "vhp-8c-16gb-amd"}
        assert avail["lhr"] == {"vc2-4c-8gb", "vc2-8c-32gb", "vhp-8c-16gb-amd"}
        assert avail["fra"] == {"vc2-8c-32gb", "vhp-8c-16gb-amd"}
        assert avail["ams"] == {"vc2-8c-32gb"}


class TestResolveVultrRegions:
    def _patch(self):
        return patch.object(
            vultr_availability, "_fetch",
            side_effect=_mock_fetch_with({"/plans": PLANS_PAGE}),
        )

    def test_happy_path_uses_preferred_regions_in_order(self):
        with self._patch():
            resolved, notes = vultr_availability.resolve_vultr_regions(
                preferred=["lhr", "fra", "ams"],
                plan="vc2-8c-32gb",
                count=3,
                token="t",
            )
        assert resolved == ["lhr", "fra", "ams"]
        assert notes == []

    def test_falls_back_when_preferred_lacks_plan(self):
        with self._patch():
            resolved, notes = vultr_availability.resolve_vultr_regions(
                preferred=["ewr", "ord", "dfw"],
                plan="vc2-8c-32gb",
                count=2,
                token="t",
                fallback_pool=["ewr", "ord", "dfw", "lhr", "fra", "ams"],
            )
        # ewr/ord/dfw don't offer vc2-8c-32gb — first two fallbacks are lhr, fra.
        assert resolved == ["lhr", "fra"]
        assert len(notes) == 2

    def test_raises_when_insufficient_regions(self):
        with self._patch():
            with pytest.raises(RuntimeError, match="only 1 region"):
                vultr_availability.resolve_vultr_regions(
                    preferred=["ewr", "ord"],
                    plan="vc2-8c-32gb",
                    count=5,
                    token="t",
                    fallback_pool=["ewr", "ord", "lhr"],
                )

    def test_raises_when_plan_doesnt_exist(self):
        with self._patch():
            with pytest.raises(RuntimeError, match="not offered in any region"):
                vultr_availability.resolve_vultr_regions(
                    preferred=["ewr"],
                    plan="bogus-plan",
                    count=1,
                    token="t",
                )

    def test_returns_empty_when_count_zero(self):
        with patch.object(
            vultr_availability, "_fetch",
            side_effect=AssertionError("should not be called"),
        ):
            resolved, notes = vultr_availability.resolve_vultr_regions(
                preferred=["ewr"],
                plan="vc2-4c-8gb",
                count=0,
                token="t",
            )
        assert resolved == []
        assert notes == []
