from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pyinfra.operations import files, server, systemd

from saorsa_deploy.binary_source import get_release_url
from saorsa_deploy.cmd.provision_genesis import cmd_provision_genesis
from saorsa_deploy.provisioning.bootstrap import (
    BINARY_INSTALL_PATH,
    DEFAULT_REWARDS_ADDRESS,
    AntBootstrapNodeProvisioner,
    _build_exec_start,
    _build_unit_file,
)
from saorsa_deploy.provisioning.volume import LOG_DIR, MOUNT_POINT

GENESIS_SERVICE_NAME = "ant-bootstrap-1"
GENESIS_ROOT_DIR = f"{MOUNT_POINT}/data/{GENESIS_SERVICE_NAME}"


class TestBuildExecStart:
    def test_minimal_flags_genesis(self):
        result = _build_exec_start(bootstrap_index=1)
        assert result == (
            f"{BINARY_INSTALL_PATH} --root-dir {GENESIS_ROOT_DIR}"
            " --enable-logging"
            " --log-format json"
            f" --log-dir {LOG_DIR}/{GENESIS_SERVICE_NAME}"
            " --stop-on-upgrade"
            " --evm-network arbitrum-sepolia"
            " --metrics-port 9100"
            f" --rewards-address {DEFAULT_REWARDS_ADDRESS}"
        )

    def test_root_dir_always_present(self):
        result = _build_exec_start(bootstrap_index=1)
        assert f"--root-dir {GENESIS_ROOT_DIR}" in result

    def test_genesis_has_no_bootstrap_flag(self):
        result = _build_exec_start(bootstrap_index=1)
        assert "--bootstrap" not in result

    def test_non_genesis_has_bootstrap_flag(self):
        result = _build_exec_start(
            bootstrap_index=2, bootstrap_peers=["10.0.0.1:5000"]
        )
        assert "--bootstrap 10.0.0.1:5000" in result

    def test_non_genesis_multiple_peers(self):
        result = _build_exec_start(
            bootstrap_index=3,
            bootstrap_peers=["10.0.0.1:5000", "10.0.0.2:5000"],
        )
        assert "--bootstrap 10.0.0.1:5000" in result
        assert "--bootstrap 10.0.0.2:5000" in result

    def test_all_flags(self):
        result = _build_exec_start(
            bootstrap_index=1, port=10000, log_level="debug", testnet=True
        )
        assert "--port 10000" in result
        assert "--log-level debug" in result
        assert "--network-mode testnet" in result

    def test_port_only(self):
        result = _build_exec_start(bootstrap_index=1, port=5000)
        assert "--port 5000" in result
        assert "--log-level" not in result
        assert "--network-mode" not in result

    def test_testnet_flag(self):
        result = _build_exec_start(bootstrap_index=1, testnet=True)
        assert "--network-mode testnet" in result

    def test_no_network_mode_when_not_testnet(self):
        result = _build_exec_start(bootstrap_index=1, testnet=False)
        assert "--network-mode" not in result

    def test_upgrade_channel_beta(self):
        result = _build_exec_start(bootstrap_index=1, upgrade_channel="beta")
        assert "--upgrade-channel beta" in result

    def test_upgrade_channel_stable(self):
        result = _build_exec_start(bootstrap_index=1, upgrade_channel="stable")
        assert "--upgrade-channel stable" in result

    def test_no_upgrade_channel_by_default(self):
        result = _build_exec_start(bootstrap_index=1)
        assert "--upgrade-channel" not in result

    def test_stop_on_upgrade_always_present(self):
        result = _build_exec_start(bootstrap_index=1)
        assert "--stop-on-upgrade" in result

    def test_enable_logging_always_present(self):
        """--enable-logging must be emitted so the other log-* flags aren't
        silently ignored by ant-node."""
        result = _build_exec_start(bootstrap_index=1)
        assert "--enable-logging" in result

    def test_enable_logging_precedes_other_log_flags(self):
        result = _build_exec_start(bootstrap_index=1)
        assert result.index("--enable-logging") < result.index("--log-format")
        assert result.index("--enable-logging") < result.index("--log-dir")

    def test_default_rewards_address(self):
        result = _build_exec_start(bootstrap_index=1)
        assert f"--rewards-address {DEFAULT_REWARDS_ADDRESS}" in result

    def test_custom_rewards_address(self):
        custom_addr = "0x1234567890abcdef1234567890abcdef12345678"
        result = _build_exec_start(bootstrap_index=1, rewards_address=custom_addr)
        assert f"--rewards-address {custom_addr}" in result
        assert DEFAULT_REWARDS_ADDRESS not in result


