"""Remote-driver analysis commands (snapshot, capture, capture-stream, fetch,
restart-nodes, stall-probe).

The remote payloads are proven on live fleets and are not exercised here; what
is under test is the local driver logic: host resolution from the inventory,
the start/wait/span-check flow that turns a short capture into a hard failure,
and the record shapes downstream analysers consume.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.analysis import capture, hosts, restart_nodes, snapshot, stall_probe
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
        "bootstrap_vms": [
            {
                "name": "b1",
                "ip": "10.0.1.1",
                "ssh_user": "root",
                "ssh_key": "k",
                "services": ["ant-bootstrap-1"],
            }
        ],
        "client_vms": [],
    }
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inv))
    return str(path)


def _res(vm, stdout, rc=0, stderr=""):
    return RemoteResult(vm, rc, stdout, stderr)


def _args(**kw):
    base = {"name": "DEV-01", "inventory": None, "ips": None, "tier": "all"}
    base.update(kw)
    return SimpleNamespace(**base)


# --- hosts -----------------------------------------------------------------


def test_select_vms_tier_and_unknown_ip(inventory):
    assert [v.ip for v in hosts.select_vms("DEV-01", None, inventory, tier="nat")] == [NAT_IP]
    assert [v.ip for v in hosts.select_vms("DEV-01", None, inventory, tier="plain")] == [PLAIN_IP]
    with pytest.raises(KeyError):
        hosts.select_vms("DEV-01", ["10.9.9.9"], inventory)
    with pytest.raises(FileNotFoundError):
        hosts.select_vms("NOPE", None, "/nonexistent/inventory.json")


# --- snapshot --------------------------------------------------------------


def test_snapshot_appends_ip_tag_and_marks_unreachable(inventory, tmp_path, capsys):
    def fake_run(vm, script, args=(), timeout=None, stdin_extra=""):
        if vm.ip == NAT_IP:
            return _res(vm, '{"host":"nat-1","ts":100,"nat":1,"ns_udp_out":5}\n')
        return _res(vm, "", rc=255, stderr="ssh: connect timed out")

    out = tmp_path / "snap.jsonl"
    with patch("saorsa_deploy.analysis.remote.run_script", side_effect=fake_run):
        rc = snapshot.cmd_snapshot(_args(inventory=inventory, tag="p1", out=str(out)))
    assert rc == 1  # one unreachable
    lines = [json.loads(ln) for ln in out.read_text().splitlines()]
    by_ip = {r["ip"]: r for r in lines}
    assert by_ip[NAT_IP]["tag"] == "p1" and by_ip[NAT_IP]["ns_udp_out"] == 5
    assert by_ip[PLAIN_IP] == {"ip": PLAIN_IP, "tag": "p1", "error": "unreachable"}
    assert NAT_IP in capsys.readouterr().out


# --- capture ---------------------------------------------------------------


def test_capture_aborts_when_a_host_fails_to_start(inventory):
    def fake_run(vm, script, args=(), timeout=None, stdin_extra=""):
        if script is capture.CAPTURE_START_SCRIPT and vm.ip == PLAIN_IP:
            return _res(vm, "STATUS=not-growing BYTES=0", rc=1)
        if script is capture.CAPTURE_START_SCRIPT:
            return _res(vm, "STATUS=started NS=1 IFACE=veth-node START=1 CLOCK=x")
        raise AssertionError("wait script must not run after a failed start")

    with patch("saorsa_deploy.analysis.remote.run_script", side_effect=fake_run):
        rc = capture.cmd_capture(
            _args(inventory=inventory, ips=[NAT_IP, PLAIN_IP], seconds=100, tag="t")
        )
    assert rc == 1


@pytest.mark.parametrize("spans,expected_rc", [((100, 96), 0), ((100, 29), 1)])
def test_capture_span_check(inventory, spans, expected_rc):
    by_ip = dict(zip([NAT_IP, PLAIN_IP], spans))

    def fake_run(vm, script, args=(), timeout=None, stdin_extra=""):
        assert args[0] == "100" and args[1] == "t" and args[2] == "saorsa-cap-t"
        if script is capture.CAPTURE_START_SCRIPT:
            return _res(vm, "STATUS=started NS=0 IFACE=eth0 START=1 CLOCK=x")
        return _res(
            vm,
            f"STATUS=done BYTES=1 PACKETS=9 SPAN={by_ip[vm.ip]} "
            "FIRST=0 LAST=1 DROPPED=0 RECEIVED=9",
        )

    with patch("saorsa_deploy.analysis.remote.run_script", side_effect=fake_run):
        rc = capture.cmd_capture(
            _args(inventory=inventory, ips=[NAT_IP, PLAIN_IP], seconds=100, tag="t")
        )
    assert rc == expected_rc


def test_capture_stream_pushes_analyser_before_starting(inventory):
    order = []

    def fake_push(vm, content, remote_path, timeout=120):
        order.append(("push", vm.ip, remote_path))
        assert "flowstat" in content.lower() or len(content) > 0
        return _res(vm, "STATUS=pushed")

    def fake_run(vm, script, args=(), timeout=None, stdin_extra=""):
        order.append(("run", vm.ip))
        if script is capture.STREAM_START_SCRIPT:
            return _res(vm, "STATUS=started NS=0 IFACE=eth0 START=1 CLOCK=x")
        return _res(vm, "STATUS=done SPAN=599.9 COMPLETE=True DROPPED=0 RECEIVED=1")

    with (
        patch("saorsa_deploy.analysis.remote.push_file", side_effect=fake_push),
        patch("saorsa_deploy.analysis.remote.run_script", side_effect=fake_run),
        patch(
            "saorsa_deploy.analysis.capture.flowstat_source",
            return_value="#!/usr/bin/env python3\n",
        ),
    ):
        rc = capture.cmd_capture_stream(
            _args(inventory=inventory, ips=[PLAIN_IP], seconds=600, tag="s")
        )
    assert rc == 0
    assert order[0] == ("push", PLAIN_IP, capture.REMOTE_FLOWSTAT)
    assert order[1][0] == "run"


def test_capture_stream_incomplete_is_failure(inventory):
    def fake_run(vm, script, args=(), timeout=None, stdin_extra=""):
        if script is capture.STREAM_START_SCRIPT:
            return _res(vm, "STATUS=started NS=0 IFACE=eth0 START=1 CLOCK=x")
        return _res(vm, "STATUS=done SPAN=29.0 COMPLETE=False DROPPED=0 RECEIVED=1")

    with (
        patch(
            "saorsa_deploy.analysis.remote.push_file",
            side_effect=lambda vm, c, p, timeout=120: _res(vm, "STATUS=pushed"),
        ),
        patch("saorsa_deploy.analysis.remote.run_script", side_effect=fake_run),
        patch("saorsa_deploy.analysis.capture.flowstat_source", return_value="x"),
    ):
        rc = capture.cmd_capture_stream(
            _args(inventory=inventory, ips=[PLAIN_IP], seconds=600, tag="s")
        )
    assert rc == 1


def test_fetch_tolerates_missing_files(inventory, tmp_path):
    def fake_fetch(vm, remote_path, local_path, timeout=None):
        if remote_path.endswith(".pcap"):
            local_path.write_bytes(b"pcap")
            return _res(vm, "")
        return _res(vm, "", rc=1, stderr="scp: No such file or directory")

    out_dir = tmp_path / "out"
    with patch("saorsa_deploy.analysis.remote.fetch_file", side_effect=fake_fetch):
        rc = capture.cmd_fetch(
            _args(
                inventory=inventory,
                ips=[PLAIN_IP],
                tag="p2",
                out_dir=str(out_dir),
                kinds="pcap,json,probe",
            )
        )
    assert rc == 0
    assert (out_dir / f"{PLAIN_IP}-p2.pcap").read_bytes() == b"pcap"
    assert not (out_dir / f"{PLAIN_IP}-p2.json").exists()


# --- restart-nodes ---------------------------------------------------------


def _restart_line(ip, svc, stop_s, active="active"):
    return json.dumps(
        {
            "ip": ip,
            "host": "h",
            "service": svc,
            "stop_start": 1000.0,
            "stop_end": 1000.0 + stop_s,
            "start_end": 1001.0 + stop_s,
            "stop_seconds": stop_s,
            "active": active,
        }
    )


def test_restart_nodes_iterates_services_and_summarises(inventory, tmp_path):
    calls = []

    def fake_run(vm, script, args=(), timeout=None, stdin_extra=""):
        calls.append((vm.ip, tuple(args)))
        lines = [_restart_line(vm.ip, s, 1.5 + i) for i, s in enumerate(args[1:])]
        return _res(vm, "\n".join(lines) + "\nSTATUS=done\n")

    out = tmp_path / "r.jsonl"
    with (
        patch("saorsa_deploy.analysis.remote.run_script", side_effect=fake_run),
        patch("saorsa_deploy.analysis.restart_nodes.time.sleep") as sleep,
    ):
        rc = restart_nodes.cmd_restart_nodes(
            _args(
                inventory=inventory,
                ips=[NAT_IP, PLAIN_IP],
                services=None,
                stagger_seconds=30,
                tag="p2",
                out=str(out),
            )
        )
    assert rc == 0
    assert calls == [
        (NAT_IP, (NAT_IP, "ant-node-1", "ant-node-2")),
        (PLAIN_IP, (PLAIN_IP, "ant-node-1")),
    ]
    sleep.assert_called_once_with(30)
    records = [json.loads(ln) for ln in out.read_text().splitlines()]
    services = [r for r in records if "service" in r]
    assert len(services) == 3 and all(r["tag"] == "p2" for r in services)
    run = records[-1]
    assert run["services"] == 3 and run["vms"] == 2
    assert run["first_stop_start_utc"].endswith("Z")


def test_restart_nodes_explicit_services_and_inactive_exit(inventory, tmp_path):
    def fake_run(vm, script, args=(), timeout=None, stdin_extra=""):
        assert tuple(args[1:]) == ("ant-node-2",)
        return _res(
            vm, _restart_line(vm.ip, "ant-node-2", 2.0, active="failed") + "\nSTATUS=done\n"
        )

    with patch("saorsa_deploy.analysis.remote.run_script", side_effect=fake_run):
        rc = restart_nodes.cmd_restart_nodes(
            _args(
                inventory=inventory,
                ips=[NAT_IP],
                services=["ant-node-2"],
                stagger_seconds=0,
                tag="p0",
                out=str(tmp_path / "r.jsonl"),
            )
        )
    assert rc == 1


# --- stall-probe -----------------------------------------------------------


def test_stall_probe_passes_dry_run_through(inventory):
    seen = {}

    def fake_probe(vm, seconds, peers, tag, dry_run):
        seen.update(ip=vm.ip, seconds=seconds, peers=peers, tag=tag, dry_run=dry_run)
        return 0

    with patch("saorsa_deploy.analysis.stall_probe.run_stall_probe", side_effect=fake_probe):
        rc = stall_probe.cmd_stall_probe(
            _args(inventory=inventory, ip=NAT_IP, seconds=3, peers=1, tag="smoke", dry_run=True)
        )
    assert rc == 0
    assert seen == {"ip": NAT_IP, "seconds": 3, "peers": 1, "tag": "smoke", "dry_run": True}


def test_stall_probe_payload_uses_generalised_log_path():
    assert "/var/tmp/saorsa-analysis-probe-${TAG}.jsonl" in stall_probe.STALL_PROBE_SCRIPT
    assert "v2-1030" not in stall_probe.STALL_PROBE_SCRIPT
