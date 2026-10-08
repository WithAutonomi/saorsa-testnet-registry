from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.cmd.upgrade_ant_binary import (
    _validate_kind,
    cmd_upgrade_ant_binary,
    group_hosts_by_unit,
)
from saorsa_deploy.provisioning.prod_continuous_uploader import (
    UPLOADER_SERVICE,
    VERIFIER_SERVICE,
)
from saorsa_deploy.provisioning.prod_downloader import (
    ANT_INSTALL_PATH,
    build_ant_binary_upgrade_command,
)

DOWNLOADER_SERVICE = "ant-prod-downloader"


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
        cmd = build_ant_binary_upgrade_command("https://example.com/ant", binary_is_archive=False)
        assert f"rm -f {ANT_INSTALL_PATH}" in cmd
        assert "test -f" not in cmd
        assert f"wget -q https://example.com/ant -O {ANT_INSTALL_PATH}" in cmd


class TestGroupHostsByUnit:
    """The grouping is where a restart could be aimed at a unit that does not
    exist on the host it is sent to, so it is pure and tested on its own."""

    @pytest.mark.parametrize("kind", ["prod-downloaders", "prod-continuous-downloader"])
    def test_downloader_kinds_are_one_group(self, kind):
        groups = group_hosts_by_unit(kind, [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}])

        assert len(groups) == 1
        assert groups[0].service == DOWNLOADER_SERVICE
        assert groups[0].host_ips == ["10.0.0.1", "10.0.0.2"]

    def test_unknown_kind_falls_back_to_the_downloader_unit(self):
        # No registry, so no kind: the command has always proceeded on client
        # state alone, and three of the four prod deployments are downloaders.
        groups = group_hosts_by_unit("", [{"ip": "10.0.0.1"}])

        assert [(g.service, g.host_ips) for g in groups] == [(DOWNLOADER_SERVICE, ["10.0.0.1"])]

    def test_native_uploader_splits_into_two_groups(self):
        groups = group_hosts_by_unit(
            "prod-continuous-uploader",
            [
                {"ip": "10.0.0.1", "role": "upload"},
                {"ip": "10.0.0.2", "role": "download"},
                {"ip": "10.0.0.3", "role": "download"},
            ],
        )

        assert [(g.label, g.service, g.host_ips) for g in groups] == [
            ("uploader", UPLOADER_SERVICE, ["10.0.0.1"]),
            ("verifier", VERIFIER_SERVICE, ["10.0.0.2", "10.0.0.3"]),
        ]

    def test_wasm_uploader_yields_the_verifier_group_only(self):
        groups = group_hosts_by_unit(
            "prod-continuous-uploader",
            [
                {"ip": "10.0.0.1", "role": "upload"},
                {"ip": "10.0.0.2", "role": "download"},
            ],
            client_kind="wasm",
        )

        assert [(g.service, g.host_ips) for g in groups] == [(VERIFIER_SERVICE, ["10.0.0.2"])]

    def test_missing_role_reads_as_the_uploader(self):
        groups = group_hosts_by_unit("prod-continuous-uploader", [{"ip": "10.0.0.1"}])

        assert [(g.service, g.host_ips) for g in groups] == [(UPLOADER_SERVICE, ["10.0.0.1"])]

    def test_hosts_without_an_ip_are_dropped(self):
        groups = group_hosts_by_unit(
            "prod-continuous-uploader",
            [{"role": "upload"}, {"ip": "10.0.0.2", "role": "download"}],
        )

        assert [(g.service, g.host_ips) for g in groups] == [(VERIFIER_SERVICE, ["10.0.0.2"])]


