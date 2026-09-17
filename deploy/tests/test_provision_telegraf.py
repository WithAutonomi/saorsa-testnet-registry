import fnmatch
import tomllib
from unittest.mock import MagicMock, patch

import pytest
from pyinfra.operations import server

from saorsa_deploy.provisioning.telegraf import (
    INFLUX_TRAFFIC_TARGET_PATTERNS,
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
        assert "ANT_TESTNET_NAME=DEV-01" in env
        assert "ANT_TESTNET_NAME_LOWER=dev-01" in env
        assert "ANT_HOST_ROLE=GENESIS" in env

    def test_contains_version_info(self):
        p = _make_provisioner(branch_commit="abc123", node_version="0.1.0")
        env = p._build_env_file()
        assert "ANT_BRANCH_COMMIT=abc123" in env
        assert "ANT_NODE_VERSION=0.1.0" in env

    def test_contains_log_dir(self):
        p = _make_provisioner()
        env = p._build_env_file()
        assert "ANT_LOG_DIR=" in env

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

    def test_promotes_node_build_fields_to_tags(self):
        # ant-node stamps `node_version` / `node_commit` on every JSON log
        # line; they must land as tags (`tag.node_version.keyword`) rather than
        # as plain fields, and beat the static `[global_tags]` fallback.
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        p = _make_provisioner()
        parsed = tomllib.loads(p._build_telegraf_config())
        tag_keys = parsed["inputs"]["tail"][0]["tag_keys"]
        assert "level" in tag_keys
        assert "target" in tag_keys
        assert "node_version" in tag_keys
        assert "node_commit" in tag_keys
        # The provisioning-time value stays as a fallback for pre-stamp binaries.
        assert parsed["global_tags"]["node_version"] == "${ANT_NODE_VERSION}"

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
        assert "${ANT_TESTNET_NAME}" in config
        assert "${ANT_LOG_DIR}" in config
        assert "${TELEGRAF_ELASTICSEARCH_URL}" in config

    def test_tail_input_glob_matches_log_files(self):
        p = _make_provisioner()
        config = p._build_telegraf_config()
        assert "ant-*/ant-node*.log*" in config

    def test_starlark_source_present(self):
        p = _make_provisioner()
        config = p._build_telegraf_config()
        assert "def apply(metric):" in config
        assert "service_number" in config

    def test_starlark_handles_ant_bootstrap(self):
        p = _make_provisioner()
        config = p._build_telegraf_config()
        assert 'part == "ant-bootstrap"' in config
        assert '"service_number"] = "1"' in config

    def test_from_beginning_true_by_default(self):
        p = _make_provisioner()
        config = p._build_telegraf_config()
        assert "from_beginning = true" in config
        assert "from_beginning = false" not in config

    def test_from_beginning_false_when_opt_out(self):
        p = _make_provisioner(from_beginning=False)
        config = p._build_telegraf_config()
        assert "from_beginning = false" in config
        assert "from_beginning = true" not in config

    def test_from_beginning_placeholder_fully_substituted(self):
        # Guards against regressions where the __FROM_BEGINNING__ sentinel
        # leaks through if the substitution is ever removed.
        p = _make_provisioner(from_beginning=True)
        assert "__FROM_BEGINNING__" not in p._build_telegraf_config()

    def test_tagdrop_placeholder_fully_substituted(self):
        p = _make_provisioner()
        assert "__TAGDROP_BLOCK__" not in p._build_telegraf_config()

    def test_default_drops_only_debug_and_trace(self):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        p = _make_provisioner()
        config = p._build_telegraf_config()
        parsed = tomllib.loads(config)
        tagdrop = parsed["inputs"]["tail"][0]["tagdrop"]
        # INFO is kept by default — post-hoc investigation needs it.
        assert set(tagdrop["level"]) == {"DEBUG", "TRACE"}

    def test_default_buffer_limits(self):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        p = _make_provisioner()
        parsed = tomllib.loads(p._build_telegraf_config())
        assert parsed["agent"]["metric_buffer_limit"] == 2_000_000
        assert parsed["inputs"]["tail"][0]["max_undelivered_lines"] == 500_000

    def test_custom_buffer_limit_scales_tail_default(self):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        p = _make_provisioner(metric_buffer_limit=400_000)
        parsed = tomllib.loads(p._build_telegraf_config())
        assert parsed["agent"]["metric_buffer_limit"] == 400_000
        # tail buffer defaults to 1/4 of output buffer
        assert parsed["inputs"]["tail"][0]["max_undelivered_lines"] == 100_000

    def test_explicit_max_undelivered_lines_wins(self):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        p = _make_provisioner(
            metric_buffer_limit=1_000_000,
            max_undelivered_lines=50_000,
        )
        parsed = tomllib.loads(p._build_telegraf_config())
        assert parsed["inputs"]["tail"][0]["max_undelivered_lines"] == 50_000

    def test_rejects_nonpositive_buffer_limit(self):
        with pytest.raises(ValueError, match="metric_buffer_limit"):
            _make_provisioner(metric_buffer_limit=0)

    def test_custom_drop_levels(self):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        p = _make_provisioner(drop_log_levels=["DEBUG"])
        config = p._build_telegraf_config()
        parsed = tomllib.loads(config)
        tagdrop = parsed["inputs"]["tail"][0]["tagdrop"]
        assert tagdrop["level"] == ["DEBUG"]

    def test_empty_drop_levels_omits_tagdrop(self):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        p = _make_provisioner(drop_log_levels=[])
        config = p._build_telegraf_config()
        parsed = tomllib.loads(config)
        # No tagdrop key at all when the caller opts out explicitly
        assert "tagdrop" not in parsed["inputs"]["tail"][0]


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
        assert p.ssh_user == "root"
        assert p.from_beginning is True

    def test_custom_ssh_user(self):
        p = _make_provisioner(ssh_user="ubuntu")
        assert p.ssh_user == "ubuntu"

    def test_ant_log_dir_defaults_to_production(self):
        from saorsa_deploy.provisioning.volume import LOG_DIR
        p = _make_provisioner()
        assert p.ant_log_dir == LOG_DIR

    def test_ant_log_dir_can_be_overridden(self):
        p = _make_provisioner(ant_log_dir="/mnt/ant-node-storage/logs")
        assert p.ant_log_dir == "/mnt/ant-node-storage/logs"

    def test_env_file_uses_custom_ant_log_dir(self):
        p = _make_provisioner(ant_log_dir="/mnt/ant-node-storage/logs")
        env = p._build_env_file()
        assert "ANT_LOG_DIR=/mnt/ant-node-storage/logs" in env


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
    def test_execute_uses_custom_ssh_user_in_inventory(
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

        p = _make_provisioner(host_ips=["10.0.0.1"], ssh_user="ubuntu")
        p.execute()

        inventory_call = mock_inventory.call_args
        hosts_data = inventory_call[0][0][0]
        assert hosts_data[0][1]["ssh_user"] == "ubuntu"

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

        # 6th add_op call (index 5) is "Enable and start". The three systemctl
        # calls are collapsed into one command (one SSH round-trip per host).
        enable_call = mock_add_op.call_args_list[5]
        commands = enable_call.kwargs.get("commands", [])
        assert len(commands) == 1
        script = commands[0]
        assert "systemctl daemon-reload" in script
        assert f"systemctl enable {TELEGRAF_SERVICE_NAME}" in script
        assert f"systemctl restart {TELEGRAF_SERVICE_NAME}" in script

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


class TestReadInventory:
    def test_reads_node_vms(self, tmp_path):
        import json

        from saorsa_deploy.cmd.provision_telegraf import read_inventory

        inv = tmp_path / "inventory.json"
        inv.write_text(json.dumps({
            "node_vms": [
                {"ip": "10.0.0.1", "ssh_user": "ubuntu", "ssh_key": "~/.ssh/key"},
                {"ip": "10.0.0.2", "ssh_user": "root", "ssh_key": "~/.ssh/id_rsa"},
            ]
        }))
        entries = read_inventory(str(inv))
        assert len(entries) == 2
        assert entries[0] == {
            "ip": "10.0.0.1",
            "ssh_user": "ubuntu",
            "ssh_key": "~/.ssh/key",
            "role": "WAN_NODE",
        }
        assert entries[1] == {
            "ip": "10.0.0.2",
            "ssh_user": "root",
            "ssh_key": "~/.ssh/id_rsa",
            "role": "WAN_NODE",
        }

    def test_defaults_ssh_user_and_key(self, tmp_path):
        import json

        from saorsa_deploy.cmd.provision_telegraf import read_inventory

        inv = tmp_path / "inventory.json"
        inv.write_text(json.dumps({"node_vms": [{"ip": "10.0.0.1"}]}))
        entries = read_inventory(str(inv))
        assert entries[0]["ssh_user"] == "root"
        assert entries[0]["ssh_key"] == "~/.ssh/id_rsa"
        assert entries[0]["role"] == "WAN_NODE"

    def test_reads_bootstrap_vms_with_role(self, tmp_path):
        import json

        from saorsa_deploy.cmd.provision_telegraf import read_inventory

        inv = tmp_path / "inventory.json"
        inv.write_text(json.dumps({
            "bootstrap_vms": [
                {
                    "name": "t-bs-1",
                    "ip": "10.0.0.100",
                    "bootstrap_index": 1,
                    "ssh_user": "root",
                    "ssh_key": "~/.ssh/id_rsa",
                },
                {
                    "name": "t-bs-2",
                    "ip": "10.0.0.101",
                    "bootstrap_index": 2,
                    "ssh_user": "root",
                    "ssh_key": "~/.ssh/id_rsa",
                },
            ],
            "node_vms": [],
        }))
        entries = read_inventory(str(inv))
        assert [(e["ip"], e["role"], e["name"]) for e in entries] == [
            ("10.0.0.100", "GENESIS", "t-bs-1"),
            ("10.0.0.101", "BOOTSTRAP", "t-bs-2"),
        ]

    def test_reads_bootstrap_and_node_vms(self, tmp_path):
        import json

        from saorsa_deploy.cmd.provision_telegraf import read_inventory

        inv = tmp_path / "inventory.json"
        inv.write_text(json.dumps({
            "bootstrap_vms": [
                {"ip": "10.0.0.100", "bootstrap_index": 1},
                {"ip": "10.0.0.101", "bootstrap_index": 2},
            ],
            "node_vms": [
                {"ip": "10.0.1.1"},
                {"ip": "10.0.1.2"},
            ],
        }))
        entries = read_inventory(str(inv))
        assert [e["role"] for e in entries] == [
            "GENESIS",
            "BOOTSTRAP",
            "WAN_NODE",
            "WAN_NODE",
        ]

    def test_raises_on_empty_inventory(self, tmp_path):
        import json

        from saorsa_deploy.cmd.provision_telegraf import read_inventory

        inv = tmp_path / "inventory.json"
        inv.write_text(json.dumps({"node_vms": [], "bootstrap_vms": []}))
        with pytest.raises(ValueError, match="No bootstrap_vms or node_vms"):
            read_inventory(str(inv))

    def test_raises_on_missing_file(self):
        from saorsa_deploy.cmd.provision_telegraf import read_inventory

        with pytest.raises(OSError):
            read_inventory("/nonexistent/inventory.json")


class TestGroupByRoleAndCredentials:
    def test_groups_by_role_and_credentials(self):
        from saorsa_deploy.cmd.provision_telegraf import (
            group_by_role_and_credentials,
        )

        entries = [
            {
                "ip": "10.0.0.1",
                "ssh_user": "root",
                "ssh_key": "~/.ssh/id_rsa",
                "role": "GENESIS",
            },
            {
                "ip": "10.0.0.2",
                "ssh_user": "root",
                "ssh_key": "~/.ssh/id_rsa",
                "role": "BOOTSTRAP",
            },
            {
                "ip": "10.0.0.3",
                "ssh_user": "root",
                "ssh_key": "~/.ssh/id_rsa",
                "role": "WAN_NODE",
            },
            {
                "ip": "10.0.0.4",
                "ssh_user": "root",
                "ssh_key": "~/.ssh/id_rsa",
                "role": "WAN_NODE",
            },
        ]
        groups = group_by_role_and_credentials(entries)
        assert groups[("GENESIS", "root", "~/.ssh/id_rsa")] == ["10.0.0.1"]
        assert groups[("BOOTSTRAP", "root", "~/.ssh/id_rsa")] == ["10.0.0.2"]
        assert groups[("WAN_NODE", "root", "~/.ssh/id_rsa")] == [
            "10.0.0.3",
            "10.0.0.4",
        ]


class TestGroupByCredentials:
    def test_groups_by_ssh_user_and_key(self):
        from saorsa_deploy.cmd.provision_telegraf import group_by_credentials

        entries = [
            {"ip": "10.0.0.1", "ssh_user": "ubuntu", "ssh_key": "~/.ssh/key"},
            {"ip": "10.0.0.2", "ssh_user": "root", "ssh_key": "~/.ssh/id_rsa"},
            {"ip": "10.0.0.3", "ssh_user": "ubuntu", "ssh_key": "~/.ssh/key"},
        ]
        groups = group_by_credentials(entries)
        assert groups[("ubuntu", "~/.ssh/key")] == ["10.0.0.1", "10.0.0.3"]
        assert groups[("root", "~/.ssh/id_rsa")] == ["10.0.0.2"]

    def test_single_group(self):
        from saorsa_deploy.cmd.provision_telegraf import group_by_credentials

        entries = [
            {"ip": "10.0.0.1", "ssh_user": "root", "ssh_key": "~/.ssh/id_rsa"},
            {"ip": "10.0.0.2", "ssh_user": "root", "ssh_key": "~/.ssh/id_rsa"},
        ]
        groups = group_by_credentials(entries)
        assert len(groups) == 1
        assert groups[("root", "~/.ssh/id_rsa")] == ["10.0.0.1", "10.0.0.2"]


class TestReadIpsFromFile:
    def test_reads_ips_from_file(self, tmp_path):
        from saorsa_deploy.cmd.provision_telegraf import read_ips_from_file

        ip_file = tmp_path / "hosts.txt"
        ip_file.write_text("10.0.0.1\n10.0.0.2\n10.0.0.3\n")
        ips = read_ips_from_file(str(ip_file))
        assert ips == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]

    def test_ignores_blank_lines(self, tmp_path):
        from saorsa_deploy.cmd.provision_telegraf import read_ips_from_file

        ip_file = tmp_path / "hosts.txt"
        ip_file.write_text("10.0.0.1\n\n10.0.0.2\n\n")
        ips = read_ips_from_file(str(ip_file))
        assert ips == ["10.0.0.1", "10.0.0.2"]

    def test_ignores_comments(self, tmp_path):
        from saorsa_deploy.cmd.provision_telegraf import read_ips_from_file

        ip_file = tmp_path / "hosts.txt"
        ip_file.write_text("# This is a comment\n10.0.0.1\n# Another\n10.0.0.2\n")
        ips = read_ips_from_file(str(ip_file))
        assert ips == ["10.0.0.1", "10.0.0.2"]

    def test_strips_whitespace(self, tmp_path):
        from saorsa_deploy.cmd.provision_telegraf import read_ips_from_file

        ip_file = tmp_path / "hosts.txt"
        ip_file.write_text("  10.0.0.1  \n  10.0.0.2  \n")
        ips = read_ips_from_file(str(ip_file))
        assert ips == ["10.0.0.1", "10.0.0.2"]

    def test_raises_on_empty_file(self, tmp_path):
        from saorsa_deploy.cmd.provision_telegraf import read_ips_from_file

        ip_file = tmp_path / "hosts.txt"
        ip_file.write_text("\n\n# only comments\n")
        with pytest.raises(ValueError, match="No IP addresses found"):
            read_ips_from_file(str(ip_file))

    def test_raises_on_missing_file(self):
        from saorsa_deploy.cmd.provision_telegraf import read_ips_from_file

        with pytest.raises(OSError):
            read_ips_from_file("/nonexistent/hosts.txt")


class TestInfluxTrafficOutput:
    """The ES forwarder's optional second output: traffic-accounting summaries
    to InfluxDB (V2-1225)."""

    _INFLUX = dict(
        influxdb_url="http://mon:8086",
        influxdb_token="s3cr3t-influx-token",
        influxdb_org="ant",
        influxdb_bucket="testnet-metrics",
    )

    def test_off_by_default_and_byte_identical(self):
        plain = _make_provisioner()
        assert not plain.influx_output_enabled
        config = plain._build_telegraf_config()
        assert "influxdb" not in config.lower()
        assert "INFLUXDB" not in plain._build_env_file()
        # No placeholder may leak when the block is empty.
        assert "__INFLUX_OUTPUT_BLOCK__" not in config

    def test_partial_credentials_rejected(self):
        with pytest.raises(ValueError, match="together"):
            _make_provisioner(influxdb_url="http://mon:8086")

    def test_env_file_gains_the_four_influx_vars(self):
        prov = _make_provisioner(**self._INFLUX)
        env = dict(
            line.split("=", 1) for line in prov._build_env_file().splitlines()
        )
        assert env["INFLUXDB_URL"] == "http://mon:8086"
        assert env["INFLUXDB_TOKEN"] == "s3cr3t-influx-token"
        assert env["INFLUXDB_ORG"] == "ant"
        assert env["INFLUXDB_BUCKET"] == "testnet-metrics"

    def test_output_block_reads_env_and_is_valid_toml(self):
        prov = _make_provisioner(**self._INFLUX)
        config = prov._build_telegraf_config()
        parsed = tomllib.loads(config)
        (out,) = parsed["outputs"]["influxdb_v2"]
        assert out["urls"] == ["${INFLUXDB_URL}"]
        assert out["token"] == "${INFLUXDB_TOKEN}"
        assert out["organization"] == "${INFLUXDB_ORG}"
        assert out["bucket"] == "${INFLUXDB_BUCKET}"
        # The ES output is untouched -- the new output is purely additive.
        assert len(parsed["outputs"]["elasticsearch"]) == 1
        assert "s3cr3t" not in config, "credentials go through the env file only"

    def test_output_is_scoped_to_tail_and_traffic_targets(self):
        """Without namepass + tagpass EVERY parsed log line would hit Influx."""
        prov = _make_provisioner(**self._INFLUX)
        (out,) = tomllib.loads(prov._build_telegraf_config())["outputs"]["influxdb_v2"]
        assert out["namepass"] == ["tail"]
        assert out["tagpass"]["target"] == list(INFLUX_TRAFFIC_TARGET_PATTERNS)

    @pytest.mark.parametrize(
        "target",
        [
            # Every target emitting a cumulative summary on the shipped build
            # (verified against ant-prod-01's Elasticsearch, 2026-09-17).
            "saorsa_transport::traffic",
            "saorsa_core::traffic",
            "ant_node::replication::traffic",
            "saorsa_transport::relay::traffic",
            "saorsa_transport::masque::relay_server",
            # The targets V2-834 (ant-node#230) adds -- must pass with no
            # deploy change.
            "ant_node::payment::traffic",
            "ant_node::storage::traffic",
            "ant_node::upgrade::traffic",
        ],
    )
    def test_tagpass_admits_every_traffic_target(self, target):
        assert _telegraf_tagpass_matches(target)

    @pytest.mark.parametrize(
        "target",
        ["ant_node", "ant_node::node", "saorsa_transport::masque", "saorsa_core::traffic_shaper"],
    )
    def test_tagpass_rejects_ordinary_targets(self, target):
        assert not _telegraf_tagpass_matches(target)


def _telegraf_tagpass_matches(target: str) -> bool:
    """Model telegraf's tagpass matching: each value is a glob compiled with NO
    separator (gobwas/glob), so ``*`` spans ``::``. ``fnmatch`` has the same
    semantics for ``*`` and literal text."""
    return any(fnmatch.fnmatchcase(target, p) for p in INFLUX_TRAFFIC_TARGET_PATTERNS)
