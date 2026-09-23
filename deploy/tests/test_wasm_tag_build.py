"""`ant_wasm` built from an ant-client git TAG rather than a branch.

The staging profile pins the WASM tier to the train's ant-client RC tag
(`ant-cli-v0.3.8-rc.1`) because no WASM package is published as a GitHub
release, so the release form has nothing to point at. Nothing in the tool had
to change for that -- `validate_branch_exists` queries
`git ls-remote --heads --tags`, and the builder does a single
`git clone --branch <ref> --depth 1` with no later checkout, both of which take
a tag exactly as they take a branch.

That is precisely why it is worth a test: the support is incidental. A
well-meaning narrowing of either (dropping `--tags`, or adding a
`git checkout <branch>` after the clone) would break every release train's
staging run and nothing else would notice.
"""

from unittest.mock import patch

from saorsa_deploy.binary_source import (
    get_ant_wasm_build_url,
    remote_branch_exists,
    validate_branch_exists,
)
from saorsa_deploy.config.models import Manifest
from saorsa_deploy.orchestrator import build_deploy_steps
from saorsa_deploy.provisioning.build_ant_wasm import AntWasmBuilder

TAG = "ant-cli-v0.3.8-rc.1"
OWNER = "WithAutonomi"
SSH = "~/.ssh/ant_deploy"


def _manifest(ref: str = TAG) -> Manifest:
    return Manifest.model_validate(
        {
            "name": "STG-01",
            "kind": "testnet",
            "purpose": "p",
            "ssh_key_path": SSH,
            "testnet": {
                "binaries": {
                    "ant_node": {"version": "0.19.0-rc.1"},
                    "ant": {"version": "0.3.8-rc.1"},
                    "ant_wasm": {"branch": ref, "repo_owner": OWNER},
                },
                "bootstraps": {"count": 1},
                "nodes": {
                    "per_vm": 5,
                    "providers": {"digital_ocean": {"vm_count": 2, "regions": 3}},
                },
                "clients": {
                    "wasm_uploaders": [{"provider": "ovh", "region": "SBG5", "file_size_mb": 900}],
                    "wasm_downloaders": [{"provider": "digital_ocean", "region": "tor1"}],
                },
            },
        }
    )


class TestManifest:
    def test_a_tag_validates_as_the_branch_form(self):
        m = _manifest()
        assert m.testnet.binaries.ant_wasm.branch == TAG
        assert m.testnet.binaries.ant_wasm.repo_owner == OWNER

    def test_the_build_step_carries_the_tag_verbatim(self):
        steps = build_deploy_steps(_manifest())
        argv = next(s.argv for s in steps if s.argv[:2] == ["build", "ant-wasm"])
        assert argv[argv.index("--branch-name") + 1] == TAG
        assert argv[argv.index("--repo-owner") + 1] == OWNER

    def test_clients_provision_fetches_the_bundle_built_from_the_tag(self):
        steps = build_deploy_steps(_manifest())
        argv = next(s.argv for s in steps if s.argv[:2] == ["clients", "provision"])
        assert argv[argv.index("--wasm-branch-name") + 1] == TAG


class TestS3Key:
    def test_key_is_namespaced_by_name_owner_and_tag(self):
        url = get_ant_wasm_build_url("STG-01", OWNER, TAG)
        assert url.endswith(f"/builds/STG-01/{OWNER}/{TAG}/ant-wasm-client.tar.gz")

    def test_the_builder_uploads_to_that_same_key(self):
        builder = AntWasmBuilder(
            name="STG-01",
            ip="203.0.113.1",
            ssh_key_path=SSH,
            repo_owner=OWNER,
            branch_name=TAG,
        )
        assert builder.s3_key == f"builds/STG-01/{OWNER}/{TAG}/ant-wasm-client.tar.gz"


class TestRefValidation:
    """`git ls-remote --heads --tags` is what makes a tag acceptable."""

    def test_ls_remote_asks_for_tags_as_well_as_heads(self):
        with patch("saorsa_deploy.binary_source.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = f"4d033fc\trefs/tags/{TAG}\n"
            assert remote_branch_exists(OWNER, "ant-client", TAG) is True
        cmd = run.call_args[0][0]
        assert cmd[:3] == ["git", "ls-remote", "--heads"]
        assert "--tags" in cmd
        assert cmd[-1] == TAG

    def test_a_resolvable_tag_passes_validation(self):
        with patch("saorsa_deploy.binary_source.remote_branch_exists", return_value=True):
            validate_branch_exists(OWNER, "ant-client", TAG)
