"""The classifier, against real captures, real registry rows and the source.

`error_kind` is entirely our invention: `ant` emits no classification and its
exit codes are 0 or 1. So the classifier is only as good as the strings it was
written against, and this file runs it over `deploy/tests/fixtures/
transfer-errors/cases.json` -- whose every case names its provenance -- rather
than over strings invented here.

It also pins the ref: every marker the classifier matches on must still appear
in `ant-core`'s source at the tag `cases.json` names, so a client upgrade that
moves these strings fails loudly instead of quietly reclassifying every failure
as `other`.
"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1]
FIXTURES = DEPLOY / "tests/fixtures/transfer-errors"
CANONICAL = DEPLOY / "saorsa_deploy/transfer_errors.py"
ANT_CLIENT = Path("~/dev/github.com/WithAutonomi/ant-client").expanduser()


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = _load(CANONICAL)
CASES = json.loads((FIXTURES / "cases.json").read_text())


def _case_id(case) -> str:
    return case["id"]


@pytest.mark.parametrize("case", CASES["cases"], ids=_case_id)
def test_every_fixture_case_classifies_as_recorded(case):
    if "text" in case:
        text = case["text"]
    else:
        text = MODULE.failure_reason((FIXTURES / case["stderr_file"]).read_text())
    assert MODULE.classify_transfer_error(text) == case["expected_kind"]


class TestTheRealCapture:
    """What the committed `ant file download` run actually produced."""

    def test_a_failed_download_writes_nothing_to_stdout(self):
        """Including under `--json`, which is why the read path reads stderr.

        The only `println!` of `DownloadJsonResult` sits after the `?` that
        aborts, so there is no document on a failure. A test, not a comment,
        because it is the whole premise of classifying from text.
        """
        for name in (
            "native-download-not-found.stdout",
            "native-download-not-found-json.stdout",
        ):
            assert (FIXTURES / name).read_text() == ""

    def test_the_reason_is_the_error_line_and_not_the_whole_stderr(self):
        stderr = (FIXTURES / "native-download-not-found.stderr").read_text()
        assert "Connected to autonomi network" in stderr
        reason = MODULE.failure_reason(stderr)
        assert reason.startswith("Failed to fetch public DataMap:")
        assert "Connected to autonomi network" not in reason

    def test_the_wrap_is_a_substring_not_a_prefix(self):
        """`not found:` arrives wrapped, so a prefix match would miss it."""
        reason = MODULE.failure_reason((FIXTURES / "native-download-not-found.stderr").read_text())
        assert not reason.startswith("not found:")
        assert MODULE.classify_transfer_error(reason) == "not_found"


class TestOrdering:
    """The order is load-bearing; each of these would be wrong without it."""

    def test_chunk_exhaustion_is_unreachable_and_not_not_found(self):
        """The sentence contains the words "not found" and must not be one."""
        text = (
            "Error: Download failed: encryption error: decryption failed: Generic "
            "error(Chunk not found after 3 deferred retry rounds: abcd)"
        )
        assert MODULE.classify_transfer_error(text) == "unreachable"

    def test_chunk_exhaustion_is_unreachable_and_not_an_encryption_other(self):
        """It is wrapped in `encryption error:`, which maps to `other`."""
        assert MODULE.classify_transfer_error("encryption error: x") == "other"

    def test_a_datamap_miss_is_not_found_because_it_is_an_answer(self):
        text = "not found: DataMap chunk not found at abcd"
        assert MODULE.classify_transfer_error(text) == "not_found"

    def test_a_partial_upload_is_classified_by_its_inner_reason(self):
        """The wrapper is tested last, so the inside decides."""
        inner = (
            "partial upload: 3/4 stored, 1 failed: close-group PUT shortfall "
            "(dial churn): Stored on 2 peers, need 4"
        )
        assert MODULE.classify_transfer_error(inner) == "quorum"
        # And a wrapper with nothing recognisable inside still reads as a
        # shortfall rather than falling through to `other`.
        assert MODULE.classify_transfer_error("partial upload: 3/4 stored") == "quorum"

    def test_over_cap_wins_over_everything(self):
        text = "browser files are limited to 1000000000 bytes and also timeout: whatever"
        assert MODULE.classify_transfer_error(text) == "over_cap"

    def test_a_manifest_size_validation_failure_is_not_an_over_cap_skip(self):
        """INFRA-511: `invalid public file size` is not the cap.

        It is `browser/manifest.rs`'s self-encryption / `MAX_SAFE_JS_INTEGER`
        bounds check, and `over_cap` is recorded as a SKIP -- so matching it
        dropped an ordinary manifest-validation failure out of every
        success-rate denominator.
        """
        text = "invalid public file size 2000000000"
        assert MODULE.classify_transfer_error(text) == "other"

    def test_insufficient_peers_is_quorum_even_when_it_talks_about_quotes(self):
        """Deliberately unlike the browser classifier; see the block's comment."""
        text = "insufficient peers: Got 0 quotes, need 1"
        assert MODULE.classify_transfer_error(text) == "quorum"

    def test_an_unmatched_string_is_other_and_never_empty(self):
        assert MODULE.classify_transfer_error("ant exit 1") == "other"
        assert MODULE.classify_transfer_error("") == "other"
        assert MODULE.classify_transfer_error(None) == "other"


