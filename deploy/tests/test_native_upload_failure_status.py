"""A native upload that never paid is `failed`, not `partial_upload` (INFRA-484).

`partial_upload` permanently excludes a file from canary selection, on the
assumption that some of its chunks are on the network. The native branch of the
daily write test computed the right answer -- `startup_failure` -- and then set
the status to `PARTIAL_UPLOAD` unconditionally, so PROD-UL-01's 2026-10-23 row
reads `partial_upload` with reason `ant exit 1` while its pool claim was
correctly RELEASED beside it: two fields disagreeing about one failure.

At 0.3.11 that status is provably wrong. ant-core converts any error into
`DataError::PartialUpload` as soon as a payment goes out or an address is
stored, so an error reaching `ant-cli` with no result document means zero spend
and zero chunks.

The claim side was always right, which is why the decision is pinned here
against the claim outcome it drives rather than only against the status.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "saorsa_deploy/resources/scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def service():
    return _load("prod_continuous_uploader")


class TestNativeFailureOutcome:
    def test_no_document_and_a_positive_exit_is_failed(self, service):
        """The case that was recorded as `partial_upload`: `ant exit 1`."""
        assert service._native_failure_outcome(None, 1) == service.STATUS_FAILED

    def test_a_document_present_is_partial(self, service):
        """`ant` emits one only for `PartialUpload`, so chunks ARE stored."""
        parsed = {"error": "partial upload: 3/4 stored, 1 failed: ...", "chunks_stored": 3}
        assert service._native_failure_outcome(parsed, 1) == service.STATUS_PARTIAL_UPLOAD

    def test_a_signal_exit_is_partial_even_with_no_document(self, service):
        """A SIGTERM or an OOM kill can land after the payment went out.

        The native path has no payment marker to ask, so this is held as
        partial: one pool entry spent, rather than already-stored chunks put in
        front of a canary.
        """
        assert service._native_failure_outcome(None, -15) == service.STATUS_PARTIAL_UPLOAD
        assert service._native_failure_outcome(None, -9) == service.STATUS_PARTIAL_UPLOAD

    def test_an_unknown_exit_code_still_reads_as_failed(self, service):
        """Preserved verbatim from the predicate this replaced."""
        assert service._native_failure_outcome(None, None) == service.STATUS_FAILED

    def test_the_two_branches_now_agree_on_the_mapping(self, service):
        """`STATUS_FAILED` is what both branches test `startup_failure` on."""
        assert service.STATUS_FAILED != service.STATUS_PARTIAL_UPLOAD
        marker = Path("/nonexistent-payment-marker-infra484")
        # The browser branch's own helper, for the same "nothing was paid" case.
        assert service._wasm_failure_outcome({}, marker) == service.STATUS_FAILED


class TestTheNativeBranchUsesIt:
    """The regression was a computed value being discarded, so pin the shape."""

    def test_the_branch_derives_its_status_from_startup_failure(self, service):
        source = (SCRIPTS / "prod_continuous_uploader.py").read_text()
        native = source[source.index("                else:\n                    killed = rc") :]
        native = native[: native.index("parsed = parsed or {}")]
        assert "_native_failure_outcome(parsed, rc)" in native
        assert "startup_failure = influx_status == STATUS_FAILED" in native
        # The unconditional assignment that caused this must not come back.
        assert "status = ProdUploadStatus.PARTIAL_UPLOAD\n" not in native


class TestEveryReasonSourceIsNormalized:
    """A stored `reason` is bounded and single-line whatever produced it.

    Only the stderr fallback went through `failure_reason`/`clip_reason`; a
    non-empty `reason` from `ant`'s own result document was stored and emitted
    verbatim (INFRA-484 review, finding 4). `ant`'s text is ant-core's raw
    text: a payment failure carries viem's multi-line message with hundreds of
    hex characters of calldata in it. That breaks two things at once — the
    500-character single-line contract this column shares with the browser
    path, and the Influx line protocol, whose field escape handles only
    backslashes and quotes, so one failure splits its own point across three
    physical lines and takes the write batch with it.
    """

    #: A native `partial_upload` document's reason, in the shape a payment
    #: failure actually produces.
    RAW = (
        "partial upload: 3/4 stored, 1 failed: payment error: RPC Request failed\n"
        'Request body: {"data":"0x' + "ab" * 300 + '"}\n'
        "Details: HTTP request failed. Status: 429"
    )

    def test_the_raw_text_is_the_thing_that_needed_bounding(self, service):
        assert len(self.RAW) > service.REASON_LIMIT
        assert "\n" in self.RAW

    def test_clipping_bounds_it_and_removes_the_newlines(self, service):
        clipped = service.clip_reason(self.RAW)
        assert len(clipped) <= service.REASON_LIMIT
        assert "\n" not in clipped
        # Head and tail both kept, so the status code at the end survives.
        assert clipped.startswith("partial upload: 3/4 stored")
        assert clipped.endswith("Status: 429")
        # And the calldata is elided rather than occupying the whole budget.
        assert "hex chars)" in clipped

    def test_a_clipped_reason_emits_one_physical_influx_line(self, service):
        line = service._build_line(
            "prod_upload",
            {"host": "h", "region": "r"},
            {"status": 1, "reason": service.clip_reason(self.RAW)},
        )
        assert "\n" not in line

    def test_the_continuous_uploader_clips_whatever_the_source(self, service):
        """The assignment itself, not just the helper, routes through it."""
        source = (SCRIPTS / "prod_continuous_uploader.py").read_text()
        block = source[source.index("if status is ProdUploadStatus.SUCCESS:\n") :]
        block = block[: block.index("storage_cost = ")]
        assert "reason = clip_reason(" in block
        # The three sources it chooses between are all inside the one call.
        assert "killed_reason" in block
        assert 'parsed.get("reason")' in block
        assert "failure_reason(stderr)" in block

    def test_the_one_shot_uploader_clips_its_document_reason(self):
        source = (SCRIPTS / "prod_uploader.py").read_text()
        assert 'reason = clip_reason(str((parsed or {}).get("reason") or ""))' in source

    def test_clipping_is_idempotent(self, service):
        """It is applied over text `failure_reason` has already clipped."""
        once = service.clip_reason(self.RAW)
        assert service.clip_reason(once) == once
