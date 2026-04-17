import json
from pathlib import Path

import pytest

from saorsa_deploy.cmd.track_upgrades.init_cmd import _enumerate_nodes
from saorsa_deploy.upgrade_tracker.db import (
    NodeUpgrade,
    Repository,
    open_db,
)
from saorsa_deploy.upgrade_tracker.state_machine import (
    apply_event,
    identify,
)


def _doc(
    *,
    ts: str = "2026-04-17T20:00:00.000Z",
    target: str = "ant_node",
    host: str = "ANT-PROD-01-AWS-AP-SE-1",
    service_number: str = "1",
    testnet_name: str = "ant-prod-logs",
    level: str = "INFO",
    message: str,
    tail_extra: dict | None = None,
) -> dict:
    tail = {"message": message}
    if tail_extra:
        tail.update(tail_extra)
    return {
        "@timestamp": ts,
        "tag": {
            "target": target,
            "host": host,
            "service_number": service_number,
            "testnet_name": testnet_name,
            "level": level,
        },
        "tail": tail,
    }


def _seed_row(
    to_version: str = "0.11.0",
    from_version: str = "0.10.0",
    running: str | None = None,
    status: str = "pending",
) -> NodeUpgrade:
    return NodeUpgrade(
        host="ANT-PROD-01-AWS-AP-SE-1",
        service_number="1",
        from_version=from_version,
        to_version=to_version,
        running_version=running or from_version,
        status=status,
        last_synced_at="2026-04-17T19:00:00.000Z",
    )


class TestDBSchema:
    def test_insert_and_read_back(self, tmp_path: Path):
        db_path = tmp_path / "t.db"
        with open_db(db_path) as conn:
            repo = Repository(conn)
            repo.insert_node(_seed_row())
        with open_db(db_path) as conn:
            repo = Repository(conn)
            got = repo.get_node(
                "ANT-PROD-01-AWS-AP-SE-1", "1", "0.11.0",
            )
            assert got is not None
            assert got.status == "pending"
            assert got.running_version == "0.10.0"

    def test_count_by_status(self, tmp_path: Path):
        db_path = tmp_path / "t.db"
        with open_db(db_path) as conn:
            repo = Repository(conn)
            repo.insert_node(_seed_row())
            repo.insert_node(NodeUpgrade(
                host="ANT-PROD-01-AWS-AP-SE-2",
                service_number="1",
                from_version="0.10.0",
                to_version="0.11.0",
                running_version="0.11.0",
                status="completed",
                last_synced_at="2026-04-17T19:05:00.000Z",
            ))
            counts = repo.count_by_status()
        assert counts["pending"] == 1
        assert counts["completed"] == 1
        assert counts["scheduled"] == 0

    def test_metadata_roundtrip(self, tmp_path: Path):
        db_path = tmp_path / "t.db"
        with open_db(db_path) as conn:
            repo = Repository(conn)
            repo.set_meta("from_version", "0.10.0")
            repo.set_meta("to_version", "0.11.0")
            assert repo.get_meta("from_version") == "0.10.0"
            repo.set_meta("from_version", "0.10.1")
            assert repo.get_meta("from_version") == "0.10.1"

    def test_list_active_excludes_terminal(self, tmp_path: Path):
        db_path = tmp_path / "t.db"
        with open_db(db_path) as conn:
            repo = Repository(conn)
            repo.insert_node(_seed_row())  # pending
            repo.insert_node(NodeUpgrade(
                host="h2", service_number="1",
                from_version="0.10.0", to_version="0.11.0",
                running_version="0.11.0",
                status="completed",
                last_synced_at="2026-04-17T19:00:00Z",
            ))
            repo.insert_node(NodeUpgrade(
                host="h3", service_number="1",
                from_version="0.10.0", to_version="0.11.0",
                running_version="0.10.0",
                status="rolled_back",
                last_synced_at="2026-04-17T19:00:00Z",
            ))
            active = repo.list_active()
        assert len(active) == 1
        assert active[0].host == "ANT-PROD-01-AWS-AP-SE-1"


