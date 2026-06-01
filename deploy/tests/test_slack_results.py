"""Tests for the per-kind result-block builders (saorsa_deploy.slack_results)."""
from datetime import datetime

from saorsa_deploy.slack_results import (
    _format_duration_ms,
    canary_result_blocks,
    file_ops_result_blocks,
    prod_upload_result_blocks,
)


def test_format_duration_ms():
    assert _format_duration_ms(None) == "no timing"
    assert _format_duration_ms(1234) == "1.2s"
    assert _format_duration_ms(500) == "0.5s"
    assert _format_duration_ms(227831) == "3m 48s"
    assert _format_duration_ms(60000) == "1m 0s"
    # rounding that would land on 60s rolls over to the next minute
    assert _format_duration_ms(119600) == "2m 0s"


def _text(blocks) -> str:
    """Flatten all section text + fields into one string for assertions."""
    out = []
    for b in blocks:
        t = b.get("text")
        if t and t.get("text"):
            out.append(t["text"])
        for f in b.get("fields") or []:
            out.append(f.get("text", ""))
    return "\n".join(out)


def test_file_ops_blocks_render_totals_and_per_host():
    aggregates = [
        {
            "client_hostname": "host-a",
            "op_type": "upload",
            "file_size_bytes": 10 * 1024 * 1024,
            "success_count": 8,
            "failure_count": 2,
            "avg_duration_ms": 100,
            "min_duration_ms": 50,
            "max_duration_ms": 200,
            "first_started_at": datetime(2026, 4, 26, 10),
            "last_finished_at": datetime(2026, 4, 26, 11),
        }
    ]
    blocks = file_ops_result_blocks(aggregates)
    text = _text(blocks)
    assert "Total successes" in text
    assert "host-a" in text
    assert "8 ok / 2 fail" in text


def test_file_ops_empty():
    blocks = file_ops_result_blocks([])
    assert "no upload/download points" in _text(blocks)


def _agg(host="host-a", op="download", ok=0, fail=3):
    return {
        "client_hostname": host,
        "op_type": op,
        "file_size_bytes": 0,
        "success_count": ok,
        "failure_count": fail,
        "avg_duration_ms": None,
        "min_duration_ms": None,
        "max_duration_ms": None,
        "first_started_at": datetime(2026, 4, 26, 10),
        "last_finished_at": datetime(2026, 4, 26, 11),
    }


def test_file_ops_renders_per_operation_detail():
    op_records = [
        {
            "client_hostname": "host-a",
            "op_type": "download",
            "status": "failure",
            "file_size_bytes": 0,
            "duration_ms": None,
            "started_at": datetime(2026, 6, 1, 15, 56, 0),
        },
        {
            "client_hostname": "host-a",
            "op_type": "download",
            "status": "success",
            "file_size_bytes": 5 * 1024 * 1024,
            "duration_ms": 1234,
            "started_at": datetime(2026, 6, 1, 15, 57, 0),
        },
    ]
    blocks = file_ops_result_blocks([_agg()], op_records=op_records)
    text = _text(blocks)
    assert "Per-operation detail:" in text
    assert "[fail]" in text
    assert "[ok]" in text
    assert "1.2s" in text  # 1234ms rendered human-readably
    assert "5.0MB" in text


def test_file_ops_per_op_detail_is_capped_with_note():
    many = [
        {
            "client_hostname": "host-a",
            "op_type": "download",
            "status": "failure",
            "file_size_bytes": 0,
            "duration_ms": None,
            "started_at": datetime(2026, 6, 1, 0, 0, i % 60),
        }
        for i in range(400)
    ]
    blocks = file_ops_result_blocks([_agg(fail=400)], op_records=many)
    text = _text(blocks)
    assert "most recent 300 of 400" in text


def test_prod_upload_blocks_summarise_and_detail():
    points = [
        {"host": "h1", "status": 0, "chunks_stored": 42, "chunks_failed": 0,
         "total_chunks": 42, "file_name": "a.bin", "source_url": "ua"},
        {"host": "h1", "status": 1, "chunks_stored": 40, "chunks_failed": 2,
         "total_chunks": 42, "file_name": "b.bin", "source_url": "ub"},
        {"host": "h2", "status": 2, "chunks_stored": 0, "chunks_failed": 0,
         "total_chunks": 0, "file_name": "c.bin", "source_url": "uc"},
    ]
    blocks = prod_upload_result_blocks(points)
    text = _text(blocks)
    assert "Uploads OK" in text
    # per-host line for h1: 1 ok / 1 partial / 0 failed
    assert "1 ok / 1 partial / 0 failed" in text
    # per-file detail with status labels
    assert "`a.bin` [ok]" in text
    assert "`c.bin` [dl-failed]" in text


def test_prod_upload_ignores_unknown_status():
    points = [{"host": "h1", "status": 99, "file_name": "x"}]
    blocks = prod_upload_result_blocks(points)
    assert "no prod_upload points" in _text(blocks)


def test_canary_blocks_render_summary_and_histogram():
    summary = {
        "upload_count": 100,
        "chunk_count": 300,
        "probe_count": 1200,
        "total": 100,
        "alive": 80,
        "pending": 15,
        "confirmed_lost": 5,
        "histogram": [("0–6h", 2), ("6–24h", 0), ("1–3d", 3)],
    }
    text = _text(canary_result_blocks(summary))
    assert "Confirmed lost" in text
    assert "Alive" in text
    assert "Hours-to-confirmed-loss" in text
    assert "0–6h: 2" in text
    assert "6–24h: 0" not in text  # zero buckets are dropped
