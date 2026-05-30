import json
from pathlib import Path

import pytest

from saorsa_deploy.cmd.track_upgrades.init_cmd import _enumerate_nodes
from saorsa_deploy.cmd.track_upgrades.reconcile_cmd import reconcile_rows
from saorsa_deploy.cmd.track_upgrades.sync_cmd import _advance_cursor
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


class TestAdvanceCursor:
    """The cursor-advance must never advance past now-lag, nor go backwards.

    This guards against the ES-ingestion-order race where a late-arriving
    doc's @timestamp is in the past relative to max_ts_seen from other hosts.
    """

    def test_advances_when_safely_behind_lag_cutoff(self):
        out = _advance_cursor(
            old_cursor="2026-04-18T12:00:00Z",
            max_ts_seen="2026-04-18T12:30:00Z",
            now_iso="2026-04-18T13:00:00Z",
            lag_seconds=120,
        )
        assert out == "2026-04-18T12:30:00Z"

    def test_clamps_when_max_ts_crosses_lag_cutoff(self):
        # now=13:00:00, lag=120s -> cutoff=12:58:00. max_ts=12:59:30 must clamp.
        out = _advance_cursor(
            old_cursor="2026-04-18T12:00:00Z",
            max_ts_seen="2026-04-18T12:59:30Z",
            now_iso="2026-04-18T13:00:00Z",
            lag_seconds=120,
        )
        assert out == "2026-04-18T12:58:00Z"

    def test_does_not_go_backwards_from_old_cursor(self):
        # Empty scan: max_ts_seen == old_cursor. Don't move.
        out = _advance_cursor(
            old_cursor="2026-04-18T12:00:00Z",
            max_ts_seen="2026-04-18T12:00:00Z",
            now_iso="2026-04-18T13:00:00Z",
            lag_seconds=120,
        )
        assert out == "2026-04-18T12:00:00Z"

    def test_does_not_go_backwards_when_clamped_below_old(self):
        # Odd case: lag_cutoff is earlier than old_cursor (e.g. very large
        # lag). Must NOT move cursor backwards.
        out = _advance_cursor(
            old_cursor="2026-04-18T12:59:00Z",
            max_ts_seen="2026-04-18T13:10:00Z",
            now_iso="2026-04-18T13:00:00Z",
            lag_seconds=3600,
        )
        assert out == "2026-04-18T12:59:00Z"

    def test_fractional_seconds_handled(self):
        out = _advance_cursor(
            old_cursor="2026-04-18T12:00:00.000000Z",
            max_ts_seen="2026-04-18T12:30:45.123456Z",
            now_iso="2026-04-18T13:00:00Z",
            lag_seconds=120,
        )
        assert out == "2026-04-18T12:30:45.123456Z"


class _FakeNodeES:
    """Stand-in for ESClient exposing scan_node_events keyed by (host, svc)."""

    def __init__(self, by_node: dict[tuple[str, str], list[dict]]):
        self.by_node = by_node
        self.calls: list[tuple[str, str]] = []

    def scan_node_events(
        self, index_pattern, testnet_name, host, service_number,
    ):
        self.calls.append((host, str(service_number)))
        return list(self.by_node.get((host, str(service_number)), []))


