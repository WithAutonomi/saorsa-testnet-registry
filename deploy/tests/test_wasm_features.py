"""The `ant_wasm.features` build option, and the `test-utils` precondition.

`wasm-pack build --features browser-wasm` was hard-coded. The
browser-downloader role needs a bundle that also carries `test-utils`, because
that is the only build with `setBrowserTrace` — the per-dial trace hook its
entire output is derived from. `test-utils` also links a deterministic mock
node, so it emphatically cannot become the default.

The precondition is what stops a cohort billing while reporting nothing: on a
bundle without the hook every round runs, SUCCEEDS, and reports zero stalls,
zero refused dials and no first reads.
"""

import pytest

from saorsa_deploy.cmd.provision_clients import _require_trace_features
from saorsa_deploy.provisioning.build_ant_wasm import AntWasmBuilder
from saorsa_deploy.wasm_build import (
    DEFAULT_FEATURES,
    TEST_UTILS_FEATURE,
    WASM_PACK_ARGV,
    build_info_command,
    features_include,
    shell,
    wasm_pack_argv,
)


class TestFeatureString:
    def test_the_default_is_browser_wasm_alone(self):
        assert DEFAULT_FEATURES == "browser-wasm"
        assert not features_include(None, TEST_UTILS_FEATURE)

    def test_membership_is_by_feature_not_by_substring(self):
        # `test-utils-extra` added upstream must not read as `test-utils`, and
        # `browser-wasm` must not read as `wasm`.
        assert features_include("browser-wasm,test-utils", "test-utils")
        assert not features_include("browser-wasm,test-utils-extra", "test-utils")
        assert not features_include("browser-wasm", "wasm")

    def test_whitespace_is_tolerated(self):
        assert features_include("browser-wasm, test-utils", "test-utils")


class TestWasmPackArgv:
    def test_the_default_invocation_is_unchanged(self):
        assert WASM_PACK_ARGV == wasm_pack_argv()
        assert list(WASM_PACK_ARGV) == [
            "wasm-pack",
            "build",
            "--target",
            "web",
            "ant-core",
            "--no-default-features",
            "--features",
            "browser-wasm",
        ]

    def test_features_are_passed_verbatim(self):
        argv = wasm_pack_argv("browser-wasm,test-utils")
        assert argv[-2:] == ("--features", "browser-wasm,test-utils")

    def test_the_feature_list_is_replaced_not_appended_to(self):
        # So a caller asking for test-utils has to name browser-wasm too, and
        # forgetting it fails loudly at ant-core's own `compile_error!` rather
        # than quietly producing a native-feature bundle.
        assert wasm_pack_argv("test-utils").count("--features") == 1
        assert "browser-wasm" not in wasm_pack_argv("test-utils")

    def test_no_default_features_is_never_dropped(self):
        assert "--no-default-features" in wasm_pack_argv("browser-wasm,test-utils")


class TestRemoteBuilder:
    def _builder(self, features=None):
        return AntWasmBuilder(
            name="DEV-01",
            ip="1.2.3.4",
            ssh_key_path="~/.ssh/id_rsa",
            repo_owner="WithAutonomi",
            branch_name="main",
            features=features,
        )

    def test_features_default_to_browser_wasm(self):
        assert self._builder().features == DEFAULT_FEATURES

    def test_the_build_command_carries_the_features(self):
        builder = self._builder("browser-wasm,test-utils")
        assert "--features browser-wasm,test-utils" in shell(wasm_pack_argv(builder.features))

    def test_the_features_are_recorded_in_the_build_provenance(self):
        # A bundle is identified by its commit PLUS its features: the same
        # commit built two ways produces two different artefacts, and only one
        # of them carries the trace hook.
        cmd = build_info_command(
            checkout_dir="/root/ant-client",
            client_src_dir="/root/wasm-client",
            dest_path="/root/build-info.json",
            ref="main",
            repo_owner="WithAutonomi",
            features="browser-wasm,test-utils",
        )
        assert '"features": "browser-wasm,test-utils"' in cmd

    def test_the_provenance_defaults_too(self):
        cmd = build_info_command(
            checkout_dir="/c",
            client_src_dir="/s",
            dest_path="/d",
            ref="main",
            repo_owner="WithAutonomi",
        )
        assert '"features": "browser-wasm"' in cmd


class _Console:
    def __init__(self):
        self.lines = []

    def print(self, line=""):
        self.lines.append(str(line))

    @property
    def text(self):
        return "\n".join(self.lines)


class _Args:
    def __init__(self, wasm_features=None):
        self.wasm_features = wasm_features


class TestProvisionPrecondition:
    def test_a_test_utils_bundle_passes(self):
        console = _Console()
        _require_trace_features(_Args("browser-wasm,test-utils"), console)
        assert console.lines == []

    def test_the_default_bundle_is_refused_with_exit_2(self):
        console = _Console()
        with pytest.raises(SystemExit) as exc:
            _require_trace_features(_Args(None), console)
        assert exc.value.code == 2

    def test_the_refusal_says_what_would_happen_and_how_to_fix_it(self):
        console = _Console()
        with pytest.raises(SystemExit):
            _require_trace_features(_Args("browser-wasm"), console)
        text = console.text
        assert "setBrowserTrace" in text
        assert "zero stalls" in text
        assert "browser-wasm,test-utils" in text
        assert "build ant-wasm" in text

    def test_a_near_miss_feature_name_is_still_refused(self):
        console = _Console()
        with pytest.raises(SystemExit):
            _require_trace_features(_Args("browser-wasm,test-utils-extra"), console)
