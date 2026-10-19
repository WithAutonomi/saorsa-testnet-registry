"""`clients select-hot-files` / `print-hot-files` (V2-1617).

The browser cohort's measurement is "many browsers reading the SAME file at the
SAME moment", so which file that is has to be a fact every VM agrees on. These
two commands are what make it one: a single object, written once by the
operator, read by every instance at every round start.

S3 is a fake here. The ordering rule, the shortfall and the idempotence are the
whole behaviour, and none of them needs a bucket.
"""

import json
import pathlib

import pytest

from saorsa_deploy import hot_files
from saorsa_deploy.client_roles import HOT_FILES_OBJECT_NAME
from saorsa_deploy.cmd.hot_files import cmd_print_hot_files, cmd_select_hot_files

FIFTY_MIB = 50 * 1024 * 1024

#: The first two lines of `addresses/DEV-02/upload-lon1-1.txt` as they stood on
#: registry run 681 (2026-10-19), verbatim. `uploader.sh` has published
#: `<address> <bytes> <sha256>` since 2026-09-29, and a parser that counted
#: fields found nothing in a pool made entirely of 50 MiB files (V2-1629).
RUN_681_LINES = (
    "dca2d2ab6cc24da56f69993719b6fc81882631f85b733a64ae6d2fac85b07711 52428800"
    " 0eb8f9ec44481eed8ed640fa00df2e06fd15d6be0b8d980f2f59548c9a6f78f4",
    "13e192e100d8dbc38b2f889737d8a1751931125b9187d5e50b50d6283982eed3 52428800"
    " ff0926b4891680fabc2a85abb9006dc5533da3daed3228f14fc3d1a5273ec0b2",
)


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

    def test_the_real_pool_of_run_681_yields_every_line(self):
        # The exact object `collect_candidates` read on 2026-10-19 and found
        # nothing in: three-field native lines, all of them 50 MiB. This is the
        # regression test for the run V2-1629 abandoned.
        s3 = _pool(**{"upload-lon1-1.txt": "".join(f"{line}\n" for line in RUN_681_LINES)})
        found = hot_files.collect_candidates("DEV-01", FIFTY_MIB, s3=s3)
        assert [(c.address, c.bytes, c.line) for c in found] == [
            (RUN_681_LINES[0].split()[0], FIFTY_MIB, 1),
            (RUN_681_LINES[1].split()[0], FIFTY_MIB, 2),
        ]

    def test_native_three_field_and_wasm_two_field_lines_both_count(self):
        # One shared pool with two producers writing different numbers of
        # fields, which is what `uploader.sh`'s own comment describes. The
        # candidate set is per KEY, so the two-field lines here are a native
        # uploader's pre-2026-09-29 lines in the same object.
        s3 = _pool(
            **{
                "upload-lon1-1.txt": (
                    f"{RUN_681_LINES[0]}\n{_addr(2)} {FIFTY_MIB}\n{RUN_681_LINES[1]}\n"
                )
            }
        )
        found = hot_files.collect_candidates("DEV-01", FIFTY_MIB, s3=s3)
        assert [c.address for c in found] == [
            RUN_681_LINES[0].split()[0],
            _addr(2),
            RUN_681_LINES[1].split()[0],
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

    def test_the_native_uploaders_three_field_line(self):
        # `<address> <bytes> <sha256>`, verbatim from run 681. The hash is for
        # `downloader.sh` to verify the bytes it got back; it is nothing to the
        # hot-file selector, which reads the first two fields and stops.
        assert hot_files.parse_pool_line(RUN_681_LINES[0]) == (
            "dca2d2ab6cc24da56f69993719b6fc81882631f85b733a64ae6d2fac85b07711",
            52428800,
        )

    def test_trailing_fields_beyond_the_hash_are_ignored_too(self):
        # A field count is not the rule: whatever a future producer appends,
        # the first two fields are the address and the size.
        assert hot_files.parse_pool_line(f"{_addr(1)} 1024 deadbeef extra") == (_addr(1), 1024)

    def test_case_is_normalised(self):
        assert hot_files.parse_pool_line(f"{_addr(1).upper()} 1024")[0] == _addr(1)

    # `"a b c"` has three fields, like a native line, but its second field is
    # not a number — so it is None for the right reason, not for its length.
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
        assert url.endswith("/saorsa-deploy/addresses/DEV-01")

    def test_the_base_url_is_the_regional_endpoint(self):
        # Matching `downloader.sh`, which reads the same pool via
        # `https://<bucket>.s3.${AWS_DEFAULT_REGION:-eu-west-2}.amazonaws.com`.
        # The global form reaches a non-us-east-1 bucket only through S3's
        # redirect, and two readers of one pool must not differ in how they get
        # to it.
        from saorsa_deploy.state import S3_ADDRESSES_BUCKET, S3_REGION

        url = hot_files.addresses_base_url("DEV-01")
        assert url.startswith(f"https://{S3_ADDRESSES_BUCKET}.s3.{S3_REGION}.amazonaws.com/")
        script = (
            pathlib.Path(hot_files.__file__).parent / "resources" / "scripts" / "downloader.sh"
        ).read_text()
        assert f".s3.${{AWS_DEFAULT_REGION:-{S3_REGION}}}.amazonaws.com" in script
