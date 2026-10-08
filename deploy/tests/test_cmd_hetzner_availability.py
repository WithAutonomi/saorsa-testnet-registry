from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy import hetzner_availability
from saorsa_deploy.cmd.hetzner_availability import (
    _build_availability_table,
    _filter_types,
    _resolve_regions,
    cmd_hetzner_availability,
)

SERVER_TYPES = [
    {
        "id": 109,
        "name": "cpx22",
        "cores": 3,
        "memory": 4,
        "disk": 80,
        "cpu_type": "shared",
        "architecture": "x86",
    },
    {
        "id": 45,
        "name": "cpx41",
        "cores": 8,
        "memory": 16,
        "disk": 240,
        "cpu_type": "shared",
        "architecture": "x86",
    },
    {
        "id": 99,
        "name": "cax11",
        "cores": 2,
        "memory": 4,
        "disk": 40,
        "cpu_type": "shared",
        "architecture": "arm",
    },
]

AVAILABILITY = {
    "fsn1": {45, 109, 99},
    "nbg1": {109, 99},
    "hel1": {45, 109},
    "sin": {45},
}


class TestFilterTypes:
    def test_no_filter_sorts_by_name(self):
        result = _filter_types(SERVER_TYPES, None)
        assert [s["name"] for s in result] == ["cax11", "cpx22", "cpx41"]

    def test_filter_returns_single_match(self):
        result = _filter_types(SERVER_TYPES, "cpx41")
        assert len(result) == 1
        assert result[0]["name"] == "cpx41"

    def test_filter_raises_on_unknown(self):
        with pytest.raises(ValueError, match="Unknown Hetzner server type"):
            _filter_types(SERVER_TYPES, "bogus")


class TestResolveRegions:
    def test_no_filter_returns_all_sorted(self):
        assert _resolve_regions(None, AVAILABILITY) == ["fsn1", "hel1", "nbg1", "sin"]

    def test_filter_returns_subset_in_canonical_order(self):
        result = _resolve_regions(["nbg1", "fsn1"], AVAILABILITY)
        assert result == ["fsn1", "nbg1"]

    def test_unknown_region_raises(self):
        with pytest.raises(ValueError, match="Unknown Hetzner region"):
            _resolve_regions(["bogus"], AVAILABILITY)


class TestBuildAvailabilityTable:
    def test_row_per_type_and_column_per_region(self):
        table = _build_availability_table(SERVER_TYPES[:2], ["fsn1", "nbg1"], AVAILABILITY)
        # 6 metadata columns + 2 region columns
        assert len(table.columns) == 8
        assert [c.header for c in table.columns[:6]] == [
            "Type",
            "Cores",
            "Memory (GB)",
            "Disk (GB)",
            "CPU",
            "Arch",
        ]
        assert [c.header for c in table.columns[6:]] == ["fsn1", "nbg1"]
        assert table.row_count == 2

    def test_marks_availability_per_region(self):
        table = _build_availability_table(
            [SERVER_TYPES[1]],  # cpx41 — available fsn1, hel1, sin; not nbg1
            ["fsn1", "nbg1"],
            AVAILABILITY,
        )
        # Cells are stored on the columns, not rows; pull them out.
        fsn_cells = list(table.columns[6].cells)
        nbg_cells = list(table.columns[7].cells)
        assert "green" in fsn_cells[0]
        assert "red" in nbg_cells[0]


class TestCmdHetznerAvailability:
    def test_errors_without_token(self, monkeypatch, capsys):
        monkeypatch.delenv("HCLOUD_TOKEN", raising=False)
        args = SimpleNamespace(server_type=None, region=None)
        with pytest.raises(SystemExit):
            cmd_hetzner_availability(args)
        out = capsys.readouterr().out
        assert "HCLOUD_TOKEN" in out

    def test_happy_path_renders_table(self, monkeypatch):
        monkeypatch.setenv("HCLOUD_TOKEN", "t")

        def fake_fetch(path, token):
            if path.startswith("/server_types"):
                return {
                    "server_types": SERVER_TYPES,
                    "meta": {"pagination": {"next_page": None}},
                }
            if path.startswith("/datacenters"):
                return {
                    "datacenters": [
                        {
                            "location": {"name": loc},
                            "server_types": {"available": list(ids)},
                        }
                        for loc, ids in AVAILABILITY.items()
                    ]
                }
            raise AssertionError(path)

        args = SimpleNamespace(server_type=None, region=None)
        with patch.object(hetzner_availability, "_fetch", side_effect=fake_fetch):
            cmd_hetzner_availability(args)

    def test_server_type_filter_errors_on_unknown(self, monkeypatch, capsys):
        monkeypatch.setenv("HCLOUD_TOKEN", "t")

        def fake_fetch(path, token):
            if path.startswith("/server_types"):
                return {
                    "server_types": SERVER_TYPES,
                    "meta": {"pagination": {"next_page": None}},
                }
            return {"datacenters": []}

        args = SimpleNamespace(server_type="not-a-type", region=None)
        with patch.object(hetzner_availability, "_fetch", side_effect=fake_fetch):
            with pytest.raises(SystemExit):
                cmd_hetzner_availability(args)
