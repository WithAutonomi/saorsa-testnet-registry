"""Light checks on the testnet client bash scripts (uploader.sh / downloader.sh).

The scripts can't be executed end-to-end in tests (they invoke ant/dd/aws),
but the BOOTSTRAP_CACHE gating added for the ant peer-cache feature is pure
shell logic, so we verify it by evaluating just the configuration prologue.
"""

import hashlib
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "saorsa_deploy" / "resources" / "scripts"


def _prologue(script: str, end_marker: str) -> str:
    """Return the script text up to (excluding) the first line containing marker."""
    text = (SCRIPTS_DIR / script).read_text()
    lines = []
    for line in text.splitlines():
        if end_marker in line:
            break
        lines.append(line)
    return "\n".join(lines)


def _run_prologue(
    script: str, end_marker: str, env_vars: dict, probe: str, prelude: str = ""
) -> subprocess.CompletedProcess:
    body = _prologue(script, end_marker)
    exports = "\n".join(f'export {k}="{v}"' for k, v in env_vars.items())
    return subprocess.run(
        ["bash", "-c", f"{exports}\n{prelude}\n{body}\n{probe}"],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("script", ["uploader.sh", "downloader.sh"])
def test_script_syntax_ok(script):
    result = subprocess.run(
        ["bash", "-n", str(SCRIPTS_DIR / script)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


_UPLOADER_ENV = {
    "FILE_SIZE_KB": "1024",
    "SECRET_KEY": "0xabc",
    "ADDRESSES_S3_BUCKET": "b",
    "ADDRESSES_S3_PREFIX": "p",
    "REGION": "lon1",
    "CLIENT_INDEX": "1",
}
_DOWNLOADER_ENV = {"REGION": "lon1"}


@pytest.mark.parametrize(
    ("script", "marker", "env"),
    [
        ("uploader.sh", "CLIENT_HOST=", _UPLOADER_ENV),
        ("downloader.sh", "dl_seq=", _DOWNLOADER_ENV),
    ],
)
class TestBootstrapCacheGating:
    def test_cache_enabled_by_default_clears_bootstrap_args(self, script, marker, env):
        result = _run_prologue(
            script,
            marker,
            {**env, "BOOTSTRAP_ARGS": "--bootstrap 1.2.3.4:10000"},
            'echo "ARGS=[$BOOTSTRAP_ARGS]"',
        )
        assert result.returncode == 0, result.stderr
        assert "ARGS=[]" in result.stdout

    def test_cache_disabled_keeps_bootstrap_args(self, script, marker, env):
        result = _run_prologue(
            script,
            marker,
            {
                **env,
                "BOOTSTRAP_CACHE": "false",
                "BOOTSTRAP_ARGS": "--bootstrap 1.2.3.4:10000",
            },
            'echo "ARGS=[$BOOTSTRAP_ARGS]"',
        )
        assert result.returncode == 0, result.stderr
        assert "ARGS=[--bootstrap 1.2.3.4:10000]" in result.stdout

    def test_cache_disabled_requires_bootstrap_args(self, script, marker, env):
        result = _run_prologue(
            script,
            marker,
            {**env, "BOOTSTRAP_CACHE": "false"},
            "true",
        )
        assert result.returncode != 0
        assert "BOOTSTRAP_ARGS must be set" in result.stderr

    def test_cache_enabled_does_not_require_bootstrap_args(self, script, marker, env):
        result = _run_prologue(script, marker, env, 'echo "ARGS=[$BOOTSTRAP_ARGS]"')
        assert result.returncode == 0, result.stderr
        assert "ARGS=[]" in result.stdout


# Marker just past the function definitions, so preserve_failure_logs is
# callable from the probe.
_FUNCS_MARKER = 'mkdir -p "$DOWNLOAD_DIR"'
# The prologue past dl_seq= runs CLIENT_HOST=$(hostname); shim it so the
# tests do not depend on a hostname binary being installed.
_HOSTNAME_SHIM = "hostname() { echo test-host; }"


class TestFailureLogPreservation:
    """downloader.sh keeps per-op ant output for FAILED downloads only.

    preserve_failure_logs moves the stdout/stderr tmp files into
    FAILURE_LOG_DIR as {seq:04d}-{addr16}.{stdout,stderr}, mirroring the prod
    downloader's per-op log naming.
    """

    def _call(self, tmp_path, seq, address, env_extra=None):
        log_dir = tmp_path / "faillogs"
        stdout_f = tmp_path / "dl.stdout"
        stderr_f = tmp_path / "dl.stderr"
        stdout_f.write_text("Downloaded file\nTime: 1.5\n")
        stderr_f.write_text("ERROR: chunk not found\n")
        result = _run_prologue(
            "downloader.sh",
            _FUNCS_MARKER,
            {
                **_DOWNLOADER_ENV,
                "FAILURE_LOG_DIR": str(log_dir),
                **(env_extra or {}),
            },
            f'preserve_failure_logs {seq} \'{address}\' "{stdout_f}" "{stderr_f}"',
            prelude=_HOSTNAME_SHIM,
        )
        return result, log_dir, stdout_f, stderr_f

    def test_failure_moves_both_streams_into_log_dir(self, tmp_path):
        address = "a1b2c3d4e5f60718" + "9" * 48
        result, log_dir, stdout_f, stderr_f = self._call(tmp_path, 7, address)
        assert result.returncode == 0, result.stderr

        stem = log_dir / "0007-a1b2c3d4e5f60718"
        assert stem.with_suffix(".stdout").read_text() == "Downloaded file\nTime: 1.5\n"
        assert stem.with_suffix(".stderr").read_text() == "ERROR: chunk not found\n"
        # tmp files were moved, not copied
        assert not stdout_f.exists()
        assert not stderr_f.exists()
        # the kept stem is echoed so the journald FAILED line can reference it
        assert str(stem) in result.stdout

    def test_seq_is_zero_padded_to_four_digits(self, tmp_path):
        result, log_dir, _, _ = self._call(tmp_path, 123, "f" * 64)
        assert result.returncode == 0, result.stderr
        assert (log_dir / ("0123-" + "f" * 16 + ".stdout")).exists()

    def test_address_is_truncated_and_sanitized(self, tmp_path):
        # path-unsafe chars in the first 16 chars become underscores
        result, log_dir, _, _ = self._call(tmp_path, 1, "ab/c$d e!fghijklmnopqr")
        assert result.returncode == 0, result.stderr
        assert (log_dir / "0001-ab_c_d_e_fghijkl.stdout").exists()

    def test_empty_address_falls_back_to_unnamed(self, tmp_path):
        result, log_dir, _, _ = self._call(tmp_path, 2, "")
        assert result.returncode == 0, result.stderr
        assert (log_dir / "0002-unnamed.stdout").exists()

    def test_prologue_does_not_create_log_dir(self, tmp_path):
        # Successful-only runs must never touch FAILURE_LOG_DIR — it is
        # created on demand by preserve_failure_logs, not at startup.
        log_dir = tmp_path / "faillogs"
        result = _run_prologue(
            "downloader.sh",
            _FUNCS_MARKER,
            {**_DOWNLOADER_ENV, "FAILURE_LOG_DIR": str(log_dir)},
            "true",
            prelude=_HOSTNAME_SHIM,
        )
        assert result.returncode == 0, result.stderr
        assert not log_dir.exists()

    def test_default_log_dir_is_var_log_ant_downloader(self):
        result = _run_prologue(
            "downloader.sh",
            _FUNCS_MARKER,
            _DOWNLOADER_ENV,
            'echo "DIR=[$FAILURE_LOG_DIR]"',
            prelude=_HOSTNAME_SHIM,
        )
        assert result.returncode == 0, result.stderr
        assert "DIR=[/var/log/ant-downloader]" in result.stdout


# ---------------------------------------------------------------------------
# Content verification (V2-757)
# ---------------------------------------------------------------------------

# Marker just past uploader.sh's function definitions, so file_sha256 and
# publish_address are callable from the probe.
_UPLOADER_FUNCS_MARKER = 'mkdir -p "$UPLOAD_DIR"'


class TestUploaderPublishesTheHash:
    """uploader.sh publishes `<address> <bytes> <sha256>`.

    The hash is the only thing that lets a downloader tell a transfer that
    returned the right bytes from one that returned different bytes and still
    exited 0 -- which every check in the system used to pass.
    """

    def _publish(self, tmp_path, address, size, sha):
        addresses = tmp_path / "addresses.txt"
        result = _run_prologue(
            "uploader.sh",
            _UPLOADER_FUNCS_MARKER,
            {**_UPLOADER_ENV, "ADDRESSES_FILE": str(addresses)},
            f"publish_address '{address}' '{size}' '{sha}'",
            prelude=_HOSTNAME_SHIM,
        )
        assert result.returncode == 0, result.stderr
        return addresses.read_text()

    def test_published_line_has_three_fields(self, tmp_path):
        text = self._publish(tmp_path, "abc123", 10485760, "f" * 64)
        assert text == f"abc123 10485760 {'f' * 64}\n"

    def test_an_empty_hash_is_omitted_not_written_blank(self, tmp_path):
        # A trailing blank field would publish a line whose third column is
        # the empty string; a reader splitting on whitespace must see two
        # fields, exactly as a pre-verification uploader wrote.
        text = self._publish(tmp_path, "abc123", 10485760, "")
        assert text == "abc123 10485760\n"

    def test_file_sha256_matches_sha256sum(self, tmp_path):
        payload = tmp_path / "testfile.bin"
        payload.write_bytes(b"the bytes that go on the network")
        expected = hashlib.sha256(payload.read_bytes()).hexdigest()
        result = _run_prologue(
            "uploader.sh",
            _UPLOADER_FUNCS_MARKER,
            _UPLOADER_ENV,
            f'echo "HASH=[$(file_sha256 "{payload}")]"',
            prelude=_HOSTNAME_SHIM,
        )
        assert result.returncode == 0, result.stderr
        assert f"HASH=[{expected}]" in result.stdout

    def test_an_unreadable_file_hashes_to_empty_not_an_error(self, tmp_path):
        # An unhashable upload must still publish: a hash-less line verifies as
        # `unavailable`, which is not a failure. Aborting the upload over it
        # would be a far worse outcome than not verifying it.
        result = _run_prologue(
            "uploader.sh",
            _UPLOADER_FUNCS_MARKER,
            _UPLOADER_ENV,
            f'echo "HASH=[$(file_sha256 "{tmp_path / "missing.bin"}")]"',
            prelude=_HOSTNAME_SHIM,
        )
        assert result.returncode == 0, result.stderr
        assert "HASH=[]" in result.stdout

    def test_the_influx_point_carries_the_hash(self):
        text = (SCRIPTS_DIR / "uploader.sh").read_text()
        assert 'sha256=\\"${file_hash}\\"' in text


class TestDownloaderCarriesTheHashThroughSelection:
    """eligible_addresses must preserve the published hash.

    By the time an address has been picked the line it came from is gone, and
    an address alone cannot say what bytes were uploaded under it -- so the
    hash has to travel with the selection or verification is impossible.
    """

    def _eligible(self, lines, max_bytes="0"):
        result = _run_prologue(
            "downloader.sh",
            _FUNCS_MARKER,
            {**_DOWNLOADER_ENV, "MAX_DOWNLOAD_BYTES": max_bytes},
            f"printf '%s\\n' {lines} | eligible_addresses",
            prelude=_HOSTNAME_SHIM,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.splitlines()

    def test_three_field_line_yields_address_and_hash(self):
        assert self._eligible("'aaa 100 deadbeef'") == ["aaa deadbeef"]

    def test_two_field_line_still_parses(self):
        # The WASM uploaders publish two-field lines into this same shared
        # pool, so a mixed run must keep working.
        assert self._eligible("'bbb 200'") == ["bbb"]

    def test_one_field_line_still_parses(self):
        assert self._eligible("'ccc'") == ["ccc"]

    def test_the_size_cap_still_applies_to_a_three_field_line(self):
        picked = self._eligible("'aaa 100 deadbeef' 'bbb 5000 cafe'", max_bytes="1000")
        assert picked == ["aaa deadbeef"]


class TestDownloaderVerifies:
    """verify_download compares the fetched bytes against the published hash."""

    def _verify(self, tmp_path, expected, path):
        result = _run_prologue(
            "downloader.sh",
            _FUNCS_MARKER,
            _DOWNLOADER_ENV,
            f"verify_download '{expected}' '{path}'",
            prelude=_HOSTNAME_SHIM,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def _payload(self, tmp_path):
        path = tmp_path / "downloaded.bin"
        path.write_bytes(b"the bytes that came back")
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_matching_content_is_status_zero(self, tmp_path):
        path, digest = self._payload(tmp_path)
        assert self._verify(tmp_path, digest, path) == f"0 {digest}"

    def test_differing_content_is_status_one_with_the_actual_hash(self, tmp_path):
        path, digest = self._payload(tmp_path)
        out = self._verify(tmp_path, "a" * 64, path)
        assert out == f"1 {digest}"

    def test_a_hashless_line_is_status_two_not_a_mismatch(self, tmp_path):
        # Unavailable, not mismatch: nothing was checked, and the content may
        # be perfectly healthy. Calling it a mismatch would fail the integrity
        # gate on a run that simply published no hashes.
        path, _ = self._payload(tmp_path)
        assert self._verify(tmp_path, "", path) == "2"

    def test_a_missing_output_file_is_status_two(self, tmp_path):
        assert self._verify(tmp_path, "f" * 64, tmp_path / "gone.bin") == "2"


class TestStatusIsClampedAtTheEmitSite:
    """Both scripts emit `status` as record-results' enum, not ant's exit code.

    `record-results` reads 0/1/2 and DROPS anything else as an unusable point,
    so an `ant` exit of 3 would be discarded rather than recorded as the
    failure it is. Neither native script has a skip path, so every non-zero
    exit is clamped to 1.
    """

    @pytest.mark.parametrize("script", ["uploader.sh", "downloader.sh"])
    def test_the_metric_line_uses_the_clamped_status(self, script):
        text = (SCRIPTS_DIR / script).read_text()
        assert "status=${op_status}i" in text
        assert "status=${exit_code}i" not in text

    @pytest.mark.parametrize("script", ["uploader.sh", "downloader.sh"])
    @pytest.mark.parametrize(("exit_code", "expected"), [(0, "0"), (1, "1"), (3, "1")])
    def test_any_nonzero_exit_clamps_to_one(self, script, exit_code, expected):
        text = (SCRIPTS_DIR / script).read_text()
        start = text.index("    op_status=0")
        snippet = text[start : text.index("\n", text.index("fi", start))]
        result = subprocess.run(
            ["bash", "-c", f'exit_code={exit_code}\n{snippet}\necho "S=[$op_status]"'],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert f"S=[{expected}]" in result.stdout
