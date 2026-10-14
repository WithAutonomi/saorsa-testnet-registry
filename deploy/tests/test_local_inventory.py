"""Retiring a destroyed deployment's local inventory snapshot.

V2-1530 ran `df` against 14 addresses from a `DEV-02` snapshot written 10 days
earlier for a deployment that had since been destroyed; all 14 had been
released back to the provider. The snapshot is the evidence of a run, so a
retirement RENAMES it — and it must be unmistakably the right file, because the
only thing distinguishing one run's snapshot from the next's is which
deployment wrote it.
"""

import json
from datetime import datetime, timezone

import pytest
from rich.console import Console

from saorsa_deploy.local_inventory import (
    default_inventory_path,
    inventory_testnet_id,
    retire_local_inventory,
)

STAMP = datetime(2026, 10, 14, 1, 2, 3, tzinfo=timezone.utc)


def _snapshot(root, name, *, testnet_id=None, manifest=True):
    path = root / name / "inventory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 7,
                "testnet_id": name if testnet_id is None else testnet_id,
                "created_at": "2026-10-03T21:13:45Z",
                "node_vms": [{"name": f"{name}-ant-node-lon1-1", "ip": "10.0.0.1"}],
            }
        )
    )
    if manifest:
        (path.parent / "devnet-manifest.json").write_text(json.dumps({"node_count": 14}))
    return path


class TestRetirement:
    def test_renames_both_files_with_a_utc_stamp(self, local_inventory_root):
        path = _snapshot(local_inventory_root, "DEV-02")

        renamed = retire_local_inventory("DEV-02", Console(), now=STAMP)

        assert [p.name for _, p in renamed] == [
            "inventory.json.destroyed-20261014T010203Z",
            "devnet-manifest.json.destroyed-20261014T010203Z",
        ]
        assert not path.exists()
        assert sorted(p.name for p in path.parent.iterdir()) == [
            "devnet-manifest.json.destroyed-20261014T010203Z",
            "inventory.json.destroyed-20261014T010203Z",
        ]

    def test_says_what_it_renamed(self, local_inventory_root, capsys):
        _snapshot(local_inventory_root, "DEV-02", manifest=False)

        retire_local_inventory("DEV-02", Console(), now=STAMP)

        out = capsys.readouterr().out
        assert "Retired" in out
        assert "inventory.json.destroyed-20261014T010203Z" in out

    def test_a_missing_snapshot_is_silent(self, local_inventory_root, capsys):
        assert retire_local_inventory("DEV-09", Console()) == []
        assert capsys.readouterr().out == ""

    def test_a_manifest_is_optional(self, local_inventory_root):
        _snapshot(local_inventory_root, "DEV-02", manifest=False)

        renamed = retire_local_inventory("DEV-02", Console(), now=STAMP)

        assert [p.name for p, _ in renamed] == ["inventory.json"]

    def test_leaves_a_foreign_testnet_id_alone(self, local_inventory_root, capsys):
        path = _snapshot(local_inventory_root, "DEV-02", testnet_id="DEV-07")

        assert retire_local_inventory("DEV-02", Console(), now=STAMP) == []

        assert path.exists()
        assert "DEV-07" in capsys.readouterr().out

    def test_leaves_an_unreadable_snapshot_alone(self, local_inventory_root):
        path = local_inventory_root / "DEV-02" / "inventory.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ not json")

        assert retire_local_inventory("DEV-02", Console(), now=STAMP) == []
        assert path.exists()

    def test_never_touches_the_bootstrap_inventory_or_earlier_retirements(
        self, local_inventory_root
    ):
        """`bootstrap-inventory.json` is the hand-maintained production file and
        an already-retired sibling is the previous run's evidence."""
        path = _snapshot(local_inventory_root, "DEV-02")
        boot = path.parent / "bootstrap-inventory.json"
        boot.write_text("{}")
        earlier = path.parent / "inventory.json.destroyed-20261001T000000Z"
        earlier.write_text("{}")

        retire_local_inventory("DEV-02", Console(), now=STAMP)

        assert boot.read_text() == "{}"
        assert earlier.read_text() == "{}"

    def test_never_touches_another_name(self, local_inventory_root):
        _snapshot(local_inventory_root, "DEV-02")
        other = _snapshot(local_inventory_root, "DEV-01")

        retire_local_inventory("DEV-02", Console(), now=STAMP)

        assert other.exists()

    def test_two_retirements_in_one_second_do_not_clobber(self, local_inventory_root):
        first = _snapshot(local_inventory_root, "DEV-02", manifest=False)
        retire_local_inventory("DEV-02", Console(), now=STAMP)
        _snapshot(local_inventory_root, "DEV-02", manifest=False)

        retire_local_inventory("DEV-02", Console(), now=STAMP)

        assert sorted(p.name for p in first.parent.iterdir()) == [
            "inventory.json.destroyed-20261014T010203Z",
            "inventory.json.destroyed-20261014T010203Z-1",
        ]

    def test_a_rename_failure_warns_and_does_not_raise(self, local_inventory_root, capsys):
        _snapshot(local_inventory_root, "DEV-02")
        path = default_inventory_path("DEV-02")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                type(path),
                "rename",
                lambda self, target: (_ for _ in ()).throw(OSError("read-only")),
            )
            assert retire_local_inventory("DEV-02", Console(), now=STAMP) == []

        assert "Warning: Failed to retire" in capsys.readouterr().out
        assert path.exists()

    def test_a_console_is_optional(self, local_inventory_root):
        _snapshot(local_inventory_root, "DEV-02", manifest=False)
        assert len(retire_local_inventory("DEV-02", now=STAMP)) == 1


class TestTestnetId:
    def test_reads_the_id(self, local_inventory_root):
        path = _snapshot(local_inventory_root, "DEV-02")
        assert inventory_testnet_id(path) == "DEV-02"

    def test_absent_file_malformed_json_and_a_list_all_read_as_unknown(self, tmp_path):
        assert inventory_testnet_id(tmp_path / "nope.json") is None
        bad = tmp_path / "bad.json"
        bad.write_text("{oops")
        assert inventory_testnet_id(bad) is None
        listy = tmp_path / "list.json"
        listy.write_text("[]")
        assert inventory_testnet_id(listy) is None
