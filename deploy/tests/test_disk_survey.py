"""Tests for the pure disk-survey logic (V2-1147)."""

import pytest

from saorsa_deploy.disk_survey import (
    SurveyFailure,
    SurveyFailureKind,
    classify_survey_failure,
    compare_host_data,
    host_data_bytes,
    should_retry,
)

GIB = 1024**3
TIB = 1024**4


class TestClassifySurveyFailure:
    def test_local_timeout_is_a_timeout(self):
        kind, msg = classify_survey_failure(timed_out=True)
        assert kind is SurveyFailureKind.TIMEOUT
        assert msg == "ssh timed out"

    def test_timeout_wins_over_any_stderr(self):
        # The probe was killed mid-run, so whatever partial stderr it left is
        # not the reason it failed.
        kind, _ = classify_survey_failure(
            timed_out=True, returncode=255, stderr="Connection refused"
        )
        assert kind is SurveyFailureKind.TIMEOUT

    def test_failure_to_launch_ssh_is_a_probe_failure(self):
        kind, msg = classify_survey_failure(os_error="[Errno 2] No such file or directory: 'ssh'")
        assert kind is SurveyFailureKind.PROBE_FAILED
        assert "No such file" in msg

    @pytest.mark.parametrize(
        "stderr",
        [
            "ssh: connect to host 1.2.3.4 port 22: Connection refused",
            "ssh: connect to host 1.2.3.4 port 22: No route to host",
            "ssh: connect to host 1.2.3.4 port 22: Connection timed out",
            "ssh: connect to host bad.example port 22: Name or service not known",
            "Connection closed by 1.2.3.4 port 22",
            "client_loop: send disconnect: Broken pipe",
        ],
    )
    def test_network_level_stderr_is_unreachable(self, stderr):
        kind, msg = classify_survey_failure(returncode=255, stderr=stderr)
        assert kind is SurveyFailureKind.UNREACHABLE
        assert msg == stderr

    @pytest.mark.parametrize(
        "stderr",
        [
            "root@1.2.3.4: Permission denied (publickey).",
            "Received disconnect from 1.2.3.4: Too many authentication failures",
            "Host key verification failed.",
        ],
    )
    def test_auth_level_stderr_is_auth(self, stderr):
        kind, _ = classify_survey_failure(returncode=255, stderr=stderr)
        assert kind is SurveyFailureKind.AUTH

    def test_remote_command_failure_is_a_probe_failure(self):
        # Non-255 means ssh connected and the remote command is what failed —
        # the host answered, so it is not unreachable.
        kind, msg = classify_survey_failure(
            returncode=1, stderr="du: cannot read directory '/mnt': Permission denied"
        )
        assert kind is SurveyFailureKind.PROBE_FAILED
        assert "cannot read directory" in msg

    def test_silent_ssh_error_still_classified_unreachable(self):
        kind, msg = classify_survey_failure(returncode=255, stderr="")
        assert kind is SurveyFailureKind.UNREACHABLE
        assert msg == "ssh exit 255"

    def test_silent_remote_failure_reports_the_exit_code(self):
        kind, msg = classify_survey_failure(returncode=2, stderr="   ")
        assert kind is SurveyFailureKind.PROBE_FAILED
        assert msg == "ssh exit 2"

    def test_auth_marker_beats_a_network_marker_in_the_same_message(self):
        kind, _ = classify_survey_failure(
            returncode=255,
            stderr="Permission denied (publickey). Connection closed by 1.2.3.4",
        )
        assert kind is SurveyFailureKind.AUTH


class TestShouldRetry:
    def test_auth_is_not_retried(self):
        # A rejected key is rejected again a minute later.
        assert should_retry(SurveyFailureKind.AUTH) is False

    @pytest.mark.parametrize(
        "kind",
        [
            SurveyFailureKind.TIMEOUT,
            SurveyFailureKind.UNREACHABLE,
            SurveyFailureKind.PROBE_FAILED,
        ],
    )
    def test_everything_else_is_retried(self, kind):
        assert should_retry(kind) is True


class TestHostDataBytes:
    def test_sums_services_per_host(self):
        services = [
            {"host": "a", "size_bytes": 10},
            {"host": "a", "size_bytes": 5},
            {"host": "b", "size_bytes": 7},
        ]
        assert host_data_bytes(services) == {"a": 15, "b": 7}

    def test_missing_and_none_sizes_count_as_zero(self):
        services = [
            {"host": "a"},
            {"host": "a", "size_bytes": None},
            {"host": "a", "size_bytes": 3},
        ]
        assert host_data_bytes(services) == {"a": 3}

    def test_host_with_no_services_is_absent(self):
        assert host_data_bytes([]) == {}


