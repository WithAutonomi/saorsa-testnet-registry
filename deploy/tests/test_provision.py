from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pyinfra.operations import server

from saorsa_deploy.cmd.provision_nodes import cmd_provision_nodes
from saorsa_deploy.provisioning.bootstrap import DEFAULT_REWARDS_ADDRESS
from saorsa_deploy.provisioning.node import (
    AntNodeProvisioner,
    _build_node_exec_start,
    _build_node_unit_file,
)
from saorsa_deploy.provisioning.volume import MOUNT_POINT


def _mock_active_hosts(host_ips):
    """Build mock Host-like objects for a mocked Pyinfra state.

    Pyinfra operations use ``state.inventory.get_active_hosts()`` to decide
    which hosts an op targets, so tests that patch State must provide a list
    whose members expose a ``.name`` attribute matching each host IP.
    """
    hosts = []
    for ip in host_ips:
        host = MagicMock()
        host.name = ip
        hosts.append(host)
    return hosts


def _configure_state_mock(mock_state, host_ips, failed=()):
    """Configure a patched State mock to behave like a real Pyinfra state."""
    instance = MagicMock()
    instance.failed_hosts = set(failed)
    instance.inventory.get_active_hosts.return_value = _mock_active_hosts(host_ips)
    mock_state.return_value = instance
    return instance


class TestBuildNodeExecStart:
    ROOT = f"{MOUNT_POINT}/data/ant-node-1"

    def test_includes_bootstrap_address(self):
        result = _build_node_exec_start(["10.0.0.1:5000"], self.ROOT)
        assert "--bootstrap 10.0.0.1:5000" in result

    def test_includes_root_dir(self):
        result = _build_node_exec_start(["10.0.0.1:5000"], self.ROOT)
        assert f"--root-dir {self.ROOT}" in result

    def test_includes_port_when_specified(self):
        result = _build_node_exec_start(["10.0.0.1:5000"], self.ROOT, port=6000)
        assert "--port 6000" in result

    def test_no_port_when_not_specified(self):
        result = _build_node_exec_start(["10.0.0.1:5000"], self.ROOT)
        assert "--port" not in result

    def test_includes_log_level(self):
        result = _build_node_exec_start(["10.0.0.1:5000"], self.ROOT, log_level="debug")
        assert "--log-level debug" in result

    def test_includes_testnet_flag(self):
        result = _build_node_exec_start(["10.0.0.1:5000"], self.ROOT, testnet=True)
        assert "--network-mode testnet" in result

    def test_no_network_mode_when_not_testnet(self):
        result = _build_node_exec_start(["10.0.0.1:5000"], self.ROOT, testnet=False)
        assert "--network-mode" not in result

    def test_upgrade_channel_beta(self):
        result = _build_node_exec_start(["10.0.0.1:5000"], self.ROOT, upgrade_channel="beta")
        assert "--upgrade-channel beta" in result

    def test_upgrade_channel_stable(self):
        result = _build_node_exec_start(["10.0.0.1:5000"], self.ROOT, upgrade_channel="stable")
        assert "--upgrade-channel stable" in result

    def test_no_upgrade_channel_by_default(self):
        result = _build_node_exec_start(["10.0.0.1:5000"], self.ROOT)
        assert "--upgrade-channel" not in result

    def test_stop_on_upgrade_always_present(self):
        result = _build_node_exec_start(["10.0.0.1:5000"], self.ROOT)
        assert "--stop-on-upgrade" in result

    def test_enable_logging_always_present(self):
        """--enable-logging must be emitted so the other log-* flags aren't
        silently ignored by ant-node."""
        result = _build_node_exec_start(["10.0.0.1:5000"], self.ROOT)
        assert "--enable-logging" in result

    def test_enable_logging_precedes_other_log_flags(self):
        result = _build_node_exec_start(
            ["10.0.0.1:5000"], self.ROOT, log_dir="/var/log/ant-node-1"
        )
        assert result.index("--enable-logging") < result.index("--log-format")
        assert result.index("--enable-logging") < result.index("--log-dir")

    def test_all_flags(self):
        result = _build_node_exec_start(
            ["10.0.0.1:5000"], self.ROOT, port=6000, log_level="info", testnet=True
        )
        assert f"--root-dir {self.ROOT}" in result
        assert "--bootstrap 10.0.0.1:5000" in result
        assert "--port 6000" in result
        assert "--log-level info" in result
        assert "--network-mode testnet" in result

    def test_default_rewards_address(self):
        result = _build_node_exec_start(["10.0.0.1:5000"], self.ROOT)
        assert f"--rewards-address {DEFAULT_REWARDS_ADDRESS}" in result

    def test_custom_rewards_address(self):
        custom_addr = "0x1234567890abcdef1234567890abcdef12345678"
        result = _build_node_exec_start(["10.0.0.1:5000"], self.ROOT, rewards_address=custom_addr)
        assert f"--rewards-address {custom_addr}" in result
        assert DEFAULT_REWARDS_ADDRESS not in result


