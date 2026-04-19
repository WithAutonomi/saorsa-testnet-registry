from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.provisioning.monitoring_clean import MonitoringCleanProvisioner
from saorsa_deploy.provisioning.telegraf_metrics import (
    TELEGRAF_CONFIG_DIR,
    TELEGRAF_ENV_PATH,
    TELEGRAF_SERVICE_NAME,
    TELEGRAF_UNIT_PATH,
)


def _make_provisioner(**overrides):
    defaults = {
        "host_ips": ["10.0.0.1"],
        "ssh_key_path": "~/.ssh/id_rsa",
    }
    defaults.update(overrides)
    return MonitoringCleanProvisioner(**defaults)


class TestMonitoringCleanProvisionerInit:
    def test_stores_params(self):
        p = _make_provisioner(
            host_ips=["1.1.1.1", "2.2.2.2"],
            ssh_user="ubuntu",
        )
        assert p.host_ips == ["1.1.1.1", "2.2.2.2"]
        assert p.ssh_user == "ubuntu"

    def test_default_ssh_user_is_root(self):
        p = _make_provisioner()
        assert p.ssh_user == "root"


class TestMonitoringCleanProvisionerExecute:
    @patch("saorsa_deploy.provisioning.monitoring_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.monitoring_clean.run_ops")
    @patch("saorsa_deploy.provisioning.monitoring_clean.add_op")
    @patch("saorsa_deploy.provisioning.monitoring_clean.connect_all")
    @patch("saorsa_deploy.provisioning.monitoring_clean.State")
    @patch("saorsa_deploy.provisioning.monitoring_clean.Inventory")
    def test_adds_three_operations(
        self, _mi, mock_state, _mc, mock_add_op, _mr, _md,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        _make_provisioner().execute()

        assert mock_add_op.call_count == 3

    @patch("saorsa_deploy.provisioning.monitoring_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.monitoring_clean.run_ops")
    @patch("saorsa_deploy.provisioning.monitoring_clean.add_op")
    @patch("saorsa_deploy.provisioning.monitoring_clean.connect_all")
    @patch("saorsa_deploy.provisioning.monitoring_clean.State")
    @patch("saorsa_deploy.provisioning.monitoring_clean.Inventory")
    def test_stop_and_disable_commands_are_idempotent(
        self, _mi, mock_state, _mc, mock_add_op, _mr, _md,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        _make_provisioner().execute()

        stop_call = mock_add_op.call_args_list[0]
        cmds = " ".join(stop_call.kwargs.get("commands", []))
        assert f"systemctl stop {TELEGRAF_SERVICE_NAME}" in cmds
        assert f"systemctl disable {TELEGRAF_SERVICE_NAME}" in cmds
        assert cmds.count("|| true") >= 2

    @patch("saorsa_deploy.provisioning.monitoring_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.monitoring_clean.run_ops")
    @patch("saorsa_deploy.provisioning.monitoring_clean.add_op")
    @patch("saorsa_deploy.provisioning.monitoring_clean.connect_all")
    @patch("saorsa_deploy.provisioning.monitoring_clean.State")
    @patch("saorsa_deploy.provisioning.monitoring_clean.Inventory")
    def test_removes_unit_config_and_env_files(
        self, _mi, mock_state, _mc, mock_add_op, _mr, _md,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        _make_provisioner().execute()

        cmds = " ".join(mock_add_op.call_args_list[1].kwargs.get("commands", []))
        assert f"rm -f {TELEGRAF_UNIT_PATH}" in cmds
        assert f"rm -rf {TELEGRAF_CONFIG_DIR}" in cmds
        assert f"rm -f {TELEGRAF_ENV_PATH}" in cmds
        assert "systemctl daemon-reload" in cmds

    @patch("saorsa_deploy.provisioning.monitoring_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.monitoring_clean.run_ops")
    @patch("saorsa_deploy.provisioning.monitoring_clean.add_op")
    @patch("saorsa_deploy.provisioning.monitoring_clean.connect_all")
    @patch("saorsa_deploy.provisioning.monitoring_clean.State")
    @patch("saorsa_deploy.provisioning.monitoring_clean.Inventory")
    def test_does_not_touch_log_forwarder_or_ant_node(
        self, _mi, mock_state, _mc, mock_add_op, _mr, _md,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        _make_provisioner().execute()

        all_cmds = " ".join(
            " ".join(c.kwargs.get("commands", []))
            for c in mock_add_op.call_args_list
        )
        assert "telegraf-ant-elasticsearch" not in all_cmds
        assert "telegraf-elasticsearch" not in all_cmds
        # Must not touch ant-node systemd services or binary
        assert "systemctl stop ant-node" not in all_cmds
        assert "systemctl disable ant-node" not in all_cmds
        assert "/usr/local/bin/ant-node" not in all_cmds

    @patch("saorsa_deploy.provisioning.monitoring_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.monitoring_clean.run_ops")
    @patch("saorsa_deploy.provisioning.monitoring_clean.add_op")
    @patch("saorsa_deploy.provisioning.monitoring_clean.connect_all")
    @patch("saorsa_deploy.provisioning.monitoring_clean.State")
    @patch("saorsa_deploy.provisioning.monitoring_clean.Inventory")
    def test_does_not_remove_telegraf_deb_or_touch_influxdb(
        self, _mi, mock_state, _mc, mock_add_op, _mr, _md,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        _make_provisioner().execute()

        all_cmds = " ".join(
            " ".join(c.kwargs.get("commands", []))
            for c in mock_add_op.call_args_list
        )
        assert "apt-get remove" not in all_cmds
        assert "apt-get purge" not in all_cmds
        assert "dpkg -r" not in all_cmds
        assert "dpkg --remove" not in all_cmds
        # Must not touch the monitoring VM / InfluxDB / Grafana
        assert "influxdb" not in all_cmds
        assert "grafana" not in all_cmds

    @patch("saorsa_deploy.provisioning.monitoring_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.monitoring_clean.run_ops")
    @patch("saorsa_deploy.provisioning.monitoring_clean.add_op")
    @patch("saorsa_deploy.provisioning.monitoring_clean.connect_all")
    @patch("saorsa_deploy.provisioning.monitoring_clean.State")
    @patch("saorsa_deploy.provisioning.monitoring_clean.Inventory")
    def test_raises_on_failed_hosts(
        self, _mi, mock_state, _mc, _ma, _mr, _md,
    ):
        failed_host = MagicMock()
        failed_host.name = "10.0.0.1"
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = {failed_host}
        mock_state.return_value = mock_state_instance

        with pytest.raises(RuntimeError, match="1 host\\(s\\) failed"):
            _make_provisioner().execute()


class TestCmdCleanMonitoring:
    @patch("saorsa_deploy.cmd.clean_monitoring.MonitoringCleanProvisioner")
    @patch("saorsa_deploy.cmd.clean_monitoring.clear_known_hosts")
    def test_inventory_path_groups_by_credentials(
        self, _mock_clear, mock_provisioner_cls, tmp_path,
    ):
        import json

        from saorsa_deploy.cmd.clean_monitoring import cmd_clean_monitoring

        inv = tmp_path / "inventory.json"
        inv.write_text(json.dumps({
            "node_vms": [
                {"ip": "10.0.0.1", "ssh_user": "ubuntu", "ssh_key": "~/.ssh/k"},
                {"ip": "10.0.0.2", "ssh_user": "root", "ssh_key": "~/.ssh/id"},
                {"ip": "10.0.0.3", "ssh_user": "ubuntu", "ssh_key": "~/.ssh/k"},
            ]
        }))
        mock_provisioner_cls.return_value.execute.return_value = None

        args = SimpleNamespace(
            name=None,
            inventory_path=str(inv),
            host_ips_file=None,
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="root",
        )
        cmd_clean_monitoring(args)

        assert mock_provisioner_cls.call_count == 2
        calls = mock_provisioner_cls.call_args_list
        ubuntu_call = next(c for c in calls if c.kwargs["ssh_user"] == "ubuntu")
        assert sorted(ubuntu_call.kwargs["host_ips"]) == ["10.0.0.1", "10.0.0.3"]
        assert ubuntu_call.kwargs["ssh_key_path"] == "~/.ssh/k"

    @patch("saorsa_deploy.cmd.clean_monitoring.MonitoringCleanProvisioner")
    @patch("saorsa_deploy.cmd.clean_monitoring.clear_known_hosts")
    def test_host_ips_file_mode(
        self, _mock_clear, mock_provisioner_cls, tmp_path,
    ):
        from saorsa_deploy.cmd.clean_monitoring import cmd_clean_monitoring

        ip_file = tmp_path / "hosts.txt"
        ip_file.write_text("10.0.0.1\n10.0.0.2\n")
        mock_provisioner_cls.return_value.execute.return_value = None

        args = SimpleNamespace(
            name=None,
            inventory_path=None,
            host_ips_file=str(ip_file),
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="ubuntu",
        )
        cmd_clean_monitoring(args)

        assert mock_provisioner_cls.call_count == 1
        call = mock_provisioner_cls.call_args_list[0]
        assert call.kwargs["host_ips"] == ["10.0.0.1", "10.0.0.2"]
        assert call.kwargs["ssh_user"] == "ubuntu"

    @patch("saorsa_deploy.cmd.clean_monitoring.MonitoringCleanProvisioner")
    @patch("saorsa_deploy.cmd.clean_monitoring.clear_known_hosts")
    @patch("saorsa_deploy.cmd.clean_monitoring.load_deployment_state")
    def test_name_mode_uses_bootstrap_plus_regulars(
        self, mock_load_state, _mock_clear, mock_provisioner_cls,
    ):
        from saorsa_deploy.cmd.clean_monitoring import cmd_clean_monitoring

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
            inventory_path=None,
            host_ips_file=None,
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="root",
        )
        cmd_clean_monitoring(args)

        assert mock_provisioner_cls.call_count == 1
        call = mock_provisioner_cls.call_args_list[0]
        assert sorted(call.kwargs["host_ips"]) == sorted(
            ["10.0.0.100", "10.0.0.1", "10.0.0.2", "10.0.0.3"]
        )

    def test_exits_when_no_mode_selected(self):
        from saorsa_deploy.cmd.clean_monitoring import cmd_clean_monitoring

        args = SimpleNamespace(
            name=None,
            inventory_path=None,
            host_ips_file=None,
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="root",
        )
        with pytest.raises(SystemExit):
            cmd_clean_monitoring(args)