class TestIdentify:
    def test_returns_host_and_service(self):
        doc = _doc(message="x")
        assert identify(doc) == ("ANT-PROD-01-AWS-AP-SE-1", "1")

    def test_handles_int_service_number(self):
        doc = _doc(message="x")
        doc["tag"]["service_number"] = 5
        assert identify(doc) == ("ANT-PROD-01-AWS-AP-SE-1", "5")

    def test_returns_none_when_missing(self):
        doc = {"@timestamp": "t", "tag": {}, "tail": {}}
        assert identify(doc) is None


class TestStateMachineStartup:
    def test_startup_baseline_no_transition(self):
        row = _seed_row()
        doc = _doc(
            target="ant_node", message="ant-node starting",
            tail_extra={"version": "0.10.0", "commit": "abc"},
        )
        out = apply_event(row, doc)
        assert out.status == "pending"
        assert out.running_version == "0.10.0"

    def test_startup_on_target_marks_completed(self):
        row = _seed_row(status="in_progress")
        doc = _doc(
            target="ant_node", message="ant-node starting",
            tail_extra={"version": "0.11.0", "commit": "def"},
            ts="2026-04-17T20:45:00Z",
        )
        out = apply_event(row, doc)
        assert out.status == "completed"
        assert out.completed_at == "2026-04-17T20:45:00Z"
        assert out.running_version == "0.11.0"

    def test_startup_completion_is_idempotent(self):
        row = _seed_row(status="completed", running="0.11.0")
        row.completed_at = "2026-04-17T20:45:00Z"
        doc = _doc(
            target="ant_node", message="ant-node starting",
            tail_extra={"version": "0.11.0"},
            ts="2026-04-17T20:50:00Z",
        )
        out = apply_event(row, doc)
        # completed_at is only set on the first transition, not updated.
        assert out.status == "completed"
        assert out.completed_at == "2026-04-17T20:45:00Z"


class TestStateMachineLifecycle:
    def test_new_version_available_records_detected(self):
        row = _seed_row()
        doc = _doc(
            target="ant_node::upgrade::monitor",
            message="New version available",
            tail_extra={
                "current_version": "0.10.0", "new_version": "0.11.0",
            },
            ts="2026-04-17T20:00:00Z",
        )
        out = apply_event(row, doc)
        assert out.detected_at == "2026-04-17T20:00:00Z"
        assert out.status == "pending"  # no transition yet

    def test_staged_rollout_computes_scheduled_at(self):
        row = _seed_row()
        doc = _doc(
            target="ant_node::upgrade::monitor",
            message="New version detected, staged rollout delay calculated",
            tail_extra={
                "new_version": "0.11.0",
                "delay_hours": "2",
                "delay_minutes": "15",
            },
            ts="2026-04-17T20:00:00Z",
        )
        out = apply_event(row, doc)
        assert out.status == "scheduled"
        # 2h 15m after 20:00:00 = 22:15:00
        assert out.scheduled_at.startswith("2026-04-17T22:15:00")

    def test_starting_upgrade_transitions_to_in_progress(self):
        row = _seed_row(status="scheduled")
        doc = _doc(
            target="ant_node::upgrade::apply",
            message="Starting auto-apply upgrade from 0.10.0 to 0.11.0",
            ts="2026-04-17T22:15:30Z",
        )
        out = apply_event(row, doc)
        assert out.status == "in_progress"
        assert out.started_at == "2026-04-17T22:15:30Z"

    def test_download_failed_rolls_back(self):
        row = _seed_row(status="in_progress")
        doc = _doc(
            target="ant_node::upgrade::apply",
            level="WARN",
            message="Download failed: connection timed out",
            ts="2026-04-17T22:16:00Z",
        )
        out = apply_event(row, doc)
        assert out.status == "rolled_back"
        assert out.error_count == 1
        assert "Download failed" in out.last_error

    def test_signature_verification_failed_rolls_back(self):
        row = _seed_row(status="in_progress")
        doc = _doc(
            target="ant_node::upgrade::apply",
            level="WARN",
            message="Signature verification failed: invalid key",
            ts="2026-04-17T22:16:00Z",
        )
        out = apply_event(row, doc)
        assert out.status == "rolled_back"

    def test_binary_replacement_failed_rolls_back(self):
        row = _seed_row(status="in_progress")
        doc = _doc(
            target="ant_node::upgrade::apply",
            level="WARN",
            message="Binary replacement failed: EBUSY",
            ts="2026-04-17T22:16:00Z",
        )
        out = apply_event(row, doc)
        assert out.status == "rolled_back"

    def test_non_rollback_warning_keeps_status(self):
        row = _seed_row(status="in_progress")
        doc = _doc(
            target="ant_node::upgrade::apply",
            level="WARN",
            message="self_replace attempt 1 failed: tmp busy, retrying in 500ms",
            ts="2026-04-17T22:16:00Z",
        )
        out = apply_event(row, doc)
        assert out.status == "in_progress"
        assert out.error_count == 1

    def test_completed_row_is_not_flipped_by_error(self):
        row = _seed_row(status="completed", running="0.11.0")
        doc = _doc(
            target="ant_node::upgrade::apply",
            level="WARN",
            message="Download failed: stale retry",
            ts="2026-04-17T23:00:00Z",
        )
        out = apply_event(row, doc)
        assert out.status == "completed"

    def test_irrelevant_target_ignored(self):
        row = _seed_row()
        doc = _doc(
            target="saorsa_core::transport_handle",
            message="some unrelated chatter",
        )
        out = apply_event(row, doc)
        assert out.status == "pending"
        assert out.error_count == 0


