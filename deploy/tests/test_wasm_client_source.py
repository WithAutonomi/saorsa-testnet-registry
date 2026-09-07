"""The shipped TypeScript client itself (V2-1169).

Mirrors `test_client_scripts.py`, which runs the real `uploader.sh`/`downloader.sh`
rather than asserting on their text: here we typecheck the real TypeScript and run
its own unit suite. Skipped when Node is unavailable, so the suite still runs on a
machine without it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CLIENT_DIR = (
    Path(__file__).resolve().parents[1]
    / "saorsa_deploy"
    / "resources"
    / "wasm-client"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or shutil.which("npm") is None,
    reason="node/npm not available",
)


@pytest.fixture(scope="module")
def installed():
    """`npm install` once for the module (offline if the cache allows)."""
    if not (CLIENT_DIR / "node_modules").exists():
        result = subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund"],
            cwd=CLIENT_DIR,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            pytest.skip(f"npm install unavailable: {result.stderr[-500:]}")
    return CLIENT_DIR


class TestPackaging:
    def test_is_an_esm_package(self):
        # wasm-pack --target web emits ESM with an explicit init; the client
        # imports it directly, so CommonJS would not load it.
        pkg = json.loads((CLIENT_DIR / "package.json").read_text())
        assert pkg["type"] == "module"

    def test_declares_the_webrtc_polyfill(self):
        # Node has no RTCPeerConnection, and ant-core's WebRTC-Direct transport
        # needs a real one — this is the single dependency that makes the WASM
        # client runnable outside a browser.
        pkg = json.loads((CLIENT_DIR / "package.json").read_text())
        assert "node-datachannel" in pkg["dependencies"]

    def test_declares_a_wallet_library(self):
        pkg = json.loads((CLIENT_DIR / "package.json").read_text())
        assert "viem" in pkg["dependencies"]

    def test_build_artifacts_are_not_committed(self):
        ignored = (CLIENT_DIR / ".gitignore").read_text().split()
        for entry in ("node_modules/", "dist/", "pkg/"):
            assert entry in ignored


class TestSources:
    def test_polyfill_is_installed_before_wasm_init(self):
        # Order is load-bearing: ant-core resolves RTCPeerConnection off
        # globalThis when its transport starts, and initialising the module
        # first leaves it bound to nothing.
        main = (CLIENT_DIR / "src" / "main.ts").read_text()
        assert main.index("installWebRtc()") < main.index("initAntCore(")

    def test_emits_the_native_measurement_names(self):
        # Reusing `upload`/`download` is what makes record-results, the per-op
        # tables and `deployments compare` work on WASM ops unchanged.
        main = (CLIENT_DIR / "src" / "main.ts").read_text()
        assert '"upload"' in main and '"download"' in main

    def test_payload_is_not_constant(self):
        # Self-encryption is content-addressed, so a constant payload would give
        # every upload the same address and turn each one after the first into a
        # no-op store of content already on the network.
        main = (CLIENT_DIR / "src" / "main.ts").read_text()
        assert "makePayload" in main
        assert "seed" in main

    def test_wallet_returns_the_exact_total_as_a_string(self):
        # ant-core rejects the upload unless totalAmount equals the sum of the
        # quote amounts exactly, and the value can exceed Number.MAX_SAFE_INTEGER.
        wallet = (CLIENT_DIR / "src" / "wallet.ts").read_text()
        assert "total.toString()" in wallet
        assert "BigInt(q.amount)" in wallet

    def test_wallet_waits_for_the_payment_receipt(self):
        # Nodes verify the transaction on-chain during put_chunk, so returning an
        # unmined hash makes every subsequent store fail.
        wallet = (CLIENT_DIR / "src" / "wallet.ts").read_text()
        assert wallet.count("waitForTransactionReceipt") == 2


class TestBuildAndUnitTests:
    def test_typechecks(self, installed):
        result = subprocess.run(
            ["./node_modules/.bin/tsc", "-p", "tsconfig.json", "--noEmit"],
            cwd=installed,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_unit_tests_pass(self, installed):
        build = subprocess.run(
            ["./node_modules/.bin/tsc", "-p", "tsconfig.json"],
            cwd=installed,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert build.returncode == 0, build.stdout + build.stderr
        result = subprocess.run(
            # A directory argument makes node treat it as a module path; the
            # glob is what selects the test files.
            ["node", "--test", "test/*.test.mjs"],
            cwd=installed,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
