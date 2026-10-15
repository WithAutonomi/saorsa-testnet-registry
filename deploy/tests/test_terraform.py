import json
import os
import subprocess
from unittest.mock import patch

import pytest

from saorsa_deploy.terraform import (
    DESTROY_RETRY_MARKER,
    TerraformRunConfig,
    build_apply_args,
    build_destroy_args,
    build_init_args,
    build_output_args,
    is_do_volume_detach_race,
    prepare_workspace,
    run_terraform,
    run_terraform_destroy,
)

# The run-670 failure verbatim: Terraform's boxed error, ANSI codes and wrapping intact. The
# wrap breaks "droplet" from its "(609422581)", which is why the matcher collapses whitespace.
RUN_670_RACE_OUTPUT = (
    "\x1b[31m\u2577\x1b[0m\n"
    "\x1b[31m\u2502\x1b[0m \x1b[1m\x1b[31mError: \x1b[0m\x1b[0m\x1b[1mError detaching the "
    "volumes from the droplet\x1b[0m\n"
    "\x1b[31m\u2502\x1b[0m (609422581): Error detaching volume\n"
    '\x1b[31m\u2502\x1b[0m "c7e3b9f1-c8b7-11f1-9fd4-d26f6d2e9370" from droplet (609422581): '
    "POST\n"
    "\x1b[31m\u2502\x1b[0m https://api.digitalocean.com/v2/volumes/"
    "c7e3b9f1-c8b7-11f1-9fd4-d26f6d2e9370/actions: 404\n"
    '\x1b[31m\u2502\x1b[0m (request "e69a191c-ac05-4d4a-8ae0-d6df2f7e7bb9") error detaching '
    "volume from droplet: not found\n"
    "\x1b[31m\u2575\x1b[0m\n"
)


@pytest.fixture
def tf_source(tmp_path):
    """Create a fake Terraform source directory with .tf files."""
    src = tmp_path / "resources" / "digitalocean"
    src.mkdir(parents=True)
    (src / "main.tf").write_text("# main")
    (src / "variables.tf").write_text("# variables")
    (src / "outputs.tf").write_text("# outputs")
    (src / "provider.tf").write_text("# provider")
    (src / "versions.tf").write_text("# versions")
    return src


@pytest.fixture
def workspace(tmp_path):
    return tmp_path / "workspaces" / "digitalocean-lon1"


@pytest.fixture
def config(tf_source, workspace):
    return TerraformRunConfig(
        provider="digitalocean",
        region="lon1",
        tf_source_dir=tf_source,
        workspace_dir=workspace,
        state_key="saorsa-deploy/do-TEST-lon1.tfstate",
        variables={
            "name": "TEST",
            "region": "lon1",
            "vm_count": "2",
            "attached_volume_size": "20",
        },
    )


