"""Unit tests for the prod_uploader.py systemd service script.

The script lives at ``deploy/saorsa_deploy/resources/scripts/prod_uploader.py``
— it isn't a regular Python module, so we load it by path and exercise its
internal helpers directly.
"""
import importlib.util
import json
from pathlib import Path

import pytest

SERVICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "saorsa_deploy/resources/scripts/prod_uploader.py"
)


@pytest.fixture(scope="module")
def service():
    spec = importlib.util.spec_from_file_location("prod_uploader", SERVICE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSafeFilename:
    def test_strips_url_path(self, service):
        assert service._safe_filename(
            "https://example.com/dir/file.bin?x=1", 7
        ) == "file.bin"

    def test_falls_back_when_no_path(self, service):
        assert service._safe_filename("https://example.com/", 3) == "download-3.bin"

    def test_safe_dotdot(self, service):
        assert (
            service._safe_filename("https://example.com/..", 9)
            == "download-9.bin"
        )


class TestJsonParsing:
    def test_picks_last_json_line(self, service):
        out = "preamble\n  \n{\"address\":\"abc\",\"mode\":\"public\"}\n"
        parsed = service._parse_ant_json(out)
        assert parsed == {"address": "abc", "mode": "public"}

    def test_returns_none_when_no_json(self, service):
        assert service._parse_ant_json("just text\n") is None

    def test_partial_upload_recognised(self, service):
        out = json.dumps({
            "error": "partial_upload",
            "total_chunks": 42,
            "chunks_stored": 40,
            "chunks_failed": 2,
        })
        parsed = service._parse_ant_json(out)
        assert service._classify(parsed) == service.STATUS_PARTIAL_UPLOAD

    def test_success_recognised(self, service):
        parsed = json.loads(
            json.dumps({
                "address": "abc",
                "mode": "public",
                "chunks_stored": 42,
                "total_chunks": 42,
                "chunks_failed": 0,
            })
        )
        assert service._classify(parsed) == service.STATUS_SUCCESS

    def test_none_classified_as_partial(self, service):
        assert service._classify(None) == service.STATUS_PARTIAL_UPLOAD


class TestInfluxLine:
    def _line(self, service, **overrides):
        defaults = dict(
            host="prodhost",
            region="lon1",
            status=0,
            total_ms=1234,
            upload_secs=2.5,
            file_size=1048576,
            start_ns=10**18,
            chunks_stored=42,
            chunks_failed=0,
            total_chunks=42,
            chunk_attempts_total=50,
            retries_round_0=5,
            retries_round_1=2,
            retries_round_2=1,
            retries_round_3=0,
            store_duration_p50_ms=120,
            store_duration_p95_ms=340,
            store_duration_max_ms=900,
            address="addr-xyz",
            file_name="file.bin",
            source_url="https://example.com/file.bin",
            reason="",
        )
        defaults.update(overrides)
        return service._build_line(**defaults)

    def test_measurement_and_tags(self, service):
        line = self._line(service)
        head, _ = line.split(" ", 1)
        assert head.startswith("prod_upload,")
        assert "host=prodhost" in head
        assert "region=lon1" in head

    def test_no_trailing_timestamp(self, service):
        # InfluxDB stamps server-receive time so clock-skewed clients aren't
        # written into the future. The line ends with the last field, not a
        # trailing nanosecond timestamp.
        line = self._line(service)
        assert line.endswith('reason=""')

    def test_integer_fields_have_i_suffix(self, service):
        line = self._line(service)
        assert "status=0i" in line
        assert "chunks_stored=42i" in line
        assert "total_chunks=42i" in line
        # chunk-retry + store-duration fields (added later) are also integers
        assert "chunk_attempts_total=50i" in line
        assert "retries_round_0=5i" in line
        assert "store_duration_p95_ms=340i" in line

    def test_string_fields_quoted(self, service):
        line = self._line(service, address="abc \"x\"")
        assert 'address="abc \\"x\\""' in line

    def test_special_chars_in_tags_escaped(self, service):
        line = self._line(service, host="my host,with=stuff")
        assert "host=my\\ host\\,with\\=stuff" in line


class TestReadUrls:
    def test_filters_blanks_and_comments(self, service, tmp_path):
        p = tmp_path / "urls.txt"
        p.write_text(
            "# top\n"
            "https://example.com/a\n"
            "\n"
            "  # indented comment\n"
            "https://example.com/b\n"
        )
        urls = service._read_urls(str(p))
        assert urls == ["https://example.com/a", "https://example.com/b"]


class TestTailLines:
    def test_returns_last_n_lines(self, service):
        text = "\n".join(f"line {i}" for i in range(1, 11)) + "\n"
        out = service._tail_lines(text, 3)
        assert out == "line 8\nline 9\nline 10\n"

    def test_keeps_all_when_fewer_than_n(self, service):
        text = "a\nb\nc\n"
        assert service._tail_lines(text, 100) == "a\nb\nc\n"

    def test_empty_input_yields_empty(self, service):
        assert service._tail_lines("", 50) == ""

    def test_no_trailing_newline_preserved(self, service):
        text = "a\nb\nc"
        assert service._tail_lines(text, 2) == "b\nc"


class TestPaymentFlag:
    def test_merkle_returns_merkle_flag(self, service):
        assert service._payment_flag("merkle") == ["--merkle"]

    def test_no_merkle_returns_no_merkle_flag(self, service):
        assert service._payment_flag("no-merkle") == ["--no-merkle"]

    def test_none_returns_empty(self, service):
        assert service._payment_flag(None) == []

    def test_empty_string_returns_empty(self, service):
        assert service._payment_flag("") == []

    def test_unknown_value_returns_empty(self, service):
        assert service._payment_flag("garbage") == []


class TestVerbosityFlag:
    @pytest.mark.parametrize(
        ("level", "expected"),
        [("info", "-v"), ("debug", "-vv"), ("trace", "-vvv")],
    )
    def test_known_levels(self, service, level, expected):
        assert service._verbosity_flag(level) == expected

    def test_unknown_level_falls_back_to_info(self, service):
        assert service._verbosity_flag("garbage") == "-v"

    def test_empty_falls_back_to_info(self, service):
        assert service._verbosity_flag("") == "-v"


class TestInfluxLineReason:
    def test_reason_field_appears_in_line(self, service):
        line = TestInfluxLine()._line(
            service, status=1, reason="wave store failed after retries"
        )
        assert 'reason="wave store failed after retries"' in line

    def test_empty_reason_emits_empty_string(self, service):
        line = TestInfluxLine()._line(service)
        assert 'reason=""' in line


class TestResolveBootstrapArgs:
    def test_cache_enabled_by_default_returns_empty(self, service, monkeypatch):
        monkeypatch.delenv("BOOTSTRAP_CACHE", raising=False)
        monkeypatch.setenv("BOOTSTRAP_ARGS", "--bootstrap 1.2.3.4:10000")
        assert service._resolve_bootstrap_args() == []

    def test_cache_enabled_explicit_returns_empty(self, service, monkeypatch):
        monkeypatch.setenv("BOOTSTRAP_CACHE", "true")
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
