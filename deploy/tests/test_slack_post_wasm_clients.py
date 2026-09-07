"""Slack post rendering of the WASM client tier (V2-1169).

Before this split, `_clients_block` branched on `role == "upload"` and let
everything else fall through — so a `wasm-upload` client was rendered as
`• download`. A post that ran both transports also listed them as one
undifferentiated set, which misreports the run: WASM and native results are not
comparable like-for-like.
"""

from saorsa_deploy.cmd.slack_post import _clients_block

NATIVE = [
    {"role": "upload", "region": "lon1", "vm_size": "s-2vcpu-4gb", "file_size_mb": 10},
    {"role": "download", "region": "lon1", "vm_size": "s-2vcpu-4gb"},
]
WASM = [
    {"role": "wasm-upload", "region": "lon1", "vm_size": "s-2vcpu-4gb", "file_size_mb": 10},
    {"role": "wasm-download", "region": "nyc1", "vm_size": "s-2vcpu-4gb"},
]


def _text(clients):
    return _clients_block(clients)["text"]["text"]


class TestNativeOnlyIsUnchanged:
    def test_exact_output(self):
        # A golden: every existing testnet's post must render byte-identically,
        # so the new grouping cannot leak into deployments that have no WASM
        # clients.
        assert _text(NATIVE) == (
            "*Clients:*\n"
            "• upload — lon1 (s-2vcpu-4gb, 10MB files) — payment: default\n"
            "• download — lon1 (s-2vcpu-4gb)"
        )

    def test_no_transport_headings(self):
        assert "_Native" not in _text(NATIVE)
        assert "WASM" not in _text(NATIVE)


class TestWasmIsListedSeparately:
    def test_both_headings_present(self):
        out = _text(NATIVE + WASM)
        assert "_Native (QUIC):_" in out
        assert "_WASM (WebRTC-Direct):_" in out

    def test_native_entries_precede_the_wasm_ones(self):
        out = _text(NATIVE + WASM)
        assert out.index("_Native (QUIC):_") < out.index("_WASM (WebRTC-Direct):_")

    def test_a_wasm_uploader_is_not_reported_as_a_downloader(self):
        # The actual regression: `role == "upload"` was False for "wasm-upload",
        # so it took the download branch and the post claimed the deployment had
        # two downloaders and no WASM uploader.
        wasm_section = _text(NATIVE + WASM).split("_WASM (WebRTC-Direct):_")[1]
        assert "• upload — lon1 (s-2vcpu-4gb, 10MB files)" in wasm_section
        assert wasm_section.count("• upload") == 1
        assert wasm_section.count("• download") == 1

    def test_every_client_appears_exactly_once(self):
        out = _text(NATIVE + WASM)
        assert out.count("• upload") == 2
        assert out.count("• download") == 2

    def test_wasm_only_deployment_still_gets_its_heading(self):
        out = _text(WASM)
        assert "_WASM (WebRTC-Direct):_" in out
        assert "_Native (QUIC):_" not in out


class TestMarkersThatDoNotApplyToWasm:
    def test_bootstrap_cache_marker_is_suppressed_for_wasm(self):
        # bootstrap_cache selects between ant's peer cache and an explicit
        # --bootstrap. A WASM client does neither — it seeds from the
        # deployment's webrtc-direct addresses — so a marker here would describe
        # a setting with no effect.
        c = [{**WASM[0], "bootstrap_cache": False}]
        native_equiv = [{**NATIVE[0], "bootstrap_cache": False}]
        assert _bootstrap_marker_present(_text(native_equiv))
        assert not _bootstrap_marker_present(_text(c))

    def test_shared_markers_still_render_for_wasm(self):
        c = [{**WASM[0], "clock_skew": 7200, "forward_logs": True}]
        out = _text(c)
        assert "clock-skew" in out
        assert "📤" in out


def _bootstrap_marker_present(text: str) -> bool:
    from saorsa_deploy.cmd.slack_post import _bootstrap_cache_marker

    marker = _bootstrap_cache_marker(False).strip()
    return bool(marker) and marker in text
