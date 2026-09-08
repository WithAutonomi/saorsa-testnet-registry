"""The WASM (browser-path) client kind, end to end (V2-1169).

`ant-cli` cannot exercise the WebRTC-Direct path at all -- it links native
ant-core, so it takes the native transport however it is invoked. These clients
run a Node.js program driving ant-core compiled to WASM instead.

Modelled on test_forward_logs_rust_log.py, which is this codebase's pattern for
a cross-cutting client option: spec parsing, manifest validation, orchestrator
emission, provisioner rendering, and the "everything else is unchanged" guards.
"""

import pytest
from pydantic import ValidationError

from saorsa_deploy.client_roles import (
    ROLE_DOWNLOAD,
    ROLE_UPLOAD,
    ROLE_WASM_DOWNLOAD,
    ROLE_WASM_UPLOAD,
    is_upload,
    is_wasm,
    unit_for_role,
)
from saorsa_deploy.cmd.infra_clients import (
    parse_wasm_downloader_spec,
    parse_wasm_uploader_spec,
)
from saorsa_deploy.config.models import Manifest
from saorsa_deploy.orchestrator import build_deploy_steps
from saorsa_deploy.provisioning.client import _build_wasm_unit

SSH = "~/.ssh/ant_deploy"


# --------------------------------------------------------------------------- #
# roles
# --------------------------------------------------------------------------- #
class TestRoles:
    def test_unit_names_are_explicit_not_derived(self):
        # These were built as f"ant-{role}er", which spells the right thing for
        # exactly the two native roles. "wasm-upload" would have become
        # "ant-wasm-uploader" by luck and the download variant not at all.
        assert unit_for_role(ROLE_UPLOAD) == "ant-uploader"
        assert unit_for_role(ROLE_DOWNLOAD) == "ant-downloader"
        assert unit_for_role(ROLE_WASM_UPLOAD) == "ant-wasm-uploader"
        assert unit_for_role(ROLE_WASM_DOWNLOAD) == "ant-wasm-downloader"

    def test_unknown_role_raises_rather_than_inventing_a_unit(self):
        with pytest.raises(ValueError, match="unknown client role"):
            unit_for_role("banana")

    def test_transport_and_direction_predicates(self):
        assert is_wasm(ROLE_WASM_UPLOAD) and is_wasm(ROLE_WASM_DOWNLOAD)
        assert not is_wasm(ROLE_UPLOAD) and not is_wasm(ROLE_DOWNLOAD)
        assert is_upload(ROLE_UPLOAD) and is_upload(ROLE_WASM_UPLOAD)
        assert not is_upload(ROLE_DOWNLOAD) and not is_upload(ROLE_WASM_DOWNLOAD)


# --------------------------------------------------------------------------- #
# spec-token parsing
# --------------------------------------------------------------------------- #
class TestSpecParsing:
    def test_minimal_uploader(self):
        spec = parse_wasm_uploader_spec("digital_ocean:lon1:100")
        assert spec["role"] == ROLE_WASM_UPLOAD
        assert spec["provider"] == "digitalocean"
        assert spec["region"] == "lon1"
        assert spec["file_size_mb"] == 100

    def test_minimal_downloader(self):
        spec = parse_wasm_downloader_spec("vultr:nrt")
        assert spec["role"] == ROLE_WASM_DOWNLOAD
        assert spec["provider"] == "vultr"
        assert spec["region"] == "nrt"

    def test_uploader_options(self):
        spec = parse_wasm_uploader_spec(
            "digital_ocean:lon1:50:s-4vcpu-8gb:payment=merkle:ant=200:eth=0.1"
            ":clock_skew=2h:forward_logs=true:network=home-fttc"
        )
        assert spec["vm_size"] == "s-4vcpu-8gb"
        assert spec["payment_type"] == "merkle"
        assert spec["ant_funding"] == "200"
        assert spec["eth_funding"] == "0.1"
        assert spec["clock_skew"] == 7200
        assert spec["forward_logs"] is True
        assert spec["network"] == "home-fttc"

    def test_rust_log_may_contain_colons_and_must_be_last(self):
        spec = parse_wasm_uploader_spec(
            "digital_ocean:lon1:10:rust_log=ant_core=debug,saorsa_core::dht=trace"
        )
        assert spec["rust_log"] == "ant_core=debug,saorsa_core::dht=trace"

    @pytest.mark.parametrize(
        "spec",
        [
            "digital_ocean:lon1:10:bootstrap_cache=false",
            "digital_ocean:lon1:10:bootstrap_cache=true",
        ],
    )
    def test_bootstrap_cache_is_rejected(self, spec):
        # bootstrap_cache selects between ant's peer cache and an explicit
        # --bootstrap invocation. A WASM client does neither -- it bootstraps from the
        # bootstraps' webrtc-direct multiaddrs -- so accepting the token would
        # silently do nothing.
        with pytest.raises(ValueError, match="unknown key 'bootstrap_cache'"):
            parse_wasm_uploader_spec(spec)

    def test_downloader_rejects_bootstrap_cache_too(self):
        with pytest.raises(ValueError, match="unknown key 'bootstrap_cache'"):
            parse_wasm_downloader_spec("digital_ocean:lon1:bootstrap_cache=false")

    def test_no_bootstrap_cache_key_in_the_parsed_spec(self):
        assert "bootstrap_cache" not in parse_wasm_uploader_spec("digital_ocean:lon1:10")

    def test_unknown_key_is_rejected(self):
        with pytest.raises(ValueError, match="unknown key 'nonsense'"):
            parse_wasm_uploader_spec("digital_ocean:lon1:10:nonsense=1")

    def test_native_parsers_still_take_bootstrap_cache(self):
        # The shared helpers must not have leaked the WASM restriction into the
        # native grammar.
        from saorsa_deploy.cmd.infra_clients import parse_uploader_spec

        assert parse_uploader_spec("digital_ocean:lon1:10:bootstrap_cache=false")[
            "bootstrap_cache"
        ] is False


