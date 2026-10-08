"""Sized address lines: `<address> <bytes>` and cap-aware downloader picking.

The address pool is shared per deployment, and the WASM downloader lists the
whole prefix, so it drew the native 1000 MB uploads and ant-core's browser cap
rejected each one *after* resolving its data map (`invalid public file size
1048576000`, DEV-03 run 588). The bare-address format gave the downloader no
way to know. Uploaders now publish the size, and a downloader with a bound
skips over-cap entries before fetching anything.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.client_roles import ROLE_WASM_DOWNLOAD, ROLE_WASM_UPLOAD, WASM_MAX_FILE_BYTES
from saorsa_deploy.provisioning.client import AntClientProvisioner

SCRIPTS_DIR = Path(__file__).parent.parent / "saorsa_deploy" / "resources" / "scripts"

CAP = 1_000_000_000
OVER = 1_048_576_000  # the native 1000 MiB uploader
UNDER = 20_971_520  # 20 MiB


def _extract_function(script: str, func_name: str) -> str:
    text = (SCRIPTS_DIR / script).read_text()
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{func_name}()"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _run(funcs, call, env_vars, stub_dir=None, stdin=""):
    body = "\n".join(_extract_function("downloader.sh", f) for f in funcs)
    exports = "\n".join(f'export {k}="{v}"' for k, v in env_vars.items())
    path = f'export PATH="{stub_dir}:$PATH"\n' if stub_dir else ""
    return subprocess.run(
        ["bash", "-c", f"{path}{exports}\n{body}\n{call}"],
        capture_output=True,
        text=True,
        input=stdin,
    )


# --------------------------------------------------------------------------- #
# downloader.sh
# --------------------------------------------------------------------------- #
MIXED = f"aaa {OVER}\nbbb {UNDER}\nccc\n"


def _eligible(max_bytes, stdin):
    env = {} if max_bytes is None else {"MAX_DOWNLOAD_BYTES": str(max_bytes)}
    return _run(["eligible_addresses"], "eligible_addresses", env, stdin=stdin).stdout.split()


class TestEligibleAddresses:
    def test_unbounded_passes_everything_and_strips_sizes(self):
        assert _eligible(0, MIXED) == ["aaa", "bbb", "ccc"]

    def test_bound_skips_over_cap_only(self):
        assert _eligible(CAP, MIXED) == ["bbb", "ccc"]

    def test_exactly_at_cap_is_eligible(self):
        assert _eligible(CAP, f"ddd {CAP}\n") == ["ddd"]

    def test_a_bare_line_is_never_excluded(self):
        # Unknown is not "too big": every pre-change list is bare and must
        # keep working under a bound.
        assert _eligible(1, "ccc\n") == ["ccc"]

    def test_unset_bound_means_unbounded(self):
        assert _eligible(None, MIXED) == ["aaa", "bbb", "ccc"]


class TestPickRandomAddressWithBound:
    FUNCS = ["eligible_addresses", "pick_random_address"]

    def test_local_file_returns_bare_address_under_cap(self, tmp_path):
        f = tmp_path / "addresses.txt"
        f.write_text(MIXED)
        env = {"ADDRESSES_FILE": str(f), "MAX_DOWNLOAD_BYTES": str(CAP), "S3_HTTP_BASE": ""}
        for _ in range(20):
            r = _run(self.FUNCS, "pick_random_address", env)
            assert r.stdout.strip() in ("bbb", "ccc"), r.stdout

    def test_local_file_all_over_cap_yields_nothing(self, tmp_path):
        f = tmp_path / "addresses.txt"
        f.write_text(f"aaa {OVER}\nzzz {OVER}\n")
        env = {"ADDRESSES_FILE": str(f), "MAX_DOWNLOAD_BYTES": str(CAP), "S3_HTTP_BASE": ""}
        r = _run(self.FUNCS, "pick_random_address", env)
        assert r.stdout.strip() == ""

    def test_local_file_unbounded_is_unchanged(self, tmp_path):
        f = tmp_path / "addresses.txt"
        f.write_text(MIXED)
        env = {"ADDRESSES_FILE": str(f), "S3_HTTP_BASE": ""}
        seen = {_run(self.FUNCS, "pick_random_address", env).stdout.strip() for _ in range(30)}
        assert seen <= {"aaa", "bbb", "ccc"}
        assert "aaa" in seen  # the over-cap entry is fine for a native downloader

    @pytest.fixture
    def curl_stub(self, tmp_path):
        """Two keys: one whose every entry is over the cap, one mixed."""
        stub_dir = tmp_path / "bin"
        stub_dir.mkdir()
        listing = (
            '<?xml version="1.0"?><ListBucketResult>'
            "<Key>saorsa-deploy/addresses/DEV-01/upload-UK1-1.txt</Key>"
            "<Key>saorsa-deploy/addresses/DEV-01/upload-RBX-A-3.txt</Key>"
            "</ListBucketResult>"
        )
        (stub_dir / "curl").write_text(
            "#!/usr/bin/env bash\n"
            'for arg in "$@"; do\n'
            '  case "$arg" in\n'
            f"    *\"?list-type=2\"*) printf %s '{listing}' ; exit 0 ;;\n"
            f'    *upload-UK1-1.txt) printf "big1 {OVER}\\nbig2 {OVER}\\n"; exit 0 ;;\n'
            f'    *upload-RBX-A-3.txt) printf "small {UNDER}\\nbig3 {OVER}\\nlegacy\\n";'
            " exit 0 ;;\n"
            "  esac\n"
            "done\n"
            "exit 0\n"
        )
        (stub_dir / "curl").chmod(0o755)
        return stub_dir

    def test_s3_path_never_returns_an_over_cap_address(self, curl_stub):
        env = {
            "ADDRESSES_FILE": "/nonexistent",
            "ADDRESSES_S3_PREFIX": "saorsa-deploy/addresses/DEV-01",
            "S3_HTTP_BASE": "https://bucket.s3.eu-west-2.amazonaws.com",
            "MAX_DOWNLOAD_BYTES": str(CAP),
        }
        seen = set()
        for _ in range(40):
            r = _run(self.FUNCS, "pick_random_address", env, curl_stub)
            assert r.returncode == 0, r.stderr
            seen.add(r.stdout.strip())
        assert seen <= {"small", "legacy", ""}
        # With 5 key attempts at 50/50, an empty pick is (1/2)^5 per run;
        # across 40 runs the eligible entries must have surfaced.
        assert {"small", "legacy"} <= seen


# --------------------------------------------------------------------------- #
# uploader.sh publishes the size
# --------------------------------------------------------------------------- #
class TestUploaderPublishesSize:
    def test_address_line_carries_file_bytes(self):
        # The line is written by publish_address now that it also carries the
        # content hash (V2-757), so the size is checked through that function
        # rather than against a literal echo.
        text = (SCRIPTS_DIR / "uploader.sh").read_text()
        assert 'publish_address "$address" "$file_bytes" "$file_hash"' in text
        assert 'echo "$address" >> "$ADDRESSES_FILE"' not in text

    def test_published_line_still_has_the_size_as_the_second_field(self, tmp_path):
        # The WASM downloader's over-cap skip reads $2, so the size must stay
        # in that position whatever follows it.
        addresses = tmp_path / "addresses.txt"
        script = (SCRIPTS_DIR / "uploader.sh").read_text()
        start = script.index("publish_address() {")
        body = script[start : script.index("\n}", start) + 2]
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'ADDRESSES_FILE="{addresses}"\n{body}\npublish_address addr123 10485760 abcdef',
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert addresses.read_text().split() == ["addr123", "10485760", "abcdef"]


# --------------------------------------------------------------------------- #
# clients provision hands the WASM downloader the cap
# --------------------------------------------------------------------------- #
BOOTSTRAP_ADDR = "/ip4/1.2.3.4/udp/42768/webrtc-direct/certhash/uEiA/p2p/" + "ab" * 32


def _wasm_env_cmds(role):
    p = AntClientProvisioner(
        clients=[],
        bootstrap_peers=["1.2.3.4:10000"],
        aws_access_key_id="k",
        aws_secret_access_key="s",
        addresses_s3_bucket="bucket",
        addresses_s3_prefix="prefix",
        console=MagicMock(),
        wasm_bundle_url="https://example/bundle.tar.gz",
        bootstrap_webrtc_multiaddrs=[BOOTSTRAP_ADDR],
        wasm_rpc_url="https://rpc.example",
    )
    host = MagicMock()
    host.name = "1.2.3.4"
    state = MagicMock()
    state.inventory.get_active_hosts.return_value = [host]
    env = {
        "1.2.3.4": {
            "role": role,
            "file_size_kb": 0,
            "region": "tor1",
            "client_index": 1,
            "payment_type": "",
            "secret_key": "",
            "heaptrack": "0",
            "bootstrap_cache": "true",
            "forward_logs": "false",
            "rust_log": "",
            "upload_presigned_url": "",
        }
    }
    with patch("saorsa_deploy.provisioning.client.add_op") as add_op:
        p._add_wasm_ops(state, env)
    return " ".join(
        " ".join(k.kwargs.get("commands", []))
        for k in add_op.call_args_list
        if "env file" in k.kwargs["name"]
    )


class TestWasmDownloaderEnvCarriesTheCap:
    def test_downloader_gets_ant_cores_browser_cap(self):
        assert f"MAX_DOWNLOAD_BYTES={WASM_MAX_FILE_BYTES}" in _wasm_env_cmds(ROLE_WASM_DOWNLOAD)
        assert WASM_MAX_FILE_BYTES == CAP

    def test_uploader_env_carries_it_too_and_ignores_it(self):
        # One env template for both WASM roles; the uploader never reads it.
        cmds = _wasm_env_cmds(ROLE_WASM_UPLOAD)
        assert f"MAX_DOWNLOAD_BYTES={WASM_MAX_FILE_BYTES}" in cmds
