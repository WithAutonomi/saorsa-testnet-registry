from datetime import datetime, timezone

from saorsa_deploy.cmd.print_file_ops import _summarise_host

# A fixed "now" so elapsed-second assertions stay deterministic.
NOW = datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc)
# 2026-04-23 11:00:00 UTC in nanoseconds — exactly 1h before NOW.
START_NS = int(
    datetime(2026, 4, 23, 11, 0, 0, tzinfo=timezone.utc).timestamp()
    * 1_000_000_000
)


def _upload(start_offset_ns: int, status: int, total_ms: int | None, concurrency):
    return {
        "_measurement": "upload",
        "host": "client-1",
        "start_ns": START_NS + start_offset_ns,
        "status": status,
        "total_ms": total_ms,
        "store_concurrency": concurrency,
    }


def _download(start_offset_ns: int, status: int, total_ms: int | None):
    return {
        "_measurement": "download",
        "host": "client-1",
        "start_ns": START_NS + start_offset_ns,
        "status": status,
        "total_ms": total_ms,
        "store_concurrency": None,
    }


def test_summarise_upload_groups_by_concurrency():
    ops = [
        _upload(0, status=0, total_ms=1000, concurrency=4),
        _upload(1_000_000_000, status=0, total_ms=3000, concurrency=4),
        _upload(2_000_000_000, status=1, total_ms=500, concurrency=4),
        _upload(3_000_000_000, status=0, total_ms=800, concurrency=8),
        _upload(4_000_000_000, status=0, total_ms=1200, concurrency=8),
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

    # Sorted by concurrency; avg_ms computed from status==0 ops only
    # (failed concurrency=4 op is counted in count but excluded from avg).
    assert summary["by_concurrency"] == [
        (4, 3, 2000.0),
        (8, 2, 1000.0),
    ]


def test_summarise_download_skips_concurrency_grouping():
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
    assert summary["by_concurrency"] == []


def test_summarise_handles_missing_total_ms_in_averages():
    ops = [
        _upload(0, status=0, total_ms=None, concurrency=4),
        _upload(1_000_000_000, status=0, total_ms=2000, concurrency=4),
    ]

    summary = _summarise_host(ops, op_type="upload", now=NOW)

    # Only the op with a real total_ms contributes.
    assert summary["overall_avg_ms"] == 2000.0
    assert summary["by_concurrency"] == [(4, 2, 2000.0)]


def test_summarise_reports_none_avg_when_no_successful_ops():
    ops = [
        _upload(0, status=1, total_ms=1000, concurrency=4),
        _upload(1_000_000_000, status=1, total_ms=2000, concurrency=4),
    ]

    summary = _summarise_host(ops, op_type="upload", now=NOW)

    assert summary["successes"] == 0
    assert summary["failures"] == 2
    assert summary["overall_avg_ms"] is None
    assert summary["by_concurrency"] == [(4, 2, None)]


def test_summarise_reports_none_times_when_no_start_ns():
    ops = [
        {
            "_measurement": "upload",
            "host": "client-1",
            "start_ns": None,
            "status": 0,
            "total_ms": 1000,
            "store_concurrency": 4,
        },
    ]

    summary = _summarise_host(ops, op_type="upload", now=NOW)

    assert summary["started_at"] is None
    assert summary["elapsed_s"] is None


def test_summarise_groups_unset_concurrency_separately():
    ops = [
        _upload(0, status=0, total_ms=1000, concurrency=None),
        _upload(1_000_000_000, status=0, total_ms=2000, concurrency=4),
    ]

    summary = _summarise_host(ops, op_type="upload", now=NOW)

    # Non-None concurrencies first, then None — per the sort key.
    assert summary["by_concurrency"] == [
        (4, 1, 2000.0),
        (None, 1, 1000.0),
    ]