class TestValidateKind:
    @patch("saorsa_deploy.cmd.upgrade_ant_binary.get_repository", return_value=None)
    def test_proceeds_when_database_unset(self, _repo):
        _validate_kind("PROD-DL-01", console=MagicMock())

    def test_proceeds_when_deployment_not_found(self):
        repo = MagicMock()
        repo.get_active_by_name.return_value = None
        repo.get_by_name.return_value = None
        with patch("saorsa_deploy.cmd.upgrade_ant_binary.get_repository", return_value=repo):
            _validate_kind("PROD-DL-01", console=MagicMock())

    @pytest.mark.parametrize(
        "kind",
        [
            "prod-downloaders",
            "prod-continuous-downloader",
            "prod-continuous-uploader",
        ],
    )
    def test_accepts_valid_kinds(self, kind):
        repo = MagicMock()
        repo.get_active_by_name.return_value = SimpleNamespace(deploy_args={"kind": kind})
        with patch("saorsa_deploy.cmd.upgrade_ant_binary.get_repository", return_value=repo):
            _validate_kind("PROD-DL-01", console=MagicMock())

    def test_rejects_wrong_kind(self):
        repo = MagicMock()
        repo.get_active_by_name.return_value = SimpleNamespace(
            deploy_args={"kind": "prod-uploaders"}
        )
        with patch("saorsa_deploy.cmd.upgrade_ant_binary.get_repository", return_value=repo):
            with pytest.raises(SystemExit):
                _validate_kind("PROD-DL-01", console=MagicMock())


