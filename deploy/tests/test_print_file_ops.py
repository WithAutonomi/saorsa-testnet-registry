from datetime import datetime, timezone
from unittest.mock import patch

from saorsa_deploy.cmd.print_file_ops import _build_payment_map, _summarise_host

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
