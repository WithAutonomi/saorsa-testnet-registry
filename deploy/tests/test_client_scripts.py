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
    script: str, end_marker: str, env_vars: dict, probe: str
) -> subprocess.CompletedProcess:
    body = _prologue(script, end_marker)
    exports = "\n".join(f'export {k}="{v}"' for k, v in env_vars.items())
    return subprocess.run(
        ["bash", "-c", f"{exports}\n{body}\n{probe}"],
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
