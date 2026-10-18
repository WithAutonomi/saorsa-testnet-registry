"""Tests for the tool's own state directory (`saorsa_deploy.paths.state_dir`).

The Terraform working copies and the parallel-lane logs used to live under
`<cwd>/.saorsa`, which made a deployment's working state a property of the
directory the command that created it happened to be run from. These assert the
replacement: one location per machine, overridable with an env var, and lane
logs keyed by deployment name so two runs cannot overwrite each other's.
"""

import io
from pathlib import Path

from rich.console import Console

from saorsa_deploy import parallel_infra, paths
from saorsa_deploy.bootstrap import _build_bootstrap_configs
from saorsa_deploy.cmd.migration_infra import _workspace_dir as migration_workspace_dir
from saorsa_deploy.orchestrator.steps import Step, StepGroup
from saorsa_deploy.paths import state_dir

# The source tree must not grow a second cwd-relative state location: the
# directory a command runs from is not allowed to decide where its state lives.
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "saorsa_deploy"


class TestStateDir:
    def test_default_is_under_the_user_data_dir(self, monkeypatch):
        monkeypatch.delenv(paths.STATE_DIR_ENV_VAR, raising=False)
        monkeypatch.setenv("HOME", "/home/someone")
        assert state_dir() == Path("/home/someone/.local/share/saorsa-deploy")

    def test_env_var_overrides_the_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv(paths.STATE_DIR_ENV_VAR, str(tmp_path / "elsewhere"))
        assert state_dir() == tmp_path / "elsewhere"

    def test_env_var_is_expanded(self, monkeypatch):
        monkeypatch.setenv(paths.STATE_DIR_ENV_VAR, "~/sd-state")
        monkeypatch.setenv("HOME", "/home/someone")
        assert state_dir() == Path("/home/someone/sd-state")

    def test_empty_env_var_falls_back_to_the_default(self, monkeypatch):
        """An exported-but-empty override is a missing one, not a relative path."""
        monkeypatch.setenv(paths.STATE_DIR_ENV_VAR, "")
        monkeypatch.setenv("HOME", "/home/someone")
        assert state_dir() == Path("/home/someone/.local/share/saorsa-deploy")

    def test_does_not_depend_on_the_working_directory(self, monkeypatch, tmp_path):
        monkeypatch.setenv(paths.STATE_DIR_ENV_VAR, str(tmp_path / "sd"))
        before = state_dir()
        monkeypatch.chdir(tmp_path)
        assert state_dir() == before


class TestWorkspacePaths:
    """Workspaces moved wholesale: same last component, new parent."""

    def test_bootstrap_workspace_is_under_the_state_dir(self, state_dir_root):
        configs = _build_bootstrap_configs("DEV-01", ["lon1"])
        assert (
            configs[0].workspace_dir == state_dir_root / "workspaces" / "do-bootstrap-DEV-01-lon1"
        )

    def test_migration_workspace_is_under_the_state_dir(self, state_dir_root):
        assert (
            migration_workspace_dir("PROD-MIG-01", "fsn1")
            == state_dir_root / "workspaces" / "migration-PROD-MIG-01-fsn1"
        )

    def test_no_source_module_builds_a_cwd_relative_state_path(self):
        """Regression guard for the whole point of `state_dir()`.

        A state location derived from the cwd makes `destroy` from another
        directory re-copy and re-`init` everything, and gives every directory a
        run is launched from its own multi-gigabyte set of Terraform copies.
        """
        offenders = [
            str(path.relative_to(SOURCE_ROOT))
            for path in SOURCE_ROOT.rglob("*.py")
            if '".saorsa"' in path.read_text() or "Path.cwd() /" in path.read_text()
        ]
        assert offenders == []


def _group() -> StepGroup:
    return StepGroup(
        name="infra",
        label="Create infrastructure (parallel)",
        lanes=[
            [Step(["nodes", "infra"], "Nodes", group="infra", lane="bn")],
            [Step(["elastic", "infra"], "Elastic", group="infra", lane="el")],
        ],
    )


class TestLaneLogs:
    def test_log_dir_is_keyed_by_deployment_name(self, state_dir_root):
        assert parallel_infra._log_dir("DEV-01") == state_dir_root / "infra-logs" / "DEV-01"

    def test_two_deployments_write_to_different_log_directories(self, state_dir_root, monkeypatch):
        """Two `up`-style runs in one process must not share a log path.

        The lane logs are named after the lane index and the step only
        (`lane00-nodes-infra.log`), so before the deployment name keyed the
        directory the second run overwrote the first run's logs in place.
        """
        written: list[Path] = []

        def fake_run(step, log_path):
            log_path.write_text(f"ran {' '.join(step.argv)}\n")
            written.append(log_path)
            return 0

        monkeypatch.setattr(parallel_infra, "_run_step_subprocess", fake_run)
        console = Console(file=io.StringIO())
        for name in ("DEV-01", "DEV-02"):
            parallel_infra.run_parallel_group(console, _group(), deployment_name=name)

        assert len(written) == 4
        assert len(set(written)) == 4
        for name in ("DEV-01", "DEV-02"):
            log_dir = state_dir_root / "infra-logs" / name
            assert sorted(p.name for p in log_dir.iterdir()) == [
                "lane00-nodes-infra.log",
                "lane01-elastic-infra.log",
            ]

    def test_client_extension_logs_are_keyed_by_the_journal_key(self, state_dir_root):
        """`<deployment>--<extension>` cannot collide with a deployment name."""
        assert (
            parallel_infra._log_dir("DEV-01--browsers")
            == state_dir_root / "infra-logs" / "DEV-01--browsers"
        )
