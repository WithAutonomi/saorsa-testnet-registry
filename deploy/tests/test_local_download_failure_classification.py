"""A local download's failure kind, and what a signal means (INFRA-484).

`prod local-downloads` runs `ant` on the operator's own machine, so unlike
every remote path it holds the child's exit status and its stderr directly.
What it recorded was `exit_code=1 (no output file)` -- the same empty signal a
remote downloader's blank row carried.

The ordering is the part worth pinning: a child the OS KILLED is `crash` from
its exit status, decided before any text, because the last stderr line of a
killed `ant -v` is whatever trace it had reached. Classifying that recorded
`error_kind="other"` with reason "Connected to autonomi network (found 5
peers)" for an OOM kill, while the vocabulary reserves `crash` for exactly
that (review finding 3).
"""

import pytest
from saorsa_db.models import TRANSFER_ERROR_KIND_VALUES, TransferErrorKind

from saorsa_deploy.cmd.prod_local_downloads import classify_download_failure
from saorsa_deploy.transfer_errors import REASON_LIMIT

#: What `ant -v` has written by the time it is killed. Not a reason.
TRACE = "Connected to autonomi network (found 5 peers)\n"
#: The real capture's shape: one `Error:` line, last.
ERROR_LINE = TRACE + (
    "Error: Failed to fetch public DataMap: not found: DataMap chunk not found at abcd\n"
)


class TestASignalIsCrash:
    @pytest.mark.parametrize("signal_number", [9, 15, 11, 2])
    def test_a_killed_child_with_no_output_names_the_signal(self, signal_number):
        kind, reason = classify_download_failure(
            exit_code=-signal_number, stderr_text="", file_size=None
        )
        assert kind == TransferErrorKind.CRASH.value
        assert reason == f"ant killed by signal {signal_number}"

    def test_an_incidental_trace_line_is_not_the_reason(self):
        """The exact value the review reproduced, now neither kind nor reason."""
        kind, reason = classify_download_failure(exit_code=-9, stderr_text=TRACE, file_size=None)
        assert kind == TransferErrorKind.CRASH.value
        assert reason == "ant killed by signal 9"
        assert "Connected to autonomi network" not in reason

    def test_the_child_s_own_error_line_still_wins(self):
        kind, reason = classify_download_failure(
            exit_code=-9, stderr_text=ERROR_LINE, file_size=None
        )
        assert kind == TransferErrorKind.CRASH.value
        assert reason.startswith("Failed to fetch public DataMap:")

    def test_a_signal_is_crash_even_with_a_partial_file_on_disk(self):
        kind, _reason = classify_download_failure(exit_code=-9, stderr_text="", file_size=4096)
        assert kind == TransferErrorKind.CRASH.value

    def test_the_no_output_note_is_not_appended_to_a_kill(self):
        """Which signal ended it is the finding; the missing file follows."""
        _kind, reason = classify_download_failure(exit_code=-9, stderr_text="", file_size=0)
        assert "no output file" not in reason


class TestAnOrdinaryFailureIsStillClassifiedFromItsText:
    def test_the_error_line_is_classified(self):
        kind, reason = classify_download_failure(
            exit_code=1, stderr_text=ERROR_LINE, file_size=None
        )
        assert kind == TransferErrorKind.NOT_FOUND.value
        assert reason.startswith("Failed to fetch public DataMap:")
        # The file is missing, so that is still said.
        assert reason.endswith("(no output file)")

    def test_no_stderr_at_all_falls_back_to_the_exit_code(self):
        kind, reason = classify_download_failure(exit_code=1, stderr_text="", file_size=None)
        assert kind == TransferErrorKind.OTHER.value
        assert reason == "exit_code=1 (no output file)"

    def test_a_truncated_file_is_a_failure_with_its_own_reason(self):
        _kind, reason = classify_download_failure(exit_code=1, stderr_text="", file_size=0)
        assert "no output file" in reason

    def test_a_non_zero_exit_with_a_file_omits_the_note(self):
        _kind, reason = classify_download_failure(exit_code=1, stderr_text="", file_size=4096)
        assert "no output file" not in reason


class TestTheContract:
    @pytest.mark.parametrize(
        "exit_code,stderr,size",
        [
            (-9, "", None),
            (-9, TRACE, 0),
            (1, "", None),
            (1, ERROR_LINE, 4096),
            (2, "Error: timeout: x\n", None),
        ],
    )
    def test_the_kind_is_always_in_the_vocabulary_and_the_reason_bounded(
        self, exit_code, stderr, size
    ):
        kind, reason = classify_download_failure(
            exit_code=exit_code, stderr_text=stderr, file_size=size
        )
        assert kind in TRANSFER_ERROR_KIND_VALUES
        assert reason
        assert len(reason) <= REASON_LIMIT
        assert "\n" not in reason

    def test_a_long_error_line_is_clipped(self):
        stderr = "Error: timeout: " + ("x" * 2000) + "\n"
        _kind, reason = classify_download_failure(exit_code=1, stderr_text=stderr, file_size=4096)
        assert len(reason) == REASON_LIMIT
