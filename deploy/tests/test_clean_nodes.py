from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.provisioning.bootstrap import BINARY_INSTALL_PATH
from saorsa_deploy.provisioning.node_clean import (
    DATA_GLOB,
    LOG_GLOB,
    UNIT_GLOB,
    NodeCleanProvisioner,
)


def _make_provisioner(**overrides):
    defaults = {
        "host_ips": ["10.0.0.1"],
        "ssh_key_path": "~/.ssh/id_rsa",
    }
    defaults.update(overrides)
    return NodeCleanProvisioner(**defaults)


class TestNodeCleanProvisionerExecute:
    @patch("saorsa_deploy.provisioning.node_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.node_clean.run_ops")
    @patch("saorsa_deploy.provisioning.node_clean.add_op")
    @patch("saorsa_deploy.provisioning.node_clean.connect_all")
    @patch("saorsa_deploy.provisioning.node_clean.State")
    @patch("saorsa_deploy.provisioning.node_clean.Inventory")
    def test_adds_three_operations(
        self, _mi, mock_state, _mc, mock_add_op, _mr, _md,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        _make_provisioner().execute()

        assert mock_add_op.call_count == 3

    @patch("saorsa_deploy.provisioning.node_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.node_clean.run_ops")
    @patch("saorsa_deploy.provisioning.node_clean.add_op")
    @patch("saorsa_deploy.provisioning.node_clean.connect_all")
    @patch("saorsa_deploy.provisioning.node_clean.State")
    @patch("saorsa_deploy.provisioning.node_clean.Inventory")
    def test_stop_and_disable_commands_are_idempotent(
        self, _mi, mock_state, _mc, mock_add_op, _mr, _md,
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

    @patch("saorsa_deploy.provisioning.node_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.node_clean.run_ops")
    @patch("saorsa_deploy.provisioning.node_clean.add_op")
    @patch("saorsa_deploy.provisioning.node_clean.connect_all")
    @patch("saorsa_deploy.provisioning.node_clean.State")
    @patch("saorsa_deploy.provisioning.node_clean.Inventory")
    def test_removes_unit_data_log_and_binary(
        self, _mi, mock_state, _mc, mock_add_op, _mr, _md,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        _make_provisioner().execute()

        cmds = " ".join(mock_add_op.call_args_list[1].kwargs.get("commands", []))
        assert f"rm -f {UNIT_GLOB}" in cmds
        assert "systemctl daemon-reload" in cmds
        assert f"rm -rf {DATA_GLOB}" in cmds
        assert f"rm -rf {LOG_GLOB}" in cmds
        assert f"rm -f {BINARY_INSTALL_PATH}" in cmds

    @patch("saorsa_deploy.provisioning.node_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.node_clean.run_ops")
    @patch("saorsa_deploy.provisioning.node_clean.add_op")
    @patch("saorsa_deploy.provisioning.node_clean.connect_all")
    @patch("saorsa_deploy.provisioning.node_clean.State")
    @patch("saorsa_deploy.provisioning.node_clean.Inventory")
    def test_does_not_touch_bootstrap_services(
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
        assert "ant-bootstrap" not in all_cmds

    @patch("saorsa_deploy.provisioning.node_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.node_clean.run_ops")
    @patch("saorsa_deploy.provisioning.node_clean.add_op")
    @patch("saorsa_deploy.provisioning.node_clean.connect_all")
    @patch("saorsa_deploy.provisioning.node_clean.State")
    @patch("saorsa_deploy.provisioning.node_clean.Inventory")
    def test_does_not_unmount_volume_or_edit_fstab(
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
        assert "umount" not in all_cmds
        assert "/etc/fstab" not in all_cmds

    @patch("saorsa_deploy.provisioning.node_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.node_clean.run_ops")
    @patch("saorsa_deploy.provisioning.node_clean.add_op")
    @patch("saorsa_deploy.provisioning.node_clean.connect_all")
    @patch("saorsa_deploy.provisioning.node_clean.State")
    @patch("saorsa_deploy.provisioning.node_clean.Inventory")
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


class TestCmdCleanNodes:
    @patch("saorsa_deploy.cmd.clean_nodes.NodeCleanProvisioner")
    @patch("saorsa_deploy.cmd.clean_nodes.clear_known_hosts")
    @patch("saorsa_deploy.cmd.clean_nodes.load_deployment_state")
    def test_name_mode_merges_all_regions(
        self, mock_load_state, _mock_clear, mock_provisioner_cls,
    ):
        from saorsa_deploy.cmd.clean_nodes import cmd_clean_nodes

        mock_load_state.return_value = {
            "vm_ips": {
                "lon1": ["10.0.0.1", "10.0.0.2"],
                "nyc1": ["10.0.0.3"],
            },
        }
        mock_provisioner_cls.return_value.execute.return_value = None

        args = SimpleNamespace(
            name="DEV-01",
            region=None,
            host_ips_file=None,
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="root",
        )
        cmd_clean_nodes(args)

        call = mock_provisioner_cls.call_args_list[0]
        assert sorted(call.kwargs["host_ips"]) == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]

    @patch("saorsa_deploy.cmd.clean_nodes.NodeCleanProvisioner")
    @patch("saorsa_deploy.cmd.clean_nodes.clear_known_hosts")
    @patch("saorsa_deploy.cmd.clean_nodes.load_deployment_state")
    def test_region_filter(
        self, mock_load_state, _mock_clear, mock_provisioner_cls,
    ):
        from saorsa_deploy.cmd.clean_nodes import cmd_clean_nodes

        mock_load_state.return_value = {
            "vm_ips": {
                "lon1": ["10.0.0.1", "10.0.0.2"],
                "nyc1": ["10.0.0.3"],
            },
        }
        mock_provisioner_cls.return_value.execute.return_value = None

        args = SimpleNamespace(
            name="DEV-01",
            region="lon1",
            host_ips_file=None,
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="root",
        )
        cmd_clean_nodes(args)

        call = mock_provisioner_cls.call_args_list[0]
        assert sorted(call.kwargs["host_ips"]) == ["10.0.0.1", "10.0.0.2"]

    @patch("saorsa_deploy.cmd.clean_nodes.load_deployment_state")
    def test_region_filter_unknown_region_exits(self, mock_load_state):
        from saorsa_deploy.cmd.clean_nodes import cmd_clean_nodes

        mock_load_state.return_value = {
            "vm_ips": {"lon1": ["10.0.0.1"]},
        }

        args = SimpleNamespace(
            name="DEV-01",
            region="xxx",
            host_ips_file=None,
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="root",
        )
        with pytest.raises(SystemExit):
            cmd_clean_nodes(args)

    @patch("saorsa_deploy.cmd.clean_nodes.NodeCleanProvisioner")
    @patch("saorsa_deploy.cmd.clean_nodes.clear_known_hosts")
    def test_host_ips_file_mode(
        self, _mock_clear, mock_provisioner_cls, tmp_path,
    ):
        from saorsa_deploy.cmd.clean_nodes import cmd_clean_nodes

        ip_file = tmp_path / "hosts.txt"
        ip_file.write_text("10.0.0.1\n10.0.0.2\n")
        mock_provisioner_cls.return_value.execute.return_value = None

        args = SimpleNamespace(
            name=None,
            region=None,
            host_ips_file=str(ip_file),
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="root",
        )
        cmd_clean_nodes(args)

        assert mock_provisioner_cls.call_count == 1

    def test_exits_when_no_mode_selected(self):
        from saorsa_deploy.cmd.clean_nodes import cmd_clean_nodes

        args = SimpleNamespace(
            name=None,
            region=None,
            host_ips_file=None,
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="root",
        )
        with pytest.raises(SystemExit):
            cmd_clean_nodes(args)