@pytest.fixture
def upgrade_patches():
    with (
        patch(
            "saorsa_deploy.cmd.upgrade_ant_binary._resolve_deployment",
            return_value=("prod-continuous-downloader", "native"),
        ) as p_resolve_deployment,
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
        patch("saorsa_deploy.cmd.upgrade_ant_binary.record_deploy_args_update") as p_deploy_args,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        p_state_cls.return_value = mock_state_instance

        yield SimpleNamespace(
            resolve_deployment=p_resolve_deployment,
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


def _ops(patches):
    """The composed command list of every op the run added, one per group."""
    return [call.kwargs.get("commands", []) for call in patches.add_op.call_args_list]


class TestCmdUpgradeAntBinary:
    def test_restarts_by_default(self, upgrade_patches):
        cmd_upgrade_ant_binary(_args())

        cmds = " ".join(upgrade_patches.add_op.call_args.kwargs.get("commands", []))
        assert f"systemctl restart {DOWNLOADER_SERVICE}" in cmds
        assert f"rm -f {ANT_INSTALL_PATH}" in cmds

    def test_downloader_kind_produces_exactly_one_group(self, upgrade_patches):
        """Regression guard: the downloader kinds must behave as they always
        have — one op, one unit, every VM in it."""
        upgrade_patches.load_state.return_value = {
            "clients": [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}]
        }

        cmd_upgrade_ant_binary(_args())

        ops = _ops(upgrade_patches)
        assert len(ops) == 1
        assert ops[0] == [
            build_ant_binary_upgrade_command("https://example.com/ant.tar.gz", True),
            f"systemctl restart {DOWNLOADER_SERVICE}",
        ]
        assert upgrade_patches.clear.call_args.args[0] == ["10.0.0.1", "10.0.0.2"]

    def test_native_uploader_upgrades_both_roles(self, upgrade_patches):
        upgrade_patches.resolve_deployment.return_value = (
            "prod-continuous-uploader",
            "native",
        )
        upgrade_patches.load_state.return_value = {
            "clients": [
                {"ip": "10.0.0.1", "role": "upload"},
                {"ip": "10.0.0.2", "role": "download"},
                {"ip": "10.0.0.3", "role": "download"},
            ]
        }

        cmd_upgrade_ant_binary(_args(name="PROD-UL-01"))

        install = build_ant_binary_upgrade_command("https://example.com/ant.tar.gz", True)
        assert _ops(upgrade_patches) == [
            [install, f"systemctl restart {UPLOADER_SERVICE}"],
            [install, f"systemctl restart {VERIFIER_SERVICE}"],
        ]
        assert [c.args[0] for c in upgrade_patches.clear.call_args_list] == [
            ["10.0.0.1"],
            ["10.0.0.2", "10.0.0.3"],
        ]

    def test_wasm_uploader_upgrades_the_verifiers_only(self, upgrade_patches):
        upgrade_patches.resolve_deployment.return_value = (
            "prod-continuous-uploader",
            "wasm",
        )
        upgrade_patches.load_state.return_value = {
            "clients": [
                {"ip": "10.0.0.1", "role": "upload"},
                {"ip": "10.0.0.2", "role": "download"},
            ]
        }

        cmd_upgrade_ant_binary(_args(name="PROD-WUL-01"))

        ops = _ops(upgrade_patches)
        assert len(ops) == 1
        assert f"systemctl restart {VERIFIER_SERVICE}" in ops[0]
        assert UPLOADER_SERVICE not in " ".join(ops[0])
        # Nothing is sent to the browser-path VM at all.
        assert [c.args[0] for c in upgrade_patches.clear.call_args_list] == [["10.0.0.2"]]

    def test_no_restart_flag_skips_restart_on_every_group(self, upgrade_patches):
        upgrade_patches.resolve_deployment.return_value = (
            "prod-continuous-uploader",
            "native",
        )
        upgrade_patches.load_state.return_value = {
            "clients": [
                {"ip": "10.0.0.1", "role": "upload"},
                {"ip": "10.0.0.2", "role": "download"},
            ]
        }

        cmd_upgrade_ant_binary(_args(name="PROD-UL-01", restart=False))

        ops = _ops(upgrade_patches)
        assert len(ops) == 2
        for commands in ops:
            assert commands == [
                build_ant_binary_upgrade_command("https://example.com/ant.tar.gz", True)
            ]
            assert "systemctl restart" not in " ".join(commands)

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

    def test_wholly_wasm_uploader_exits_without_installing(self, upgrade_patches):
        upgrade_patches.resolve_deployment.return_value = (
            "prod-continuous-uploader",
            "wasm",
        )
        upgrade_patches.load_state.return_value = {
            "clients": [{"ip": "10.0.0.1", "role": "upload"}]
        }

        with pytest.raises(SystemExit):
            cmd_upgrade_ant_binary(_args(name="PROD-WUL-01"))

        upgrade_patches.add_op.assert_not_called()

    def test_unsupported_kind_exits_before_anything_is_installed(self):
        repo = MagicMock()
        repo.get_active_by_name.return_value = SimpleNamespace(
            deploy_args={"kind": "prod-uploaders"}
        )
        with (
            patch(
                "saorsa_deploy.cmd.upgrade_ant_binary.get_repository",
                return_value=repo,
            ),
            patch("saorsa_deploy.cmd.upgrade_ant_binary.add_op") as p_add_op,
            patch("saorsa_deploy.cmd.upgrade_ant_binary.load_client_state") as p_state,
        ):
            with pytest.raises(SystemExit):
                cmd_upgrade_ant_binary(_args(name="PROD-UL-02"))

        p_state.assert_not_called()
        p_add_op.assert_not_called()

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

    def test_one_failing_group_does_not_stop_the_next(self, upgrade_patches):
        """A verifier VM that cannot be reached must still leave the uploader
        upgraded: a partial upgrade with an accurate record beats a halt."""
        upgrade_patches.resolve_deployment.return_value = (
            "prod-continuous-uploader",
            "native",
        )
        upgrade_patches.load_state.return_value = {
            "clients": [
                {"ip": "10.0.0.1", "role": "upload"},
                {"ip": "10.0.0.2", "role": "download"},
            ]
        }
        upgrade_patches.connect.side_effect = [None, RuntimeError("unreachable")]

        with pytest.raises(SystemExit):
            cmd_upgrade_ant_binary(_args(name="PROD-UL-01"))

        ops = _ops(upgrade_patches)
        assert len(ops) == 1
        assert f"systemctl restart {UPLOADER_SERVICE}" in ops[0]
        failed = [
            c.args[2]["failed_hosts"]
            for c in upgrade_patches.event.call_args_list
            if c.args[1] == "ant_binary_upgrade_failed"
        ]
        assert failed == [["10.0.0.2"]]

    def test_wasm_downloader_is_warned_about_but_still_upgraded(self, upgrade_patches, capsys):
        """PROD-WDL-01 is wholly WASM: nothing on it runs `ant`, so the
        install is pointless — but refusing it would narrow behaviour that
        predates this command, so it warns and proceeds."""
        upgrade_patches.resolve_deployment.return_value = (
            "prod-continuous-downloader",
            "wasm",
        )

        cmd_upgrade_ant_binary(_args(name="PROD-WDL-01"))

        out = " ".join(capsys.readouterr().out.split())
        assert "client_kind: wasm" in out
        assert "upgrade-wasm-client" in out
        ops = _ops(upgrade_patches)
        assert len(ops) == 1
        assert f"systemctl restart {DOWNLOADER_SERVICE}" in ops[0]
