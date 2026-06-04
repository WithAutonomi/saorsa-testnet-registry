from datetime import datetime, timezone
from unittest.mock import patch

from saorsa_deploy.cmd.print_file_ops import (
    _build_bootstrap_cache_map,
    _build_payment_map,
    _clock_skew_label,
    _clock_skew_seconds,
    _fmt_started,
    _summarise_host,
)

# A fixed "now" so elapsed-second assertions stay deterministic.
NOW = datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc)
# 2026-04-23 11:00:00 UTC in nanoseconds — exactly 1h before NOW.
START_NS = int(
    datetime(2026, 4, 23, 11, 0, 0, tzinfo=timezone.utc).timestamp()
    * 1_000_000_000
)


def _upload(start_offset_ns: int, status: int, total_ms: int | None):
    return {
        "_measurement": "upload",
        "host": "client-1",
        "start_ns": START_NS + start_offset_ns,
        "status": status,
        "total_ms": total_ms,
    }


def _download(start_offset_ns: int, status: int, total_ms: int | None):
    return {
        "_measurement": "download",
        "host": "client-1",
        "start_ns": START_NS + start_offset_ns,
        "status": status,
        "total_ms": total_ms,
    }


def test_summarise_upload_aggregates_counts_and_avg():
    ops = [
        _upload(0, status=0, total_ms=1000),
        _upload(1_000_000_000, status=0, total_ms=3000),
        _upload(2_000_000_000, status=1, total_ms=500),
        _upload(3_000_000_000, status=0, total_ms=800),
        _upload(4_000_000_000, status=0, total_ms=1200),
    ]

    summary = _summarise_host(ops, op_type="upload", now=NOW)

    assert summary["started_at"] == datetime(
        2026, 4, 23, 11, 0, 0, tzinfo=timezone.utc
    )
    assert summary["elapsed_s"] == 3600.0
    assert summary["total"] == 5
    assert summary["successes"] == 4
    assert summary["failures"] == 1
    assert summary["overall_avg_ms"] == (1000 + 3000 + 800 + 1200) / 4


def test_summarise_download_aggregates_counts_and_avg():
    ops = [
        _download(0, status=0, total_ms=2000),
        _download(1_000_000_000, status=0, total_ms=4000),
        _download(2_000_000_000, status=1, total_ms=500),
    ]

    summary = _summarise_host(ops, op_type="download", now=NOW)

    assert summary["total"] == 3
    assert summary["successes"] == 2
    assert summary["failures"] == 1
    assert summary["overall_avg_ms"] == 3000.0


def test_summarise_handles_missing_total_ms_in_averages():
    ops = [
        _upload(0, status=0, total_ms=None),
        _upload(1_000_000_000, status=0, total_ms=2000),
    ]

    summary = _summarise_host(ops, op_type="upload", now=NOW)

    # Only the op with a real total_ms contributes.
    assert summary["overall_avg_ms"] == 2000.0


def test_summarise_reports_none_avg_when_no_successful_ops():
    ops = [
        _upload(0, status=1, total_ms=1000),
        _upload(1_000_000_000, status=1, total_ms=2000),
    ]

    summary = _summarise_host(ops, op_type="upload", now=NOW)

    assert summary["successes"] == 0
    assert summary["failures"] == 2
    assert summary["overall_avg_ms"] is None


def test_build_payment_map_returns_label_per_upload_host():
    state = {
        "clients": [
            {
                "role": "upload", "region": "tor1", "client_index": 9,
                "payment_type": "merkle",
            },
            {
                "role": "upload", "region": "lon1", "client_index": 1,
                "payment_type": None,
            },
            {
                "role": "download", "region": "fra1", "client_index": 2,
                "payment_type": None,
            },
        ]
    }
    with patch(
        "saorsa_deploy.cmd.print_file_ops.load_client_state",
        return_value=state,
    ):
        m = _build_payment_map("STG-01")

    assert m == {
        "STG-01-ant-client-upload-tor1-9": "merkle",
        "STG-01-ant-client-upload-lon1-1": "default",
    }


def test_build_payment_map_returns_empty_when_state_missing():
    with patch(
        "saorsa_deploy.cmd.print_file_ops.load_client_state",
        side_effect=RuntimeError("no state"),
    ):
        assert _build_payment_map("MISSING-01") == {}


