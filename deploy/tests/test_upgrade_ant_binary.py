from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.cmd.upgrade_ant_binary import _validate_kind, cmd_upgrade_ant_binary
from saorsa_deploy.provisioning.prod_downloader import (
    ANT_INSTALL_PATH,
    build_ant_binary_upgrade_command,
)


def _args(**overrides):
    defaults = {
        "name": "PROD-DL-01",
        "branch_name": None,
        "repo_owner": None,
        "ant_version": None,
        "ssh_key_path": "~/.ssh/id_rsa",
        "restart": True,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestBuildAntBinaryUpgradeCommand:
    def test_archive_forces_overwrite(self):
        cmd = build_ant_binary_upgrade_command("https://example.com/ant.tar.gz")
        assert f"rm -f {ANT_INSTALL_PATH}" in cmd
        assert "test -f" not in cmd
        assert "wget -q https://example.com/ant.tar.gz" in cmd
        assert "ANT_BINARY:UPGRADED" in cmd

    def test_raw_binary_forces_overwrite(self):
        cmd = build_ant_binary_upgrade_command(
            "https://example.com/ant", binary_is_archive=False
        )
        assert f"rm -f {ANT_INSTALL_PATH}" in cmd
        assert "test -f" not in cmd
        assert f"wget -q https://example.com/ant -O {ANT_INSTALL_PATH}" in cmd


class TestValidateKind:
    @patch("saorsa_deploy.cmd.upgrade_ant_binary.get_repository", return_value=None)
    def test_proceeds_when_database_unset(self, _repo):
        _validate_kind("PROD-DL-01", console=MagicMock())

    def test_proceeds_when_deployment_not_found(self):
        repo = MagicMock()
        repo.get_active_by_name.return_value = None
        repo.get_by_name.return_value = None
        with patch(
            "saorsa_deploy.cmd.upgrade_ant_binary.get_repository", return_value=repo
        ):
            _validate_kind("PROD-DL-01", console=MagicMock())

    @pytest.mark.parametrize("kind", ["prod-downloaders", "prod-continuous-downloader"])
    def test_accepts_valid_kinds(self, kind):
        repo = MagicMock()
        repo.get_active_by_name.return_value = SimpleNamespace(deploy_args={"kind": kind})
        with patch(
            "saorsa_deploy.cmd.upgrade_ant_binary.get_repository", return_value=repo
        ):
            _validate_kind("PROD-DL-01", console=MagicMock())

    def test_rejects_wrong_kind(self):
        repo = MagicMock()
        repo.get_active_by_name.return_value = SimpleNamespace(
            deploy_args={"kind": "prod-uploaders"}
        )
        with patch(
            "saorsa_deploy.cmd.upgrade_ant_binary.get_repository", return_value=repo
        ):
            with pytest.raises(SystemExit):
                _validate_kind("PROD-DL-01", console=MagicMock())


@pytest.fixture
def upgrade_patches():
    with (
        patch("saorsa_deploy.cmd.upgrade_ant_binary._validate_kind") as p_validate,
        patch(
            "saorsa_deploy.cmd.upgrade_ant_binary.load_client_state",
            return_value={"clients": [{"ip": "10.0.0.1"}]},
        ) as p_state,
        patch(
            "saorsa_deploy.cmd.upgrade_ant_binary._resolve_ant_binary",
            return_value=("https://example.com/ant.tar.gz", True),
        ) as p_resolve,
        patch("saorsa_deploy.cmd.upgrade_ant_binary.clear_known_hosts") as p_clear,
        patch("saorsa_deploy.cmd.upgrade_ant_binary.Inventory") as p_inv,
        patch("saorsa_deploy.cmd.upgrade_ant_binary.State") as p_state_cls,
        patch("saorsa_deploy.cmd.upgrade_ant_binary.connect_all") as p_connect,
        patch("saorsa_deploy.cmd.upgrade_ant_binary.disconnect_all") as p_disconnect,
        patch("saorsa_deploy.cmd.upgrade_ant_binary.add_op") as p_add_op,
        patch("saorsa_deploy.cmd.upgrade_ant_binary.run_ops") as p_run_ops,
        patch("saorsa_deploy.cmd.upgrade_ant_binary.record_event") as p_event,
        patch(
            "saorsa_deploy.cmd.upgrade_ant_binary.record_deploy_args_update"
        ) as p_deploy_args,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        p_state_cls.return_value = mock_state_instance

        yield SimpleNamespace(
            validate=p_validate,
            load_state=p_state,
            resolve_binary=p_resolve,
            clear=p_clear,
            inventory=p_inv,
            state_cls=p_state_cls,
            state_instance=mock_state_instance,
            connect=p_connect,
            disconnect=p_disconnect,
            add_op=p_add_op,
            run_ops=p_run_ops,
            event=p_event,
            deploy_args=p_deploy_args,
        )


class TestCmdUpgradeAntBinary:
    def test_restarts_by_default(self, upgrade_patches):
        cmd_upgrade_ant_binary(_args())

        cmds = " ".join(upgrade_patches.add_op.call_args.kwargs.get("commands", []))
        assert "systemctl restart ant-prod-downloader" in cmds
        assert f"rm -f {ANT_INSTALL_PATH}" in cmds

    def test_no_restart_flag_skips_restart(self, upgrade_patches):
        cmd_upgrade_ant_binary(_args(restart=False))

        cmds = " ".join(upgrade_patches.add_op.call_args.kwargs.get("commands", []))
        assert "systemctl restart" not in cmds

    def test_resolves_latest_when_binary_url_is_none(self, upgrade_patches):
        upgrade_patches.resolve_binary.return_value = (None, True)
        with patch(
            "saorsa_deploy.binary_source.get_ant_release_url",
            return_value="https://example.com/latest.tar.gz",
        ) as p_latest:
            cmd_upgrade_ant_binary(_args())

        p_latest.assert_called_once()
        cmds = " ".join(upgrade_patches.add_op.call_args.kwargs.get("commands", []))
        assert "https://example.com/latest.tar.gz" in cmds

    def test_no_client_ips_exits(self, upgrade_patches):
        upgrade_patches.load_state.return_value = {"clients": []}

        with pytest.raises(SystemExit):
            cmd_upgrade_ant_binary(_args())

        upgrade_patches.add_op.assert_not_called()

    def test_records_completed_event_and_deploy_args_on_success(self, upgrade_patches):
        cmd_upgrade_ant_binary(_args())

        event_types = [c.args[1] for c in upgrade_patches.event.call_args_list]
        assert "ant_binary_upgrade_started" in event_types
        assert "ant_binary_upgrade_completed" in event_types
        upgrade_patches.deploy_args.assert_called_once()

    def test_failed_hosts_exit_and_skip_success_bookkeeping(self, upgrade_patches):
        upgrade_patches.state_instance.failed_hosts = {MagicMock(name="10.0.0.1")}

        with pytest.raises(SystemExit):
            cmd_upgrade_ant_binary(_args())

        event_types = [c.args[1] for c in upgrade_patches.event.call_args_list]
        assert "ant_binary_upgrade_failed" in event_types
        assert "ant_binary_upgrade_completed" not in event_types
        upgrade_patches.deploy_args.assert_not_called()