class TestEnumerateNodes:
    def test_enumerates_services_uppercased(self, tmp_path: Path):
        inv = tmp_path / "inv.json"
        inv.write_text(json.dumps({
            "node_vms": [
                {
                    "name": "ant-prod-01-aws-ap-se-1",
                    "nodes": [
                        {"service_name": "ant-node-1"},
                        {"service_name": "ant-node-2"},
                        {"service_name": "ant-node-15"},
                    ],
                },
                {
                    "name": "ant-prod-01-hz-fsn1-1",
                    "services": ["ant-node-1", "ant-node-2"],
                },
            ]
        }))
        pairs = list(_enumerate_nodes(inv))
        assert ("ANT-PROD-01-AWS-AP-SE-1", "1") in pairs
        assert ("ANT-PROD-01-AWS-AP-SE-1", "2") in pairs
        assert ("ANT-PROD-01-AWS-AP-SE-1", "15") in pairs
        assert ("ANT-PROD-01-HZ-FSN1-1", "1") in pairs
        assert ("ANT-PROD-01-HZ-FSN1-1", "2") in pairs
        assert len(pairs) == 5

    def test_genesis_mapped_to_genesis_service(self, tmp_path: Path):
        inv = tmp_path / "inv.json"
        inv.write_text(json.dumps({
            "node_vms": [
                {
                    "name": "genesis-vm",
                    "nodes": [{"service_name": "ant-genesis-node"}],
                }
            ]
        }))
        pairs = list(_enumerate_nodes(inv))
        assert pairs == [("GENESIS-VM", "genesis")]

    def test_dedups_when_nodes_and_services_overlap(self, tmp_path: Path):
        inv = tmp_path / "inv.json"
        inv.write_text(json.dumps({
            "node_vms": [
                {
                    "name": "vm",
                    "nodes": [{"service_name": "ant-node-1"}],
                    "services": ["ant-node-1"],
                }
            ]
        }))
        pairs = list(_enumerate_nodes(inv))
        assert pairs == [("VM", "1")]


class TestAntProd01FullShape:
    def test_real_inventory_shape(self):
        inv_path = Path(
            "/home/chris/.autonomi/testnets/ant-prod-01/inventory.json"
        )
        if not inv_path.exists():
            pytest.skip("ant-prod-01 inventory not available")
        pairs = list(_enumerate_nodes(inv_path))
        # Expect 66 VMs × 15 nodes each = 990 distinct pairs
        assert len(pairs) == 990
        # All hosts should be uppercased.
        assert all(h == h.upper() for h, _ in pairs)
        # Service numbers are 1..15 (strings).
        svc_numbers = {svc for _, svc in pairs}
        assert svc_numbers == {str(i) for i in range(1, 16)}
