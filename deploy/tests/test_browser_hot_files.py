"""`clients select-hot-files` / `print-hot-files` (V2-1617).

The browser cohort's measurement is "many browsers reading the SAME file at the
SAME moment", so which file that is has to be a fact every VM agrees on. These
two commands are what make it one: a single object, written once by the
operator, read by every instance at every round start.

S3 is a fake here. The ordering rule, the shortfall and the idempotence are the
whole behaviour, and none of them needs a bucket.
"""

import json

import pytest

from saorsa_deploy import hot_files
from saorsa_deploy.client_roles import HOT_FILES_OBJECT_NAME
from saorsa_deploy.cmd.hot_files import cmd_print_hot_files, cmd_select_hot_files

FIFTY_MIB = 50 * 1024 * 1024


def _addr(n: int) -> str:
    return f"{n:064x}"


class FakeS3:
    """The three calls `hot_files` makes, over an in-memory key/value store."""

    class exceptions:  # noqa: N801 — mirrors boto3's client.exceptions shape
        class NoSuchKey(Exception):
            pass

    def __init__(self, objects: dict[str, str] | None = None):
        self.objects = dict(objects or {})
        self.puts: list[tuple[str, str]] = []

    def get_paginator(self, _name):
        outer = self

        class Paginator:
            def paginate(self, Bucket, Prefix):  # noqa: N803 — boto3 kwarg names
                contents = [{"Key": key} for key in sorted(outer.objects) if key.startswith(Prefix)]
                return [{"Contents": contents}]

        return Paginator()

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey(Key)

        class Body:
            def __init__(self, text):
                self._text = text

            def read(self):
                return self._text.encode()

        return {"Body": Body(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None):  # noqa: N803
        self.objects[Key] = Body
        self.puts.append((Key, Body))


def _pool(**keys: str) -> FakeS3:
    prefix = hot_files.addresses_prefix("DEV-01")
    return FakeS3({f"{prefix}/{name}": body for name, body in keys.items()})


class TestCandidateSelection:
    def test_only_lines_of_exactly_the_requested_size_are_candidates(self):
        s3 = _pool(
            **{
                "upload-lon1-1.txt": (
                    f"{_addr(1)} {FIFTY_MIB}\n{_addr(2)} {FIFTY_MIB - 1}\n{_addr(3)} {FIFTY_MIB}\n"
                )
            }
        )
        found = hot_files.collect_candidates("DEV-01", FIFTY_MIB, s3=s3)
        assert [c.address for c in found] == [_addr(1), _addr(3)]

    def test_order_is_key_then_line_number_which_is_upload_order(self):
        # Lines are appended as each upload completes, so (key, line) is upload
        # order — and the first N are therefore the OLDEST files of that size.
        s3 = _pool(
            **{
                "upload-sgp-1.txt": f"{_addr(9)} {FIFTY_MIB}\n",
                "upload-lon1-1.txt": f"{_addr(1)} {FIFTY_MIB}\n{_addr(2)} {FIFTY_MIB}\n",
            }
        )
        found = hot_files.collect_candidates("DEV-01", FIFTY_MIB, s3=s3)
        assert [(c.source_key, c.line) for c in found] == [
            ("upload-lon1-1.txt", 1),
            ("upload-lon1-1.txt", 2),
            ("upload-sgp-1.txt", 1),
        ]

    def test_a_bare_line_is_never_a_candidate(self):
        # The candidate set is "files of exactly this size"; an unknown size is
        # not a match for any size. Treating it as 0 would never match either,
        # but by accident rather than by rule.
        s3 = _pool(**{"upload-lon1-1.txt": f"{_addr(1)}\n{_addr(2)} {FIFTY_MIB}\n"})
        found = hot_files.collect_candidates("DEV-01", FIFTY_MIB, s3=s3)
        assert [c.address for c in found] == [_addr(2)]

    def test_only_the_native_uploaders_keys_are_read(self):
        # A WASM uploader publishes under `wasm-upload-*` and its files are
        # capped differently; the point of the candidate set is files of one
        # exact size published by one kind of uploader.
        s3 = _pool(
            **{
                "upload-lon1-1.txt": f"{_addr(1)} {FIFTY_MIB}\n",
                "wasm-upload-lon1-1.txt": f"{_addr(2)} {FIFTY_MIB}\n",
                "pointer-lon1-1.txt": f"{_addr(3)} {FIFTY_MIB}\n",
                HOT_FILES_OBJECT_NAME: "{}",
            }
        )
        found = hot_files.collect_candidates("DEV-01", FIFTY_MIB, s3=s3)
        assert [c.address for c in found] == [_addr(1)]

    def test_the_document_records_where_each_file_came_from(self):
        s3 = _pool(**{"upload-lon1-1.txt": f"{_addr(1)} {FIFTY_MIB}\n"})
        doc = hot_files.select_hot_files("DEV-01", count=1, size_mb=50, s3=s3)
        assert doc["deployment"] == "DEV-01"
        assert doc["size_bytes"] == FIFTY_MIB
        assert doc["files"] == [
            {
                "index": 0,
                "address": _addr(1),
                "bytes": FIFTY_MIB,
                "source_key": "upload-lon1-1.txt",
                "line": 1,
            }
        ]

    def test_a_shortfall_raises_naming_the_count_found(self):
        s3 = _pool(**{"upload-lon1-1.txt": f"{_addr(1)} {FIFTY_MIB}\n"})
        with pytest.raises(hot_files.HotFileError, match="found 1 file"):
            hot_files.select_hot_files("DEV-01", count=3, size_mb=50, s3=s3)

    def test_a_shortfall_writes_nothing(self):
        # So it cannot half-replace a live cohort's selection.
        s3 = _pool(**{"upload-lon1-1.txt": f"{_addr(1)} {FIFTY_MIB}\n"})
        with pytest.raises(hot_files.HotFileError):
            hot_files.select_hot_files("DEV-01", count=3, size_mb=50, s3=s3)
        assert s3.puts == []

    def test_size_mb_is_binary(self):
        # Matching an uploader's `file_size_mb`, which generates
        # file_size_mb * 1024 * 1024 bytes. A decimal reading here would match
        # nothing any uploader ever published.
        s3 = _pool(**{"upload-lon1-1.txt": f"{_addr(1)} 50000000\n"})
        with pytest.raises(hot_files.HotFileError, match="52,428,800"):
            hot_files.select_hot_files("DEV-01", count=1, size_mb=50, s3=s3)


class TestPoolLineParsing:
    def test_an_address_and_a_size(self):
        assert hot_files.parse_pool_line(f"{_addr(1)} 1024") == (_addr(1), 1024)

    def test_case_is_normalised(self):
        assert hot_files.parse_pool_line(f"{_addr(1).upper()} 1024")[0] == _addr(1)

    @pytest.mark.parametrize("line", ["", "   ", _addr(1), f"{_addr(1)} notanumber", "a b c"])
    def test_anything_else_is_not_a_candidate_line(self, line):
        assert hot_files.parse_pool_line(line) is None


class TestCommands:
    def _args(self, **kwargs):
        return type("Args", (), {"name": "DEV-01", "replace": False, **kwargs})()

    def test_select_writes_the_manifest(self, monkeypatch):
        s3 = _pool(**{"upload-lon1-1.txt": f"{_addr(1)} {FIFTY_MIB}\n"})
        monkeypatch.setattr(hot_files, "_s3", lambda: s3)
        cmd_select_hot_files(self._args(count=1, size_mb=50))
        key = hot_files.hot_files_key("DEV-01")
        assert json.loads(s3.objects[key])["files"][0]["address"] == _addr(1)

    def test_select_is_idempotent_without_replace(self, monkeypatch, capsys):
        # A re-run during a live cohort is almost always someone checking;
        # silently re-selecting would change what the next round reads
        # mid-measurement.
        s3 = _pool(
            **{
                "upload-lon1-1.txt": f"{_addr(1)} {FIFTY_MIB}\n{_addr(2)} {FIFTY_MIB}\n",
                HOT_FILES_OBJECT_NAME: json.dumps(
                    {"deployment": "DEV-01", "files": [{"index": 0, "address": _addr(7)}]}
                ),
            }
        )
        monkeypatch.setattr(hot_files, "_s3", lambda: s3)
        cmd_select_hot_files(self._args(count=1, size_mb=50))
        assert s3.puts == []
        assert "already has a hot-file selection" in capsys.readouterr().out

    def test_replace_overwrites(self, monkeypatch):
        s3 = _pool(
            **{
                "upload-lon1-1.txt": f"{_addr(1)} {FIFTY_MIB}\n",
                HOT_FILES_OBJECT_NAME: json.dumps(
                    {"deployment": "DEV-01", "files": [{"index": 0, "address": _addr(7)}]}
                ),
            }
        )
        monkeypatch.setattr(hot_files, "_s3", lambda: s3)
        cmd_select_hot_files(self._args(count=1, size_mb=50, replace=True))
        key = hot_files.hot_files_key("DEV-01")
        assert json.loads(s3.objects[key])["files"][0]["address"] == _addr(1)

    def test_select_exits_2_on_a_shortfall(self, monkeypatch):
        s3 = _pool(**{"upload-lon1-1.txt": f"{_addr(1)} {FIFTY_MIB}\n"})
        monkeypatch.setattr(hot_files, "_s3", lambda: s3)
        with pytest.raises(SystemExit) as exc:
            cmd_select_hot_files(self._args(count=5, size_mb=50))
        assert exc.value.code == 2
        assert s3.puts == []

    def test_print_renders_the_selection(self, monkeypatch, capsys):
        s3 = _pool(
            **{
                HOT_FILES_OBJECT_NAME: json.dumps(
                    {
                        "deployment": "DEV-01",
                        "selected_at": "2026-10-18T12:00:00Z",
                        "files": [
                            {
                                "index": 0,
                                "address": _addr(1),
                                "bytes": FIFTY_MIB,
                                "source_key": "upload-lon1-1.txt",
                                "line": 4,
                            }
                        ],
                    }
                )
            }
        )
        monkeypatch.setattr(hot_files, "_s3", lambda: s3)
        cmd_print_hot_files(self._args())
        out = capsys.readouterr().out
        assert "upload-lon1-1.txt:4" in out
        assert "52,428,800" in out

    def test_print_exits_2_when_there_is_none(self, monkeypatch, capsys):
        monkeypatch.setattr(hot_files, "_s3", lambda: _pool())
        with pytest.raises(SystemExit) as exc:
            cmd_print_hot_files(self._args())
        assert exc.value.code == 2
        assert "select-hot-files" in capsys.readouterr().out


class TestHarnessContract:
    def test_the_object_name_is_shared_with_the_harness(self):
        from pathlib import Path

        js = (
            Path(__file__).resolve().parents[1]
            / "saorsa_deploy"
            / "resources"
            / "browser-client"
            / "lib"
            / "hotfiles.mjs"
        ).read_text()
        assert f'HOT_FILES_OBJECT_NAME = "{HOT_FILES_OBJECT_NAME}"' in js

    def test_the_base_url_is_unsigned_public_https(self):
        # The VM signs nothing: SigV4 rejects requests from a clock-skewed
        # client, and the addresses bucket is public-read for exactly this.
        url = hot_files.addresses_base_url("DEV-01")
        assert url.startswith("https://saorsa-testnet-addresses.s3.amazonaws.com/")
        assert url.endswith("/saorsa-deploy/addresses/DEV-01")
