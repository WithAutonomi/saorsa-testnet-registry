"""The testnet uploader's `error_kind` holds a kind, not a sentence (INFRA-484).

`uploader.sh` mapped `ant --json`'s own `error` value -- a whole sentence -- into
a field it called `error_kind`, and `influx.py` read it back under that name.
Once `deployment_upload_records` gained both columns that producer would have
filled `error_kind` with prose while four others filled it with a short kind,
and a column holding both can be neither grouped nor counted on, which is the
entire purpose of having it.

The text moved to `reason` and the kind is classified from it. The embedded
Python is executed here as the script executes it, against the committed
captures, rather than being read for shape.

**The field's existing contents are not migrated.** A comparison spanning this
change reads every earlier row's `error_kind` as unclassified prose.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1]
UPLOADER_SH = DEPLOY / "saorsa_deploy/resources/scripts/uploader.sh"
FIXTURES = DEPLOY / "tests/fixtures/transfer-errors"

HEREDOC_OPEN = 'error_parsed=$(python3 - "$output_file" "$stderr_file" <<\'ERRPY\'\n'
HEREDOC_CLOSE = "\nERRPY\n"


@pytest.fixture(scope="module")
def embedded(tmp_path_factory) -> Path:
    """The embedded block, extracted to a file so it can be run as the script runs it."""
    text = UPLOADER_SH.read_text()
    start = text.index(HEREDOC_OPEN) + len(HEREDOC_OPEN)
    code = text[start : text.index(HEREDOC_CLOSE, start)]
    path = tmp_path_factory.mktemp("errpy") / "errpy.py"
    path.write_text(code)
    return path


def _run(embedded: Path, tmp_path: Path, stdout_text: str, stderr_text: str):
    out = tmp_path / "out.json"
    err = tmp_path / "err.txt"
    out.write_text(stdout_text)
    err.write_text(stderr_text)
    result = subprocess.run(
        [sys.executable, "-I", str(embedded), str(out), str(err)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.split("\n")
    return lines[0], lines[1]


class TestTheEmbeddedClassifier:
    def test_a_result_document_s_error_becomes_reason_and_a_kind(self, embedded, tmp_path):
        document = {
            "error": (
                "partial upload: 3/4 stored, 1 failed: close-group PUT shortfall "
                "(dial churn): Stored on 2 peers, need 4"
            ),
            "chunks_stored": 3,
        }
        kind, fields = _run(embedded, tmp_path, json.dumps(document) + "\n", "trace\n")
        assert kind == "quorum"
        assert fields.startswith(',error_kind="quorum",reason="partial upload: 3/4 stored')
        # The sentence is in `reason` and nowhere near `error_kind`.
        assert 'error_kind="partial upload' not in fields

    def test_no_document_falls_back_to_the_stderr_error_line(self, embedded, tmp_path):
        """`ant` prints a document for one error variant only."""
        stderr = (FIXTURES / "native-download-not-found.stderr").read_text()
        kind, fields = _run(embedded, tmp_path, "", stderr)
        assert kind == "not_found"
        assert 'reason="Failed to fetch public DataMap: not found:' in fields
        # The `-v` trace line before it is not the reason.
        assert "Connected to autonomi network" not in fields

    def test_a_success_emits_nothing_at_all(self, embedded, tmp_path):
        """So a successful point is byte-identical to what it always was."""
        document = {"address": "ab" * 32, "chunks_stored": 4, "error": None}
        kind, fields = _run(embedded, tmp_path, json.dumps(document) + "\n", "")
        assert kind == ""
        assert fields == ""

    def test_the_fields_are_escaped_for_line_protocol(self, embedded, tmp_path):
        document = {"error": 'timeout: he said "stop" \\ now'}
        _kind, fields = _run(embedded, tmp_path, json.dumps(document) + "\n", "")
        assert '\\"stop\\"' in fields
        assert "\\\\" in fields

    def test_a_long_reason_is_clipped_to_the_shared_cap(self, embedded, tmp_path):
        document = {"error": "timeout: " + ("x" * 2000)}
        _kind, fields = _run(embedded, tmp_path, json.dumps(document) + "\n", "")
        reason = fields.split('reason="', 1)[1].rstrip('"')
        assert len(reason) == 500

    def test_unreadable_files_do_not_raise(self, embedded, tmp_path):
        """A missing per-op file must not take the upload loop down with it."""
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(embedded),
                str(tmp_path / "missing.json"),
                str(tmp_path / "missing.err"),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "\n\n"


class TestTheScriptWiring:
    def test_error_kind_is_no_longer_the_last_read_r_field(self):
        """It used to absorb the remainder of the line, prose and all."""
        text = UPLOADER_SH.read_text()
        assert "storage_cost_atto gas_cost_wei \\" in text
        assert "storage_cost_atto gas_cost_wei error_kind \\" not in text

    def test_the_influx_line_no_longer_inlines_error_kind(self):
        text = UPLOADER_SH.read_text()
        assert 'error_kind=\\"${error_kind}\\"' not in text
        assert 'line="${line}${error_influx_fields}"' in text

    def test_the_fragment_is_appended_only_when_there_is_a_failure(self):
        text = UPLOADER_SH.read_text()
        assert 'if [ -n "$error_influx_fields" ]; then' in text

    def test_the_non_migration_is_stated_in_the_script(self):
        text = UPLOADER_SH.read_text()
        assert "NOT migrated" in text

    def test_the_script_is_still_valid_bash(self):
        result = subprocess.run(["bash", "-n", str(UPLOADER_SH)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
