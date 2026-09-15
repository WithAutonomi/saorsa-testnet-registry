"""Tests for `prod restart-telegraf` (cmd/prod_restart_telegraf.py)."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.prod_restart_telegraf import (
    _format_bytes,
    cmd_prod_restart_telegraf,
)

GIB = 1024**3


def _entry(name, ip):
    return {
        "name": name,
        "provider": "ovh",
        "provider_id": f"id-{name}",
        "region": "WAW1",
        "ip": ip,
        "ssh_user": "root",
        "ssh_key": "~/.ssh/ant_deploy",
        "services": ["ant-node-1"],
    }


def _write_inventory(tmp_path, hosts):
    path = tmp_path / "inventory.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "testnet_id": "ant-prod-01",
                "node_vms": [_entry(n, ip) for n, ip in hosts],
            }
        )
    )
    return str(path)


def _args(inventory_path, **overrides):
    defaults = {
        "inventory_path": inventory_path,
        "provider": None,
        "limit_to": None,
        "min_held_gb": 0.0,
        "dry_run": False,
        "ssh_key_path": None,
        "max_parallel": None,
        "restart_parallel": None,
        "yes": True,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _probe_out(held_bytes, fds, free_bytes, state="active", pid=999):
    return [
        f"ANT_TG:STATE:{state}:{pid}",
        f"ANT_TG:HELD:{held_bytes}:{fds}",
        f"ANT_TG:DF:528000000000:1:{free_bytes}",
    ]


def _restart_out(held_bytes, free_before, free_after):
    return [
        "ANT_TG:STATE:active:999",
        f"ANT_TG:HELD:{held_bytes}:685",
        f"ANT_TG:DF:528000000000:1:{free_before}",
        "ANT_TG:RESTARTED:telegraf-ant-elasticsearch",
        f"ANT_TG:DF_AFTER:528000000000:1:{free_after}",
    ]


class _FakeSsh:
    """Routes a command to probe or restart output, per host IP."""

    def __init__(self, by_ip):
        self.by_ip = by_ip
        self.restarted = []
        self.commands = []

    def __call__(self, vm, ssh_key, command, timeout):
        self.commands.append((vm.name, command))
        if "systemctl restart" in command:
            self.restarted.append(vm.name)
            return True, self.by_ip[vm.ip]["restart"], ""
        return True, self.by_ip[vm.ip]["probe"], ""


class TestDryRun:
    def test_probes_but_restarts_nothing(self, tmp_path):
        inv = _write_inventory(tmp_path, [("host-a", "10.0.0.1")])
        ssh = _FakeSsh(
            {"10.0.0.1": {"probe": _probe_out(13 * GIB, 685, 3 * GIB), "restart": []}}
        )
        with patch(
            "saorsa_deploy.cmd.prod_restart_telegraf._ssh_capture", ssh
        ):
            cmd_prod_restart_telegraf(_args(inv, dry_run=True))
        assert ssh.restarted == []
        assert all("systemctl restart" not in c for _, c in ssh.commands)


class TestRestart:
    def test_restarts_every_probed_host_by_default(self, tmp_path):
        inv = _write_inventory(
            tmp_path, [("host-a", "10.0.0.1"), ("host-b", "10.0.0.2")]
        )
        ssh = _FakeSsh(
            {
                "10.0.0.1": {
                    "probe": _probe_out(13 * GIB, 685, 3 * GIB),
                    "restart": _restart_out(13 * GIB, 3 * GIB, 16 * GIB),
                },
                "10.0.0.2": {
                    "probe": _probe_out(8 * GIB, 400, 30 * GIB),
                    "restart": _restart_out(8 * GIB, 30 * GIB, 38 * GIB),
                },
            }
        )
        with patch("saorsa_deploy.cmd.prod_restart_telegraf._ssh_capture", ssh):
            cmd_prod_restart_telegraf(_args(inv))
        assert sorted(ssh.restarted) == ["host-a", "host-b"]

    def test_min_held_gb_filters_which_hosts_are_restarted(self, tmp_path):
        inv = _write_inventory(
            tmp_path, [("host-a", "10.0.0.1"), ("host-b", "10.0.0.2")]
        )
        ssh = _FakeSsh(
            {
                "10.0.0.1": {
                    "probe": _probe_out(13 * GIB, 685, 3 * GIB),
                    "restart": _restart_out(13 * GIB, 3 * GIB, 16 * GIB),
                },
                "10.0.0.2": {
                    "probe": _probe_out(2 * GIB, 90, 30 * GIB),
                    "restart": _restart_out(2 * GIB, 30 * GIB, 32 * GIB),
                },
            }
        )
        with patch("saorsa_deploy.cmd.prod_restart_telegraf._ssh_capture", ssh):
            cmd_prod_restart_telegraf(_args(inv, min_held_gb=10.0))
        assert ssh.restarted == ["host-a"]

    def test_a_probe_failure_does_not_stop_the_other_hosts(self, tmp_path):
        inv = _write_inventory(
            tmp_path, [("host-a", "10.0.0.1"), ("host-b", "10.0.0.2")]
        )

        def fake(vm, ssh_key, command, timeout):
            if vm.ip == "10.0.0.1":
                return False, [], "timed out after 60s"
            if "systemctl restart" in command:
                return True, _restart_out(8 * GIB, 30 * GIB, 38 * GIB), ""
            return True, _probe_out(8 * GIB, 400, 30 * GIB), ""

        with patch("saorsa_deploy.cmd.prod_restart_telegraf._ssh_capture", fake):
            cmd_prod_restart_telegraf(_args(inv))

    def test_exits_nonzero_when_a_restart_fails(self, tmp_path):
        inv = _write_inventory(tmp_path, [("host-a", "10.0.0.1")])

        def fake(vm, ssh_key, command, timeout):
            if "systemctl restart" in command:
                # No RESTARTED marker — systemctl returned non-zero but the
                # script ran on, which is exactly the case parse_restarted
                # exists to catch.
                return True, _probe_out(13 * GIB, 685, 3 * GIB), ""
            return True, _probe_out(13 * GIB, 685, 3 * GIB), ""

        with patch("saorsa_deploy.cmd.prod_restart_telegraf._ssh_capture", fake):
            with pytest.raises(SystemExit) as exc:
                cmd_prod_restart_telegraf(_args(inv))
        assert exc.value.code == 1


class TestSelection:
    def test_limit_to_restricts_the_hosts(self, tmp_path):
        inv = _write_inventory(
            tmp_path, [("host-a", "10.0.0.1"), ("host-b", "10.0.0.2")]
        )
        ssh = _FakeSsh(
            {
                "10.0.0.1": {
                    "probe": _probe_out(13 * GIB, 685, 3 * GIB),
                    "restart": _restart_out(13 * GIB, 3 * GIB, 16 * GIB),
                },
                "10.0.0.2": {"probe": _probe_out(1, 1, 1), "restart": []},
            }
        )
        with patch("saorsa_deploy.cmd.prod_restart_telegraf._ssh_capture", ssh):
            cmd_prod_restart_telegraf(_args(inv, limit_to="host-a"))
        assert ssh.restarted == ["host-a"]

    def test_unmatched_limit_to_exits(self, tmp_path):
        inv = _write_inventory(tmp_path, [("host-a", "10.0.0.1")])
        with pytest.raises(SystemExit):
            cmd_prod_restart_telegraf(_args(inv, limit_to="nope"))

    def test_missing_inventory_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            cmd_prod_restart_telegraf(_args(str(tmp_path / "absent.json")))


class TestConfirmation:
    def test_declining_the_prompt_aborts_without_restarting(self, tmp_path):
        inv = _write_inventory(tmp_path, [("host-a", "10.0.0.1")])
        ssh = _FakeSsh(
            {"10.0.0.1": {"probe": _probe_out(13 * GIB, 685, 3 * GIB), "restart": []}}
        )
        with patch("saorsa_deploy.cmd.prod_restart_telegraf._ssh_capture", ssh):
            with patch("builtins.input", return_value="n"):
                with pytest.raises(SystemExit):
                    cmd_prod_restart_telegraf(_args(inv, yes=False))
        assert ssh.restarted == []


class TestFormatBytes:
    @pytest.mark.parametrize(
        "value,expected",
        [(0, "0 B"), (1536, "1.5 KiB"), (13 * GIB, "13.0 GiB"), (-GIB, "-1.0 GiB")],
    )
    def test_scales_and_signs(self, value, expected):
        assert _format_bytes(value) == expected