class TestReasonClipping:
    """A native `reason` and a browser `reason` must be the same shape."""

    def test_the_cap_is_the_browser_path_cap(self):
        assert MODULE.REASON_LIMIT == 500

    def test_a_long_reason_keeps_its_head_and_its_tail(self):
        text = "HEAD" + ("x" * 2000) + "TAIL"
        clipped = MODULE.clip_reason(text)
        assert len(clipped) == 500
        assert clipped.startswith("HEAD")
        assert clipped.endswith("TAIL")
        assert "…" in clipped

    def test_whitespace_is_collapsed(self):
        assert MODULE.clip_reason("a\n  b\tc  ") == "a b c"

    def test_a_long_hex_blob_is_elided_by_length(self):
        blob = "ab" * 100
        assert MODULE.clip_reason(f"calldata 0x{blob} end") == ("calldata 0x…(200 hex chars) end")

    def test_a_chunk_address_is_not_elided(self):
        """64 hex characters with no `0x` is an identifier, not calldata."""
        address = "d3da53369b9d08264405cccd5f3f6284be26a041787f535f570761ff7ef790d4"
        assert address in MODULE.clip_reason(f"not found: DataMap chunk not found at {address}")

    def test_the_observed_rows_are_already_within_the_cap(self):
        """They were written by `describeError`, whose cap this mirrors."""
        for case in CASES["cases"]:
            if case["provenance"] != "observed":
                continue
            assert len(case["text"]) <= MODULE.REASON_LIMIT, case["id"]


class TestFailureReason:
    def test_the_last_error_line_wins(self):
        stderr = "Error: first\ntrace\nError: second\n"
        assert MODULE.failure_reason(stderr) == "second"

    def test_the_whole_error_BLOCK_is_kept_not_just_its_first_line(self):
        """`eprintln!("Error: {e:#}")` does not promise one physical line.

        An error whose Display carries newlines prints several, and taking the
        first threw away the actual cause — "payment error: RPC Request failed"
        kept, "nonce too low" lost, which is the whole reason the field exists
        (review finding 1).
        """
        stderr = (
            "Connected to autonomi network (found 5 peers)\n"
            "Error: payment error: RPC Request failed\n"
            "URL: https://rpc.example.invalid\n"
            "Details: nonce too low\n"
        )
        reason = MODULE.failure_reason(stderr)
        assert reason == (
            "payment error: RPC Request failed URL: https://rpc.example.invalid "
            "Details: nonce too low"
        )
        # The trace BEFORE the block is still not part of it.
        assert "Connected to autonomi network" not in reason
        assert MODULE.classify_transfer_error(reason) == "payment"

    def test_a_long_block_keeps_the_cause_at_its_tail(self):
        """Which is what the head-and-tail clip is for."""
        stderr = (
            "Error: payment error: RPC failed\n"
            + "\n".join(f"frame {i}" for i in range(200))
            + "\nDetails: nonce too low\n"
        )
        reason = MODULE.failure_reason(stderr)
        assert len(reason) == MODULE.REASON_LIMIT
        assert reason.startswith("payment error: RPC failed")
        assert reason.endswith("Details: nonce too low")

    def test_a_single_line_error_is_unchanged(self):
        """The committed capture's shape, which is the common case."""
        stderr = (FIXTURES / "native-download-not-found.stderr").read_text()
        assert MODULE.failure_reason(stderr) == (
            "Failed to fetch public DataMap: not found: DataMap chunk not found "
            "at d3da53369b9d08264405cccd5f3f6284be26a041787f535f570761ff7ef790d4"
        )

    def test_blank_lines_inside_a_block_do_not_split_it(self):
        stderr = "Error: timeout: waiting\n\nDetails: 60s elapsed\n"
        assert MODULE.failure_reason(stderr) == "timeout: waiting Details: 60s elapsed"

    def test_a_block_is_still_refused_when_only_the_error_line_counts(self):
        """`require_error_line` is about a KILL, and is unaffected by this."""
        assert MODULE.failure_reason("trace\nmore trace\n", require_error_line=True) == ""
        assert (
            MODULE.failure_reason("Error: timeout: x\nDetails: y\n", require_error_line=True)
            == "timeout: x Details: y"
        )

    def test_a_stream_with_no_error_line_falls_back_to_the_last_line(self):
        """Covers a kill that ended the process before it could print one."""
        assert MODULE.failure_reason("trace one\ntrace two\n") == "trace two"

    def test_an_empty_stream_is_an_empty_reason(self):
        assert MODULE.failure_reason("") == ""
        assert MODULE.failure_reason(None) == ""


