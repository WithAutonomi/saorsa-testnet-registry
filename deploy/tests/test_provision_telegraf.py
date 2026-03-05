from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pyinfra.operations import server

from saorsa_deploy.provisioning.telegraf import (
    TELEGRAF_CONFIG_DIR,
    TELEGRAF_CONFIG_PATH,
    TELEGRAF_ENV_PATH,
    TELEGRAF_SERVICE_NAME,
    TELEGRAF_UNIT_PATH,
    TelegrafProvisioner,
)


def _make_provisioner(**overrides):
    defaults = {
        "host_ips": ["10.0.0.1"],
        "ssh_key_path": "~/.ssh/id_rsa",
        "testnet_name": "DEV-01",
        "elasticsearch_url": "https://logs.example.com/es/api",
        "elasticsearch_username": "telegraf-user",
        "elasticsearch_password": "secret123",
        "host_role": "WAN_NODE",
        "branch_commit": "abc123",
        "node_version": "0.1.0",
    }
    defaults.update(overrides)
    return TelegrafProvisioner(**defaults)


class TestBuildEnvFile:
    def test_contains_elasticsearch_credentials(self):
        p = _make_provisioner()
        env = p._build_env_file()
        assert "TELEGRAF_ELASTICSEARCH_URL=https://logs.example.com/es/api" in env
        assert "TELEGRAF_ELASTICSEARCH_USERNAME=telegraf-user" in env
        assert "TELEGRAF_ELASTICSEARCH_PASSWORD=secret123" in env

    def test_contains_testnet_metadata(self):
        p = _make_provisioner(testnet_name="DEV-01", host_role="GENESIS")
        env = p._build_env_file()
        assert "SAORSA_TESTNET_NAME=DEV-01" in env
        assert "SAORSA_TESTNET_NAME_LOWER=dev-01" in env
        assert "SAORSA_HOST_ROLE=GENESIS" in env

    def test_contains_version_info(self):
        p = _make_provisioner(branch_commit="abc123", node_version="0.1.0")
        env = p._build_env_file()
        assert "SAORSA_BRANCH_COMMIT=abc123" in env
        assert "SAORSA_NODE_VERSION=0.1.0" in env

    def test_contains_log_dir(self):
        p = _make_provisioner()
        env = p._build_env_file()
        assert "SAORSA_LOG_DIR=" in env

    def test_each_line_is_key_value(self):
        p = _make_provisioner()
        env = p._build_env_file()
        for line in env.strip().split("\n"):
            assert "=" in line, f"Line is not KEY=VALUE: {line}"
            key, _ = line.split("=", 1)
            assert key.isidentifier(), f"Invalid env var name: {key}"

    def test_ends_with_newline(self):
        p = _make_provisioner()
        env = p._build_env_file()
        assert env.endswith("\n")


class TestBuildTelegrafConfig:
    def test_is_valid_toml(self):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        p = _make_provisioner()
        config = p._build_telegraf_config()
        parsed = tomllib.loads(config)
        assert "global_tags" in parsed
        assert "agent" in parsed

    def test_has_inputs_tail(self):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        p = _make_provisioner()
        config = p._build_telegraf_config()
        parsed = tomllib.loads(config)
        assert "inputs" in parsed
        assert "tail" in parsed["inputs"]

    def test_has_outputs_elasticsearch(self):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        p = _make_provisioner()
        config = p._build_telegraf_config()
        parsed = tomllib.loads(config)
        assert "outputs" in parsed
        assert "elasticsearch" in parsed["outputs"]

    def test_has_processors(self):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        p = _make_provisioner()
        config = p._build_telegraf_config()
        parsed = tomllib.loads(config)
        assert "processors" in parsed
        assert "rename" in parsed["processors"]
        assert "starlark" in parsed["processors"]
        assert "strings" in parsed["processors"]

    def test_uses_env_var_references(self):
        p = _make_provisioner()
        config = p._build_telegraf_config()
        assert "${SAORSA_TESTNET_NAME}" in config
        assert "${SAORSA_LOG_DIR}" in config
        assert "${TELEGRAF_ELASTICSEARCH_URL}" in config

    def test_tail_input_glob_matches_log_files(self):
        p = _make_provisioner()
        config = p._build_telegraf_config()
        assert "saorsa-*/saorsa-node*.log*" in config

    def test_starlark_source_present(self):
        p = _make_provisioner()
        config = p._build_telegraf_config()
        assert "def apply(metric):" in config
        assert "service_number" in config


