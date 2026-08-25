"""`analysis isolate` and `analysis recovery` (V2-1085).

The nft payload is proven on a live VM, not here; under test is the driver's
state check (apply must leave the table present, lift must leave it absent),
the record shape, and the recovery scorer's ordering rules.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.analysis import isolate, recovery
from saorsa_deploy.analysis.remote import RemoteResult

NAT_IP = "10.0.0.1"
PLAIN_IP = "10.0.0.2"


@pytest.fixture()
def inventory(tmp_path):
    inv = {
        "schema_version": 6,
        "node_vms": [
            {
                "name": "DEV-01-ant-node-nat-1",
                "provider": "ovh",
                "provider_id": "x",
                "region": "SBG5",
                "ip": NAT_IP,
                "ssh_user": "root",
                "ssh_key": "~/.ssh/ant_deploy",
                "nat_simulated": True,
                "services": ["ant-node-1", "ant-node-2"],
            },
            {
                "name": "DEV-01-ant-node-plain-1",
                "provider": "vultr",
                "provider_id": "y",
                "region": "ord",
                "ip": PLAIN_IP,
                "ssh_user": "root",
                "ssh_key": "~/.ssh/ant_deploy",
                "nat_simulated": False,
                "services": ["ant-node-1"],
            },
        ],
        "bootstrap_vms": [],
        "client_vms": [],
    }
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inv))
    return str(path)


def _args(**kw):
    base = {"name": "DEV-01", "inventory": None, "ips": None, "tier": "all"}
    base.update(kw)
    return SimpleNamespace(**base)


def _line(ip, action, before, after, rc=0):
    return (
        json.dumps(
            {
                "ip": ip,
                "host": "h",
                "action": action,
                "present_before": before,
                "present_after": after,
                "ts_utc": "2026-08-27T10:00:00.000Z",
                "rc": rc,
            }
        )
        + "\nSTATUS=done\n"
    )


# --- isolate ---------------------------------------------------------------


def test_isolate_ruleset_covers_all_three_hooks_udp_only():
    for hook in ("input", "output", "forward"):
        assert f"hook {hook}" in isolate.RULESET
    assert "meta l4proto udp drop" in isolate.RULESET
    assert "tcp" not in isolate.RULESET
    assert "dport 53 accept" in isolate.RULESET
    assert isolate.RULESET.rstrip("\n") in isolate.ISOLATE_SCRIPT


def test_isolate_apply_records_edges_and_checks_state(inventory, tmp_path):
    calls = []

    def fake_run(vm, script, args=(), timeout=None, stdin_extra=""):
        calls.append(tuple(args))
        return RemoteResult(vm, 0, _line(vm.ip, "apply", False, True), "")

    out = tmp_path / "i.jsonl"
    with patch("saorsa_deploy.analysis.remote.run_script", side_effect=fake_run):
        rc = isolate.cmd_isolate(
            _args(
                inventory=inventory,
                ips=[NAT_IP, PLAIN_IP],
                apply=True,
                lift=False,
                status=False,
                tag="p2",
                out=str(out),
            )
        )
    assert rc == 0
    assert sorted(calls) == [(NAT_IP, "apply"), (PLAIN_IP, "apply")]
    records = [json.loads(ln) for ln in out.read_text().splitlines()]
    per_vm = [r for r in records if "ip" in r]
    assert {r["ip"] for r in per_vm} == {NAT_IP, PLAIN_IP}
    nat = next(r for r in per_vm if r["ip"] == NAT_IP)
    assert nat["nat"] is True and nat["ok"] is True and nat["tag"] == "p2"
    assert nat["local_start_utc"].endswith("Z") and nat["ts_utc"].endswith("Z")
    run = records[-1]
    assert run["action"] == "apply" and run["vms"] == 2 and run["failed"] == 0


def test_isolate_lift_that_leaves_table_present_is_failure(inventory, tmp_path):
    def fake_run(vm, script, args=(), timeout=None, stdin_extra=""):
        after = True if vm.ip == NAT_IP else False
        return RemoteResult(vm, 0, _line(vm.ip, "lift", True, after), "")

    with patch("saorsa_deploy.analysis.remote.run_script", side_effect=fake_run):
        rc = isolate.cmd_isolate(
            _args(
                inventory=inventory,
                ips=[NAT_IP, PLAIN_IP],
                apply=False,
                lift=True,
                status=False,
                tag="p2",
                out=str(tmp_path / "i.jsonl"),
            )
        )
    assert rc == 1


def test_isolate_unparseable_output_is_failure(inventory, tmp_path):
    def fake_run(vm, script, args=(), timeout=None, stdin_extra=""):
        return RemoteResult(vm, 255, "", "ssh: connect to host: Connection refused")

    with patch("saorsa_deploy.analysis.remote.run_script", side_effect=fake_run):
        rc = isolate.cmd_isolate(
            _args(
                inventory=inventory,
                ips=[PLAIN_IP],
                apply=True,
                lift=False,
                status=False,
                tag="p2",
                out=str(tmp_path / "i.jsonl"),
            )
        )
    assert rc == 1


def test_isolate_status_accepts_either_state(inventory, tmp_path):
    def fake_run(vm, script, args=(), timeout=None, stdin_extra=""):
        return RemoteResult(vm, 0, _line(vm.ip, "status", True, True), "")

    with patch("saorsa_deploy.analysis.remote.run_script", side_effect=fake_run):
        rc = isolate.cmd_isolate(
            _args(
                inventory=inventory,
                ips=[PLAIN_IP],
                apply=False,
                lift=False,
                status=True,
                tag="s",
                out=str(tmp_path / "i.jsonl"),
            )
        )
    assert rc == 0


# --- recovery --------------------------------------------------------------

LIFT = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)


def _doc(kind, host, service, offset_s, msg=None):
    ts = LIFT.timestamp() + offset_s
    text = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    default = {
        "redial": (
            "Auto re-bootstrap: no connected peers — re-dialing 7 configured bootstrap peer(s)"
        ),
        "unreachable": "Auto re-bootstrap: all 7 configured bootstrap peer(s) unreachable",
        "discovered": "Auto re-bootstrap discovered 12 peers, routing table size now 9",
        "added": "Added peer abcd to DHT routing table",
    }[kind]
    return {
        "@timestamp": text,
        "tag": {"host": host, "service_number": service},
        "tail": {"message": msg or default},
    }


def test_score_service_recovers_via_discovered_line_and_attributes():
    docs = {
        "redial": [_doc("redial", "h1", "1", 240)],
        "unreachable": [],
        "discovered": [
            _doc(
                "discovered",
                "h1",
                "1",
                241,
                "Auto re-bootstrap discovered 3 peers, routing table size now 0",
            ),
            _doc("discovered", "h1", "1", 250),
        ],
        "added": [_doc("added", "h1", "1", 245), _doc("added", "h1", "1", 246)],
    }
    scores = recovery.score_docs(docs, LIFT)
    s = scores[("H1", "1")]
    assert s["recovered"] and s["recovered_via"] == "discovered" and s["rt_after"] == 9
    assert s["time_to_recovery_s"] == 250.0
    assert s["attributed"] is True
    assert s["first_redial_utc"] == "2026-08-27T10:04:00Z"


def test_score_service_falls_back_to_third_admission_and_flags_unattributed():
    docs = {
        "redial": [_doc("redial", "h1", "2", 300)],
        "unreachable": [],
        "discovered": [
            _doc(
                "discovered",
                "h1",
                "2",
                301,
                "Auto re-bootstrap discovered 5 peers, routing table size now 2",
            )
        ],
        # an admission *before* the fallback fired: someone else reconnected us
        "added": [_doc("added", "h1", "2", 100 + i) for i in range(4)],
    }
    s = recovery.score_docs(docs, LIFT)[("H1", "2")]
    assert s["recovered"] and s["recovered_via"] == "admissions"
    assert s["time_to_recovery_s"] == 102.0
    assert s["attributed"] is False


def test_score_service_not_recovered_and_attempt_gap_measured():
    docs = {
        "redial": [_doc("redial", "h1", "3", -900), _doc("redial", "h1", "3", -600)],
        "unreachable": [_doc("unreachable", "h1", "3", -899), _doc("unreachable", "h1", "3", -599)],
        "discovered": [],
        "added": [],
    }
    s = recovery.score_docs(docs, LIFT)[("H1", "3")]
    assert s["recovered"] is False and s["time_to_recovery_s"] is None
    assert s["unreachable_count"] == 2 and s["min_attempt_gap_s"] == 300.0
    assert s["attributed"] is True  # redial seen, nothing admitted before it


def test_summarise_counts_window_and_cooldown_breaches():
    scores = {
        ("H", "1"): {
            "recovered": True,
            "recovered_via": "discovered",
            "attributed": True,
            "time_to_recovery_s": 200.0,
            "min_attempt_gap_s": 305.0,
        },
        ("H", "2"): {
            "recovered": True,
            "recovered_via": "admissions",
            "attributed": False,
            "time_to_recovery_s": 1000.0,
            "min_attempt_gap_s": 120.0,
        },
        ("H", "3"): {
            "recovered": False,
            "recovered_via": None,
            "attributed": None,
            "time_to_recovery_s": None,
            "min_attempt_gap_s": None,
        },
    }
    summary = recovery.summarise(scores, expected_services=4, within_s=900)
    assert summary["expected_services"] == 4 and summary["services_with_events"] == 3
    assert summary["recovered"] == 2 and summary["recovered_within_window"] == 1
    assert summary["recovered_via_admissions_only"] == 1 and summary["unattributed"] == 1
    assert summary["ttr_p50_s"] == 600.0 and summary["ttr_max_s"] == 1000.0
    assert summary["min_attempt_gap_s"] == 120.0 and summary["attempt_gap_below_cooldown"] == 1


def test_fetch_docs_filters_each_phrase_by_host():
    seen = []

    class FakeRun:
        def hits(self, filters, start, end, index=None, fields=None, page_size=1000):
            seen.append((filters, start, end, fields))
            return []

    docs = recovery.fetch_docs(
        FakeRun(), ["DEV-01-ant-node-nat-1"], "2026-08-27T10:00:00Z", "2026-08-27T10:30:00Z"
    )
    assert set(docs) == {"redial", "unreachable", "discovered", "added"}
    assert len(seen) == 4
    for filters, start, end, fields in seen:
        assert filters[0]["match_phrase"]["tail.message"] in recovery.PHRASES.values()
        assert any("tag.host" in json.dumps(f) for f in filters[1:])
        assert "tail.message" in fields and "@timestamp" in fields
