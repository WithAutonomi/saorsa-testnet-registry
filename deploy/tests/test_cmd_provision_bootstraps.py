"""Tests for the `bootstraps provision` command wiring.

The command derives the genesis peer from state, clears known_hosts for the
additional bootstrap IPs, and hands all additional nodes (index 2+) to the
multi-host `AntBootstrapNodesProvisioner` in a single call (the per-node loop
was replaced when provisioning was parallelised).
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.provision_bootstraps import cmd_provision_bootstraps
from saorsa_deploy.provisioning.bootstrap import DEFAULT_REWARDS_ADDRESS


def _args(**overrides):
    base = dict(
        name="DEV-01",
        ssh_key_path="~/.ssh/id_rsa",
        port=10000,
        log_level=None,
        testnet=True,
        upgrade_channel=None,
        rewards_address=DEFAULT_REWARDS_ADDRESS,
        branch=None,
        repo_owner=None,
        ant_node_version=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


_STATE = {
    "bootstrap_nodes": [
        {"ip": "10.0.0.1", "region": "lon1", "bootstrap_index": 1},
        {"ip": "10.0.0.2", "region": "ams3", "bootstrap_index": 2},
        {"ip": "10.0.0.3", "region": "fra1", "bootstrap_index": 3},
    ],
    "bootstrap_port": 10000,
}


def _common_patches():
    return (
        patch("saorsa_deploy.cmd.provision_bootstraps.load_deployment_state"),
        patch("saorsa_deploy.cmd.provision_bootstraps.update_deployment_state"),
        patch("saorsa_deploy.cmd.provision_bootstraps.clear_known_hosts"),
        patch("saorsa_deploy.cmd.provision_bootstraps.record_event"),
        patch(
            "saorsa_deploy.cmd.provision_bootstraps._resolve_binary_source",
            return_value=(None, True),
        ),
        patch(
            "saorsa_deploy.cmd.provision_bootstraps.AntBootstrapNodesProvisioner"
        ),
    )


class TestCmdProvisionBootstraps:
    def test_provisions_additional_nodes_in_single_call(self):
        load, update, clear, _event, _bin, prov_cls = (
            p.start() for p in _common_patches()
        )
        try:
            load.return_value = dict(_STATE)
            cmd_provision_bootstraps(_args())

            # One provisioner, constructed once, executed once.
            assert prov_cls.call_count == 1
            prov_cls.return_value.execute.assert_called_once()

            kwargs = prov_cls.call_args.kwargs
            # Only index 2+ nodes are passed (genesis excluded).
            passed_idx = sorted(n["bootstrap_index"] for n in kwargs["nodes"])
            assert passed_idx == [2, 3]
            assert kwargs["bootstrap_peers"] == ["10.0.0.1:10000"]
            assert kwargs["testnet_name"] == "DEV-01"
        finally:
            patch.stopall()

    def test_clears_known_hosts_for_additional_ips(self):
        load, _update, clear, _event, _bin, _prov_cls = (
            p.start() for p in _common_patches()
        )
        try:
            load.return_value = dict(_STATE)
            cmd_provision_bootstraps(_args())
            called_ips = clear.call_args[0][0]
            assert called_ips == ["10.0.0.2", "10.0.0.3"]
        finally:
            patch.stopall()

    def test_single_bootstrap_is_noop(self):
        load, _update, _clear, _event, _bin, prov_cls = (
            p.start() for p in _common_patches()
        )
        try:
            load.return_value = {
                "bootstrap_nodes": [
                    {"ip": "10.0.0.1", "region": "lon1", "bootstrap_index": 1}
                ],
                "bootstrap_port": 10000,
            }
            cmd_provision_bootstraps(_args())
            prov_cls.assert_not_called()
        finally:
            patch.stopall()

    def test_provisioner_failure_marks_failed_and_exits(self):
        load, _update, _clear, _event, _bin, prov_cls = (
            p.start() for p in _common_patches()
        )
        status_patch = patch("saorsa_deploy.registry.record_status")
        record_status = status_patch.start()
        try:
            load.return_value = dict(_STATE)
            prov_cls.return_value.execute.side_effect = RuntimeError("boom")
            with pytest.raises(SystemExit) as exc:
                cmd_provision_bootstraps(_args())
            assert exc.value.code == 1
            assert record_status.called
        finally:
            patch.stopall()
