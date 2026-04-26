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
            store_concurrency=8,
            chunks_stored=42,
            chunks_failed=0,
            total_chunks=42,
            address="addr-xyz",
            file_name="file.bin",
            source_url="https://example.com/file.bin",
            timestamp_ns=10**18 + 1,
        )
        defaults.update(overrides)
        return service._build_line(**defaults)

    def test_measurement_and_tags(self, service):
        line = self._line(service)
        head, _, _ = line.split(" ")
        assert head.startswith("prod_upload,")
        assert "host=prodhost" in head
        assert "region=lon1" in head

    def test_integer_fields_have_i_suffix(self, service):
        line = self._line(service)
        assert "status=0i" in line
        assert "chunks_stored=42i" in line
        assert "total_chunks=42i" in line
        assert "store_concurrency=8i" in line

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