class TestBuildUnitFile:
    def test_has_systemd_sections(self):
        p = _make_provisioner()
        unit = p._build_unit_file()
        assert "[Unit]" in unit
        assert "[Service]" in unit
        assert "[Install]" in unit

    def test_has_environment_file(self):
        p = _make_provisioner()
        unit = p._build_unit_file()
        assert f"EnvironmentFile=-{TELEGRAF_ENV_PATH}" in unit

    def test_has_correct_exec_start(self):
        p = _make_provisioner()
        unit = p._build_unit_file()
        assert f"ExecStart=/usr/bin/telegraf -config {TELEGRAF_CONFIG_PATH}" in unit

    def test_runs_as_telegraf_user(self):
        p = _make_provisioner()
        unit = p._build_unit_file()
        assert "User=telegraf" in unit

    def test_has_restart_policy(self):
        p = _make_provisioner()
        unit = p._build_unit_file()
        assert "Restart=on-failure" in unit

    def test_wanted_by_multi_user(self):
        p = _make_provisioner()
        unit = p._build_unit_file()
        assert "WantedBy=multi-user.target" in unit


class TestTelegrafProvisionerInit:
    def test_init_stores_all_params(self):
        p = _make_provisioner(
            host_ips=["10.0.0.1", "10.0.0.2"],
            host_role="GENESIS",
            testnet_name="TEST-01",
        )
        assert p.host_ips == ["10.0.0.1", "10.0.0.2"]
        assert p.host_role == "GENESIS"
        assert p.testnet_name == "TEST-01"
        assert p.elasticsearch_url == "https://logs.example.com/es/api"

    def test_defaults(self):
        p = TelegrafProvisioner(
            host_ips=["10.0.0.1"],
            ssh_key_path="~/.ssh/id_rsa",
            testnet_name="DEV-01",
            elasticsearch_url="https://example.com",
            elasticsearch_username="user",
            elasticsearch_password="pass",
        )
        assert p.host_role == "regular"
        assert p.branch_commit == "unknown"
        assert p.node_version == "unknown"


