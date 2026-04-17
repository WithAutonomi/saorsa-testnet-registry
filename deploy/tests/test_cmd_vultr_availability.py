from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy import vultr_availability
from saorsa_deploy.cmd.vultr_availability import (
    _build_availability_table,
    _filter_plans,
    _resolve_regions,
    cmd_vultr_availability,
)

PLANS = [
    {
        "id": "vc2-4c-8gb",
        "vcpu_count": 4,
        "ram": 8192,
        "disk": 160,
        "monthly_cost": 40.0,
        "locations": ["ewr", "lhr"],
    },
    {
        "id": "vc2-8c-32gb",
        "vcpu_count": 8,
        "ram": 32768,
        "disk": 640,
        "monthly_cost": 160.0,
        "locations": ["lhr", "fra"],
    },
]

AVAILABILITY = {
    "ewr": {"vc2-4c-8gb"},
    "lhr": {"vc2-4c-8gb", "vc2-8c-32gb"},
    "fra": {"vc2-8c-32gb"},
}


class TestFilterPlans:
    def test_no_filter_sorts_by_id(self):
        result = _filter_plans(PLANS, None)
        assert [p["id"] for p in result] == ["vc2-4c-8gb", "vc2-8c-32gb"]

    def test_filter_returns_single_match(self):
        result = _filter_plans(PLANS, "vc2-8c-32gb")
        assert len(result) == 1
        assert result[0]["id"] == "vc2-8c-32gb"

    def test_filter_raises_on_unknown(self):
        with pytest.raises(ValueError, match="Unknown Vultr plan"):
            _filter_plans(PLANS, "vc2-8c-16gb")


class TestResolveRegions:
    def test_no_filter_returns_all_sorted(self):
        assert _resolve_regions(None, AVAILABILITY) == ["ewr", "fra", "lhr"]

    def test_filter_returns_subset_in_canonical_order(self):
        assert _resolve_regions(["lhr", "ewr"], AVAILABILITY) == ["ewr", "lhr"]

    def test_unknown_region_raises(self):
        with pytest.raises(ValueError, match="Unknown Vultr region"):
            _resolve_regions(["bogus"], AVAILABILITY)


class TestBuildAvailabilityTable:
    def test_row_per_plan_and_column_per_region(self):
        table = _build_availability_table(PLANS, ["ewr", "lhr", "fra"], AVAILABILITY)
        # 5 metadata columns + 3 region columns
        assert len(table.columns) == 8
        assert [c.header for c in table.columns[:5]] == [
            "Plan", "vCPU", "RAM (MB)", "Disk (GB)", "$/mo"
        ]
        assert [c.header for c in table.columns[5:]] == ["ewr", "lhr", "fra"]
        assert table.row_count == 2

    def test_marks_availability_per_region(self):
        table = _build_availability_table(
            [PLANS[1]],  # vc2-8c-32gb — available lhr, fra; not ewr
            ["ewr", "lhr"],
            AVAILABILITY,
        )
        ewr_cells = list(table.columns[5].cells)
        lhr_cells = list(table.columns[6].cells)
        assert "red" in ewr_cells[0]
        assert "green" in lhr_cells[0]


class TestCmdVultrAvailability:
    def test_errors_without_token(self, monkeypatch, capsys):
        monkeypatch.delenv("VULTR_API_KEY", raising=False)
        args = SimpleNamespace(plan=None, region=None)
        with pytest.raises(SystemExit):
            cmd_vultr_availability(args)
        out = capsys.readouterr().out
        assert "VULTR_API_KEY" in out

    def test_happy_path_renders_table(self, monkeypatch):
        monkeypatch.setenv("VULTR_API_KEY", "t")

        def fake_fetch(path, token):
            if path.startswith("/plans"):
                return {"plans": PLANS, "meta": {"links": {"next": ""}}}
            raise AssertionError(path)

        args = SimpleNamespace(plan=None, region=None)
        with patch.object(vultr_availability, "_fetch", side_effect=fake_fetch):
            cmd_vultr_availability(args)

    def test_plan_filter_errors_on_unknown(self, monkeypatch):
        monkeypatch.setenv("VULTR_API_KEY", "t")

        def fake_fetch(path, token):
            if path.startswith("/plans"):
                return {"plans": PLANS, "meta": {"links": {"next": ""}}}
            raise AssertionError(path)

        args = SimpleNamespace(plan="vc2-8c-16gb", region=None)
        with patch.object(vultr_availability, "_fetch", side_effect=fake_fetch):
            with pytest.raises(SystemExit):
                cmd_vultr_availability(args)
