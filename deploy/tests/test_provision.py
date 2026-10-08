from unittest.mock import MagicMock, patch

import pytest
from pyinfra.operations import server

from saorsa_deploy.provisioning.bootstrap import DEFAULT_REWARDS_ADDRESS
from saorsa_deploy.provisioning.node import (
    AntNodeProvisioner,
    _build_node_exec_start,
    _build_node_unit_file,
)
from saorsa_deploy.provisioning.volume import MOUNT_POINT


@pytest.fixture(autouse=True)
def _mock_operator_authorized_keys_add_op():
    """Neutralize the operator-SSH-keys op's internal ``add_op``.

    ``execute()`` calls ``add_operator_authorized_keys_op`` (added in
    ``feat: install operator SSH keys on every VM``), which invokes the real
    pyinfra ``add_op`` from ``provisioning.authorized_keys`` — a module these
    tests don't patch. Against the mocked State that makes pyinfra raise
    "Operation called within another operation". Patch that helper's ``add_op``
    the same way the tests already patch each provisioner module's ``add_op``.
    """
    with patch("saorsa_deploy.provisioning.authorized_keys.add_op"):
        yield


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
        result = _build_node_exec_start(["10.0.0.1:5000"], self.ROOT, log_dir="/var/log/ant-node-1")
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
        # disable-uu + disable-ufw + volume-mount + log-dirs + download
        # + write-units + enable = 7
        assert mock_add_op.call_count == 7
        mock_run_ops.assert_called_once()
        mock_disconnect.assert_called_once()

    @patch("saorsa_deploy.provisioning.node.disconnect_all")
    @patch("saorsa_deploy.provisioning.node.run_ops")
    @patch("saorsa_deploy.provisioning.node.add_op")
    @patch("saorsa_deploy.provisioning.node.connect_all")
    @patch("saorsa_deploy.provisioning.node.State")
    @patch("saorsa_deploy.provisioning.node.Inventory")
    @patch("saorsa_deploy.provisioning.node.get_release_url")
    def test_execute_folds_extra_op_providers_into_pass(
        self,
        mock_release_url,
        _mock_inventory,
        mock_state,
        _mock_connect,
        _mock_add_op,
        mock_run_ops,
        _mock_disconnect,
    ):
        # Telegraf forwarders are added to the SAME pyinfra state before run_ops:
        # the first installs the deb (install=True), the rest reuse it.
        mock_release_url.return_value = "https://github.com/download/v1.0.0/asset.tar.gz"
        _configure_state_mock(mock_state, ["10.0.0.1"])
        es = MagicMock()
        metrics = MagicMock()

        AntNodeProvisioner(
            host_ips=["10.0.0.1"],
            bootstrap_peers=["10.0.0.100:5000"],
            extra_op_providers=[es, metrics],
        ).execute()

        state_instance = mock_state.return_value
        es.add_ops.assert_called_once_with(state_instance, install=True)
        metrics.add_ops.assert_called_once_with(state_instance, install=False)
        mock_run_ops.assert_called_once()

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

        # disable-uu + disable-ufw + volume-mount + log-dirs + download
        # + write-units + enable = 7
        assert mock_add_op.call_count == 7

    @patch("saorsa_deploy.provisioning.node.disconnect_all")
    @patch("saorsa_deploy.provisioning.node.run_ops")
    @patch("saorsa_deploy.provisioning.node.add_op")
    @patch("saorsa_deploy.provisioning.node.connect_all")
    @patch("saorsa_deploy.provisioning.node.State")
    @patch("saorsa_deploy.provisioning.node.Inventory")
    @patch("saorsa_deploy.provisioning.node.get_release_url")
    def test_multi_file_ops_collapse_to_single_command(
        self,
        mock_release_url,
        _mock_inventory,
        mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        # log-dirs and unit-file writes must each be ONE command (one SSH
        # round-trip per host), not one command per node service — otherwise an
        # op over 15 services costs 15 sequential round-trips on the slowest host.
        mock_release_url.return_value = "https://github.com/download/v1.0.0/asset.tar.gz"
        _configure_state_mock(mock_state, ["10.0.0.1"])

        AntNodeProvisioner(
            host_ips=["10.0.0.1"],
            bootstrap_peers=["10.0.0.100:5000"],
            node_count=15,
        ).execute()

        ops = {
            call.kwargs.get("name"): call.kwargs.get("commands", [])
            for call in mock_add_op.call_args_list
        }
        # One mkdir -p with every directory as an argument.
        log_cmds = ops["Create log directories"]
        assert len(log_cmds) == 1
        assert log_cmds[0].startswith("mkdir -p ")
        assert "ant-node-1" in log_cmds[0] and "ant-node-15" in log_cmds[0]
        # One script writing all 15 unit files.
        unit_cmds = ops["Write systemd unit files"]
        assert len(unit_cmds) == 1
        assert unit_cmds[0].count("cat > /etc/systemd/system/ant-node-") == 15

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

        # Op order: disable-uu, disable-ufw, mount-volume, create-logs,
        # binary-install, ...
        download_call = mock_add_op.call_args_list[4]
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
        # `nodes start` handles that after NAT rules are in place). The
        # daemon-reload + per-service enables are collapsed into ONE command
        # (one SSH round-trip per host) rather than one command per service.
        enable_call = mock_add_op.call_args_list[-1]
        commands = enable_call.kwargs.get("commands", [])
        assert len(commands) == 1
        script = commands[0]
        assert script.startswith("set -e\n")
        assert "systemctl daemon-reload" in script
        assert "systemctl is-enabled --quiet ant-node-1" in script
        assert "systemctl enable ant-node-1" in script
        assert "systemctl is-enabled --quiet ant-node-2" in script
        assert "--now" not in script
