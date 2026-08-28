"""Tests for `nodes swap-binary`: the in-VM swap script and the command wrapper."""

import base64
import hashlib
import os
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rich.console import Console

from saorsa_deploy.cmd.swap_nodes_binary import EVENT_TYPE, _summarise, cmd_swap_nodes_binary
from saorsa_deploy.provisioning.bootstrap import BINARY_INSTALL_PATH
from saorsa_deploy.provisioning.swap_binary import (
    MARKER_BAD_BINARY,
    MARKER_DONE,
    MARKER_NO_EXECSTART,
    MARKER_NOT_ACTIVE,
    NODE_CONFIG_DROPIN,
    NODE_CONFIG_PATH,
    _node_config_block,
    build_swap_binary_command,
    service_names,
)

SHED_TOML = (
    "[storage]\ndisk_reserve_mb = 95000\n\n"
    "[storage.migration]\nshed_hold_hours = 0\nwave_hours = 0\ncopier_slack_mb = 0\n"
)

RELEASE_URL = "https://github.com/WithAutonomi/ant-node/releases/download/v0.17.2/ant-node-cli-linux-x64.tar.gz"
BUILD_URL = (
    "https://ant-node-builds.s3.eu-west-2.amazonaws.com/builds/DEV-01/WithAutonomi/br/ant-node"
)


class TestServiceNames:
    def test_matches_provisioned_unit_names(self):
        assert service_names(3) == ["ant-node-1", "ant-node-2", "ant-node-3"]

    @pytest.mark.parametrize("bad", [0, -1, 1.5, "2", True])
    def test_rejects_non_positive_or_non_int(self, bad):
        with pytest.raises(ValueError):
            service_names(bad)


