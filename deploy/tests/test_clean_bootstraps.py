from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.provisioning.bootstrap import BINARY_INSTALL_PATH
from saorsa_deploy.provisioning.bootstrap_clean import (
    DATA_GLOB,
    LOG_GLOB,
    UNIT_GLOB,
    BootstrapCleanProvisioner,
)


def _make_provisioner(**overrides):
    defaults = {
        "host_ips": ["10.0.0.1"],
        "ssh_key_path": "~/.ssh/id_rsa",
    }
    defaults.update(overrides)
    return BootstrapCleanProvisioner(**defaults)


class TestBootstrapCleanProvisionerInit:
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


class TestBootstrapCleanProvisionerExecute:
    @patch("saorsa_deploy.provisioning.bootstrap_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.run_ops")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.add_op")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.connect_all")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.State")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.Inventory")
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

        _make_provisioner().execute()

        assert mock_add_op.call_count == 3

    @patch("saorsa_deploy.provisioning.bootstrap_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.run_ops")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.add_op")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.connect_all")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.State")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.Inventory")
    def test_stop_and_disable_commands_are_idempotent(
        self,
        _mi,
        mock_state,
        _mc,
        mock_add_op,
        _mr,
        _md,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        _make_provisioner().execute()

        stop_call = mock_add_op.call_args_list[0]
        cmds = " ".join(stop_call.kwargs.get("commands", []))
        assert "systemctl stop" in cmds
        assert "systemctl disable" in cmds
        assert cmds.count("|| true") >= 2

    @patch("saorsa_deploy.provisioning.bootstrap_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.run_ops")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.add_op")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.connect_all")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.State")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.Inventory")
    def test_removes_unit_data_log_and_binary(
        self,
        _mi,
        mock_state,
        _mc,
        mock_add_op,
        _mr,
        _md,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        _make_provisioner().execute()

        remove_call = mock_add_op.call_args_list[1]
        cmds = " ".join(remove_call.kwargs.get("commands", []))
        assert f"rm -f {UNIT_GLOB}" in cmds
        assert "systemctl daemon-reload" in cmds
        assert f"rm -rf {DATA_GLOB}" in cmds
        assert f"rm -rf {LOG_GLOB}" in cmds
        assert f"rm -f {BINARY_INSTALL_PATH}" in cmds

    @patch("saorsa_deploy.provisioning.bootstrap_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.run_ops")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.add_op")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.connect_all")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.State")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.Inventory")
    def test_does_not_touch_ant_node_services(
        self,
        _mi,
        mock_state,
        _mc,
        mock_add_op,
        _mr,
        _md,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        _make_provisioner().execute()

        all_cmds = " ".join(
            " ".join(c.kwargs.get("commands", [])) for c in mock_add_op.call_args_list
        )
        # Must not manage ant-node-*.service systemd units (only ant-bootstrap-*).
        assert "ant-node-*.service" not in all_cmds
        assert "systemctl stop ant-node" not in all_cmds
        assert "systemctl disable ant-node" not in all_cmds
        assert "/etc/systemd/system/ant-node-" not in all_cmds

    @patch("saorsa_deploy.provisioning.bootstrap_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.run_ops")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.add_op")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.connect_all")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.State")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.Inventory")
    def test_does_not_unmount_volume_or_edit_fstab(
        self,
        _mi,
        mock_state,
        _mc,
        mock_add_op,
        _mr,
        _md,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        _make_provisioner().execute()

        all_cmds = " ".join(
            " ".join(c.kwargs.get("commands", [])) for c in mock_add_op.call_args_list
        )
        assert "umount" not in all_cmds
        assert "/etc/fstab" not in all_cmds

    @patch("saorsa_deploy.provisioning.bootstrap_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.run_ops")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.add_op")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.connect_all")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.State")
    @patch("saorsa_deploy.provisioning.bootstrap_clean.Inventory")
    def test_raises_on_failed_hosts(
        self,
        _mi,
        mock_state,
        _mc,
        _ma,
        _mr,
        _md,
    ):
        failed_host = MagicMock()
        failed_host.name = "10.0.0.1"
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = {failed_host}
        mock_state.return_value = mock_state_instance

        with pytest.raises(RuntimeError, match="1 host\\(s\\) failed"):
            _make_provisioner().execute()


