from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.provisioning.telegraf import (
    TELEGRAF_CONFIG_DIR,
    TELEGRAF_ENV_PATH,
    TELEGRAF_SERVICE_NAME,
    TELEGRAF_UNIT_PATH,
)
from saorsa_deploy.provisioning.telegraf_clean import TelegrafCleanProvisioner


def _make_clean_provisioner(**overrides):
    defaults = {
        "host_ips": ["10.0.0.1"],
        "ssh_key_path": "~/.ssh/id_rsa",
    }
    defaults.update(overrides)
    return TelegrafCleanProvisioner(**defaults)


class TestCleanProvisionerInit:
    def test_stores_params(self):
        p = _make_clean_provisioner(
            host_ips=["1.1.1.1", "2.2.2.2"],
            ssh_user="ubuntu",
        )
        assert p.host_ips == ["1.1.1.1", "2.2.2.2"]
        assert p.ssh_user == "ubuntu"

    def test_default_ssh_user_is_root(self):
        p = _make_clean_provisioner()
        assert p.ssh_user == "root"


class TestCleanProvisionerExecute:
    @patch("saorsa_deploy.provisioning.telegraf_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf_clean.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf_clean.add_op")
    @patch("saorsa_deploy.provisioning.telegraf_clean.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf_clean.State")
    @patch("saorsa_deploy.provisioning.telegraf_clean.Inventory")
    def test_adds_three_operations(
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

        p = _make_clean_provisioner()
        p.execute()

        # stop+disable, remove files, verify = 3
        assert mock_add_op.call_count == 3

    @patch("saorsa_deploy.provisioning.telegraf_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf_clean.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf_clean.add_op")
    @patch("saorsa_deploy.provisioning.telegraf_clean.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf_clean.State")
    @patch("saorsa_deploy.provisioning.telegraf_clean.Inventory")
    def test_stop_and_disable_commands_are_idempotent(
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

        p = _make_clean_provisioner()
        p.execute()

        stop_call = mock_add_op.call_args_list[0]
        commands = stop_call.kwargs.get("commands", [])
        stop_cmd = " ".join(commands)
        assert f"systemctl stop {TELEGRAF_SERVICE_NAME}" in stop_cmd
        assert f"systemctl disable {TELEGRAF_SERVICE_NAME}" in stop_cmd
        # Both must be tolerant of the service not being present.
        assert stop_cmd.count("|| true") >= 2

    @patch("saorsa_deploy.provisioning.telegraf_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf_clean.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf_clean.add_op")
    @patch("saorsa_deploy.provisioning.telegraf_clean.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf_clean.State")
    @patch("saorsa_deploy.provisioning.telegraf_clean.Inventory")
    def test_removes_unit_config_and_env_files(
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

        p = _make_clean_provisioner()
        p.execute()

        remove_call = mock_add_op.call_args_list[1]
        commands = remove_call.kwargs.get("commands", [])
        removed = " ".join(commands)
        assert f"rm -f {TELEGRAF_UNIT_PATH}" in removed
        assert f"rm -rf {TELEGRAF_CONFIG_DIR}" in removed
        assert f"rm -f {TELEGRAF_ENV_PATH}" in removed
        assert "systemctl daemon-reload" in removed

    @patch("saorsa_deploy.provisioning.telegraf_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf_clean.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf_clean.add_op")
    @patch("saorsa_deploy.provisioning.telegraf_clean.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf_clean.State")
    @patch("saorsa_deploy.provisioning.telegraf_clean.Inventory")
    def test_does_not_touch_telegraf_metrics_service(
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

        p = _make_clean_provisioner()
        p.execute()

        all_cmds = " ".join(
            " ".join(call.kwargs.get("commands", []))
            for call in mock_add_op.call_args_list
        )
        assert "telegraf-ant-metrics" not in all_cmds
        assert "telegraf-metrics" not in all_cmds

    @patch("saorsa_deploy.provisioning.telegraf_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf_clean.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf_clean.add_op")
    @patch("saorsa_deploy.provisioning.telegraf_clean.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf_clean.State")
    @patch("saorsa_deploy.provisioning.telegraf_clean.Inventory")
    def test_does_not_remove_telegraf_deb(
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

        p = _make_clean_provisioner()
        p.execute()

        all_cmds = " ".join(
            " ".join(call.kwargs.get("commands", []))
            for call in mock_add_op.call_args_list
        )
        assert "apt-get remove" not in all_cmds
        assert "apt remove" not in all_cmds
        assert "dpkg -r" not in all_cmds
        assert "dpkg --remove" not in all_cmds

    @patch("saorsa_deploy.provisioning.telegraf_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf_clean.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf_clean.add_op")
    @patch("saorsa_deploy.provisioning.telegraf_clean.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf_clean.State")
    @patch("saorsa_deploy.provisioning.telegraf_clean.Inventory")
    def test_does_not_touch_ant_node(
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

        p = _make_clean_provisioner()
        p.execute()

        all_cmds = " ".join(
            " ".join(call.kwargs.get("commands", []))
            for call in mock_add_op.call_args_list
        )
        assert "ant-node" not in all_cmds

    @patch("saorsa_deploy.provisioning.telegraf_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf_clean.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf_clean.add_op")
    @patch("saorsa_deploy.provisioning.telegraf_clean.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf_clean.State")
    @patch("saorsa_deploy.provisioning.telegraf_clean.Inventory")
    def test_raises_on_failed_hosts(
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

        p = _make_clean_provisioner()
        with pytest.raises(RuntimeError, match="1 host\\(s\\) failed"):
            p.execute()


class TestCmdCleanElastic:
    @patch("saorsa_deploy.cmd.clean_elastic.TelegrafCleanProvisioner")
    @patch("saorsa_deploy.cmd.clean_elastic.clear_known_hosts")
    def test_inventory_path_groups_by_credentials(
        self,
        mock_clear,
        mock_provisioner_cls,
        tmp_path,
    ):
        import json

        from saorsa_deploy.cmd.clean_elastic import cmd_clean_elastic

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
        cmd_clean_elastic(args)

        # One provisioner per credential group
        assert mock_provisioner_cls.call_count == 2
        calls = mock_provisioner_cls.call_args_list
        ubuntu_call = next(
            c for c in calls if c.kwargs["ssh_user"] == "ubuntu"
        )
        assert sorted(ubuntu_call.kwargs["host_ips"]) == ["10.0.0.1", "10.0.0.3"]
        assert ubuntu_call.kwargs["ssh_key_path"] == "~/.ssh/k"

    @patch("saorsa_deploy.cmd.clean_elastic.TelegrafCleanProvisioner")
    @patch("saorsa_deploy.cmd.clean_elastic.clear_known_hosts")
    def test_host_ips_file_mode(
        self,
        mock_clear,
        mock_provisioner_cls,
        tmp_path,
    ):
        from saorsa_deploy.cmd.clean_elastic import cmd_clean_elastic

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
        cmd_clean_elastic(args)

        assert mock_provisioner_cls.call_count == 1
        call_kwargs = mock_provisioner_cls.call_args_list[0].kwargs
        assert call_kwargs["host_ips"] == ["10.0.0.1", "10.0.0.2"]
        assert call_kwargs["ssh_user"] == "ubuntu"

    @patch("saorsa_deploy.cmd.clean_elastic.TelegrafCleanProvisioner")
    @patch("saorsa_deploy.cmd.clean_elastic.clear_known_hosts")
    @patch("saorsa_deploy.cmd.clean_elastic.load_deployment_state")
    def test_name_mode_uses_bootstrap_plus_regulars(
        self,
        mock_load_state,
        mock_clear,
        mock_provisioner_cls,
    ):
        from saorsa_deploy.cmd.clean_elastic import cmd_clean_elastic

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
        cmd_clean_elastic(args)

        assert mock_provisioner_cls.call_count == 1
        call_kwargs = mock_provisioner_cls.call_args_list[0].kwargs
        assert sorted(call_kwargs["host_ips"]) == sorted(
            ["10.0.0.100", "10.0.0.1", "10.0.0.2", "10.0.0.3"]
        )

    def test_exits_when_no_mode_selected(self):
        from saorsa_deploy.cmd.clean_elastic import cmd_clean_elastic

        args = SimpleNamespace(
            name=None,
            inventory_path=None,
            host_ips_file=None,
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="root",
        )
        with pytest.raises(SystemExit):
            cmd_clean_elastic(args)