#: Markers whose literal does not appear in `ant-core/src/data/error.rs` as
#: written, with the file that does carry them. The `#[error(...)]` attribute
#: splits a few of these across the format string and its payload.
_MARKER_SOURCES = {
    "Chunk not found after": "ant-core/src/data/client/file/native.rs",
    "DataMap chunk not found": "ant-core/src/data/client/data.rs",
    "browser files are limited to": "ant-core/src/browser.rs",
    "partial upload:": "ant-core/src/data/error.rs",
}


@pytest.mark.skipif(
    not (ANT_CLIENT / ".git").exists(),
    reason="the ant-client checkout is not present (CI); this is a developer tripwire",
)
class TestTheMarkersStillExistAtThePinnedRef:
    """A client upgrade that moves these strings must fail loudly.

    Quietly reclassifying every failure as `other` is the alternative, and the
    kind's own distribution is the only thing that would ever have shown it.
    """

    def _grep(self, needle: str) -> str:
        ref = CASES["ant_client_ref"]
        result = subprocess.run(
            ["git", "grep", "-l", "-F", needle, ref, "--", "ant-core", "ant-cli"],
            cwd=ANT_CLIENT,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def test_the_ref_exists(self):
        ref = CASES["ant_client_ref"]
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
            cwd=ANT_CLIENT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{ref} is not in the ant-client checkout"

    @pytest.mark.parametrize("marker", [m for m, _ in MODULE.TRANSFER_ERROR_MARKERS])
    def test_each_marker_is_still_in_the_source(self, marker):
        hits = self._grep(marker)
        assert hits, f"{marker!r} no longer appears in ant-core/ant-cli at the pinned ref"
        expected_file = _MARKER_SOURCES.get(marker)
        if expected_file:
            assert expected_file in hits, f"{marker!r} moved: now in {hits!r}"


class TestASignalIsCrashNotWhateverTheTraceSaid:
    """A killed child's last stderr line is not a reason (review finding 3).

    `ant -v` writes continuously, so a child the OS kills leaves whatever trace
    it had reached as the last line. Classifying that recorded
    `error_kind="other"` with reason "Connected to autonomi network (found 5
    peers)" for an OOM kill -- while the vocabulary reserves `crash` for
    exactly this case and the exit status says so outright.

    `require_error_line` is what lets a caller that knows it was a signal take
    the child's own `Error:` line and nothing else.
    """

    TRACE = "Connected to autonomi network (found 5 peers)\n"

    def test_an_incidental_trace_line_is_not_taken_as_the_reason(self):
        assert MODULE.failure_reason(self.TRACE) == (
            "Connected to autonomi network (found 5 peers)"
        )
        assert MODULE.failure_reason(self.TRACE, require_error_line=True) == ""

    def test_the_child_s_own_error_line_still_wins(self):
        stderr = self.TRACE + "Error: Upload failed: timeout: whatever\n"
        assert (
            MODULE.failure_reason(stderr, require_error_line=True)
            == "Upload failed: timeout: whatever"
        )

    def test_empty_stderr_yields_nothing_to_fall_back_on(self):
        assert MODULE.failure_reason("", require_error_line=True) == ""

    def test_the_default_is_unchanged(self):
        """Every non-signal caller keeps the last-line fallback."""
        assert MODULE.failure_reason("trace one\ntrace two\n") == "trace two"

    def test_crash_is_in_the_vocabulary_and_no_marker_produces_it(self):
        """So it can only ever come from an exit status, never from text."""
        assert "crash" in MODULE.TRANSFER_ERROR_KINDS
        assert "crash" not in {kind for _, kind in MODULE.TRANSFER_ERROR_MARKERS}
        assert MODULE.classify_transfer_error("killed by signal 9") == "other"