# --------------------------------------------------------------------------- #
# manifest schema
# --------------------------------------------------------------------------- #
def _manifest(clients: dict, binaries: dict | None = None, **extra):
    base = {
        "name": "DEV-01",
        "kind": "testnet",
        "purpose": "p",
        "ssh_key_path": SSH,
        "testnet": {
            "binaries": binaries
            or {
                "ant_node": {"version": "1.0"},
                "ant_wasm": {"branch": "web-support", "repo_owner": "WithAutonomi"},
            },
            "bootstraps": {"count": 1},
            "nodes": {
                "per_vm": 5,
                "providers": {"digital_ocean": {"vm_count": 2, "regions": 3}},
            },
            "clients": clients,
            **extra,
        },
    }
    return Manifest.model_validate(base)


WASM_PAIR = {
    "wasm_uploaders": [{"provider": "digital_ocean", "region": "lon1", "file_size_mb": 10}],
    "wasm_downloaders": [{"provider": "digital_ocean", "region": "lon1"}],
}


class TestSchema:
    def test_wasm_pair_is_valid(self):
        m = _manifest(WASM_PAIR)
        assert len(m.testnet.clients.wasm_uploaders) == 1
        assert m.testnet.clients.has_wasm

    def test_uploader_without_downloader_is_refused(self):
        # WASM downloaders read the addresses WASM uploaders publish, so one
        # without the other has nothing to do.
        with pytest.raises(ValidationError, match="wasm_uploader AND one wasm_downloader"):
            _manifest({"wasm_uploaders": WASM_PAIR["wasm_uploaders"]})

    def test_downloader_without_uploader_is_refused(self):
        with pytest.raises(ValidationError, match="wasm_uploader AND one wasm_downloader"):
            _manifest({"wasm_downloaders": WASM_PAIR["wasm_downloaders"]})

    def test_the_two_transports_are_checked_separately(self):
        # Native uploaders + WASM downloaders would satisfy a check on the union
        # of all clients while leaving both halves with nothing to consume.
        with pytest.raises(ValidationError):
            _manifest(
                {
                    "uploaders": [
                        {"provider": "digital_ocean", "region": "lon1", "file_size_mb": 10}
                    ],
                    "wasm_downloaders": WASM_PAIR["wasm_downloaders"],
                },
                binaries={
                    "ant_node": {"version": "1.0"},
                    "ant": {"version": "0.3.0"},
                    "ant_wasm": {"branch": "web-support", "repo_owner": "WithAutonomi"},
                },
            )

    def test_ant_wasm_binary_is_required(self):
        with pytest.raises(ValidationError, match="binaries.ant_wasm is required"):
            _manifest(WASM_PAIR, binaries={"ant_node": {"version": "1.0"}})

    def test_ant_wasm_must_be_a_branch_build(self):
        # No ant-core WASM package is published as a GitHub release, so there is
        # no version to pin — the version form would name something that cannot
        # exist.
        with pytest.raises(ValidationError, match="must use the branch form"):
            _manifest(
                WASM_PAIR,
                binaries={"ant_node": {"version": "1.0"}, "ant_wasm": {"version": "0.5.1"}},
            )

    def test_native_ant_binary_not_required_for_a_wasm_only_deployment(self):
        assert _manifest(WASM_PAIR).testnet.binaries.ant is None

    def test_wasm_entry_rejects_bootstrap_cache(self):
        with pytest.raises(ValidationError):
            _manifest(
                {
                    "wasm_uploaders": [
                        {
                            "provider": "digital_ocean",
                            "region": "lon1",
                            "file_size_mb": 10,
                            "bootstrap_cache": False,
                        }
                    ],
                    "wasm_downloaders": WASM_PAIR["wasm_downloaders"],
                }
            )

    def test_forward_logs_still_requires_elastic(self):
        with pytest.raises(ValidationError, match="forward_logs requires elastic.enabled"):
            _manifest(
                {
                    "wasm_uploaders": [
                        {
                            "provider": "digital_ocean",
                            "region": "lon1",
                            "file_size_mb": 10,
                            "forward_logs": True,
                        }
                    ],
                    "wasm_downloaders": WASM_PAIR["wasm_downloaders"],
                }
            )


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
def _step(steps, head):
    return next(s for s in steps if s.argv[: len(head)] == head)