class TestBuildUnitFile:
    def test_contains_exec_start(self):
        exec_start = "/usr/local/bin/ant-node --port 10000"
        unit = _build_unit_file(GENESIS_SERVICE_NAME, exec_start)
        assert f"ExecStart={exec_start}" in unit

    def test_contains_service_section(self):
        unit = _build_unit_file(GENESIS_SERVICE_NAME, "/usr/local/bin/ant-node")
        assert "[Service]" in unit
        assert "[Unit]" in unit
        assert "[Install]" in unit
        assert "Restart=on-success" in unit
        assert "WantedBy=multi-user.target" in unit

    def test_contains_service_name_in_description(self):
        unit = _build_unit_file(GENESIS_SERVICE_NAME, "/usr/local/bin/ant-node")
        assert f"Description=Ant Bootstrap Node ({GENESIS_SERVICE_NAME})" in unit


class TestGetReleaseUrl:
    @patch("saorsa_deploy.binary_source.requests.get")
    def test_returns_latest_download_url(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "assets": [
                {
                    "name": "ant-node-cli-linux-x64.tar.gz",
                    "browser_download_url": (
                        "https://github.com/download/v1.0.0/asset.tar.gz"
                    ),
                },
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        url = get_release_url()
        assert url == "https://github.com/download/v1.0.0/asset.tar.gz"
        mock_get.assert_called_once_with(
            "https://api.github.com/repos/WithAutonomi/ant-node/releases/latest",
            timeout=30,
        )

    @patch("saorsa_deploy.binary_source.requests.get")
    def test_returns_versioned_download_url(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "assets": [
                {
                    "name": "ant-node-cli-linux-x64.tar.gz",
                    "browser_download_url": (
                        "https://github.com/download/v0.2.0/asset.tar.gz"
                    ),
                },
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        url = get_release_url("0.2.0")
        assert url == "https://github.com/download/v0.2.0/asset.tar.gz"
        mock_get.assert_called_once_with(
            "https://api.github.com/repos/WithAutonomi/ant-node/releases/tags/v0.2.0",
            timeout=30,
        )

    @patch("saorsa_deploy.binary_source.requests.get")
    def test_raises_when_asset_not_found(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "assets": [
                {"name": "other-asset.tar.gz", "browser_download_url": "https://x"}
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        with pytest.raises(RuntimeError, match="Could not find asset"):
            get_release_url()

    @patch("saorsa_deploy.binary_source.requests.get")
    def test_raises_when_no_assets(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"assets": []}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        with pytest.raises(RuntimeError, match="Could not find asset"):
            get_release_url()


class TestAntBootstrapNodeProvisioner:
    def test_init_defaults(self):
        node = AntBootstrapNodeProvisioner(ip="10.0.0.1", bootstrap_index=1)
        assert node.ip == "10.0.0.1"
        assert node.bootstrap_index == 1
        assert node.ssh_key_path == "~/.ssh/id_rsa"
        assert node.port is None
        assert node.log_level is None
        assert node.testnet is False
        assert node.upgrade_channel is None
        assert node.rewards_address == DEFAULT_REWARDS_ADDRESS
        assert node.service_name == "ant-bootstrap-1"

    def test_init_all_params(self):
        node = AntBootstrapNodeProvisioner(
            ip="10.0.0.1",
            bootstrap_index=2,
            ssh_key_path="/tmp/key",
            bootstrap_peers=["10.0.0.100:5000"],
            port=10000,
            log_level="debug",
            testnet=True,
            upgrade_channel="beta",
            rewards_address="0xdeadbeef",
        )
        assert node.ip == "10.0.0.1"
        assert node.bootstrap_index == 2
        assert node.ssh_key_path == "/tmp/key"
        assert node.bootstrap_peers == ["10.0.0.100:5000"]
        assert node.port == 10000
        assert node.log_level == "debug"
        assert node.testnet is True
        assert node.upgrade_channel == "beta"
        assert node.rewards_address == "0xdeadbeef"
        assert node.service_name == "ant-bootstrap-2"

    @patch("saorsa_deploy.provisioning.bootstrap.disconnect_all")
    @patch("saorsa_deploy.provisioning.bootstrap.run_ops")
    @patch("saorsa_deploy.provisioning.bootstrap.add_op")
    @patch("saorsa_deploy.provisioning.bootstrap.connect_all")
    @patch("saorsa_deploy.provisioning.bootstrap.State")
    @patch("saorsa_deploy.provisioning.bootstrap.Inventory")
    @patch("saorsa_deploy.provisioning.bootstrap.get_release_url")
    def test_provision_calls_pyinfra_operations(
        self,
        mock_release_url,
        _mock_inventory,
        _mock_state,
        mock_connect,
        mock_add_op,
        mock_run_ops,
        mock_disconnect,
    ):
        mock_release_url.return_value = (
            "https://github.com/download/v1.0.0/asset.tar.gz"
        )

        node = AntBootstrapNodeProvisioner(
            ip="10.0.0.1", bootstrap_index=1, port=10000
        )
        node.execute()

        mock_connect.assert_called_once()
        # disable-uu + volume-mount + log-dir + download + files.put + daemon_reload + service = 7
        assert mock_add_op.call_count == 7
        mock_run_ops.assert_called_once()
        mock_disconnect.assert_called_once()

    @patch("saorsa_deploy.provisioning.bootstrap.disconnect_all")
    @patch("saorsa_deploy.provisioning.bootstrap.run_ops")
    @patch("saorsa_deploy.provisioning.bootstrap.add_op")
    @patch("saorsa_deploy.provisioning.bootstrap.connect_all")
    @patch("saorsa_deploy.provisioning.bootstrap.State")
    @patch("saorsa_deploy.provisioning.bootstrap.Inventory")
    @patch("saorsa_deploy.provisioning.bootstrap.get_release_url")
    def test_provision_sets_hostname_when_testnet_name_given(
        self,
        mock_release_url,
        _mock_inventory,
        _mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_release_url.return_value = (
            "https://github.com/download/v1.0.0/asset.tar.gz"
        )

        node = AntBootstrapNodeProvisioner(
            ip="10.0.0.1",
            bootstrap_index=3,
            port=10000,
            testnet_name="DEV-07",
        )
        node.execute()

        # The hostname op is added immediately after the unattended-upgrades
        # disable op, before any volume/log/binary work.
        hostname_call = mock_add_op.call_args_list[1]
        commands = hostname_call.kwargs.get("commands", [])
        assert len(commands) == 1
        assert "DEV-07-ant-bootstrap-3" in commands[0]
        assert "hostnamectl set-hostname" in commands[0]

    def test_no_hostname_op_when_testnet_name_omitted(self):
        # The hostname property is None when testnet_name isn't provided,
        # so no hostname op will be emitted during execute(). Guards the
        # back-compat case for callers that don't yet pass testnet_name.
        node = AntBootstrapNodeProvisioner(ip="10.0.0.1", bootstrap_index=1)
        assert node.hostname is None

    @patch("saorsa_deploy.provisioning.bootstrap.disconnect_all")
    @patch("saorsa_deploy.provisioning.bootstrap.run_ops")
    @patch("saorsa_deploy.provisioning.bootstrap.add_op")
    @patch("saorsa_deploy.provisioning.bootstrap.connect_all")
    @patch("saorsa_deploy.provisioning.bootstrap.State")
    @patch("saorsa_deploy.provisioning.bootstrap.Inventory")
    @patch("saorsa_deploy.provisioning.bootstrap.get_release_url")
    def test_provision_disconnects_on_error(
        self,
        mock_release_url,
        _mock_inventory,
        _mock_state,
        _mock_connect,
        _mock_add_op,
        mock_run_ops,
        mock_disconnect,
    ):
        mock_release_url.return_value = (
            "https://github.com/download/v1.0.0/asset.tar.gz"
        )
        mock_run_ops.side_effect = RuntimeError("connection failed")

        node = AntBootstrapNodeProvisioner(ip="10.0.0.1", bootstrap_index=1)
        with pytest.raises(RuntimeError, match="connection failed"):
            node.execute()

        mock_disconnect.assert_called_once()

    @patch("saorsa_deploy.provisioning.bootstrap.disconnect_all")
    @patch("saorsa_deploy.provisioning.bootstrap.run_ops")
    @patch("saorsa_deploy.provisioning.bootstrap.add_op")
    @patch("saorsa_deploy.provisioning.bootstrap.connect_all")
    @patch("saorsa_deploy.provisioning.bootstrap.State")
    @patch("saorsa_deploy.provisioning.bootstrap.Inventory")
    @patch("saorsa_deploy.provisioning.bootstrap.get_release_url")
    def test_provision_creates_inventory_with_correct_host(
        self,
        mock_release_url,
        mock_inventory,
        _mock_state,
        _mock_connect,
        _mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_release_url.return_value = (
            "https://github.com/download/v1.0.0/asset.tar.gz"
        )

        node = AntBootstrapNodeProvisioner(
            ip="192.168.1.100", bootstrap_index=1, ssh_key_path="/tmp/mykey"
        )
        node.execute()

        inventory_call = mock_inventory.call_args
        hosts = inventory_call[0][0][0]
        host_tuple = hosts[0]
        assert host_tuple[0] == "192.168.1.100"
        assert host_tuple[1]["ssh_user"] == "root"
        assert host_tuple[1]["ssh_key"] == "/tmp/mykey"

    @patch("saorsa_deploy.provisioning.bootstrap.disconnect_all")
    @patch("saorsa_deploy.provisioning.bootstrap.run_ops")
    @patch("saorsa_deploy.provisioning.bootstrap.add_op")
    @patch("saorsa_deploy.provisioning.bootstrap.connect_all")
    @patch("saorsa_deploy.provisioning.bootstrap.State")
    @patch("saorsa_deploy.provisioning.bootstrap.Inventory")
    @patch("saorsa_deploy.provisioning.bootstrap.get_release_url")
    def test_provision_uses_idempotent_operations(
        self,
        mock_release_url,
        _mock_inventory,
        _mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_release_url.return_value = (
            "https://github.com/download/v1.0.0/asset.tar.gz"
        )

        node = AntBootstrapNodeProvisioner(
            ip="10.0.0.1", bootstrap_index=1, port=10000
        )
        node.execute()

        op_types = [call[0][1] for call in mock_add_op.call_args_list]
        # disable-uu, volume mount, log-dir, binary install, files.put, daemon_reload, service
        assert op_types[0] is server.shell
        assert op_types[1] is server.shell
        assert op_types[2] is server.shell
        assert op_types[3] is server.shell
        assert op_types[4] is files.put
        assert op_types[5] is systemd.daemon_reload
        assert op_types[6] is systemd.service

    @patch("saorsa_deploy.provisioning.bootstrap.disconnect_all")
    @patch("saorsa_deploy.provisioning.bootstrap.run_ops")
    @patch("saorsa_deploy.provisioning.bootstrap.add_op")
    @patch("saorsa_deploy.provisioning.bootstrap.connect_all")
    @patch("saorsa_deploy.provisioning.bootstrap.State")
    @patch("saorsa_deploy.provisioning.bootstrap.Inventory")
    @patch("saorsa_deploy.provisioning.bootstrap.get_release_url")
    def test_provision_binary_install_has_existence_guard(
        self,
        mock_release_url,
        _mock_inventory,
        _mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_release_url.return_value = (
            "https://github.com/download/v1.0.0/asset.tar.gz"
        )

        node = AntBootstrapNodeProvisioner(ip="10.0.0.1", bootstrap_index=1)
        node.execute()

        # Op order: disable-uu, volume-mount, log-dir, binary-install, ...
        download_call = mock_add_op.call_args_list[3]
        commands = download_call.kwargs.get("commands", [])
        assert len(commands) == 1
        assert commands[0].startswith("test -f /usr/local/bin/ant-node")


class TestCmdProvisionGenesisClearsKnownHosts:
    @patch("saorsa_deploy.cmd.provision_genesis.update_deployment_state")
    @patch("saorsa_deploy.cmd.provision_genesis.AntBootstrapNodeProvisioner")
    @patch("saorsa_deploy.cmd.provision_genesis.clear_known_hosts")
    @patch("saorsa_deploy.cmd.provision_genesis.load_deployment_state")
    def test_clears_known_hosts_for_bootstrap_ip(
        self,
        mock_load_state,
        mock_clear_known_hosts,
        mock_provisioner_cls,
        mock_update_state,
    ):
        mock_load_state.return_value = {
            "bootstrap_nodes": [
                {"ip": "10.0.0.1", "region": "lon1", "bootstrap_index": 1}
            ],
        }
        mock_provisioner_cls.return_value.execute.return_value = None

        args = SimpleNamespace(
            name="test-deploy",
            ssh_key_path="~/.ssh/id_rsa",
            port=None,
            log_level=None,
            testnet=False,
            upgrade_channel=None,
            rewards_address=DEFAULT_REWARDS_ADDRESS,
        )
        cmd_provision_genesis(args)

        mock_clear_known_hosts.assert_called_once()
        called_ips = mock_clear_known_hosts.call_args[0][0]
        assert called_ips == ["10.0.0.1"]

    @patch("saorsa_deploy.cmd.provision_genesis.update_deployment_state")
    @patch("saorsa_deploy.cmd.provision_genesis.AntBootstrapNodeProvisioner")
    @patch("saorsa_deploy.cmd.provision_genesis.clear_known_hosts")
    @patch("saorsa_deploy.cmd.provision_genesis.load_deployment_state")
    def test_clears_known_hosts_before_provisioner_executes(
        self,
        mock_load_state,
        mock_clear_known_hosts,
        mock_provisioner_cls,
        mock_update_state,
    ):
        mock_load_state.return_value = {
            "bootstrap_nodes": [
                {"ip": "10.0.0.1", "region": "lon1", "bootstrap_index": 1}
            ],
        }
        call_order = []
        mock_clear_known_hosts.side_effect = (
            lambda *a, **kw: call_order.append("clear_known_hosts")
        )
        mock_provisioner_cls.return_value.execute.side_effect = (
            lambda: call_order.append("execute")
        )

        args = SimpleNamespace(
            name="test-deploy",
            ssh_key_path="~/.ssh/id_rsa",
            port=None,
            log_level=None,
            testnet=False,
            upgrade_channel=None,
            rewards_address=DEFAULT_REWARDS_ADDRESS,
        )
        cmd_provision_genesis(args)

        assert call_order == ["clear_known_hosts", "execute"]