class TestCmdCleanBootstraps:
    @patch("saorsa_deploy.cmd.clean_bootstraps.BootstrapCleanProvisioner")
    @patch("saorsa_deploy.cmd.clean_bootstraps.clear_known_hosts")
    @patch("saorsa_deploy.cmd.clean_bootstraps.load_deployment_state")
    def test_name_mode_uses_all_bootstrap_nodes_including_genesis(
        self,
        mock_load_state,
        _mock_clear,
        mock_provisioner_cls,
    ):
        from saorsa_deploy.cmd.clean_bootstraps import cmd_clean_bootstraps

        mock_load_state.return_value = {
            "bootstrap_nodes": [
                {"ip": "10.0.0.1", "bootstrap_index": 1, "region": "lon1"},
                {"ip": "10.0.0.2", "bootstrap_index": 2, "region": "nyc1"},
                {"ip": "10.0.0.3", "bootstrap_index": 3, "region": "syd1"},
            ],
        }
        mock_provisioner_cls.return_value.execute.return_value = None

        args = SimpleNamespace(
            name="DEV-01",
            host_ips_file=None,
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="root",
        )
        cmd_clean_bootstraps(args)

        assert mock_provisioner_cls.call_count == 1
        call = mock_provisioner_cls.call_args_list[0]
        assert sorted(call.kwargs["host_ips"]) == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]

    @patch("saorsa_deploy.cmd.clean_bootstraps.BootstrapCleanProvisioner")
    @patch("saorsa_deploy.cmd.clean_bootstraps.clear_known_hosts")
    @patch("saorsa_deploy.cmd.clean_bootstraps.load_deployment_state")
    def test_name_mode_falls_back_to_bootstrap_ip(
        self,
        mock_load_state,
        _mock_clear,
        mock_provisioner_cls,
    ):
        from saorsa_deploy.cmd.clean_bootstraps import cmd_clean_bootstraps

        mock_load_state.return_value = {
            "bootstrap_ip": "10.0.0.99",
        }
        mock_provisioner_cls.return_value.execute.return_value = None

        args = SimpleNamespace(
            name="DEV-01",
            host_ips_file=None,
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="root",
        )
        cmd_clean_bootstraps(args)

        call = mock_provisioner_cls.call_args_list[0]
        assert call.kwargs["host_ips"] == ["10.0.0.99"]

    @patch("saorsa_deploy.cmd.clean_bootstraps.BootstrapCleanProvisioner")
    @patch("saorsa_deploy.cmd.clean_bootstraps.clear_known_hosts")
    def test_host_ips_file_mode(
        self,
        _mock_clear,
        mock_provisioner_cls,
        tmp_path,
    ):
        from saorsa_deploy.cmd.clean_bootstraps import cmd_clean_bootstraps

        ip_file = tmp_path / "hosts.txt"
        ip_file.write_text("10.0.0.1\n10.0.0.2\n")
        mock_provisioner_cls.return_value.execute.return_value = None

        args = SimpleNamespace(
            name=None,
            host_ips_file=str(ip_file),
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="ubuntu",
        )
        cmd_clean_bootstraps(args)

        assert mock_provisioner_cls.call_count == 1
        call = mock_provisioner_cls.call_args_list[0]
        assert call.kwargs["host_ips"] == ["10.0.0.1", "10.0.0.2"]
        assert call.kwargs["ssh_user"] == "ubuntu"

    def test_exits_when_no_mode_selected(self):
        from saorsa_deploy.cmd.clean_bootstraps import cmd_clean_bootstraps

        args = SimpleNamespace(
            name=None,
            host_ips_file=None,
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="root",
        )
        with pytest.raises(SystemExit):
            cmd_clean_bootstraps(args)
