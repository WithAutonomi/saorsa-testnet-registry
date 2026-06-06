"""Light checks on the testnet client bash scripts (uploader.sh / downloader.sh).

The scripts can't be executed end-to-end in tests (they invoke ant/dd/aws),
but the BOOTSTRAP_CACHE gating added for the ant peer-cache feature is pure
shell logic, so we verify it by evaluating just the configuration prologue.
"""

import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = (
    Path(__file__).parent.parent / "saorsa_deploy" / "resources" / "scripts"
)


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
            f"preserve_failure_logs {seq} '{address}' \"{stdout_f}\" \"{stderr_f}\"",
            prelude=_HOSTNAME_SHIM,
        )
        return result, log_dir, stdout_f, stderr_f

    def test_failure_moves_both_streams_into_log_dir(self, tmp_path):
        address = "a1b2c3d4e5f60718" + "9" * 48
        result, log_dir, stdout_f, stderr_f = self._call(tmp_path, 7, address)
        assert result.returncode == 0, result.stderr

        stem = log_dir / "0007-a1b2c3d4e5f60718"
        assert (
            stem.with_suffix(".stdout").read_text()
            == "Downloaded file\nTime: 1.5\n"
        )
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