class TestTelegrafProvisionerExecute:
    @patch("saorsa_deploy.provisioning.telegraf.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf.add_op")
    @patch("saorsa_deploy.provisioning.telegraf.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf.State")
    @patch("saorsa_deploy.provisioning.telegraf.Inventory")
    def test_execute_adds_six_operations(
        self,
        _mock_inventory,
        mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        p = _make_provisioner()
        p.execute()

        # install + grant access + write env + write config + write unit + enable/start + verify = 7
        assert mock_add_op.call_count == 7

    @patch("saorsa_deploy.provisioning.telegraf.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf.add_op")
    @patch("saorsa_deploy.provisioning.telegraf.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf.State")
    @patch("saorsa_deploy.provisioning.telegraf.Inventory")
    def test_execute_all_ops_are_server_shell(
        self,
        _mock_inventory,
        mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        p = _make_provisioner()
        p.execute()

        op_types = [call[0][1] for call in mock_add_op.call_args_list]
        assert all(op is server.shell for op in op_types)

    @patch("saorsa_deploy.provisioning.telegraf.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf.add_op")
    @patch("saorsa_deploy.provisioning.telegraf.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf.State")
    @patch("saorsa_deploy.provisioning.telegraf.Inventory")
    def test_execute_disconnects_on_error(
        self,
        _mock_inventory,
        _mock_state,
        _mock_connect,
        _mock_add_op,
        mock_run_ops,
        mock_disconnect,
    ):
        mock_run_ops.side_effect = RuntimeError("connection failed")

        p = _make_provisioner()
        with pytest.raises(RuntimeError, match="connection failed"):
            p.execute()

        mock_disconnect.assert_called_once()

    @patch("saorsa_deploy.provisioning.telegraf.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf.add_op")
    @patch("saorsa_deploy.provisioning.telegraf.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf.State")
    @patch("saorsa_deploy.provisioning.telegraf.Inventory")
    def test_execute_raises_on_failed_hosts(
        self,
        _mock_inventory,
        mock_state,
        _mock_connect,
        _mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        failed_host = MagicMock()
        failed_host.name = "10.0.0.1"
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = {failed_host}
        mock_state.return_value = mock_state_instance

        p = _make_provisioner()
        with pytest.raises(RuntimeError, match="1 host\\(s\\) failed Telegraf provisioning"):
            p.execute()

    @patch("saorsa_deploy.provisioning.telegraf.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf.add_op")
    @patch("saorsa_deploy.provisioning.telegraf.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf.State")
    @patch("saorsa_deploy.provisioning.telegraf.Inventory")
    def test_execute_creates_inventory_with_all_hosts(
        self,
        mock_inventory,
        mock_state,
        _mock_connect,
        _mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        p = _make_provisioner(host_ips=["10.0.0.1", "10.0.0.2"])
        p.execute()

        inventory_call = mock_inventory.call_args
        hosts_data = inventory_call[0][0][0]
        assert len(hosts_data) == 2
        assert hosts_data[0][0] == "10.0.0.1"
        assert hosts_data[0][1]["ssh_user"] == "root"
        assert hosts_data[1][0] == "10.0.0.2"

    @patch("saorsa_deploy.provisioning.telegraf.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf.add_op")
    @patch("saorsa_deploy.provisioning.telegraf.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf.State")
    @patch("saorsa_deploy.provisioning.telegraf.Inventory")
    def test_execute_writes_config_to_correct_path(
        self,
        _mock_inventory,
        mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        p = _make_provisioner()
        p.execute()

        # 4th add_op call (index 3) is "Write Telegraf configuration"
        config_call = mock_add_op.call_args_list[3]
        commands = config_call.kwargs.get("commands", [])
        assert any(TELEGRAF_CONFIG_PATH in cmd for cmd in commands)
        assert any(f"mkdir -p {TELEGRAF_CONFIG_DIR}" in cmd for cmd in commands)

    @patch("saorsa_deploy.provisioning.telegraf.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf.add_op")
    @patch("saorsa_deploy.provisioning.telegraf.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf.State")
    @patch("saorsa_deploy.provisioning.telegraf.Inventory")
    def test_execute_writes_unit_to_correct_path(
        self,
        _mock_inventory,
        mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        p = _make_provisioner()
        p.execute()

        # 5th add_op call (index 4) is "Write Telegraf systemd unit"
        unit_call = mock_add_op.call_args_list[4]
        commands = unit_call.kwargs.get("commands", [])
        assert any(TELEGRAF_UNIT_PATH in cmd for cmd in commands)

    @patch("saorsa_deploy.provisioning.telegraf.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf.add_op")
    @patch("saorsa_deploy.provisioning.telegraf.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf.State")
    @patch("saorsa_deploy.provisioning.telegraf.Inventory")
    def test_execute_enables_and_starts_service(
        self,
        _mock_inventory,
        mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        p = _make_provisioner()
        p.execute()

        # 6th add_op call (index 5) is "Enable and start"
        enable_call = mock_add_op.call_args_list[5]
        commands = enable_call.kwargs.get("commands", [])
        assert "systemctl daemon-reload" in commands
        assert f"systemctl enable {TELEGRAF_SERVICE_NAME}" in commands
        assert f"systemctl restart {TELEGRAF_SERVICE_NAME}" in commands

    @patch("saorsa_deploy.provisioning.telegraf.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf.add_op")
    @patch("saorsa_deploy.provisioning.telegraf.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf.State")
    @patch("saorsa_deploy.provisioning.telegraf.Inventory")
    def test_execute_verifies_service_is_active(
        self,
        _mock_inventory,
        mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        p = _make_provisioner()
        p.execute()

        # 7th add_op call (index 6) is "Verify Telegraf service is active"
        verify_call = mock_add_op.call_args_list[6]
        commands = verify_call.kwargs.get("commands", [])
        assert any(
            f"systemctl is-active --quiet {TELEGRAF_SERVICE_NAME}" in cmd for cmd in commands
        )
        assert any(f"test -f {TELEGRAF_CONFIG_PATH}" in cmd for cmd in commands)
        assert any(f"test -f {TELEGRAF_ENV_PATH}" in cmd for cmd in commands)


class TestCmdProvisionTelegraf:
    @patch("saorsa_deploy.cmd.provision_telegraf.TelegrafProvisioner")
    @patch("saorsa_deploy.cmd.provision_telegraf.clear_known_hosts")
    @patch("saorsa_deploy.cmd.provision_telegraf.load_deployment_state")
    def test_provisions_genesis_and_regular_vms(
        self,
        mock_load_state,
        mock_clear_known_hosts,
        mock_provisioner_cls,
    ):
        from saorsa_deploy.cmd.provision_telegraf import cmd_provision_telegraf

        mock_load_state.return_value = {
            "bootstrap_ip": "10.0.0.100",
            "vm_ips": {
                "lon1": ["10.0.0.1", "10.0.0.2"],
                "nyc1": ["10.0.0.3"],
            },
        }
        mock_provisioner_cls.return_value.execute.return_value = None

        args = SimpleNamespace(
            name="DEV-01",
            ssh_key_path="~/.ssh/id_rsa",
            elasticsearch_url="https://logs.example.com/es/api",
            elasticsearch_username="user",
            elasticsearch_password="pass",
            node_version=None,
            branch_commit="abc123",
            repo_owner="maidsafe",
        )
        cmd_provision_telegraf(args)

        assert mock_provisioner_cls.call_count == 2

        # First call: genesis
        genesis_call = mock_provisioner_cls.call_args_list[0]
        assert genesis_call.kwargs["host_ips"] == ["10.0.0.100"]
        assert genesis_call.kwargs["host_role"] == "GENESIS"

        # Second call: regular VMs
        regular_call = mock_provisioner_cls.call_args_list[1]
        assert sorted(regular_call.kwargs["host_ips"]) == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
        assert regular_call.kwargs["host_role"] == "WAN_NODE"

    @patch("saorsa_deploy.cmd.provision_telegraf.TelegrafProvisioner")
    @patch("saorsa_deploy.cmd.provision_telegraf.clear_known_hosts")
    @patch("saorsa_deploy.cmd.provision_telegraf.load_deployment_state")
    def test_clears_known_hosts_before_provisioning(
        self,
        mock_load_state,
        mock_clear_known_hosts,
        mock_provisioner_cls,
    ):
        from saorsa_deploy.cmd.provision_telegraf import cmd_provision_telegraf

        mock_load_state.return_value = {
            "bootstrap_ip": "10.0.0.100",
            "vm_ips": {"lon1": ["10.0.0.1"]},
        }
        call_order = []
        mock_clear_known_hosts.side_effect = lambda *a, **kw: call_order.append(
            "clear_known_hosts"
        )
        mock_provisioner_cls.return_value.execute.side_effect = lambda: call_order.append(
            "execute"
        )

        args = SimpleNamespace(
            name="DEV-01",
            ssh_key_path="~/.ssh/id_rsa",
            elasticsearch_url="https://logs.example.com/es/api",
            elasticsearch_username="user",
            elasticsearch_password="pass",
            node_version=None,
            branch_commit=None,
            repo_owner="maidsafe",
        )
        cmd_provision_telegraf(args)

        assert call_order[0] == "clear_known_hosts"
