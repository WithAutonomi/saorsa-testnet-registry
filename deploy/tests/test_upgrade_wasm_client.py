"""`deployments upgrade-wasm-client` — V2-1346.

The properties worth holding on to are all about what happens BEFORE and AFTER
the build: a refusal must cost nothing, one build must reach every named
deployment, and a failure on one must not strand the others on a mix of builds
with nothing recording which is which.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.cmd.upgrade_wasm_client import (
    SERVICE_BY_KIND,
    _manifest_ref,
    _resolve_targets,
    cmd_upgrade_wasm_client,
)

MODULE = "saorsa_deploy.cmd.upgrade_wasm_client"

DOWNLOADER_SERVICE = SERVICE_BY_KIND["prod-continuous-downloader"]
UPLOADER_SERVICE = SERVICE_BY_KIND["prod-continuous-uploader"]

BUILD_INFO = {
    "commit": "aaaaaaaaaaaabbbbbbbbbbbb",
    "ref": "main",
    "repo_owner": "WithAutonomi",
    "node_datachannel_version": "0.33.2",
}


def _args(names=("PROD-WDL-01",), **overrides):
    defaults = {
        "name": list(names),
        "branch_name": "main",
        "repo_owner": "WithAutonomi",
        "ssh_key_path": "~/.ssh/ant_deploy",
        "build_vm_size": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _deployment(kind="prod-continuous-downloader", client_kind="wasm", commit="old000000000"):
    return SimpleNamespace(
        deploy_args={
            "kind": kind,
            "client_kind": client_kind,
            "wasm_build": {"commit": commit, "ref": "ant-cli-v0.3.8"},
        }
    )


def _repo(deployments):
    repo = MagicMock()
    repo.get_active_by_name.side_effect = lambda name: deployments.get(name)
    repo.get_by_name.side_effect = lambda name: deployments.get(name)
    return repo


@pytest.fixture
def upgrade_patches():
    """Everything that would touch a build VM, SSH or the registry."""
    deployments = {
        "PROD-WDL-01": _deployment("prod-continuous-downloader"),
        "PROD-WUL-01": _deployment("prod-continuous-uploader"),
    }
    client_state = {
        "PROD-WDL-01": {"clients": [{"ip": "10.0.0.1"}]},
        "PROD-WUL-01": {
            "clients": [
                {"ip": "10.0.0.2", "role": "upload"},
                {"ip": "10.0.0.3", "role": "verify"},
            ]
        },
    }
    with (
        patch(f"{MODULE}.get_repository", return_value=_repo(deployments)),
        patch(
            f"{MODULE}.load_client_state",
            side_effect=lambda name: client_state[name],
        ),
        patch(f"{MODULE}._manifest_ref", return_value=None) as p_manifest,
        patch("saorsa_deploy.cmd.build_ant_wasm.cmd_build_ant_wasm") as p_build,
        patch(
            f"{MODULE}.get_ant_wasm_build_url",
            return_value="https://s3/builds/PROD-WDL-01/WithAutonomi/main/b.tar.gz",
        ),
        patch(f"{MODULE}.fetch_ant_wasm_build_info", return_value=dict(BUILD_INFO)),
        patch(f"{MODULE}.clear_known_hosts"),
        patch(f"{MODULE}.Inventory"),
        patch(f"{MODULE}.State") as p_state_cls,
        patch(f"{MODULE}.connect_all"),
        patch(f"{MODULE}.disconnect_all"),
        patch(f"{MODULE}.add_op") as p_add_op,
        patch(f"{MODULE}.run_ops") as p_run_ops,
        patch(f"{MODULE}.record_event") as p_event,
        patch(f"{MODULE}.record_deploy_args_update") as p_deploy_args,
    ):
        states = []

        def _make_state(*_a, **_kw):
            state = MagicMock()
            state.failed_hosts = set()
            states.append(state)
            return state

        p_state_cls.side_effect = _make_state
        yield SimpleNamespace(
            deployments=deployments,
            client_state=client_state,
            manifest_ref=p_manifest,
            build=p_build,
            add_op=p_add_op,
            run_ops=p_run_ops,
            event=p_event,
            deploy_args=p_deploy_args,
            states=states,
        )


def _commands(add_op_call):
    return add_op_call.kwargs.get("commands", [])


def _flat(text: str) -> str:
    """Rich hard-wraps to the console width, so assert on unwrapped text."""
    return " ".join(text.split())


class TestRefusalBeforeBuilding:
    """Every refusal costs nothing: no build VM, no SSH, no registry write."""

    def test_unknown_name_is_refused(self, upgrade_patches):
        with pytest.raises(SystemExit):
            cmd_upgrade_wasm_client(_args(names=("NOPE-01",)))
        upgrade_patches.build.assert_not_called()
        upgrade_patches.add_op.assert_not_called()

    def test_unsupported_kind_is_refused(self, upgrade_patches):
        upgrade_patches.deployments["PROD-DL-01"] = _deployment("prod-downloaders")
        with pytest.raises(SystemExit):
            cmd_upgrade_wasm_client(_args(names=("PROD-DL-01",)))
        upgrade_patches.build.assert_not_called()

    def test_native_deployment_is_refused(self, upgrade_patches):
        upgrade_patches.deployments["PROD-DL-02"] = _deployment(
            "prod-continuous-downloader", client_kind="native"
        )
        with pytest.raises(SystemExit):
            cmd_upgrade_wasm_client(_args(names=("PROD-DL-02",)))
        upgrade_patches.build.assert_not_called()

    def test_one_bad_name_refuses_the_whole_run(self, upgrade_patches):
        # Partially applying a multi-deployment upgrade would leave the set on
        # two builds, which is the state the command exists to avoid.
        with pytest.raises(SystemExit):
            cmd_upgrade_wasm_client(_args(names=("PROD-WDL-01", "NOPE-01")))
        upgrade_patches.build.assert_not_called()

    def test_missing_client_state_is_refused(self, upgrade_patches):
        upgrade_patches.client_state["PROD-WDL-01"] = {"clients": []}
        with pytest.raises(SystemExit):
            cmd_upgrade_wasm_client(_args())
        upgrade_patches.build.assert_not_called()

    def test_no_registry_is_refused(self):
        with patch(f"{MODULE}.get_repository", return_value=None):
            with pytest.raises(SystemExit):
                _resolve_targets(["PROD-WDL-01"], MagicMock())


class TestOneBuildForEveryDeployment:
    def test_several_names_build_once_and_share_the_bundle(self, upgrade_patches):
        cmd_upgrade_wasm_client(_args(names=("PROD-WDL-01", "PROD-WUL-01")))

        assert upgrade_patches.build.call_count == 1
        # Under the FIRST named deployment: the S3 key is namespaced by name.
        assert upgrade_patches.build.call_args.args[0].name == "PROD-WDL-01"

        urls = set()
        for call in upgrade_patches.add_op.call_args_list:
            install = _commands(call)[0]
            urls.add(install.split("wget -q '")[1].split("'")[0])
        assert len(upgrade_patches.add_op.call_args_list) == 2
        assert urls == {"https://s3/builds/PROD-WDL-01/WithAutonomi/main/b.tar.gz"}

    def test_a_repeated_name_is_upgraded_once(self, upgrade_patches):
        cmd_upgrade_wasm_client(_args(names=("PROD-WDL-01", "PROD-WDL-01")))
        assert len(upgrade_patches.add_op.call_args_list) == 1


class TestSshSequence:
    def test_install_then_cli_then_restart(self, upgrade_patches):
        cmd_upgrade_wasm_client(_args())

        install, cli, restart = _commands(upgrade_patches.add_op.call_args)
        assert "rm -rf /opt/ant-wasm-client" in install
        # require_cli: a bundle with no dist/cli.js installs cleanly and then
        # fails every transfer.
        assert "test -f /opt/ant-wasm-client/dist/cli.js" in install
        assert "/usr/local/bin/ant-wasm" in cli
        assert restart == f"systemctl restart {DOWNLOADER_SERVICE}"

    def test_the_uploader_kind_restarts_its_own_unit(self, upgrade_patches):
        cmd_upgrade_wasm_client(_args(names=("PROD-WUL-01",)))
        assert (
            _commands(upgrade_patches.add_op.call_args)[-1]
            == f"systemctl restart {UPLOADER_SERVICE}"
        )

    def test_verifier_vms_are_not_targeted(self, upgrade_patches):
        # The verifiers are deliberately NATIVE: a digest mismatch they report
        # must stay attributable to the upload, not to a browser read path.
        with patch(f"{MODULE}.Inventory") as p_inv:
            cmd_upgrade_wasm_client(_args(names=("PROD-WUL-01",)))
        hosts = p_inv.call_args.args[0][0]
        assert [ip for ip, _ in hosts] == ["10.0.0.2"]


class TestRecording:
    def test_deploy_args_and_event_carry_old_and_new_commits(self, upgrade_patches):
        cmd_upgrade_wasm_client(_args())

        upgrade_patches.deploy_args.assert_called_once_with(
            "PROD-WDL-01", {"wasm_build": BUILD_INFO}
        )
        upgraded = [
            call
            for call in upgrade_patches.event.call_args_list
            if call.args[1] == "wasm_client_upgraded"
        ]
        assert len(upgraded) == 1
        details = upgraded[0].args[2]
        assert details["old_commit"] == "old000000000"
        assert details["new_commit"] == BUILD_INFO["commit"]
        assert details["ref"] == "main"
        assert details["repo_owner"] == "WithAutonomi"
        assert details["upgraded_at"]

    def test_nothing_is_recorded_for_a_failed_deployment(self, upgrade_patches):
        # `deploy_args.wasm_build` is what every later reader treats as the
        # build this deployment is running; writing it for a VM that never took
        # the bundle would make the registry lie about live infrastructure.
        def _fail_second(*_a, **_kw):
            state = MagicMock()
            state.failed_hosts = (
                [SimpleNamespace(name="10.0.0.2")] if len(upgrade_patches.states) else []
            )
            upgrade_patches.states.append(state)
            return state

        with patch(f"{MODULE}.State", side_effect=_fail_second):
            with pytest.raises(SystemExit):
                cmd_upgrade_wasm_client(_args(names=("PROD-WDL-01", "PROD-WUL-01")))

        # The first deployment still upgraded and recorded; the second did not.
        assert upgrade_patches.deploy_args.call_args_list[0].args[0] == "PROD-WDL-01"
        assert upgrade_patches.deploy_args.call_count == 1
        failed = [
            call.args[0]
            for call in upgrade_patches.event.call_args_list
            if call.args[1] == "wasm_client_upgrade_failed"
        ]
        assert failed == ["PROD-WUL-01"]

    def test_a_partial_failure_still_upgrades_the_rest(self, upgrade_patches):
        def _fail_first(*_a, **_kw):
            state = MagicMock()
            state.failed_hosts = (
                [] if len(upgrade_patches.states) else [SimpleNamespace(name="10.0.0.1")]
            )
            upgrade_patches.states.append(state)
            return state

        with patch(f"{MODULE}.State", side_effect=_fail_first):
            with pytest.raises(SystemExit):
                cmd_upgrade_wasm_client(_args(names=("PROD-WDL-01", "PROD-WUL-01")))

        assert len(upgrade_patches.add_op.call_args_list) == 2
        assert upgrade_patches.deploy_args.call_args_list[0].args[0] == "PROD-WUL-01"


class TestManifestDrift:
    def test_warns_when_the_manifest_still_pins_the_old_ref(self, upgrade_patches, capsys):
        upgrade_patches.manifest_ref.return_value = "ant-cli-v0.3.8"
        cmd_upgrade_wasm_client(_args())
        out = _flat(capsys.readouterr().out)
        assert "still pins wasm_binary.branch: ant-cli-v0.3.8" in out

    def test_silent_when_the_manifest_matches(self, upgrade_patches, capsys):
        upgrade_patches.manifest_ref.return_value = "main"
        cmd_upgrade_wasm_client(_args())
        assert "still pins" not in _flat(capsys.readouterr().out)

    def test_silent_when_no_manifest_names_the_deployment(self, upgrade_patches, capsys):
        cmd_upgrade_wasm_client(_args())
        assert "still pins" not in _flat(capsys.readouterr().out)


class TestManifestRefLookup:
    def _write(self, tmp_path, filename, body):
        (tmp_path / filename).write_text(body)

    def test_reads_the_branch_from_a_matching_manifest(self, tmp_path):
        self._write(
            tmp_path,
            "PROD-WDL-01.yaml",
            "name: PROD-WDL-01\nkind: prod-continuous-downloader\n"
            "prod_continuous_downloader:\n  wasm_binary:\n"
            "    branch: ant-cli-v0.3.8\n    repo_owner: WithAutonomi\n",
        )
        with patch(f"{MODULE}.RUNS_MANIFEST_DIR", tmp_path):
            ref = _manifest_ref("PROD-WDL-01", "prod-continuous-downloader")
        assert ref == "ant-cli-v0.3.8"

    def test_a_manifest_that_does_not_parse_is_skipped(self, tmp_path):
        # The warning must never be able to fail an upgrade of running
        # infrastructure, so a half-edited manifest is simply not consulted.
        self._write(tmp_path, "broken.yaml", "name: [PROD-WDL-01\n")
        with patch(f"{MODULE}.RUNS_MANIFEST_DIR", tmp_path):
            assert _manifest_ref("PROD-WDL-01", "prod-continuous-downloader") is None

    def test_no_manifest_reads_as_none(self, tmp_path):
        with patch(f"{MODULE}.RUNS_MANIFEST_DIR", tmp_path):
            assert _manifest_ref("PROD-WDL-01", "prod-continuous-downloader") is None