class TestBuildSwapBinaryCommand:
    def test_release_archive_is_extracted_then_staged_beside_the_target(self):
        cmd = build_swap_binary_command(RELEASE_URL, True, 2)
        assert "tar -xzf" in cmd
        # Staged on the same filesystem as the install path so the final mv is a rename.
        assert f'STAGING="{BINARY_INSTALL_PATH}.swap.$$"' in cmd
        assert f'mv -f "$STAGING" "{BINARY_INSTALL_PATH}"' in cmd

    def test_custom_build_is_downloaded_directly(self):
        cmd = build_swap_binary_command(BUILD_URL, False, 2)
        assert "tar -xzf" not in cmd
        assert f'wget -q "{BUILD_URL}" -O "$STAGING"' in cmd

    def test_new_binary_is_verified_before_the_old_one_is_replaced(self):
        cmd = build_swap_binary_command(BUILD_URL, False, 1)
        verify = cmd.index('"$STAGING" --version')
        replace = cmd.index('mv -f "$STAGING"')
        assert verify < replace
        assert MARKER_BAD_BINARY in cmd
        assert "set -euo pipefail" in cmd

    def test_restarts_every_provisioned_service_and_checks_they_are_active(self):
        cmd = build_swap_binary_command(BUILD_URL, False, 3)
        assert "systemctl restart ant-node-1 ant-node-2 ant-node-3" in cmd
        assert "systemctl is-active --quiet ant-node-1 ant-node-2 ant-node-3" in cmd
        assert MARKER_NOT_ACTIVE in cmd
        assert f"{MARKER_DONE} old=$OLD_VER new=$NEW_VER restarted=yes" in cmd

    def test_no_restart_leaves_services_alone(self):
        cmd = build_swap_binary_command(BUILD_URL, False, 3, restart=False)
        assert "systemctl restart" not in cmd
        assert "restarted=no" in cmd

    def test_staging_file_is_cleaned_up_on_failure(self):
        cmd = build_swap_binary_command(BUILD_URL, False, 1)
        assert "trap 'rm -f \"$STAGING\"' EXIT" in cmd

    def test_requires_a_url(self):
        with pytest.raises(ValueError):
            build_swap_binary_command("", False, 1)

    def test_without_node_config_units_are_untouched(self):
        cmd = build_swap_binary_command(BUILD_URL, False, 2)
        assert NODE_CONFIG_PATH not in cmd
        assert "daemon-reload" not in cmd
        assert "config=unchanged" in cmd

    def test_node_config_is_installed_and_every_service_gets_a_dropin(self):
        cmd = build_swap_binary_command(BUILD_URL, False, 2, node_config=SHED_TOML)
        encoded = base64.b64encode(SHED_TOML.encode()).decode()
        assert f'echo {encoded} | base64 -d > "{NODE_CONFIG_PATH}"' in cmd
        for svc in ("ant-node-1", "ant-node-2"):
            assert f'UNIT="/etc/systemd/system/{svc}.service"' in cmd
            assert f'"$UNIT.d/{NODE_CONFIG_DROPIN}"' in cmd
        assert f"--config {NODE_CONFIG_PATH}" in cmd
        assert MARKER_NO_EXECSTART in cmd
        assert "config=applied" in cmd

    def test_dropins_are_written_and_reloaded_before_the_restart(self):
        cmd = build_swap_binary_command(BUILD_URL, False, 1, node_config=SHED_TOML)
        assert cmd.index("mv -f") < cmd.index("daemon-reload") < cmd.index("systemctl restart")


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
class TestNodeConfigBlockUnderBash:
    """Run the config/drop-in part of the script for real against a fake /etc."""

    def _run(self, tmp_path, unit_execstart: str):
        etc = tmp_path / "etc"
        (etc / "systemd/system").mkdir(parents=True)
        unit = etc / "systemd/system/ant-node-1.service"
        unit.write_text(f"[Unit]\nDescription=x\n\n[Service]\nExecStart={unit_execstart}\n")
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "systemctl").write_text('#!/bin/sh\necho systemctl "$@" >> "$SYSTEMCTL_LOG"\n')
        (fake_bin / "systemctl").chmod(0o755)

        block = _node_config_block(SHED_TOML, ["ant-node-1"])
        block = block.replace("/etc/systemd/system", str(etc / "systemd/system")).replace(
            NODE_CONFIG_PATH, str(etc / "ant-node/config.toml")
        )
        log = tmp_path / "systemctl.log"
        proc = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", block],
            env={"PATH": f"{fake_bin}:{os.environ['PATH']}", "SYSTEMCTL_LOG": str(log)},
            capture_output=True,
            text=True,
        )
        return proc, etc, log

    def test_plain_unit(self, tmp_path):
        exec_start = (
            "/usr/local/bin/ant-node --root-dir /mnt/ant-node-storage/data/ant-node-1 "
            "--bootstrap 1.2.3.4:10000 --enable-logging --log-format json"
        )
        proc, etc, log = self._run(tmp_path, exec_start)
        assert proc.returncode == 0, proc.stderr
        assert (etc / "ant-node/config.toml").read_text() == SHED_TOML
        dropin = (etc / "systemd/system/ant-node-1.service.d" / NODE_CONFIG_DROPIN).read_text()
        assert dropin == (
            "[Service]\nExecStart=\n"
            f"ExecStart={exec_start} --config {etc / 'ant-node/config.toml'}\n"
        )
        assert log.read_text().strip() == "systemctl daemon-reload"

    def test_nat_unit_keeps_its_netns_prefix(self, tmp_path):
        exec_start = "ip netns exec ant_nat /usr/local/bin/ant-node --root-dir /d --port 10001"
        proc, etc, _ = self._run(tmp_path, exec_start)
        assert proc.returncode == 0, proc.stderr
        dropin = (etc / "systemd/system/ant-node-1.service.d" / NODE_CONFIG_DROPIN).read_text()
        assert f"ExecStart={exec_start} --config " in dropin

    def test_unit_without_execstart_fails_loudly(self, tmp_path):
        etc = tmp_path / "etc"
        (etc / "systemd/system").mkdir(parents=True)
        (etc / "systemd/system/ant-node-1.service").write_text("[Unit]\nDescription=x\n")
        block = (
            _node_config_block(SHED_TOML, ["ant-node-1"])
            .replace("/etc/systemd/system", str(etc / "systemd/system"))
            .replace(NODE_CONFIG_PATH, str(etc / "ant-node/config.toml"))
        )
        proc = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", block], capture_output=True, text=True
        )
        assert proc.returncode == 1
        assert f"{MARKER_NO_EXECSTART} ant-node-1" in proc.stdout


