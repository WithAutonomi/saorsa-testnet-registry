from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.provisioning.client import (
    ANT_INSTALL_PATH,
    CLIENT_DATA_DIR,
    SCRIPTS_DIR,
)
from saorsa_deploy.provisioning.client_clean import (
    DOWNLOADER_UNIT,
    ENV_FILE,
    UPLOADER_UNIT,
    ClientCleanProvisioner,
)


def _make_provisioner(**overrides):
    defaults = {
        "host_ips": ["10.0.0.1"],
        "ssh_key_path": "~/.ssh/id_rsa",
    }
    defaults.update(overrides)
    return ClientCleanProvisioner(**defaults)


class TestClientCleanProvisionerExecute:
    @patch("saorsa_deploy.provisioning.client_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.client_clean.run_ops")
    @patch("saorsa_deploy.provisioning.client_clean.add_op")
    @patch("saorsa_deploy.provisioning.client_clean.connect_all")
    @patch("saorsa_deploy.provisioning.client_clean.State")
    @patch("saorsa_deploy.provisioning.client_clean.Inventory")
    def test_adds_three_operations(
        self, _mi, mock_state, _mc, mock_add_op, _mr, _md,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        _make_provisioner().execute()

        assert mock_add_op.call_count == 3

    @patch("saorsa_deploy.provisioning.client_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.client_clean.run_ops")
    @patch("saorsa_deploy.provisioning.client_clean.add_op")
    @patch("saorsa_deploy.provisioning.client_clean.connect_all")
    @patch("saorsa_deploy.provisioning.client_clean.State")
    @patch("saorsa_deploy.provisioning.client_clean.Inventory")
    def test_stop_and_disable_commands_are_idempotent(
        self, _mi, mock_state, _mc, mock_add_op, _mr, _md,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        _make_provisioner().execute()

        stop_call = mock_add_op.call_args_list[0]
        cmds = " ".join(stop_call.kwargs.get("commands", []))
        assert "systemctl stop ant-uploader" in cmds
        assert "systemctl stop ant-downloader" in cmds
        assert "systemctl disable ant-uploader" in cmds
        assert "systemctl disable ant-downloader" in cmds
        assert cmds.count("|| true") >= 4

    @patch("saorsa_deploy.provisioning.client_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.client_clean.run_ops")
    @patch("saorsa_deploy.provisioning.client_clean.add_op")
    @patch("saorsa_deploy.provisioning.client_clean.connect_all")
    @patch("saorsa_deploy.provisioning.client_clean.State")
    @patch("saorsa_deploy.provisioning.client_clean.Inventory")
    def test_removes_units_scripts_env_data_and_binary(
        self, _mi, mock_state, _mc, mock_add_op, _mr, _md,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        mock_state.return_value = mock_state_instance

        _make_provisioner().execute()

        cmds = " ".join(mock_add_op.call_args_list[1].kwargs.get("commands", []))
        assert f"rm -f {UPLOADER_UNIT}" in cmds
        assert f"rm -f {DOWNLOADER_UNIT}" in cmds
        assert "systemctl daemon-reload" in cmds
        assert f"rm -rf {SCRIPTS_DIR}" in cmds
        assert f"rm -f {ENV_FILE}" in cmds
        assert f"rm -rf {CLIENT_DATA_DIR}" in cmds
        assert f"rm -f {ANT_INSTALL_PATH}" in cmds

    @patch("saorsa_deploy.provisioning.client_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.client_clean.run_ops")
    @patch("saorsa_deploy.provisioning.client_clean.add_op")
    @patch("saorsa_deploy.provisioning.client_clean.connect_all")
    @patch("saorsa_deploy.provisioning.client_clean.State")
    @patch("saorsa_deploy.provisioning.client_clean.Inventory")
    def test_does_not_touch_node_or_bootstrap_services(
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
        assert "ant-node" not in all_cmds
        assert "ant-bootstrap" not in all_cmds

    @patch("saorsa_deploy.provisioning.client_clean.disconnect_all")
    @patch("saorsa_deploy.provisioning.client_clean.run_ops")
    @patch("saorsa_deploy.provisioning.client_clean.add_op")
    @patch("saorsa_deploy.provisioning.client_clean.connect_all")
    @patch("saorsa_deploy.provisioning.client_clean.State")
    @patch("saorsa_deploy.provisioning.client_clean.Inventory")
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


class TestCmdCleanClients:
    @patch("saorsa_deploy.cmd.clean_clients.ClientCleanProvisioner")
    @patch("saorsa_deploy.cmd.clean_clients.clear_known_hosts")
    @patch("saorsa_deploy.cmd.clean_clients.load_client_state")
    def test_name_mode_uses_clients_from_state(
        self, mock_load_state, _mock_clear, mock_provisioner_cls,
    ):
        from saorsa_deploy.cmd.clean_clients import cmd_clean_clients

        mock_load_state.return_value = {
            "clients": [
                {"ip": "10.0.0.1", "client_index": 1, "region": "lon1"},
                {"ip": "10.0.0.2", "client_index": 2, "region": "nyc1"},
            ],
        }
        mock_provisioner_cls.return_value.execute.return_value = None

        args = SimpleNamespace(
            name="DEV-01",
            host_ips_file=None,
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="root",
        )
        cmd_clean_clients(args)

        call = mock_provisioner_cls.call_args_list[0]
        assert sorted(call.kwargs["host_ips"]) == ["10.0.0.1", "10.0.0.2"]

    @patch("saorsa_deploy.cmd.clean_clients.ClientCleanProvisioner")
    @patch("saorsa_deploy.cmd.clean_clients.clear_known_hosts")
    def test_host_ips_file_mode(
        self, _mock_clear, mock_provisioner_cls, tmp_path,
    ):
        from saorsa_deploy.cmd.clean_clients import cmd_clean_clients

        ip_file = tmp_path / "hosts.txt"
        ip_file.write_text("10.0.0.1\n")
        mock_provisioner_cls.return_value.execute.return_value = None

        args = SimpleNamespace(
            name=None,
            host_ips_file=str(ip_file),
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="ubuntu",
        )
        cmd_clean_clients(args)

        call = mock_provisioner_cls.call_args_list[0]
        assert call.kwargs["host_ips"] == ["10.0.0.1"]
        assert call.kwargs["ssh_user"] == "ubuntu"

    def test_exits_when_no_mode_selected(self):
        from saorsa_deploy.cmd.clean_clients import cmd_clean_clients

        args = SimpleNamespace(
            name=None,
            host_ips_file=None,
            ssh_key_path="~/.ssh/id_rsa",
            ssh_user="root",
        )
        with pytest.raises(SystemExit):
            cmd_clean_clients(args)