class TestReconcile:
    def _stuck_pending(self, conn):
        """A node frozen at pending whose upgrade actually completed in ES."""
        repo = Repository(conn)
        repo.insert_node(NodeUpgrade(
            host="ANT-PROD-01-OVH-BHS5-3",
            service_number="14",
            from_version="0.11.4",
            to_version="0.11.5",
            running_version="0.11.4",
            status="pending",
            last_synced_at="2026-05-30T17:59:00.000Z",
        ))
        return repo

    def test_recovers_missed_completion(self, tmp_path: Path):
        db_path = tmp_path / "t.db"
        with open_db(db_path) as conn:
            self._stuck_pending(conn)
        host, svc = "ANT-PROD-01-OVH-BHS5-3", "14"
        events = [
            _doc(ts="2026-05-28T14:39:10.000Z", host=host, service_number=svc,
                 target="ant_node::upgrade::monitor", testnet_name="ant-prod-01",
                 message="New version detected, staged rollout delay calculated",
                 tail_extra={"delay_hours": 0, "delay_minutes": 25}),
            _doc(ts="2026-05-28T15:04:08.000Z", host=host, service_number=svc,
                 target="ant_node::upgrade::monitor", testnet_name="ant-prod-01",
                 message="Starting auto-apply upgrade from 0.11.4 to 0.11.5"),
            _doc(ts="2026-05-28T15:04:24.000Z", host=host, service_number=svc,
                 target="ant_node", testnet_name="ant-prod-01",
                 message="ant-node starting", tail_extra={"version": "0.11.5"}),
        ]
        es = _FakeNodeES({(host, svc): events})
        with open_db(db_path) as conn:
            repo = Repository(conn)
            changes = reconcile_rows(
                repo, es, "telegraf-ant-nodes-ant-prod-01-*", "ant-prod-01",
            )
            got = repo.get_node(host, svc, "0.11.5")
        assert es.calls == [(host, svc)]
        assert len(changes) == 1
        assert changes[0][2:4] == ("pending", "completed")
        assert got.status == "completed"
        assert got.running_version == "0.11.5"
        assert got.completed_at == "2026-05-28T15:04:24.000Z"

    def test_leaves_genuinely_unupgraded_row_untouched(self, tmp_path: Path):
        db_path = tmp_path / "t.db"
        with open_db(db_path) as conn:
            self._stuck_pending(conn)
        host, svc = "ANT-PROD-01-OVH-BHS5-3", "14"
        # Only a (still-on-old-version) startup — no 0.11.5 boot anywhere.
        es = _FakeNodeES({(host, svc): [
            _doc(ts="2026-05-28T12:00:00.000Z", host=host, service_number=svc,
                 target="ant_node", testnet_name="ant-prod-01",
                 message="ant-node starting", tail_extra={"version": "0.11.4"}),
        ]})
        with open_db(db_path) as conn:
            repo = Repository(conn)
            changes = reconcile_rows(
                repo, es, "telegraf-ant-nodes-ant-prod-01-*", "ant-prod-01",
            )
            got = repo.get_node(host, svc, "0.11.5")
        assert changes == []
        assert got.status == "pending"

    def test_skips_terminal_rows(self, tmp_path: Path):
        db_path = tmp_path / "t.db"
        with open_db(db_path) as conn:
            repo = Repository(conn)
            repo.insert_node(NodeUpgrade(
                host="H", service_number="1", from_version="0.11.4",
                to_version="0.11.5", running_version="0.11.5",
                status="completed", last_synced_at="2026-05-30T00:00:00.000Z",
            ))
        es = _FakeNodeES({})
        with open_db(db_path) as conn:
            repo = Repository(conn)
            changes = reconcile_rows(repo, es, "idx", "ant-prod-01")
        # Terminal rows are never scanned.
        assert es.calls == []
        assert changes == []


class TestScanPagination:
    def test_scan_pages_and_closes_pit(self):
        from saorsa_deploy.upgrade_tracker.es_client import (
            ESClient,
            ESCredentials,
        )

        es = ESClient(ESCredentials(url="http://es:9200", api_key="k"))
        # Two full pages then a short page; identical @timestamp across the
        # page boundary is exactly the case the old sort key dropped.
        pages = [
            {"pit_id": "p1", "hits": {"hits": [
                {"_source": {"n": 0}, "sort": [1000, 10]},
                {"_source": {"n": 1}, "sort": [1000, 11]},
            ]}},
            {"pit_id": "p2", "hits": {"hits": [
                {"_source": {"n": 2}, "sort": [1000, 12]},
                {"_source": {"n": 3}, "sort": [1001, 13]},
            ]}},
            {"pit_id": "p3", "hits": {"hits": [
                {"_source": {"n": 4}, "sort": [1002, 14]},
            ]}},
        ]
        calls = {"open": 0, "close": 0, "search": 0, "search_after": []}

        def fake_request(method, path, body):
            if "_pit" in path and method == "POST":
                calls["open"] += 1
                return {"id": "pit-abc"}
            if path == "/_pit" and method == "DELETE":
                calls["close"] += 1
                return {"succeeded": True}
            # /_search
            calls["search"] += 1
            calls["search_after"].append(body.get("search_after"))
            assert body["pit"]["id"]  # PIT carried on every search
            assert body["sort"][-1] == {"_shard_doc": "asc"}
            return pages[calls["search"] - 1]

        es._request = fake_request
        out = list(es._scan("idx-*", {"match_all": {}}, page_size=2))
        assert [d["n"] for d in out] == [0, 1, 2, 3, 4]
        assert calls["open"] == 1
        assert calls["close"] == 1  # PIT always cleaned up
        # search_after advances using the last hit's full (unique) sort tuple.
        assert calls["search_after"] == [None, [1000, 11], [1001, 13]]


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