class TestCompareHostData:
    def test_like_for_like_when_coverage_is_identical(self):
        prev = {"a": 100, "b": 200}
        latest = {"a": 110, "b": 190}
        cmp = compare_host_data(prev, latest)
        assert cmp.common_hosts == ("a", "b")
        assert cmp.raw_delta == 0
        assert cmp.common_delta == 0
        assert cmp.coverage_changed is False
        assert cmp.misleading is False

    def test_dropped_host_is_excluded_from_the_like_for_like_delta(self):
        prev = {"a": 100, "b": 200, "c": 300}
        latest = {"a": 110, "b": 210}
        cmp = compare_host_data(prev, latest)
        assert cmp.dropped_hosts == ("c",)
        assert cmp.added_hosts == ()
        assert cmp.raw_delta == 320 - 600  # -280, the lie
        assert cmp.common_delta == 20  # the truth: both hosts grew
        assert cmp.coverage_changed is True
        assert cmp.misleading is True

    def test_added_host_is_also_excluded(self):
        prev = {"a": 100}
        latest = {"a": 90, "b": 500}
        cmp = compare_host_data(prev, latest)
        assert cmp.added_hosts == ("b",)
        assert cmp.dropped_hosts == ()
        assert cmp.raw_delta == 490
        assert cmp.common_delta == -10
        assert cmp.misleading is True

    def test_v2_1147_survey_39_shape(self):
        """The observed case: 65 hosts, two dropped, raw shows a 1.192 TiB
        reclaim while like-for-like across the 63 common hosts shows growth."""
        # 63 common hosts each grew by 0.341269... GiB (21.5 GiB spread over 63)
        growth_per_host = 21.5 * GIB / 63
        prev = {f"host-{i}": int(400 * GIB) for i in range(63)}
        latest = {f"host-{i}": int(400 * GIB + growth_per_host) for i in range(63)}
        # The two Contabo hosts that dropped out, holding ~0.6 TiB each.
        prev["cntb-US-central-1"] = int(0.6 * TIB)
        prev["cntb-US-central-3"] = int(0.6 * TIB)

        cmp = compare_host_data(prev, latest)

        assert cmp.dropped_hosts == ("cntb-US-central-1", "cntb-US-central-3")
        assert len(cmp.common_hosts) == 63
        # Raw: an apparent reclaim of roughly the two dropped hosts' data.
        assert cmp.raw_delta < 0
        assert cmp.raw_delta / TIB == pytest.approx(-1.17, abs=0.05)
        # Like-for-like: growth of ~21.5 GiB, ~0.34 GiB per host.
        assert cmp.common_delta > 0
        assert cmp.common_delta / GIB == pytest.approx(21.5, abs=0.1)
        assert cmp.common_delta_per_host / GIB == pytest.approx(0.34, abs=0.01)
        # And the pair disagree on direction, which is the whole defect.
        assert cmp.misleading is True

    def test_agreeing_deltas_are_not_flagged_misleading(self):
        # Coverage changed, but both readings still say "grew" — worth a
        # caveat, not an alarm.
        prev = {"a": 100, "c": 50}
        latest = {"a": 400}
        cmp = compare_host_data(prev, latest)
        assert cmp.coverage_changed is True
        assert cmp.raw_delta > 0 and cmp.common_delta > 0
        assert cmp.misleading is False

    def test_a_zero_delta_is_not_a_direction_disagreement(self):
        prev = {"a": 100, "c": 50}
        latest = {"a": 100}
        cmp = compare_host_data(prev, latest)
        assert cmp.common_delta == 0
        assert cmp.raw_delta == -50
        assert cmp.misleading is False

    def test_no_overlap_gives_an_empty_like_for_like(self):
        cmp = compare_host_data({"a": 100}, {"b": 200})
        assert cmp.common_hosts == ()
        assert cmp.common_delta == 0
        assert cmp.common_delta_per_host == 0.0
        assert cmp.dropped_hosts == ("a",)
        assert cmp.added_hosts == ("b",)

    def test_empty_previous_survey(self):
        cmp = compare_host_data({}, {"a": 100})
        assert cmp.common_hosts == ()
        assert cmp.raw_delta == 100
        assert cmp.common_delta == 0


class TestSurveyFailure:
    def test_serialises_for_storage(self):
        f = SurveyFailure(
            host="cntb-US-central-1",
            kind=SurveyFailureKind.TIMEOUT,
            message="ssh timed out",
            attempts=2,
        )
        assert f.to_dict() == {
            "kind": "timeout",
            "message": "ssh timed out",
            "attempts": 2,
        }

    def test_label_reads_operationally(self):
        f = SurveyFailure("h", SurveyFailureKind.TIMEOUT, "ssh timed out")
        assert "probably up" in f.label