def _args(**overrides):
    defaults = {
        "name": "DEV-01",
        "ssh_key_path": "~/.ssh/ant_deploy",
        "node_version": None,
        "branch_name": None,
        "repo_owner": None,
        "region": None,
        "hosts": None,
        "stagger_secs": 0,
        "no_restart": False,
        "node_config": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _Host:
    def __init__(self, name):
        self.name = name


class _Meta:
    def __init__(self, lines):
        self.stdout_lines = lines


@pytest.fixture
def patches():
    with (
        patch("saorsa_deploy.cmd.swap_nodes_binary.load_deployment_state") as p_load,
        patch("saorsa_deploy.cmd.swap_nodes_binary.update_deployment_state") as p_update,
        patch("saorsa_deploy.cmd.swap_nodes_binary.record_event") as p_event,
        patch("saorsa_deploy.cmd.swap_nodes_binary.record_deploy_args_update") as p_args,
        patch("saorsa_deploy.cmd.swap_nodes_binary._resolve_binary_source") as p_resolve,
        patch("saorsa_deploy.cmd.swap_nodes_binary.clear_known_hosts"),
        patch("saorsa_deploy.cmd.swap_nodes_binary._swap_hosts") as p_swap,
        patch("saorsa_deploy.cmd.swap_nodes_binary.time.sleep") as p_sleep,
    ):
        p_load.return_value = {
            "node_count": 15,
            "vm_ips": {"digitalocean/lon1": ["10.0.0.1", "10.0.0.2"], "vultr/ams": ["10.0.1.1"]},
        }
        p_resolve.return_value = (BUILD_URL, False)
        p_swap.return_value = (["10.0.0.1", "10.0.0.2", "10.0.1.1"], [])
        yield SimpleNamespace(
            load=p_load,
            update=p_update,
            event=p_event,
            args=p_args,
            resolve=p_resolve,
            swap=p_swap,
            sleep=p_sleep,
        )


class TestCmdSwapNodesBinary:
    def test_refuses_without_an_explicit_target(self, patches):
        with pytest.raises(SystemExit):
            cmd_swap_nodes_binary(_args())
        patches.swap.assert_not_called()

    def test_swaps_every_vm_in_one_pass_and_records_the_swap(self, patches):
        cmd_swap_nodes_binary(_args(branch_name="br", repo_owner="WithAutonomi"))

        patches.swap.assert_called_once()
        host_ips = patches.swap.call_args.args[0]
        assert host_ips == ["10.0.0.1", "10.0.0.2", "10.0.1.1"]
        assert patches.swap.call_args.args[1:] == (
            "~/.ssh/ant_deploy",
            BUILD_URL,
            False,
            15,
            True,
            patches.swap.call_args.args[6],
        )

        patches.event.assert_called_once()
        name, event_type, details = patches.event.call_args.args
        assert (name, event_type) == ("DEV-01", EVENT_TYPE)
        assert details["node_binary"] == {
            "kind": "branch",
            "branch": "br",
            "repo_owner": "WithAutonomi",
            "debug": False,
        }
        assert details["swapped"] == 3 and details["failed"] == []
        patches.args.assert_called_once_with("DEV-01", {"node_binary": details["node_binary"]})
        patches.update.assert_called_once()

    def test_stagger_swaps_one_vm_at_a_time_with_sleeps_between(self, patches):
        patches.swap.side_effect = lambda ips, *a, **k: (list(ips), [])
        cmd_swap_nodes_binary(_args(node_version="0.17.2", stagger_secs=30))

        assert [c.args[0] for c in patches.swap.call_args_list] == [
            ["10.0.0.1"],
            ["10.0.0.2"],
            ["10.0.1.1"],
        ]
        assert patches.sleep.call_count == 2
        patches.sleep.assert_called_with(30)

    def test_region_limits_the_hosts_and_does_not_rewrite_the_fleet_binary(self, patches):
        patches.swap.return_value = (["10.0.1.1"], [])
        cmd_swap_nodes_binary(_args(node_version="0.17.2", region="vultr/ams"))
        assert patches.swap.call_args.args[0] == ["10.0.1.1"]
        patches.event.assert_called_once()
        patches.args.assert_not_called()
        patches.update.assert_not_called()

    def test_hosts_limits_the_swap_to_the_named_vms_in_the_order_given(self, patches):
        patches.swap.return_value = (["10.0.1.1", "10.0.0.1"], [])
        cmd_swap_nodes_binary(_args(node_version="0.17.2", hosts="10.0.1.1, 10.0.0.1"))
        assert patches.swap.call_args.args[0] == ["10.0.1.1", "10.0.0.1"]

    def test_hosts_swap_is_partial_and_does_not_rewrite_the_fleet_binary(self, patches):
        patches.swap.return_value = (["10.0.0.1"], [])
        cmd_swap_nodes_binary(_args(node_version="0.17.2", hosts="10.0.0.1"))
        patches.event.assert_called_once()
        assert patches.event.call_args.args[2]["hosts"] == "10.0.0.1"
        patches.args.assert_not_called()
        patches.update.assert_not_called()

    def test_an_ip_outside_the_deployment_is_an_error(self, patches):
        with pytest.raises(SystemExit):
            cmd_swap_nodes_binary(_args(node_version="0.17.2", hosts="10.0.0.1,9.9.9.9"))
        patches.swap.assert_not_called()

    def test_hosts_and_region_together_are_refused(self, patches):
        with pytest.raises(SystemExit):
            cmd_swap_nodes_binary(
                _args(node_version="0.17.2", hosts="10.0.0.1", region="vultr/ams")
            )
        patches.swap.assert_not_called()

    def test_hosts_with_no_usable_address_is_an_error(self, patches):
        with pytest.raises(SystemExit):
            cmd_swap_nodes_binary(_args(node_version="0.17.2", hosts=" , "))
        patches.swap.assert_not_called()

    def test_unknown_region_is_an_error(self, patches):
        with pytest.raises(SystemExit):
            cmd_swap_nodes_binary(_args(node_version="0.17.2", region="nowhere"))
        patches.swap.assert_not_called()

    def test_failed_hosts_exit_nonzero_and_do_not_update_the_fleet_binary(self, patches):
        patches.swap.return_value = (["10.0.0.1", "10.0.0.2"], ["10.0.1.1"])
        with pytest.raises(SystemExit) as exc:
            cmd_swap_nodes_binary(_args(node_version="0.17.2"))
        assert exc.value.code == 1
        details = patches.event.call_args.args[2]
        assert details["failed"] == ["10.0.1.1"]
        patches.args.assert_not_called()
        patches.update.assert_not_called()

    def test_no_restart_is_passed_through(self, patches):
        cmd_swap_nodes_binary(_args(node_version="0.17.2", no_restart=True))
        assert patches.swap.call_args.args[5] is False
        assert patches.event.call_args.args[2]["restarted"] is False

    def test_node_config_file_is_read_passed_through_and_recorded(self, patches, tmp_path):
        cfg = tmp_path / "shed.toml"
        cfg.write_text(SHED_TOML)
        cmd_swap_nodes_binary(_args(node_version="0.17.2", node_config=str(cfg)))

        assert patches.swap.call_args.kwargs["node_config"] == SHED_TOML
        recorded = patches.event.call_args.args[2]["node_config"]
        assert recorded["content"] == SHED_TOML
        assert recorded["path"] == NODE_CONFIG_PATH
        assert recorded["sha256"] == hashlib.sha256(SHED_TOML.encode()).hexdigest()

    def test_without_node_config_nothing_is_passed(self, patches):
        cmd_swap_nodes_binary(_args(node_version="0.17.2"))
        assert patches.swap.call_args.kwargs["node_config"] is None
        assert "node_config" not in patches.event.call_args.args[2]

    def test_invalid_toml_is_refused_before_touching_any_host(self, patches, tmp_path):
        cfg = tmp_path / "bad.toml"
        cfg.write_text("[storage\nshed_hold_hours = 0\n")
        with pytest.raises(SystemExit):
            cmd_swap_nodes_binary(_args(node_version="0.17.2", node_config=str(cfg)))
        patches.swap.assert_not_called()

    def test_missing_or_empty_config_file_is_refused(self, patches, tmp_path):
        with pytest.raises(SystemExit):
            cmd_swap_nodes_binary(
                _args(node_version="0.17.2", node_config=str(tmp_path / "nope.toml"))
            )
        empty = tmp_path / "empty.toml"
        empty.write_text("# nothing\n")
        with pytest.raises(SystemExit):
            cmd_swap_nodes_binary(_args(node_version="0.17.2", node_config=str(empty)))
        patches.swap.assert_not_called()


class TestSummarise:
    def test_marker_lines_decide_success(self):
        results = {
            _Host("a"): _Meta([f"{MARKER_DONE} old=0.17.2 new=0.18.0 restarted=yes"]),
            _Host("b"): _Meta([MARKER_BAD_BINARY]),
            _Host("c"): _Meta([]),
        }
        swapped, failed = _summarise(results, Console(quiet=True))
        assert swapped == ["a"]
        assert failed == ["b", "c"]
