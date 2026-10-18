"""The browser-downloader client role, end to end (V2-1617).

The WASM roles run ant-core compiled to WASM under Node.js, one process per VM,
drawing random files from the address pool. That cannot measure what ADR-0023
(hot-record read spreading) changes: many REAL browsers reading the SAME file at
the SAME moment. This role does.

Modelled on test_wasm_clients.py, which is this codebase's pattern for a new
client role: roles, spec parsing, manifest validation, orchestrator emission,
provisioner rendering, and the "everything else is unchanged" guards.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from saorsa_deploy.client_roles import (
    ALL_CLIENT_ROLES,
    BROWSER_DEFAULT_VM_SIZE,
    BROWSER_MAX_INSTANCES,
    DOWNLOAD_ROLES,
    ROLE_BROWSER_DOWNLOAD,
    ROLE_DOWNLOAD,
    ROLE_UPLOAD,
    is_browser,
    is_upload,
    is_wasm,
    unit_for_role,
)
from saorsa_deploy.cmd.infra_clients import parse_browser_downloader_spec
from saorsa_deploy.config.models import Manifest
from saorsa_deploy.orchestrator import build_deploy_steps
from saorsa_deploy.orchestrator.clients import planned_client_vms
from saorsa_deploy.provisioning import browser_client as browser
from saorsa_deploy.provisioning.client import AntClientProvisioner
from saorsa_deploy.provisioning.client_clean import ALL_CLIENT_UNITS

SSH = "~/.ssh/ant_deploy"
HARNESS_DIR = Path(__file__).resolve().parents[1] / "saorsa_deploy" / "resources" / "browser-client"


# --------------------------------------------------------------------------- #
# roles
# --------------------------------------------------------------------------- #
class TestRole:
    def test_has_its_own_unit(self):
        assert unit_for_role(ROLE_BROWSER_DOWNLOAD) == "ant-browser-downloader"

    def test_is_a_download_role_but_not_a_wasm_one(self):
        # Both run the same ant-core WASM bundle, but `is_wasm` selects the
        # Node.js harness, its env file and its unit -- folding this role in
        # would provision a browser VM as a `wasm-download` one and leave
        # `clients start` looking for a unit nobody wrote.
        assert is_browser(ROLE_BROWSER_DOWNLOAD)
        assert not is_wasm(ROLE_BROWSER_DOWNLOAD)
        assert ROLE_BROWSER_DOWNLOAD in DOWNLOAD_ROLES

    def test_carries_no_wallet(self):
        # It only reads, and what it reads is named by `select-hot-files`.
        assert not is_upload(ROLE_BROWSER_DOWNLOAD)

    def test_clean_removes_its_unit(self):
        # `clients clean` is given IPs, never roles, so it stops and removes
        # EVERY role's unit. A role missing from that list leaves its service
        # running on a "cleaned" VM.
        assert ROLE_BROWSER_DOWNLOAD in ALL_CLIENT_ROLES
        assert "ant-browser-downloader" in ALL_CLIENT_UNITS

    def test_the_instance_cap_is_the_per_ip_session_quota(self):
        # A node admits at most 4 WebRTC sessions per source IPv4 address and
        # one browser client holds one session per node. The VM side keeps its
        # own copy (it validates what actually reached it); the two must agree.
        js = (HARNESS_DIR / "lib" / "limits.mjs").read_text()
        assert f"BROWSER_MAX_INSTANCES = {BROWSER_MAX_INSTANCES}" in js


# --------------------------------------------------------------------------- #
# spec-token parsing
# --------------------------------------------------------------------------- #
class TestSpecParsing:
    def test_minimal(self):
        spec = parse_browser_downloader_spec("digital_ocean:lon1")
        assert spec["role"] == ROLE_BROWSER_DOWNLOAD
        assert spec["provider"] == "digitalocean"
        assert spec["region"] == "lon1"
        assert spec["instances"] == 4
        assert spec["round_period_secs"] == 600
        assert spec["download_timeout_secs"] == 300

    def test_default_vm_size_is_a_size_up_from_the_other_roles(self):
        # Four Chromium instances each holding ~4x a 50 MiB file in WASM memory
        # do not fit in 4 GB.
        assert parse_browser_downloader_spec("digital_ocean:lon1")["vm_size"] == (
            BROWSER_DEFAULT_VM_SIZE
        )

    def test_all_options(self):
        spec = parse_browser_downloader_spec(
            "hetzner:fsn1:cpx41:instances=2:round_period_secs=300"
            ":download_timeout_secs=120:forward_logs=true"
        )
        assert spec["vm_size"] == "cpx41"
        assert spec["instances"] == 2
        assert spec["round_period_secs"] == 300
        assert spec["download_timeout_secs"] == 120
        assert spec["forward_logs"] is True

    def test_instances_over_the_quota_is_refused_naming_it(self):
        with pytest.raises(ValueError, match="WebRTC sessions per source IPv4"):
            parse_browser_downloader_spec("digital_ocean:lon1:instances=5")

    def test_zero_instances_is_refused(self):
        with pytest.raises(ValueError, match="between 1 and"):
            parse_browser_downloader_spec("digital_ocean:lon1:instances=0")

    def test_too_short_a_round_is_refused(self):
        with pytest.raises(ValueError, match="cold browser client's own startup"):
            parse_browser_downloader_spec("digital_ocean:lon1:round_period_secs=60")

    def test_a_timeout_that_can_outlive_its_round_is_refused(self):
        with pytest.raises(ValueError, match="must be < round_period_secs"):
            parse_browser_downloader_spec(
                "digital_ocean:lon1:round_period_secs=300:download_timeout_secs=300"
            )

    @pytest.mark.parametrize("key", ["clock_skew=2h", "network=home-fttc", "bootstrap_cache=true"])
    def test_options_that_would_distort_the_measurement_are_refused(self, key):
        # What is measured is WHEN each instance read and WHICH holder served
        # it. A shifted clock or a shaped link distorts exactly that.
        with pytest.raises(ValueError, match="unknown key"):
            parse_browser_downloader_spec(f"digital_ocean:lon1:{key}")

    def test_rust_log_is_refused_by_name(self):
        # `rust_log` is pulled off the end of every spec before the unknown-key
        # check, so it would otherwise be accepted here and then dropped.
        with pytest.raises(ValueError, match="no meaning on this role"):
            parse_browser_downloader_spec("digital_ocean:lon1:rust_log=debug")

    def test_a_non_integer_knob_is_refused(self):
        with pytest.raises(ValueError, match="must be an integer"):
            parse_browser_downloader_spec("digital_ocean:lon1:instances=four")

    def test_there_is_no_count_token(self):
        # `count` is manifest-only: the orchestrator expands it to repeated
        # flags, because `clients infra` allocates one client_index per flag.
        with pytest.raises(ValueError, match="unknown key 'count'"):
            parse_browser_downloader_spec("digital_ocean:lon1:count=3")


# --------------------------------------------------------------------------- #
# manifest schema
# --------------------------------------------------------------------------- #
WASM_BINARIES = {
    "ant_node": {"version": "1.0"},
    "ant_wasm": {
        "branch": "main",
        "repo_owner": "WithAutonomi",
        "features": "browser-wasm,test-utils",
    },
}


def _manifest(clients: dict, binaries: dict | None = None, **extra):
    base = {
        "name": "DEV-01",
        "kind": "testnet",
        "purpose": "p",
        "ssh_key_path": SSH,
        "testnet": {
            "binaries": binaries or WASM_BINARIES,
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


BROWSER = {"browser_downloaders": [{"provider": "digital_ocean", "region": "lon1"}]}


class TestSchema:
    def test_a_browser_tier_alone_is_valid(self):
        m = _manifest(BROWSER)
        assert len(m.testnet.clients.browser_downloaders) == 1
        assert m.testnet.clients.has_browser

    def test_it_needs_the_wasm_binary(self):
        # Same bundle as the WASM roles, run in Chromium rather than under Node.
        with pytest.raises(ValidationError, match="binaries.ant_wasm is required"):
            _manifest(BROWSER, binaries={"ant_node": {"version": "1.0"}})

    def test_it_does_not_need_the_native_ant_binary(self):
        assert _manifest(BROWSER).testnet.binaries.ant is None

    def test_there_is_no_pairing_rule(self):
        # Unlike the wasm_uploader/wasm_downloader pair: the file this tier
        # reads is named by `select-hot-files` out of what is ALREADY in the
        # pool, so the tier is added to a deployment whose uploads have already
        # happened — which is the only time a hot file exists.
        assert _manifest(BROWSER).testnet.clients.browser_downloaders

    def test_count_below_one_is_refused(self):
        with pytest.raises(ValidationError, match="count must be >= 1"):
            _manifest(
                {"browser_downloaders": [{"provider": "digital_ocean", "region": "l", "count": 0}]}
            )

    def test_instances_outside_the_quota_is_refused_naming_it(self):
        for bad in (0, BROWSER_MAX_INSTANCES + 1):
            with pytest.raises(ValidationError, match="max_connections_per_ip"):
                _manifest(
                    {
                        "browser_downloaders": [
                            {"provider": "digital_ocean", "region": "l", "instances": bad}
                        ]
                    }
                )

    def test_too_short_a_round_is_refused(self):
        with pytest.raises(ValidationError, match="round_period_secs must be >="):
            _manifest(
                {
                    "browser_downloaders": [
                        {"provider": "digital_ocean", "region": "l", "round_period_secs": 119}
                    ]
                }
            )

    def test_a_timeout_that_can_outlive_its_round_is_refused(self):
        with pytest.raises(ValidationError, match="must be < round_period_secs"):
            _manifest(
                {
                    "browser_downloaders": [
                        {
                            "provider": "digital_ocean",
                            "region": "l",
                            "round_period_secs": 300,
                            "download_timeout_secs": 300,
                        }
                    ]
                }
            )

    def test_the_defaults_are_runnable(self):
        entry = _manifest(BROWSER).testnet.clients.browser_downloaders[0]
        assert entry.instances == BROWSER_MAX_INSTANCES
        assert entry.round_period_secs == 600
        assert entry.download_timeout_secs == 300
        assert entry.count == 1

    def test_forward_logs_still_requires_elastic(self):
        with pytest.raises(ValidationError, match="forward_logs requires elastic.enabled"):
            _manifest(
                {
                    "browser_downloaders": [
                        {"provider": "digital_ocean", "region": "l", "forward_logs": True}
                    ]
                }
            )

    @pytest.mark.parametrize("key", ["clock_skew", "network", "rust_log"])
    def test_measurement_distorting_keys_are_not_fields(self, key):
        with pytest.raises(ValidationError):
            _manifest(
                {"browser_downloaders": [{"provider": "digital_ocean", "region": "l", key: "2h"}]}
            )


class TestWasmFeatures:
    def test_features_default_to_browser_wasm_alone(self):
        # `test-utils` links a deterministic mock node, so a production bundle
        # must not carry it.
        m = _manifest(
            BROWSER,
            binaries={
                "ant_node": {"version": "1.0"},
                "ant_wasm": {"branch": "main", "repo_owner": "WithAutonomi"},
            },
        )
        assert m.testnet.binaries.ant_wasm.features is None
        assert m.testnet.binaries.ant_wasm.resolved_features == "browser-wasm"

    def test_features_are_carried_verbatim(self):
        assert _manifest(BROWSER).testnet.binaries.ant_wasm.features == "browser-wasm,test-utils"

    def test_features_are_not_a_field_on_the_other_binaries(self):
        with pytest.raises(ValidationError):
            _manifest(
                BROWSER,
                binaries={
                    "ant_node": {"version": "1.0", "features": "x"},
                    "ant_wasm": {"branch": "main"},
                },
            )


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
def _step(steps, head):
    return next(s for s in steps if s.argv[: len(head)] == head)


def _maybe_step(steps, head):
    return next((s for s in steps if s.argv[: len(head)] == head), None)


class TestOrchestrator:
    def test_infra_emits_the_flag(self):
        argv = _step(build_deploy_steps(_manifest(BROWSER)), ["clients", "infra"]).argv
        assert argv[argv.index("--browser-downloader") + 1] == "digital_ocean:lon1"

    def test_count_expands_to_one_flag_per_vm(self):
        # `clients infra` allocates one consecutive client_index per repeated
        # flag, and the argv is what the step journal compares — so it has to be
        # a literal description of the VMs that will exist.
        m = _manifest(
            {"browser_downloaders": [{"provider": "digital_ocean", "region": "lon1", "count": 3}]}
        )
        argv = _step(build_deploy_steps(m), ["clients", "infra"]).argv
        assert argv.count("--browser-downloader") == 3

    def test_non_default_knobs_appear_in_the_token(self):
        m = _manifest(
            {
                "browser_downloaders": [
                    {
                        "provider": "digital_ocean",
                        "region": "lon1",
                        "instances": 2,
                        "round_period_secs": 300,
                        "download_timeout_secs": 120,
                    }
                ]
            }
        )
        argv = _step(build_deploy_steps(m), ["clients", "infra"]).argv
        assert argv[argv.index("--browser-downloader") + 1] == (
            "digital_ocean:lon1:instances=2:round_period_secs=300:download_timeout_secs=120"
        )

    def test_a_default_entry_emits_the_short_token(self):
        argv = _step(build_deploy_steps(_manifest(BROWSER)), ["clients", "infra"]).argv
        token = argv[argv.index("--browser-downloader") + 1]
        assert "instances=" not in token and "round_period_secs=" not in token

    def test_the_wasm_build_step_carries_the_features(self):
        argv = _step(build_deploy_steps(_manifest(BROWSER)), ["build", "ant-wasm"]).argv
        assert argv[argv.index("--features") + 1] == "browser-wasm,test-utils"

    def test_clients_provision_carries_the_features(self):
        argv = _step(build_deploy_steps(_manifest(BROWSER)), ["clients", "provision"]).argv
        assert argv[argv.index("--wasm-features") + 1] == "browser-wasm,test-utils"

    def test_webrtc_addresses_runs_before_clients_provision(self):
        # A browser client bootstraps from the bootstrap tier's WebRTC-Direct
        # multiaddrs and has no other way into the network. The address embeds
        # the DTLS certhash, which exists only once a node has started.
        steps = build_deploy_steps(_manifest(BROWSER))
        argvs = [s.argv for s in steps]
        collect = argvs.index(_step(steps, ["deployments", "webrtc-addresses"]).argv)
        provision = argvs.index(_step(steps, ["clients", "provision"]).argv)
        assert collect < provision

    def test_a_default_features_manifest_emits_no_features_flag(self):
        # So every existing WASM deployment's argv -- and the `up` journal hash
        # taken from it -- is byte-for-byte unchanged.
        m = _manifest(
            {
                "wasm_uploaders": [
                    {"provider": "digital_ocean", "region": "lon1", "file_size_mb": 10}
                ],
                "wasm_downloaders": [{"provider": "digital_ocean", "region": "lon1"}],
            },
            binaries={
                "ant_node": {"version": "1.0"},
                "ant_wasm": {"branch": "main", "repo_owner": "WithAutonomi"},
            },
        )
        steps = build_deploy_steps(m)
        assert "--features" not in _step(steps, ["build", "ant-wasm"]).argv
        assert "--wasm-features" not in _step(steps, ["clients", "provision"]).argv

    def test_a_native_only_deployment_is_completely_unaffected(self):
        m = _manifest(
            {
                "uploaders": [{"provider": "digital_ocean", "region": "lon1", "file_size_mb": 10}],
                "downloaders": [{"provider": "digital_ocean", "region": "lon1"}],
            },
            binaries={"ant_node": {"version": "1.0"}, "ant": {"version": "0.3.0"}},
        )
        steps = build_deploy_steps(m)
        assert _maybe_step(steps, ["deployments", "webrtc-addresses"]) is None
        assert _maybe_step(steps, ["build", "ant-wasm"]) is None
        assert "--browser-downloader" not in _step(steps, ["clients", "infra"]).argv


# --------------------------------------------------------------------------- #
# extension plan (`kind: clients`)
# --------------------------------------------------------------------------- #
class TestExtensionPlan:
    def _extension(self, entries):
        return Manifest.model_validate(
            {
                "name": "browsers",
                "kind": "clients",
                "purpose": "p",
                "ssh_key_path": SSH,
                "deployment": "DEV-01",
                "clients": {"browser_downloaders": entries},
            }
        )

    def test_each_expanded_vm_is_planned(self):
        vms = planned_client_vms(
            self._extension([{"provider": "digital_ocean", "region": "lon1", "count": 3}])
        )
        assert [v["client_index"] for v in vms] == [1, 2, 3]
        assert all(v["role"] == ROLE_BROWSER_DOWNLOAD for v in vms)
        assert vms[2]["hostname"] == "DEV-01-browsers-ant-client-browser-download-lon1-3"

    def test_the_planned_vm_size_is_this_role_s_default(self):
        vms = planned_client_vms(self._extension([{"provider": "digital_ocean", "region": "l"}]))
        assert vms[0]["vm_size"] == BROWSER_DEFAULT_VM_SIZE

    def test_indexes_continue_from_the_deployment_s_own(self):
        vms = planned_client_vms(
            self._extension([{"provider": "digital_ocean", "region": "l", "count": 2}]),
            {ROLE_BROWSER_DOWNLOAD: 7},
        )
        assert [v["client_index"] for v in vms] == [8, 9]


# --------------------------------------------------------------------------- #
# provisioner rendering
# --------------------------------------------------------------------------- #
class TestProvisioning:
    def test_the_unit_runs_the_harness_and_restarts_always(self):
        unit = browser.build_systemd_unit()
        assert "ExecStart=/usr/bin/node /opt/ant-browser-downloader/harness.mjs" in unit
        assert "EnvironmentFile=/etc/ant-browser-downloader.env" in unit
        # A round the harness missed because it was not running is a hole in the
        # measurement that nothing can fill in afterwards.
        assert "Restart=always" in unit

    def test_the_unit_points_playwright_at_the_shared_browser_path(self):
        # Installed as root at provision time into /opt/ms-playwright; without
        # the variable the service looks under $HOME and finds no browser.
        assert "Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright" in (
            browser.build_systemd_unit()
        )

    def test_playwright_is_pinned_and_installs_chromium_with_deps(self):
        cmd = browser.build_playwright_install_command()
        assert f"playwright@{browser.PLAYWRIGHT_VERSION}" in cmd
        # --with-deps pulls the ~100 shared libraries a headless Chromium links;
        # without them the browser exits on launch with a missing-library error
        # that reads like a harness bug.
        assert "install --with-deps chromium" in cmd
        # A partial install must fail the op, not leave every round reporting a
        # launch failure -- and the check is the real one (ask the package where
        # its Chromium is and test the file), not `install --dry-run`, which
        # prints what it would do and exits 0 whatever is on disk.
        assert "PLAYWRIGHT:FAILED" in cmd and "exit 1" in cmd
        assert "executablePath()" in cmd
        assert "--dry-run" not in cmd
        assert "PLAYWRIGHT:NO_CHROMIUM" in cmd

    def test_the_bundle_install_verifies_the_wasm_is_there(self):
        cmd = browser.build_bundle_install_command("https://example/b.tar.gz")
        assert "test -f /opt/ant-browser-downloader/bundle/pkg/ant_core_bg.wasm" in cmd
        # Always re-fetched, never presence-gated: the bundle is namespaced by
        # deployment name and `build ant-wasm` overwrites it, so a re-provision
        # after a rebuild has to pick up the new one.
        assert cmd.startswith("rm -rf /opt/ant-browser-downloader/bundle")

    def test_the_trace_hook_is_checked_on_the_installed_bytes(self):
        # Without `setBrowserTrace` every round runs, succeeds and reports zero
        # stalls, zero refusals and no first reads -- which reads as a clean
        # network rather than as a missing instrument.
        cmd = browser.build_trace_hook_check_command()
        assert "setBrowserTrace" in cmd
        assert "test-utils" in cmd
        assert "exit 1" in cmd

    def _provisioner(self, **kwargs):
        return AntClientProvisioner(
            clients=[],
            bootstrap_peers=["1.2.3.4:10000"],
            aws_access_key_id="",
            aws_secret_access_key="",
            addresses_s3_bucket="b",
            addresses_s3_prefix="p",
            ssh_key_path=SSH,
            deployment_name="DEV-01",
            **kwargs,
        )

    def test_the_env_knobs_come_from_the_client_s_own_spec(self):
        env = AntClientProvisioner._browser_env(
            {"instances": 2, "round_period_secs": 300, "download_timeout_secs": 120}
        )
        assert env == {
            "instances": 2,
            "round_period_secs": 300,
            "download_timeout_secs": 120,
        }

    def test_the_env_file_quotes_the_multiaddr_list_and_the_url(self):
        body = browser.render_env_file(
            {"BOOTSTRAP_WEBRTC_MULTIADDRS": "a,b", "INSTANCES": 4},
            quoted=("BOOTSTRAP_WEBRTC_MULTIADDRS",),
        )
        assert 'BOOTSTRAP_WEBRTC_MULTIADDRS="a,b"' in body
        assert "INSTANCES=4" in body

    def test_log_forwarding_names_the_role(self):
        # A role missing from this map forwards NOTHING while reporting nothing
        # wrong, which is how both WASM roles silently forwarded nothing.
        globs, label = AntClientProvisioner._FORWARD_ROLE_FILES[ROLE_BROWSER_DOWNLOAD]
        assert label == "browser_downloader"
        assert browser.BROWSER_STDOUT_LOG in globs
        assert browser.BROWSER_STDERR_LOG in globs

    def test_the_native_roles_forwarding_is_unchanged(self):
        for role in (ROLE_UPLOAD, ROLE_DOWNLOAD):
            globs, _ = AntClientProvisioner._FORWARD_ROLE_FILES[role]
            assert all("browser" not in g for g in globs)


# --------------------------------------------------------------------------- #
# the harness's own sources and the shared trace fixture
# --------------------------------------------------------------------------- #
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "browser-trace"


class TestHarnessSources:
    def test_the_harness_is_plain_esm_with_no_build_step(self):
        # Copied onto the VM exactly as committed, which is what lets `node
        # --test` run the same bytes here and there.
        pkg = json.loads((HARNESS_DIR / "package.json").read_text())
        assert pkg["type"] == "module"
        assert (HARNESS_DIR / "harness.mjs").exists()

    def test_playwright_is_the_pinned_dependency(self):
        pkg = json.loads((HARNESS_DIR / "package.json").read_text())
        assert pkg["dependencies"]["playwright"] == browser.PLAYWRIGHT_VERSION

    def test_node_modules_is_ignored_and_excluded_from_the_wheel(self):
        # hatchling honours only the ROOT .gitignore, so a nested one alone put
        # 12,478 node_modules entries into the saorsa-deploy wheel once already.
        assert "node_modules/" in (HARNESS_DIR / ".gitignore").read_text().split()
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        assert "saorsa_deploy/resources/browser-client/node_modules" in pyproject

    def test_the_trace_hook_is_installed_before_the_client_is_constructed(self):
        # Order is load-bearing: the bootstrap dials are what the stall and
        # refusal counts are read off, and a hook installed afterwards misses
        # them entirely.
        page = (HARNESS_DIR / "lib" / "page.mjs").read_text()
        assert page.index("setBrowserTrace") < page.index("new mod.BrowserNetworkClient")

    def test_the_client_is_closed_and_freed(self):
        # A leaked client holds its WebRTC sessions open, and the next round's
        # instance would then be the fifth session from this address.
        page = (HARNESS_DIR / "lib" / "page.mjs").read_text()
        assert "client.close()" in page and "client.free()" in page

    def test_the_measurement_is_its_own(self):
        influx = (HARNESS_DIR / "lib" / "influx.mjs").read_text()
        assert 'MEASUREMENT = "browser_download"' in influx

    def test_the_bucket_default_is_the_wasm_bucket(self):
        from saorsa_deploy.provisioning.monitoring import INFLUXDB_WASM_BUCKET

        config = (HARNESS_DIR / "lib" / "config.mjs").read_text()
        assert f'INFLUX_BUCKET || "{INFLUXDB_WASM_BUCKET}"' in config

    def test_the_summary_line_is_grep_able_in_a_fixed_order(self):
        logger = (HARNESS_DIR / "lib" / "logger.mjs").read_text()
        for key in ("round=", "instance=", "status=", "kind=", "total_ms=", "address="):
            assert key in logger

    def test_the_page_has_a_backstop_deadline_outside_itself(self):
        # The in-page race covers the download only. A `connect()` that never
        # settles would leave `page.evaluate` pending forever, and the overrun
        # guard would then turn every later round for that instance into a skip
        # — one hung dial silently ending the instance's contribution for the
        # life of the deployment.
        harness = (HARNESS_DIR / "harness.mjs").read_text()
        assert "PAGE_GRACE_MS" in harness
        assert "Promise.race" in harness

    def test_traces_are_never_deleted_by_the_harness(self):
        # The traces ARE the dataset this role produces; a rotation policy here
        # would discard the evidence for the round somebody is asking about.
        harness = (HARNESS_DIR / "harness.mjs").read_text()
        assert "Never deleted by the harness" in harness


class TestTraceFixture:
    """The hand-checked expectations the harness's own tests assert against.

    Pinned here as well as in `expected.json` so the Python suite states the
    values independently: the node tests prove the implementation produces
    them, and these prove nobody edited the expectations to match a change in
    the implementation.
    """

    def test_the_fixture_and_its_expectations_are_committed(self):
        assert (FIXTURES / "hot-read.jsonl").exists()
        assert (FIXTURES / "expected.json").exists()

    def test_the_expected_derived_values(self):
        expected = json.loads((FIXTURES / "expected.json").read_text())
        # Two dials hit the ~10 s channel-open timeout without opening a
        # channel (dial-2 at 10012 ms, dial-3 at 9340 ms). dial-4 failed in
        # 74 ms, which is a refusal; dial-5 failed at 9600 ms having opened its
        # channel, which is an admission failure.
        assert expected["stalls"] == 2
        # dial-4 alone; counted separately from a stall so a per-IP quota
        # mistake cannot be read as a NAT'd fleet.
        assert expected["refused_dials"] == 1
        assert expected["first_reads"] == [
            {"chunk": "chunk01", "holder": "peerA", "known_holders": 3},
            {"chunk": "chunk02", "holder": "peerE", "known_holders": 1},
            {"chunk": "chunk03", "holder": "peerA", "known_holders": 0},
            {"chunk": "chunk06", "holder": "peerG", "known_holders": 4},
        ]

    def test_the_fixture_exercises_every_decision(self):
        lines = [
            json.loads(line)
            for line in (FIXTURES / "hot-read.jsonl").read_text().splitlines()
            if line.strip()
        ]
        header = lines[0]
        assert header["header"] is True
        assert {"round_id", "instance", "hot_file_index", "address", "seeds", "boundary"} <= set(
            header
        )
        records = lines[1:]
        # A fallback read (a second GET for a chunk already read), a PUT rpc, a
        # peer-less GET and a candidate record carrying its set in `detail` are
        # all present — each is a case `first_reads` has to decide.
        assert sum(1 for r in records if r.get("target") == "chunk01") >= 5
        assert any("PUT" in str(r.get("detail")) for r in records)
        assert any(r.get("operation") == "rpc" and "peer" not in r for r in records)
        assert any(isinstance(r.get("detail"), dict) for r in records)


BOOTSTRAP_ADDR = (
    "/ip4/1.2.3.4/udp/42768/webrtc-direct/certhash/"
    "uEiAHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBw/p2p/" + "ab" * 32
)


def _browser_env(**overrides):
    env = {
        "role": ROLE_BROWSER_DOWNLOAD,
        "file_size_kb": 0,
        "region": "lon1",
        "client_index": 1,
        "payment_type": "",
        "secret_key": "",
        "heaptrack": "0",
        "bootstrap_cache": "true",
        "forward_logs": "false",
        "rust_log": "",
        "upload_presigned_url": "",
        "instances": 4,
        "round_period_secs": 600,
        "download_timeout_secs": 300,
    }
    env.update(overrides)
    return {"1.2.3.4": env}


class TestBrowserOps:
    """The pyinfra ops `clients provision` adds for a browser-role host."""

    def _provisioner(self, **overrides):
        kwargs = dict(
            clients=[],
            bootstrap_peers=["1.2.3.4:10000"],
            aws_access_key_id="k",
            aws_secret_access_key="s",
            addresses_s3_bucket="bucket",
            addresses_s3_prefix="prefix",
            deployment_name="DEV-01",
            console=MagicMock(),
        )
        kwargs.update(overrides)
        return AntClientProvisioner(**kwargs)

    def _run(self, env=None, **overrides):
        provisioner = self._provisioner(
            wasm_bundle_url="https://example/bundle.tar.gz",
            bootstrap_webrtc_multiaddrs=[
                BOOTSTRAP_ADDR,
                BOOTSTRAP_ADDR.replace("1.2.3.4", "5.6.7.8"),
            ],
            **overrides,
        )
        host = MagicMock()
        host.name = "1.2.3.4"
        state = MagicMock()
        state.inventory.get_active_hosts.return_value = [host]
        with patch("saorsa_deploy.provisioning.client.add_op") as add_op:
            provisioner._add_browser_ops(state, env or _browser_env())
        return [c.kwargs for c in add_op.call_args_list]

    def _commands(self, ops, needle):
        return " ".join(" ".join(k.get("commands", [])) for k in ops if needle in k.get("name", ""))

    def test_a_deployment_with_no_browser_clients_is_untouched(self):
        provisioner = self._provisioner()
        with patch("saorsa_deploy.provisioning.client.add_op") as add_op:
            provisioner._add_browser_ops(MagicMock(), {"1.2.3.4": {"role": ROLE_UPLOAD}})
        add_op.assert_not_called()
        assert provisioner.browser_clients == []

    def test_a_missing_bundle_fails_before_any_op(self):
        provisioner = self._provisioner(bootstrap_webrtc_multiaddrs=[BOOTSTRAP_ADDR])
        with pytest.raises(RuntimeError, match="test-utils"):
            provisioner._add_browser_ops(MagicMock(), _browser_env())

    def test_missing_bootstrap_addresses_fail_before_any_op(self):
        provisioner = self._provisioner(wasm_bundle_url="https://example/b.tar.gz")
        with pytest.raises(RuntimeError, match="deployments webrtc-addresses"):
            provisioner._add_browser_ops(MagicMock(), _browser_env())

    def test_installs_node_playwright_the_bundle_and_the_harness(self):
        names = [k["name"] for k in self._run()]
        assert any("Install Node.js" in n for n in names)
        assert any("Install Playwright" in n for n in names)
        assert any("Install WASM bundle for the browser harness" in n for n in names)
        assert any("Verify the bundle carries the browser trace hook" in n for n in names)
        assert "Upload harness.mjs" in names
        assert "Upload lib/derive.mjs" in names
        # package.json goes up BEFORE the npm install, so npm has a manifest to
        # install into rather than synthesising one.
        assert names.index("Upload package.json") < next(
            i for i, n in enumerate(names) if "Install Playwright" in n
        )

    def test_every_library_module_is_uploaded(self):
        uploaded = {
            k["name"].removeprefix("Upload lib/")
            for k in self._run()
            if k.get("name", "").startswith("Upload lib/")
        }
        on_disk = {p.name for p in (HARNESS_DIR / "lib").glob("*.mjs")}
        # A module the provisioner forgets is an import that fails on the VM and
        # nowhere else.
        assert uploaded == on_disk

    def test_the_env_file_carries_every_knob_the_harness_reads(self):
        cmds = self._commands(self._run(), "env file")
        for key in (
            "TESTNET_NAME=DEV-01",
            "HOST_ROLE=browser_downloader",
            "REGION=lon1",
            "INSTANCES=4",
            "ROUND_PERIOD_SECS=600",
            "DOWNLOAD_TIMEOUT_SECS=300",
            "BOOTSTRAP_WEBRTC_MULTIADDRS=",
            "ADDRESSES_BASE_URL=",
            "TRACE_DIR=",
            "FORWARD_LOGS=",
        ):
            assert key in cmds, key

    def test_the_env_file_carries_every_bootstrap_address(self):
        # A failed endpoint gets a 30-minute cooldown inside ant-core, so a
        # single dead bootstrap would strand the client for that long.
        cmds = self._commands(self._run(), "env file")
        assert cmds.count("/webrtc-direct/") == 2

    def test_the_knobs_are_this_vm_s_own(self):
        cmds = self._commands(
            self._run(_browser_env(instances=2, round_period_secs=300, download_timeout_secs=120)),
            "env file",
        )
        assert "INSTANCES=2" in cmds
        assert "ROUND_PERIOD_SECS=300" in cmds
        assert "DOWNLOAD_TIMEOUT_SECS=120" in cmds

    def test_the_influx_bucket_is_the_wasm_bucket(self):
        from saorsa_deploy.provisioning.monitoring import INFLUXDB_WASM_BUCKET

        cmds = self._commands(
            self._run(influx_url="http://1.2.3.4:8086", influx_token="t"), "env file"
        )
        assert f"INFLUX_BUCKET={INFLUXDB_WASM_BUCKET}" in cmds

    def test_no_influx_credentials_means_no_influx_keys(self):
        cmds = self._commands(self._run(), "env file")
        assert "INFLUX_URL=" not in cmds

    def test_the_env_file_is_not_world_readable(self):
        # It carries the InfluxDB write token.
        cmds = self._commands(self._run(), "env file")
        assert "chmod 600 /etc/ant-browser-downloader.env" in cmds

    def test_the_unit_written_is_this_role_s(self):
        cmds = self._commands(self._run(), "env file")
        assert "/etc/systemd/system/ant-browser-downloader.service" in cmds
        assert "systemctl daemon-reload" in cmds

    def test_the_native_unit_writer_skips_browser_hosts(self):
        # The native writer's role case block has no arm for this role, so a
        # host left in would be given an `ant-downloader.service` that `clients
        # start` cannot find (it maps the real role to its unit). That happened
        # to both WASM roles once already.
        mixed = {
            "1.1.1.1": {"role": ROLE_UPLOAD},
            "2.2.2.2": {"role": ROLE_DOWNLOAD},
            "3.3.3.3": {"role": ROLE_BROWSER_DOWNLOAD},
            "4.4.4.4": {"role": "wasm-download"},
        }
        assert set(AntClientProvisioner._native_only(mixed)) == {"1.1.1.1", "2.2.2.2"}


class TestResultsPipelineIsUntouched:
    def test_the_role_implies_no_upload_download_transport(self):
        # It writes `browser_download` and never an `upload` or a `download`, so
        # it has no row for a transport label to go on. `native` -- what an
        # `is_wasm` test alone gives -- would be the wrong label on a browser
        # host the moment anything started reading its operations.
        from saorsa_deploy.client_transport import NATIVE, WASM, transport_for_role

        assert transport_for_role(ROLE_BROWSER_DOWNLOAD) is None
        assert transport_for_role(ROLE_DOWNLOAD) == NATIVE
        assert transport_for_role("wasm-download") == WASM

    def test_a_browser_host_is_absent_from_the_transport_map(self):
        from saorsa_deploy.client_transport import transport_map_from_client_state

        state = {
            "clients": [
                {"role": ROLE_DOWNLOAD, "region": "lon1", "client_index": 1},
                {"role": ROLE_BROWSER_DOWNLOAD, "region": "lon1", "client_index": 1},
            ]
        }
        hosts = transport_map_from_client_state("DEV-01", state)
        assert hosts == {"DEV-01-ant-client-download-lon1-1": "native"}
