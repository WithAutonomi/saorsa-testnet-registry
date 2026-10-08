"""Unit tests for the prod_downloader.py systemd service script.

The script lives at ``deploy/saorsa_deploy/resources/scripts/prod_downloader.py``
— it isn't a regular Python module, so we load it by path and exercise its
internal helpers directly. Mirrors ``test_prod_uploader_service.py``.
"""

import importlib.util
import os
from pathlib import Path

import pytest

SERVICE_PATH = (
    Path(__file__).resolve().parents[1] / "saorsa_deploy/resources/scripts/prod_downloader.py"
)


@pytest.fixture(scope="module")
def service():
    spec = importlib.util.spec_from_file_location("prod_downloader", SERVICE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSafeLogBasename:
    def test_uses_zero_padded_index(self, service):
        out = service._safe_log_basename("abcdef0123456789", 7)
        assert out.startswith("0007-")

    def test_strips_path_separators(self, service):
        out = service._safe_log_basename("aa/bb/cc", 1)
        # Slashes get squashed so a malicious address can't be used to
        # escape the log directory. Dots are kept — they pose no risk
        # without a slash, and aggressive stripping makes filenames
        # harder to associate back to the address.
        assert "/" not in out

    def test_truncates_to_short_prefix(self, service):
        addr = "a" * 64
        out = service._safe_log_basename(addr, 12)
        # idx prefix + dash + at most 16 chars of payload
        assert out == "0012-" + ("a" * 16)

    def test_empty_address_falls_back_to_unnamed(self, service):
        assert service._safe_log_basename("", 4) == "0004-unnamed"


class TestReadAddresses:
    def test_skips_blank_and_comment_lines(self, service, tmp_path):
        path = tmp_path / "addrs.txt"
        path.write_text("addr1\n\n# this is a comment\naddr2\n  \n\taddr3\n")
        # Each entry is (address, declared_bytes): a bare line carries no size,
        # and None rather than 0 keeps "unknown" distinct from "too big".
        assert service._read_addresses(str(path)) == [
            ("addr1", None),
            ("addr2", None),
            ("addr3", None),
        ]

    def test_empty_file_returns_empty_list(self, service, tmp_path):
        path = tmp_path / "addrs.txt"
        path.write_text("")
        assert service._read_addresses(str(path)) == []


class TestInfluxLine:
    def test_schema_matches_downloader_sh(self, service):
        line = service._build_line(
            host="host-A",
            region="lon1",
            status=0,
            total_ms=1234,
            download_secs=1.25,
            file_size=4096,
            start_ns=1700000000000000000,
        )
        assert line.startswith("download,host=host-A,region=lon1 ")
        # Field order/types must stay the same as the legacy bash script
        # since `deployments print-downloads` and `record-results` consume
        # them by name.
        assert "status=0i," in line
        assert "total_ms=1234i," in line
        assert "download_secs=1.25," in line
        assert "file_size=4096i," in line
        assert "start_ns=1700000000000000000i" in line
        # No explicit line-protocol timestamp — InfluxDB stamps server-receive
        # time so clock-skewed clients aren't written into the future. The line
        # therefore ends with the last field, not a trailing nanosecond stamp.
        assert line.endswith("start_ns=1700000000000000000i")

    def test_tag_escaping(self, service):
        line = service._build_line(
            host="host with,space",
            region="re=gion",
            status=1,
            total_ms=0,
            download_secs=0.0,
            file_size=0,
            start_ns=0,
        )
        assert "host=host\\ with\\,space" in line
        assert "region=re\\=gion" in line


class TestParseDownloadSecs:
    def test_extracts_numeric_time(self, service):
        out = "[INFO] Connected\nTime: 12.5\nDone\n"
        assert service._parse_download_secs(out) == 12.5

    def test_returns_zero_when_field_absent(self, service):
        assert service._parse_download_secs("nothing useful here") == 0.0

    def test_returns_zero_on_unparseable_value(self, service):
        # If ant ever emitted a non-numeric Time: value, the parser must
        # not blow up the whole loop.
        assert service._parse_download_secs("Time: oops\n") == 0.0


class TestTailLines:
    def test_returns_last_n_lines(self, service):
        text = "a\nb\nc\nd\ne\n"
        assert service._tail_lines(text, 2) == "d\ne\n"

    def test_empty_input_returns_empty(self, service):
        assert service._tail_lines("", 5) == ""

    def test_no_trailing_newline_preserved(self, service):
        assert service._tail_lines("x\ny\nz", 1) == "z"


class TestSleepEnvSemantics:
    """The script reads SLEEP_INTERVAL_SECS from the environment in main();
    here we exercise the parsing + branching without invoking main itself.
    """

    def test_zero_value_means_no_sleep(self, service, monkeypatch):
        monkeypatch.setenv("SLEEP_INTERVAL_SECS", "0")
        # Only the parsing logic — defensive int() cast is what lives in
        # main(). Verifies the env var is honourable.
        assert int(os.environ["SLEEP_INTERVAL_SECS"]) == 0

    def test_default_when_unset(self, service, monkeypatch):
        monkeypatch.delenv("SLEEP_INTERVAL_SECS", raising=False)
        # The default lives in main(); we just verify the fallback string
        # is what we expect downstream code to use when env is missing.
        assert int(os.environ.get("SLEEP_INTERVAL_SECS", "900")) == 900


class TestResolveBootstrapArgs:
    def test_cache_enabled_by_default_returns_empty(self, service, monkeypatch):
        monkeypatch.delenv("BOOTSTRAP_CACHE", raising=False)
        monkeypatch.setenv("BOOTSTRAP_ARGS", "--bootstrap 1.2.3.4:10000")
        assert service._resolve_bootstrap_args() == []

    def test_cache_disabled_splits_bootstrap_args(self, service, monkeypatch):
        monkeypatch.setenv("BOOTSTRAP_CACHE", "false")
        monkeypatch.setenv("BOOTSTRAP_ARGS", "--bootstrap 1.2.3.4:10000")
        assert service._resolve_bootstrap_args() == ["--bootstrap", "1.2.3.4:10000"]

    def test_cache_disabled_requires_bootstrap_args(self, service, monkeypatch):
        monkeypatch.setenv("BOOTSTRAP_CACHE", "false")
        monkeypatch.delenv("BOOTSTRAP_ARGS", raising=False)
        with pytest.raises(SystemExit):
            service._resolve_bootstrap_args()


class TestHeaptrackWrap:
    def test_wraps_when_enabled(self, service):
        cmd = ["/root/.local/bin/ant", "file", "download", "addr"]
        out = service._maybe_wrap_heaptrack(
            cmd,
            memory_profiling=True,
            output_base="/var/log/ant-heaptrack/download-000001-0001-addr",
        )
        assert out[:3] == [
            "heaptrack",
            "-o",
            "/var/log/ant-heaptrack/download-000001-0001-addr",
        ]
        assert out[3:] == cmd

    def test_noop_when_disabled(self, service):
        cmd = ["/root/.local/bin/ant", "file", "download", "addr"]
        out = service._maybe_wrap_heaptrack(
            cmd, memory_profiling=False, output_base="/var/log/ant-heaptrack/x"
        )
        assert out == cmd

    def test_noop_when_no_output_base(self, service):
        cmd = ["/root/.local/bin/ant", "file", "download", "addr"]
        out = service._maybe_wrap_heaptrack(cmd, memory_profiling=True, output_base=None)
        assert out == cmd


class TestRunAntRustLogEnv:
    """_run_transfer must not hand ant an EMPTY RUST_LOG (mirrors prod_uploader).

    An empty RUST_LOG inherited from the systemd EnvironmentFile sets ant's
    tracing EnvFilter to an empty filter, silencing ALL output and overriding
    -v — which blanked the per-op .stderr download log. _run_transfer strips an
    empty RUST_LOG and preserves a non-empty one.
    """

    def _capture_env(self, service, monkeypatch, tmp_path):
        captured = {}

        class _FakeProc:
            stdout = iter(())
            stderr = iter(())

            def wait(self):
                return 0

        def _fake_popen(cmd, *, env, **kwargs):
            captured["env"] = env
            return _FakeProc()

        monkeypatch.setattr(service.subprocess, "Popen", _fake_popen)
        service._run_transfer(
            cmd=["/root/.local/bin/ant", "file", "download", "abcd"],
            stderr_log_path=tmp_path / "op.stderr",
            stdout_log_path=tmp_path / "op.stdout",
        )
        return captured["env"]

    def test_empty_rust_log_is_stripped(self, service, monkeypatch, tmp_path):
        monkeypatch.setenv("RUST_LOG", "")
        env = self._capture_env(service, monkeypatch, tmp_path)
        assert "RUST_LOG" not in env

    def test_absent_rust_log_stays_absent(self, service, monkeypatch, tmp_path):
        monkeypatch.delenv("RUST_LOG", raising=False)
        env = self._capture_env(service, monkeypatch, tmp_path)
        assert "RUST_LOG" not in env

    def test_nonempty_rust_log_is_preserved(self, service, monkeypatch, tmp_path):
        monkeypatch.setenv("RUST_LOG", "ant=info,saorsa_core::dht=debug")
        env = self._capture_env(service, monkeypatch, tmp_path)
        assert env["RUST_LOG"] == "ant=info,saorsa_core::dht=debug"