class TestBuildNodeUnitFile:
    def test_contains_service_name_in_description(self):
        unit = _build_node_unit_file("ant-node-1", "/usr/local/bin/ant-node --port 5000")
        assert "Description=Ant Node (ant-node-1)" in unit

    def test_contains_exec_start(self):
        exec_start = "/usr/local/bin/ant-node --bootstrap 10.0.0.1:5000 --port 6000"
        unit = _build_node_unit_file("ant-node-1", exec_start)
        assert f"ExecStart={exec_start}" in unit

    def test_contains_service_sections(self):
        unit = _build_node_unit_file("ant-node-1", "/usr/local/bin/ant-node")
        assert "[Service]" in unit
        assert "[Unit]" in unit
        assert "[Install]" in unit
        assert "Restart=on-success" in unit
        assert "WantedBy=multi-user.target" in unit


class TestAntNodeProvisioner:
    def test_init_defaults(self):
        provisioner = AntNodeProvisioner(
            host_ips=["10.0.0.1", "10.0.0.2"],
            bootstrap_peers=["10.0.0.100:5000"],
        )
        assert provisioner.host_ips == ["10.0.0.1", "10.0.0.2"]
        assert provisioner.bootstrap_peers == ["10.0.0.100:5000"]
        assert provisioner.ssh_key_path == "~/.ssh/id_rsa"
        assert provisioner.node_count == 1
        assert provisioner.initial_port is None
        assert provisioner.log_level is None
        assert provisioner.testnet is False
        assert provisioner.upgrade_channel is None
        assert provisioner.rewards_address == DEFAULT_REWARDS_ADDRESS

    def test_init_all_params(self):
        provisioner = AntNodeProvisioner(
            host_ips=["10.0.0.1"],
            bootstrap_peers=["10.0.0.100:5000"],
            ssh_key_path="/tmp/key",
            node_count=3,
            initial_port=6000,
            log_level="debug",
            testnet=True,
            upgrade_channel="stable",
            rewards_address="0xdeadbeef",
        )
        assert provisioner.ssh_key_path == "/tmp/key"
        assert provisioner.node_count == 3
        assert provisioner.initial_port == 6000
        assert provisioner.log_level == "debug"
        assert provisioner.testnet is True
        assert provisioner.upgrade_channel == "stable"
        assert provisioner.rewards_address == "0xdeadbeef"

    @patch("saorsa_deploy.provisioning.node.disconnect_all")
    @patch("saorsa_deploy.provisioning.node.run_ops")
    @patch("saorsa_deploy.provisioning.node.add_op")
    @patch("saorsa_deploy.provisioning.node.connect_all")
    @patch("saorsa_deploy.provisioning.node.State")
    @patch("saorsa_deploy.provisioning.node.Inventory")
    @patch("saorsa_deploy.provisioning.node.get_release_url")
    def test_execute_calls_pyinfra_operations(
        self,
        mock_release_url,
        _,
        mock_state,
        mock_connect,
        mock_add_op,
        mock_run_ops,
        mock_disconnect,
    ):
        mock_release_url.return_value = "https://github.com/download/v1.0.0/asset.tar.gz"
        _configure_state_mock(mock_state, ["10.0.0.1"])

        provisioner = AntNodeProvisioner(
            host_ips=["10.0.0.1"],
            bootstrap_peers=["10.0.0.100:5000"],
            node_count=2,
        )
        provisioner.execute()

        mock_connect.assert_called_once()
        # disable-uu + volume-mount + log-dirs + download + write-units + enable = 6
        assert mock_add_op.call_count == 6
        mock_run_ops.assert_called_once()
        mock_disconnect.assert_called_once()

    @patch("saorsa_deploy.provisioning.node.disconnect_all")
    @patch("saorsa_deploy.provisioning.node.run_ops")
    @patch("saorsa_deploy.provisioning.node.add_op")
    @patch("saorsa_deploy.provisioning.node.connect_all")
    @patch("saorsa_deploy.provisioning.node.State")
    @patch("saorsa_deploy.provisioning.node.Inventory")
    @patch("saorsa_deploy.provisioning.node.get_release_url")
    def test_execute_disconnects_on_error(
        self,
        mock_release_url,
        _mock_inventory,
        _mock_state,
        _mock_connect,
        _mock_add_op,
        mock_run_ops,
        mock_disconnect,
    ):
        mock_release_url.return_value = "https://github.com/download/v1.0.0/asset.tar.gz"
        mock_run_ops.side_effect = RuntimeError("connection failed")

        provisioner = AntNodeProvisioner(
            host_ips=["10.0.0.1"],
            bootstrap_peers=["10.0.0.100:5000"],
        )
        with pytest.raises(RuntimeError, match="connection failed"):
            provisioner.execute()

        mock_disconnect.assert_called_once()

    @patch("saorsa_deploy.provisioning.node.disconnect_all")
    @patch("saorsa_deploy.provisioning.node.run_ops")
    @patch("saorsa_deploy.provisioning.node.add_op")
    @patch("saorsa_deploy.provisioning.node.connect_all")
    @patch("saorsa_deploy.provisioning.node.State")
    @patch("saorsa_deploy.provisioning.node.Inventory")
    @patch("saorsa_deploy.provisioning.node.get_release_url")
    def test_execute_creates_inventory_with_all_hosts(
        self,
        mock_release_url,
        mock_inventory,
        mock_state,
        _mock_connect,
        _mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_release_url.return_value = "https://github.com/download/v1.0.0/asset.tar.gz"
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        provisioner = AntNodeProvisioner(
            host_ips=["10.0.0.1", "10.0.0.2", "10.0.0.3"],
            bootstrap_peers=["10.0.0.100:5000"],
            ssh_key_path="/tmp/mykey",
        )
        provisioner.execute()

        inventory_call = mock_inventory.call_args
        hosts_data = inventory_call[0][0][0]
        assert len(hosts_data) == 3
        assert hosts_data[0][0] == "10.0.0.1"
        assert hosts_data[0][1]["ssh_user"] == "root"
        assert hosts_data[0][1]["ssh_key"] == "/tmp/mykey"
        assert hosts_data[1][0] == "10.0.0.2"
        assert hosts_data[2][0] == "10.0.0.3"

    @patch("saorsa_deploy.provisioning.node.disconnect_all")
    @patch("saorsa_deploy.provisioning.node.run_ops")
    @patch("saorsa_deploy.provisioning.node.add_op")
    @patch("saorsa_deploy.provisioning.node.connect_all")
    @patch("saorsa_deploy.provisioning.node.State")
    @patch("saorsa_deploy.provisioning.node.Inventory")
    @patch("saorsa_deploy.provisioning.node.get_release_url")
    def test_execute_creates_correct_service_count(
        self,
        mock_release_url,
        _mock_inventory,
        mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_release_url.return_value = "https://github.com/download/v1.0.0/asset.tar.gz"
        _configure_state_mock(mock_state, ["10.0.0.1"])

        provisioner = AntNodeProvisioner(
            host_ips=["10.0.0.1"],
            bootstrap_peers=["10.0.0.100:5000"],
            node_count=3,
        )
        provisioner.execute()

        # disable-uu + volume-mount + log-dirs + download + write-units + enable = 6
        assert mock_add_op.call_count == 6

    @patch("saorsa_deploy.provisioning.node.disconnect_all")
    @patch("saorsa_deploy.provisioning.node.run_ops")
    @patch("saorsa_deploy.provisioning.node.add_op")
    @patch("saorsa_deploy.provisioning.node.connect_all")
    @patch("saorsa_deploy.provisioning.node.State")
    @patch("saorsa_deploy.provisioning.node.Inventory")
    @patch("saorsa_deploy.provisioning.node.get_release_url")
    def test_execute_raises_when_hosts_fail(
        self,
        mock_release_url,
        _mock_inventory,
        mock_state,
        _mock_connect,
        _mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_release_url.return_value = "https://github.com/download/v1.0.0/asset.tar.gz"
        failed_host = MagicMock()
        failed_host.name = "10.0.0.1"
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = {failed_host}
        mock_state.return_value = mock_state_instance

        provisioner = AntNodeProvisioner(
            host_ips=["10.0.0.1"],
            bootstrap_peers=["10.0.0.100:5000"],
        )
        with pytest.raises(RuntimeError, match="1 host\\(s\\) failed provisioning"):
            provisioner.execute()

    @patch("saorsa_deploy.provisioning.node.disconnect_all")
    @patch("saorsa_deploy.provisioning.node.run_ops")
    @patch("saorsa_deploy.provisioning.node.add_op")
    @patch("saorsa_deploy.provisioning.node.connect_all")
    @patch("saorsa_deploy.provisioning.node.State")
    @patch("saorsa_deploy.provisioning.node.Inventory")
    @patch("saorsa_deploy.provisioning.node.get_release_url")
    def test_execute_all_ops_are_server_shell(
        self,
        mock_release_url,
        _mock_inventory,
        mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_release_url.return_value = "https://github.com/download/v1.0.0/asset.tar.gz"
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        provisioner = AntNodeProvisioner(
            host_ips=["10.0.0.1"],
            bootstrap_peers=["10.0.0.100:5000"],
            node_count=2,
        )
        provisioner.execute()

        op_types = [call[0][1] for call in mock_add_op.call_args_list]
        assert all(op is server.shell for op in op_types)

    @patch("saorsa_deploy.provisioning.node.disconnect_all")
    @patch("saorsa_deploy.provisioning.node.run_ops")
    @patch("saorsa_deploy.provisioning.node.add_op")
    @patch("saorsa_deploy.provisioning.node.connect_all")
    @patch("saorsa_deploy.provisioning.node.State")
    @patch("saorsa_deploy.provisioning.node.Inventory")
    @patch("saorsa_deploy.provisioning.node.get_release_url")
    def test_execute_binary_install_has_existence_guard(
        self,
        mock_release_url,
        _mock_inventory,
        mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_release_url.return_value = "https://github.com/download/v1.0.0/asset.tar.gz"
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        provisioner = AntNodeProvisioner(
            host_ips=["10.0.0.1"],
            bootstrap_peers=["10.0.0.100:5000"],
        )
        provisioner.execute()

        # Op order: disable-uu, mount-volume, create-logs, binary-install, ...
        download_call = mock_add_op.call_args_list[3]
        commands = download_call.kwargs.get("commands", [])
        assert len(commands) == 1
        assert commands[0].startswith("test -f /usr/local/bin/ant-node")

    @patch("saorsa_deploy.provisioning.node.disconnect_all")
    @patch("saorsa_deploy.provisioning.node.run_ops")
    @patch("saorsa_deploy.provisioning.node.add_op")
    @patch("saorsa_deploy.provisioning.node.connect_all")
    @patch("saorsa_deploy.provisioning.node.State")
    @patch("saorsa_deploy.provisioning.node.Inventory")
    @patch("saorsa_deploy.provisioning.node.get_release_url")
    def test_execute_service_enable_has_is_enabled_guard(
        self,
        mock_release_url,
        _mock_inventory,
        mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_release_url.return_value = "https://github.com/download/v1.0.0/asset.tar.gz"
        _configure_state_mock(mock_state, ["10.0.0.1"])

        provisioner = AntNodeProvisioner(
            host_ips=["10.0.0.1"],
            bootstrap_peers=["10.0.0.100:5000"],
            node_count=2,
        )
        provisioner.execute()

        # Last add_op is the enable step (services are not started here —
        # `nodes start` handles that after NAT rules are in place)
        enable_call = mock_add_op.call_args_list[-1]
        commands = enable_call.kwargs.get("commands", [])
        assert commands[0] == "systemctl daemon-reload"
        assert "systemctl is-enabled --quiet ant-node-1" in commands[1]
        assert "systemctl enable ant-node-1" in commands[1]
        assert "--now" not in commands[1]
        assert "systemctl is-enabled --quiet ant-node-2" in commands[2]
        assert "--now" not in commands[2]


class TestCmdProvisionClearsKnownHosts:
    @patch("saorsa_deploy.cmd.provision_nodes.update_deployment_state")
    @patch("saorsa_deploy.cmd.provision_nodes.AntNodeProvisioner")
    @patch("saorsa_deploy.cmd.provision_nodes.clear_known_hosts")
    @patch("saorsa_deploy.cmd.provision_nodes.load_deployment_state")
    def test_clears_known_hosts_for_all_vm_ips(
        self,
        mock_load_state,
        mock_clear_known_hosts,
        mock_provisioner_cls,
        _mock_update_state,
    ):
        mock_load_state.return_value = {
            "bootstrap_nodes": [
                {"ip": "10.0.0.100", "region": "lon1", "bootstrap_index": 1, "port": 5000},
            ],
            "bootstrap_port": 5000,
            "bootstrap_ip": "10.0.0.100",
            "vm_ips": {
                "lon1": ["10.0.0.1", "10.0.0.2"],
                "nyc1": ["10.0.0.3"],
            },
        }
        mock_provisioner_cls.return_value.execute.return_value = None

        args = SimpleNamespace(
            name="test-deploy",
            ssh_key_path="~/.ssh/id_rsa",
            node_count=1,
            port=None,

            log_level=None,
            testnet=False,
            region=None,
            upgrade_channel=None,
            rewards_address=DEFAULT_REWARDS_ADDRESS,
        )
        cmd_provision_nodes(args)

        mock_clear_known_hosts.assert_called_once()
        called_ips = mock_clear_known_hosts.call_args[0][0]
        assert sorted(called_ips) == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]

    @patch("saorsa_deploy.cmd.provision_nodes.update_deployment_state")
    @patch("saorsa_deploy.cmd.provision_nodes.AntNodeProvisioner")
    @patch("saorsa_deploy.cmd.provision_nodes.clear_known_hosts")
    @patch("saorsa_deploy.cmd.provision_nodes.load_deployment_state")
    def test_clears_known_hosts_for_single_region(
        self,
        mock_load_state,
        mock_clear_known_hosts,
        mock_provisioner_cls,
        _mock_update_state,
    ):
        mock_load_state.return_value = {
            "bootstrap_nodes": [
                {"ip": "10.0.0.100", "region": "lon1", "bootstrap_index": 1, "port": 5000},
            ],
            "bootstrap_port": 5000,
            "bootstrap_ip": "10.0.0.100",
            "vm_ips": {
                "lon1": ["10.0.0.1", "10.0.0.2"],
                "nyc1": ["10.0.0.3"],
            },
        }
        mock_provisioner_cls.return_value.execute.return_value = None

        args = SimpleNamespace(
            name="test-deploy",
            ssh_key_path="~/.ssh/id_rsa",
            node_count=1,
            port=None,

            log_level=None,
            testnet=False,
            region="lon1",
            upgrade_channel=None,
            rewards_address=DEFAULT_REWARDS_ADDRESS,
        )
        cmd_provision_nodes(args)

        mock_clear_known_hosts.assert_called_once()
        called_ips = mock_clear_known_hosts.call_args[0][0]
        assert called_ips == ["10.0.0.1", "10.0.0.2"]

    @patch("saorsa_deploy.cmd.provision_nodes.update_deployment_state")
    @patch("saorsa_deploy.cmd.provision_nodes.AntNodeProvisioner")
    @patch("saorsa_deploy.cmd.provision_nodes.clear_known_hosts")
    @patch("saorsa_deploy.cmd.provision_nodes.load_deployment_state")
    def test_clears_known_hosts_before_provisioner_executes(
        self,
        mock_load_state,
        mock_clear_known_hosts,
        mock_provisioner_cls,
        _mock_update_state,
    ):
        mock_load_state.return_value = {
            "bootstrap_nodes": [
                {"ip": "10.0.0.100", "region": "lon1", "bootstrap_index": 1, "port": 5000},
            ],
            "bootstrap_port": 5000,
            "bootstrap_ip": "10.0.0.100",
            "vm_ips": {"lon1": ["10.0.0.1"]},
        }
        call_order = []
        mock_clear_known_hosts.side_effect = lambda *a, **kw: call_order.append("clear_known_hosts")
        mock_provisioner_cls.return_value.execute.side_effect = lambda: call_order.append("execute")

        args = SimpleNamespace(
            name="test-deploy",
            ssh_key_path="~/.ssh/id_rsa",
            node_count=1,
            port=None,

            log_level=None,
            testnet=False,
            region=None,
            upgrade_channel=None,
            rewards_address=DEFAULT_REWARDS_ADDRESS,
        )
        cmd_provision_nodes(args)

        assert call_order == ["clear_known_hosts", "execute"]