def _maybe_step(steps, head):
    return next((s for s in steps if s.argv[: len(head)] == head), None)


class TestOrchestrator:
    def test_infra_emits_wasm_flags(self):
        steps = build_deploy_steps(_manifest(WASM_PAIR))
        argv = _step(steps, ["clients", "infra"]).argv
        assert argv[argv.index("--wasm-uploader") + 1] == "digital_ocean:lon1:10"
        assert argv[argv.index("--wasm-downloader") + 1] == "digital_ocean:lon1"

    def test_wasm_spec_omits_bootstrap_cache(self):
        steps = build_deploy_steps(_manifest(WASM_PAIR))
        argv = _step(steps, ["clients", "infra"]).argv
        assert "bootstrap_cache" not in argv[argv.index("--wasm-uploader") + 1]

    def test_build_ant_wasm_step_is_emitted(self):
        steps = build_deploy_steps(_manifest(WASM_PAIR))
        argv = _step(steps, ["build", "ant-wasm"]).argv
        assert argv[argv.index("--branch-name") + 1] == "web-support"
        assert argv[argv.index("--repo-owner") + 1] == "WithAutonomi"

    def test_webrtc_address_collection_runs_before_clients_provision(self):
        # The address embeds the node's DTLS certificate hash, which only exists
        # once the node has started — so it cannot be collected at bootstrap
        # provision time, and it must exist before a WASM client is provisioned.
        steps = build_deploy_steps(_manifest(WASM_PAIR))
        argvs = [s.argv for s in steps]
        collect = argvs.index(_step(steps, ["deployments", "webrtc-addresses"]).argv)
        provision = argvs.index(_step(steps, ["clients", "provision"]).argv)
        assert collect < provision

    def test_clients_provision_carries_the_wasm_branch(self):
        steps = build_deploy_steps(_manifest(WASM_PAIR))
        argv = _step(steps, ["clients", "provision"]).argv
        assert argv[argv.index("--wasm-branch-name") + 1] == "web-support"
        assert argv[argv.index("--wasm-repo-owner") + 1] == "WithAutonomi"

    def test_native_only_deployment_is_completely_unaffected(self):
        m = _manifest(
            {
                "uploaders": [
                    {"provider": "digital_ocean", "region": "lon1", "file_size_mb": 10}
                ],
                "downloaders": [{"provider": "digital_ocean", "region": "lon1"}],
            },
            binaries={"ant_node": {"version": "1.0"}, "ant": {"version": "0.3.0"}},
        )
        steps = build_deploy_steps(m)
        assert _maybe_step(steps, ["deployments", "webrtc-addresses"]) is None
        assert _maybe_step(steps, ["build", "ant-wasm"]) is None
        argv = _step(steps, ["clients", "provision"]).argv
        assert "--wasm-branch-name" not in argv
        infra = _step(steps, ["clients", "infra"]).argv
        assert "--wasm-uploader" not in infra


# --------------------------------------------------------------------------- #
# provisioner
# --------------------------------------------------------------------------- #
class TestUnitRendering:
    def test_uploader_unit(self):
        unit = _build_wasm_unit(ROLE_WASM_UPLOAD)
        assert "Description=Ant WASM Client Uploader" in unit
        assert "ExecStart=/usr/bin/node /opt/ant-wasm-client/dist/main.js" in unit
        assert "EnvironmentFile=/etc/ant-wasm-client.env" in unit

    def test_downloader_unit(self):
        assert "Description=Ant WASM Client Downloader" in _build_wasm_unit(
            ROLE_WASM_DOWNLOAD
        )

    def test_both_roles_run_the_same_program(self):
        # One program branching on ROLE, because upload and download share the
        # whole WASM setup: module load, WebRTC polyfill, bootstrap addresses.
        up = _build_wasm_unit(ROLE_WASM_UPLOAD)
        down = _build_wasm_unit(ROLE_WASM_DOWNLOAD)
        exec_line = "ExecStart=/usr/bin/node /opt/ant-wasm-client/dist/main.js"
        assert exec_line in up and exec_line in down