def _make_completed_process(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestPrepareWorkspace:
    def test_copies_tf_files_to_workspace(self, config):
        prepare_workspace(config)
        assert (config.workspace_dir / "main.tf").exists()
        assert (config.workspace_dir / "variables.tf").exists()
        assert (config.workspace_dir / "outputs.tf").exists()
        assert (config.workspace_dir / "provider.tf").exists()
        assert (config.workspace_dir / "versions.tf").exists()

    def test_creates_workspace_directory(self, config):
        assert not config.workspace_dir.exists()
        prepare_workspace(config)
        assert config.workspace_dir.is_dir()

    def test_only_copies_tf_files(self, config):
        (config.tf_source_dir / "notes.txt").write_text("not terraform")
        prepare_workspace(config)
        assert not (config.workspace_dir / "notes.txt").exists()


class TestBuildInitArgs:
    def test_includes_backend_config_key(self, config):
        args = build_init_args(config)
        assert args == [
            "terraform",
            "init",
            "-input=false",
            "-reconfigure",
            "-backend-config=key=saorsa-deploy/do-TEST-lon1.tfstate",
        ]


class TestBuildApplyArgs:
    def test_includes_variables_sorted(self, config):
        args = build_apply_args(config)
        assert args == [
            "terraform",
            "apply",
            "-auto-approve",
            "-input=false",
            "-var=attached_volume_size=20",
            "-var=name=TEST",
            "-var=region=lon1",
            "-var=vm_count=2",
        ]

    def test_no_variables(self, config):
        config.variables = {}
        args = build_apply_args(config)
        assert args == [
            "terraform",
            "apply",
            "-auto-approve",
            "-input=false",
        ]


class TestBuildOutputArgs:
    def test_returns_correct_args(self):
        args = build_output_args()
        assert args == ["terraform", "output", "-json"]


class TestRunTerraform:
    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_calls_init_with_correct_args(self, mock_run, config):
        mock_run.return_value = _make_completed_process()
        run_terraform(config)

        init_call = mock_run.call_args_list[0]
        assert init_call.args[0] == [
            "terraform",
            "init",
            "-input=false",
            "-reconfigure",
            "-backend-config=key=saorsa-deploy/do-TEST-lon1.tfstate",
        ]
        assert init_call.kwargs["cwd"] == str(config.workspace_dir)
        assert init_call.kwargs["capture_output"] is True
        assert init_call.kwargs["text"] is True

    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_calls_apply_with_correct_args(self, mock_run, config):
        mock_run.return_value = _make_completed_process()
        run_terraform(config)

        apply_call = mock_run.call_args_list[1]
        assert apply_call.args[0] == [
            "terraform",
            "apply",
            "-auto-approve",
            "-input=false",
            "-var=attached_volume_size=20",
            "-var=name=TEST",
            "-var=region=lon1",
            "-var=vm_count=2",
        ]

    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_passes_do_token_as_tf_var(self, mock_run, config):
        mock_run.return_value = _make_completed_process()
        original = os.environ.get("DO_TOKEN")
        try:
            os.environ["DO_TOKEN"] = "test-token-123"
            run_terraform(config)

            init_call = mock_run.call_args_list[0]
            assert init_call.kwargs["env"]["TF_VAR_do_token"] == "test-token-123"
        finally:
            if original is None:
                os.environ.pop("DO_TOKEN", None)
            else:
                os.environ["DO_TOKEN"] = original

    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_successful_run_returns_success(self, mock_run, config):
        mock_run.return_value = _make_completed_process()
        result = run_terraform(config)
        assert result.success is True
        assert result.provider == "digitalocean"
        assert result.region == "lon1"

    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_init_failure_returns_failure_without_apply(self, mock_run, config):
        mock_run.return_value = _make_completed_process(
            returncode=1, stdout="init failed", stderr="init error"
        )
        result = run_terraform(config)
        assert result.success is False
        assert result.stderr == "init error"
        assert mock_run.call_count == 1

    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_apply_failure_returns_failure(self, mock_run, config):
        mock_run.side_effect = [
            _make_completed_process(),  # init succeeds
            _make_completed_process(returncode=1, stderr="apply error"),  # apply fails
        ]
        result = run_terraform(config)
        assert result.success is False
        assert result.stderr == "apply error"
        assert mock_run.call_count == 2

    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_workspace_files_exist_after_run(self, mock_run, config):
        mock_run.return_value = _make_completed_process()
        run_terraform(config)
        assert (config.workspace_dir / "main.tf").exists()

    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_captures_terraform_outputs(self, mock_run, config):
        output_json = json.dumps(
            {
                "droplet_ips": {"value": ["10.0.0.1", "10.0.0.2"]},
                "droplet_ids": {"value": [123, 456]},
                "volume_ids": {"value": ["vol-1", "vol-2"]},
            }
        )
        mock_run.side_effect = [
            _make_completed_process(),  # init
            _make_completed_process(),  # apply
            _make_completed_process(stdout=output_json),  # output
        ]
        result = run_terraform(config)
        assert result.success is True
        assert result.outputs["droplet_ips"] == ["10.0.0.1", "10.0.0.2"]
        assert result.outputs["droplet_ids"] == [123, 456]
        assert mock_run.call_count == 3

    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_output_failure_still_succeeds(self, mock_run, config):
        mock_run.side_effect = [
            _make_completed_process(),  # init
            _make_completed_process(),  # apply
            _make_completed_process(returncode=1),  # output fails
        ]
        result = run_terraform(config)
        assert result.success is True
        assert result.outputs == {}

    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_calls_init_apply_output_in_order(self, mock_run, config):
        mock_run.return_value = _make_completed_process()
        run_terraform(config)

        assert mock_run.call_count == 3
        assert mock_run.call_args_list[0].args[0][1] == "init"
        assert mock_run.call_args_list[1].args[0][1] == "apply"
        assert mock_run.call_args_list[2].args[0][1] == "output"


class TestBuildDestroyArgs:
    def test_includes_variables_sorted(self, config):
        args = build_destroy_args(config)
        assert args == [
            "terraform",
            "destroy",
            "-auto-approve",
            "-input=false",
            "-var=attached_volume_size=20",
            "-var=name=TEST",
            "-var=region=lon1",
            "-var=vm_count=2",
        ]

    def test_no_variables(self, config):
        config.variables = {}
        args = build_destroy_args(config)
        assert args == [
            "terraform",
            "destroy",
            "-auto-approve",
            "-input=false",
        ]


class TestRunTerraformDestroy:
    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_calls_init_then_destroy(self, mock_run, config):
        mock_run.return_value = _make_completed_process()
        run_terraform_destroy(config)

        assert mock_run.call_count == 2
        init_call = mock_run.call_args_list[0]
        assert init_call.args[0][1] == "init"
        destroy_call = mock_run.call_args_list[1]
        assert destroy_call.args[0][1] == "destroy"

    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_calls_destroy_with_correct_args(self, mock_run, config):
        mock_run.return_value = _make_completed_process()
        run_terraform_destroy(config)

        destroy_call = mock_run.call_args_list[1]
        assert destroy_call.args[0] == [
            "terraform",
            "destroy",
            "-auto-approve",
            "-input=false",
            "-var=attached_volume_size=20",
            "-var=name=TEST",
            "-var=region=lon1",
            "-var=vm_count=2",
        ]

    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_successful_destroy_returns_success(self, mock_run, config):
        mock_run.return_value = _make_completed_process()
        result = run_terraform_destroy(config)
        assert result.success is True
        assert result.provider == "digitalocean"
        assert result.region == "lon1"

    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_init_failure_skips_destroy(self, mock_run, config):
        mock_run.return_value = _make_completed_process(returncode=1, stderr="init error")
        result = run_terraform_destroy(config)
        assert result.success is False
        assert mock_run.call_count == 1

    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_destroy_failure_returns_failure(self, mock_run, config):
        mock_run.side_effect = [
            _make_completed_process(),
            _make_completed_process(returncode=1, stderr="destroy error"),
        ]
        result = run_terraform_destroy(config)
        assert result.success is False
        assert result.stderr == "destroy error"

    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_passes_do_token_as_tf_var(self, mock_run, config):
        mock_run.return_value = _make_completed_process()
        original = os.environ.get("DO_TOKEN")
        try:
            os.environ["DO_TOKEN"] = "test-token-456"
            run_terraform_destroy(config)

            init_call = mock_run.call_args_list[0]
            assert init_call.kwargs["env"]["TF_VAR_do_token"] == "test-token-456"
        finally:
            if original is None:
                os.environ.pop("DO_TOKEN", None)
            else:
                os.environ["DO_TOKEN"] = original


class TestIsDoVolumeDetachRace:
    def test_detects_the_volume_detach_race(self):
        assert is_do_volume_detach_race(RUN_670_RACE_OUTPUT) is True

    def test_rejects_other_destroy_errors(self):
        other_resource_404 = (
            "Error: Error deleting droplet: DELETE "
            "https://api.digitalocean.com/v2/droplets/609422581: 404 (request "
            '"e69a191c-ac05-4d4a-8ae0-d6df2f7e7bb9") droplet not found'
        )
        assert is_do_volume_detach_race(other_resource_404) is False

        detach_422 = RUN_670_RACE_OUTPUT.replace("actions: 404", "actions: 422")
        assert is_do_volume_detach_race(detach_422) is False

        assert is_do_volume_detach_race("") is False


class TestRunTerraformDestroyRetriesTheVolumeDetachRace:
    @patch("saorsa_deploy.terraform.time.sleep")
    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_retries_once_on_the_race_and_succeeds(self, mock_run, mock_sleep, config):
        mock_run.side_effect = [
            _make_completed_process(),
            _make_completed_process(returncode=1, stdout="first out", stderr=RUN_670_RACE_OUTPUT),
            _make_completed_process(stdout="second out", stderr="second err"),
        ]
        result = run_terraform_destroy(config)

        assert result.success is True
        assert result.retried is True
        assert mock_run.call_count == 3
        destroy_calls = mock_run.call_args_list[1:]
        assert destroy_calls[0].args[0] == destroy_calls[1].args[0]
        assert destroy_calls[0].args[0][1] == "destroy"
        assert destroy_calls[0].kwargs["cwd"] == destroy_calls[1].kwargs["cwd"]
        assert "first out" in result.stdout
        assert "second out" in result.stdout
        assert DESTROY_RETRY_MARKER in result.stdout
        assert RUN_670_RACE_OUTPUT in result.stderr
        assert "second err" in result.stderr
        assert DESTROY_RETRY_MARKER in result.stderr
        mock_sleep.assert_called_once_with(10)

    @patch("saorsa_deploy.terraform.time.sleep")
    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_retries_at_most_once(self, mock_run, mock_sleep, config):
        mock_run.side_effect = [
            _make_completed_process(),
            _make_completed_process(returncode=1, stderr=RUN_670_RACE_OUTPUT),
            _make_completed_process(returncode=1, stderr=RUN_670_RACE_OUTPUT),
        ]
        result = run_terraform_destroy(config, retry_delay_secs=0)

        assert result.success is False
        assert result.retried is True
        assert len(mock_run.call_args_list[1:]) == 2
        mock_sleep.assert_called_once_with(0)

    @patch("saorsa_deploy.terraform.time.sleep")
    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_no_retry_on_other_failures(self, mock_run, mock_sleep, config):
        mock_run.side_effect = [
            _make_completed_process(),
            _make_completed_process(returncode=1, stderr="Error: some other destroy error"),
        ]
        result = run_terraform_destroy(config)

        assert result.success is False
        assert result.retried is False
        assert mock_run.call_count == 2
        mock_sleep.assert_not_called()

    @patch("saorsa_deploy.terraform.time.sleep")
    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_no_retry_for_other_providers(self, mock_run, mock_sleep, config):
        config.provider = "ovh"
        mock_run.side_effect = [
            _make_completed_process(),
            _make_completed_process(returncode=1, stderr=RUN_670_RACE_OUTPUT),
        ]
        result = run_terraform_destroy(config)

        assert result.success is False
        assert result.retried is False
        assert mock_run.call_count == 2
        mock_sleep.assert_not_called()

    @patch("saorsa_deploy.terraform.time.sleep")
    @patch("saorsa_deploy.terraform.subprocess.run")
    def test_init_failure_still_skips_destroy(self, mock_run, mock_sleep, config):
        mock_run.return_value = _make_completed_process(returncode=1, stderr="init error")
        result = run_terraform_destroy(config)

        assert result.success is False
        assert result.retried is False
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()