def test_summarise_reports_none_times_when_no_start_ns():
    ops = [
        {
            "_measurement": "upload",
            "host": "client-1",
            "start_ns": None,
            "status": 0,
            "total_ms": 1000,
        },
    ]

    summary = _summarise_host(ops, op_type="upload", now=NOW)

    assert summary["started_at"] is None
    assert summary["elapsed_s"] is None


# ---------------------------------------------------------------------------
# Clock-skew detection + Started-column derivation
# ---------------------------------------------------------------------------


def _op_with_skew(completed, total_ms, skew_s):
    """An op whose server `_time` is `completed`, server-frame start is
    `completed - total_ms`, and whose client `start_ns` is shifted by `skew_s`
    seconds (mimicking a clock-skewed client emitting into the future)."""
    server_start = completed.timestamp() - total_ms / 1000.0
    return {
        "_time": completed,
        "start_ns": int((server_start + skew_s) * 1_000_000_000),
        "total_ms": total_ms,
    }


def test_clock_skew_seconds_recovers_offset():
    op = _op_with_skew(NOW, total_ms=180_000, skew_s=10_800.0)  # +3h
    assert abs(_clock_skew_seconds([op]) - 10_800.0) < 1.0


def test_clock_skew_seconds_is_median_across_ops():
    ops = [
        _op_with_skew(NOW, total_ms=1000, skew_s=10_790.0),
        _op_with_skew(NOW, total_ms=1000, skew_s=10_800.0),
        _op_with_skew(NOW, total_ms=1000, skew_s=10_820.0),
    ]
    assert abs(_clock_skew_seconds(ops) - 10_800.0) < 1.0


def test_clock_skew_seconds_none_without_usable_ops():
    assert _clock_skew_seconds([]) is None
    assert _clock_skew_seconds([{"_time": NOW, "start_ns": None, "total_ms": 1}]) is None


def test_clock_skew_label_formats_positive_skew():
    op = _op_with_skew(NOW, total_ms=180_000, skew_s=10_800.0)
    assert _clock_skew_label([op]) == "+3h00m"


def test_clock_skew_label_formats_negative_skew():
    op = _op_with_skew(NOW, total_ms=180_000, skew_s=-10_800.0)
    assert _clock_skew_label([op]) == "-3h00m"


def test_clock_skew_label_none_for_unskewed_host():
    # A few seconds of drift / write-lag must not be reported as injected skew.
    op = _op_with_skew(NOW, total_ms=2000, skew_s=5.0)
    assert _clock_skew_label([op]) is None


def test_fmt_started_is_completion_minus_duration_not_skewed_start():
    # start_ns is +3h in the future; Started must still track _time - duration.
    op = _op_with_skew(NOW, total_ms=180_000, skew_s=10_800.0)
    assert _fmt_started(op) == "2026-04-23 11:57:00"
    assert _fmt_started({"_time": NOW, "total_ms": None}) == "2026-04-23 12:00:00"
    assert _fmt_started({"_time": None, "total_ms": 1000}) == "-"


def test_build_bootstrap_cache_map_covers_both_roles():
    state = {
        "clients": [
            {
                "role": "upload", "region": "tor1", "client_index": 9,
                "bootstrap_cache": True,
            },
            {
                "role": "download", "region": "fra1", "client_index": 2,
                "bootstrap_cache": False,
            },
            {
                # predates the option — absent from the map
                "role": "download", "region": "ams3", "client_index": 3,
            },
        ]
    }
    with patch(
        "saorsa_deploy.cmd.print_file_ops.load_client_state",
        return_value=state,
    ):
        m = _build_bootstrap_cache_map("STG-01")

    assert m == {
        "STG-01-ant-client-upload-tor1-9": True,
        "STG-01-ant-client-download-fra1-2": False,
    }


def test_build_bootstrap_cache_map_returns_empty_when_state_missing():
    with patch(
        "saorsa_deploy.cmd.print_file_ops.load_client_state",
        side_effect=RuntimeError("no state"),
    ):
        assert _build_bootstrap_cache_map("MISSING-01") == {}
