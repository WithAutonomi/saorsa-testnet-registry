"""Unit tests for the `debug: true` binary-build option.

Covers the manifest-model validation, the orchestrator build-step argv, and the
builder's cargo-flag / target-subdir derivation (release vs debug, musl vs not).
"""
import pytest

from saorsa_deploy.config.models import Binary
from saorsa_deploy.orchestrator.prod import _binary_debug_flag
from saorsa_deploy.orchestrator.steps import (
    ant_build_steps,
    build_steps_for_branches,
)
from saorsa_deploy.provisioning.build import AntNodeBuilder
from saorsa_deploy.provisioning.build_ant import AntBuilder


class TestBinaryDebugValidation:
    def test_debug_allowed_with_branch(self):
        b = Binary(branch="rc-2026.6.2", repo_owner="WithAutonomi", debug=True)
        assert b.debug is True

    def test_debug_rejected_with_version(self):
        with pytest.raises(ValueError, match="debug.*only valid with.*branch"):
            Binary(version="1.2.3", debug=True)

    def test_debug_defaults_none(self):
        b = Binary(branch="x", repo_owner="y")
        assert b.debug is None


class TestOrchestratorEmitsDebug:
    def test_ant_build_step_appends_debug(self):
        ant = Binary(branch="rc-2026.6.2", repo_owner="WithAutonomi", debug=True)
        steps = ant_build_steps("STG-01", "~/.ssh/id_rsa", ant)
        assert steps[0].argv[-1] == "--debug"

    def test_no_debug_token_when_unset(self):
        ant = Binary(branch="rc-2026.6.2", repo_owner="WithAutonomi")
        steps = ant_build_steps("STG-01", "~/.ssh/id_rsa", ant)
        assert "--debug" not in steps[0].argv

    def test_debug_is_independent_per_binary(self):
        # ant_node debug, ant release — only the ant-node step carries --debug.
        node = Binary(branch="v2-463", repo_owner="jacderida", debug=True)
        ant = Binary(branch="rc-2026.6.2", repo_owner="WithAutonomi")
        steps = build_steps_for_branches("STG-01", "~/.ssh/id_rsa", node, ant)
        node_step = next(s for s in steps if s.argv[:2] == ["build", "ant-node"])
        ant_step = next(s for s in steps if s.argv[:2] == ["build", "ant"])
        assert "--debug" in node_step.argv
        assert "--debug" not in ant_step.argv


class TestBuilderCargoFlags:
    @pytest.mark.parametrize("cls", [AntBuilder, AntNodeBuilder])
    def test_release_default(self, cls):
        b = cls(
            name="STG-01", ip="1.2.3.4", ssh_key_path="~/.ssh/id_rsa",
            repo_owner="o", branch_name="br",
        )
        assert b._cargo_release_flag == " --release"
        assert b._target_subdir == "release"

    @pytest.mark.parametrize("cls", [AntBuilder, AntNodeBuilder])
    def test_debug_drops_release(self, cls):
        b = cls(
            name="STG-01", ip="1.2.3.4", ssh_key_path="~/.ssh/id_rsa",
            repo_owner="o", branch_name="br", debug=True,
        )
        assert b._cargo_release_flag == ""
        assert b._target_subdir == "debug"

    @pytest.mark.parametrize("cls", [AntBuilder, AntNodeBuilder])
    def test_debug_musl_target_subdir(self, cls):
        b = cls(
            name="STG-01", ip="1.2.3.4", ssh_key_path="~/.ssh/id_rsa",
            repo_owner="o", branch_name="br", musl=True, debug=True,
        )
        assert b._target_subdir == "x86_64-unknown-linux-musl/debug"
        assert b._cargo_target_flag == " --target x86_64-unknown-linux-musl"

    @pytest.mark.parametrize("cls", [AntBuilder, AntNodeBuilder])
    def test_release_musl_unchanged(self, cls):
        b = cls(
            name="STG-01", ip="1.2.3.4", ssh_key_path="~/.ssh/id_rsa",
            repo_owner="o", branch_name="br", musl=True,
        )
        assert b._target_subdir == "x86_64-unknown-linux-musl/release"


class TestProdDeployDebugFlag:
    """The informational --debug on the deploy step (drives slack-post)."""

    def test_emitted_when_debug(self):
        b = Binary(branch="rc", repo_owner="WithAutonomi", debug=True)
        assert _binary_debug_flag(b) == ["--debug"]

    def test_absent_when_unset(self):
        b = Binary(branch="rc", repo_owner="WithAutonomi")
        assert _binary_debug_flag(b) == []

    def test_absent_for_release(self):
        b = Binary(version="0.2.5")
        assert _binary_debug_flag(b) == []
