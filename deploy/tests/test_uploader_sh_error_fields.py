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

HEREDOC_OPEN = 'error_parsed=$(python3 - "$output_file" "$stderr_file" "$exit_code" <<\'ERRPY\'\n'
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


#: The `-v` stderr of a perfectly good upload. `uploader.sh` always passes `-v`,
#: so this is what an ungated stderr fallback classified as a failure.
HEALTHY_STDERR = "Connected to autonomi network (found 5 peers)\n"


def _run(embedded: Path, tmp_path: Path, stdout_text: str, stderr_text: str, exit_code="1"):
    out = tmp_path / "out.json"
    err = tmp_path / "err.txt"
    out.write_text(stdout_text)
    err.write_text(stderr_text)
    result = subprocess.run(
        [sys.executable, "-I", str(embedded), str(out), str(err), exit_code],
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
        kind, fields = _run(embedded, tmp_path, json.dumps(document) + "\n", "", exit_code="0")
        assert kind == ""
        assert fields == ""

    def test_a_success_with_ordinary_trace_on_stderr_emits_nothing(self, embedded, tmp_path):
        """The regression the review caught (INFRA-484 finding 3).

        `uploader.sh` always runs `ant -v`, so stderr is NEVER empty on a
        healthy upload. The fallback to its last line was ungated, which gave
        every successful upload `error_kind="other"` and a reason of
        "Connected to autonomi network (found 5 peers)" -- archived into
        Postgres and counted in the kind distribution as a failure that never
        happened. The old success test passed only because it used empty
        stderr.
        """
        document = {"address": "ab" * 32, "chunks_stored": 4, "elapsed_secs": 1.2}
        kind, fields = _run(
            embedded, tmp_path, json.dumps(document) + "\n", HEALTHY_STDERR, exit_code="0"
        )
        assert kind == ""
        assert fields == ""

    def test_a_non_zero_exit_with_the_same_stderr_does_classify(self, embedded, tmp_path):
        """The gate is the outcome, not the presence of stderr."""
        stderr = HEALTHY_STDERR + "Error: Upload failed: payment error: nonce too low\n"
        kind, fields = _run(embedded, tmp_path, "", stderr, exit_code="1")
        assert kind == "payment"
        assert 'reason="Upload failed: payment error: nonce too low"' in fields
        assert "Connected to autonomi network" not in fields

    def test_a_result_document_error_classifies_whatever_the_exit_code(self, embedded, tmp_path):
        """`ant --json` omits `error` on success and `ant-wasm` sets it null.

        So the VALUE of `error` is evidence of a failure in its own right --
        the same rule `prod_uploader._classify` applies.
        """
        document = {"error": "timeout: Timeout waiting for store response after 60s"}
        for exit_code in ("0", "1"):
            kind, fields = _run(
                embedded, tmp_path, json.dumps(document) + "\n", HEALTHY_STDERR, exit_code
            )
            assert kind == "timeout", exit_code
            assert "Timeout waiting for store response" in fields, exit_code

    def test_a_missing_exit_code_argument_does_not_crash(self, embedded, tmp_path):
        """Defensive: the block is run by a shell that could be mid-edit."""
        out = tmp_path / "o.json"
        err = tmp_path / "e.txt"
        out.write_text("")
        err.write_text(HEALTHY_STDERR)
        result = subprocess.run(
            [sys.executable, "-I", str(embedded), str(out), str(err)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "\n\n"

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
    def test_the_block_is_given_the_exit_code(self):
        """It cannot gate on the outcome without being told what it was."""
        text = UPLOADER_SH.read_text()
        assert '"$output_file" "$stderr_file" "$exit_code" <<' in text

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
