"""`analysis swarm-reads`: every section and all four gates, against fixtures.

The fixture is one deployment carrying both arms of an ADR-0026 swarm run — a
swarm cohort (native readers plus browsers) on the even rounds and a control
cohort (released native) on the odd ones — with four rounds that carry rows, a
missing round in each arm, an off-parity row, and rows of a third extension to
ignore. Every expected figure is worked out on paper in
`fixtures/swarm-reads/README.md`, and the numbers asserted here are those — not
whatever the code happened to produce, which is the only way a test over
arithmetic this interlinked says anything.

The gate the whole report exists for is G3: the median served `get_rpc` for the
hot file's chunks over the swarm rounds against the median over the control
rounds. It is the only figure that answers "do the nodes serve fewer GETs when a
swarm forms", and it is the one most easily got confidently wrong — a chunk set
read from the wrong rounds, or a missing round folded in as a zero, would move it
without making anything look broken.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from saorsa_db.models.browser_download_record import DeploymentBrowserDownloadRecord
from saorsa_db.models.hot_file_download_record import (
    SWARM_COUNTER_COLUMNS,
    DeploymentHotFileDownloadRecord,
)

from saorsa_deploy.analysis.swarm_reads import EXIT_ERROR, EXIT_GATE_FAIL, cmd_swarm_reads
from saorsa_deploy.browser_reads import rounds_in_window
from saorsa_deploy.browser_traces import normalise_trace_doc
from saorsa_deploy.node_gets import (
    CHUNK_ADDRESS_FIELD,
    GET_RPC_EVENT,
    HOST_TERMS_SIZE,
    REQUEST_FIELD,
    STORAGE_OUTCOME_FIELD,
    get_rpc_query_body,
    parse_get_rpc_response,
    top_hosts,
)
from saorsa_deploy.swarm_reads import (
    ARM_CONTROL,
    ARM_SWARM,
    DISCOVERY_TOTAL_COLUMNS,
    GATE_FAIL,
    GATE_NOT_ASSESSED,
    GATE_PASS,
    GATE_WARN,
    SwarmReadsError,
    arm_of_round,
    as_epoch_bounds,
    build_report,
    check_parities,
    chunk_set_from_traces,
    evaluate_gates,
    render_markdown,
    rows_of_arm,
    summarise_browser_gets,
    verdict_of,
    window_round_ids,
)

FIXTURES = Path(__file__).parent / "fixtures" / "swarm-reads"

PERIOD = 600
R0 = 2987190  # boundary 2026-10-18T09:00:00Z
WINDOW_START = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)
#: Six rounds, R0..R0+5 — the last two of which nobody ran.
WINDOW_END = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
#: Three rounds, R0..R0+2 — two swarm and ONE control, which is G3's NOT
#: ASSESSED case.
NARROW_END = datetime(2026, 10, 18, 9, 30, tzinfo=timezone.utc)

PASS_THRESHOLDS = {
    "min_completion": 0.85,
    "min_peer_share": 0.70,
    "max_get_ratio": 0.80,
}


def _rows(name: str) -> list[dict]:
    raw = json.loads((FIXTURES / name).read_text())
    for row in raw:
        row["started_at"] = datetime.fromisoformat(row["started_at"])
    return raw


def _json(name: str):
    return json.loads((FIXTURES / name).read_text())


def _trace_docs() -> list[dict]:
    return [normalise_trace_doc(d) for d in _json("trace-docs.json")]


def _gets() -> dict[int, dict]:
    return {
        int(round_id): parse_get_rpc_response(response)
        for round_id, response in _json("get-rpc-responses.json").items()
    }


def _report(*, window_end: datetime = WINDOW_END, **thresholds) -> dict:
    native = rounds_in_window(_rows("native-rows.json"), PERIOD, WINDOW_START, window_end)
    browser = rounds_in_window(_rows("browser-rows.json"), PERIOD, WINDOW_START, window_end)
    swarm_native = [r for r in native if r["extension"] == "swarm"]
    swarm_browser = [r for r in browser if r["extension"] == "swarm"]
    control_native = [r for r in native if r["extension"] == "control"]
    kept = len(swarm_native) + len(swarm_browser) + len(control_native)
    docs = _trace_docs()
    round_ids = window_round_ids(PERIOD, WINDOW_START, window_end)
    swarm_rounds = [r for r in round_ids if arm_of_round(r) == ARM_SWARM]
    gets = {r: v for r, v in _gets().items() if r in round_ids}
    return build_report(
        deployment="DEV-03",
        deployment_id=702,
        swarm_extension="swarm",
        control_extension="control",
        swarm_native=swarm_native,
        swarm_browser=swarm_browser,
        control_native=control_native,
        ignored_rows=len(native) + len(browser) - kept,
        config_entries=_json("hot-file-config.json"),
        browser_config_entries=_json("browser-config.json"),
        hot_files=_json("hot-files.json"),
        hot_files_source="deploy_args (hot-file selection snapshot)",
        chunk_set=chunk_set_from_traces(docs, swarm_rounds),
        gets_by_round=gets,
        trace_docs=docs,
        period_secs=PERIOD,
        period_source="deploy_args.hot_file_readers",
        window_start=WINDOW_START,
        window_end=window_end,
        **{**PASS_THRESHOLDS, **thresholds},
    )


@pytest.fixture
def report():
    return _report()


def _round(report: dict, round_id: int) -> dict:
    return next(r for r in report["rounds"] if r["round_id"] == round_id)


# ---------------------------------------------------------------------------
# The window, the arms and the rounds
# ---------------------------------------------------------------------------


def test_window_round_ids_is_half_open():
    """Two adjacent windows partition a run's rounds exactly once between them."""
    assert window_round_ids(PERIOD, WINDOW_START, WINDOW_END) == list(range(R0, R0 + 6))
    assert window_round_ids(PERIOD, WINDOW_START, NARROW_END) == [R0, R0 + 1, R0 + 2]
    # The end boundary belongs to the NEXT window, not this one.
    first = window_round_ids(PERIOD, WINDOW_START, NARROW_END)
    second = window_round_ids(PERIOD, NARROW_END, WINDOW_END)
    assert set(first) & set(second) == set()
    assert sorted(first + second) == list(range(R0, R0 + 6))


def test_a_window_starting_mid_round_takes_the_next_boundary():
    """A round whose boundary precedes the window is not in it.

    The window selects on BOUNDARIES, so a window opening one second after a
    round began excludes that round entirely rather than covering part of it —
    which is what makes a labelled phase of a run a whole number of rounds.
    """
    start = datetime(2026, 10, 18, 9, 0, 1, tzinfo=timezone.utc)
    assert window_round_ids(PERIOD, start, NARROW_END) == [R0 + 1, R0 + 2]


def test_even_rounds_are_the_swarm_arm_and_odd_the_control():
    assert arm_of_round(R0) == ARM_SWARM
    assert arm_of_round(R0 + 1) == ARM_CONTROL
    assert [arm_of_round(r) for r in (R0 + 2, R0 + 3, R0 + 4, R0 + 5)] == [
        ARM_SWARM,
        ARM_CONTROL,
        ARM_SWARM,
        ARM_CONTROL,
    ]


def test_totals_count_what_was_read_and_what_was_ignored(report):
    assert report["totals"] == {
        "rounds": 6,
        "rounds_missing": 2,
        # 8 in-arm rows plus the one off-parity row.
        "swarm_native_rows": 9,
        "swarm_browser_rows": 4,
        "control_native_rows": 8,
        # Two `other` native rows, one with no extension, one `other` browser.
        "ignored_rows": 4,
        "off_parity_rows": 1,
        "trace_records": 22,
    }


def test_a_round_nobody_ran_is_missing_not_zero(report):
    for round_id in (R0 + 4, R0 + 5):
        entry = _round(report, round_id)
        assert entry["missing"] is True
        assert entry["native"]["attempted"] == 0
        assert entry["native"]["completion"] is None
        assert entry["native"]["peer_share"] is None
    assert report["arms"][ARM_SWARM]["rounds_missing"] == [R0 + 4]
    assert report["arms"][ARM_CONTROL]["rounds_missing"] == [R0 + 5]
    assert report["arms"][ARM_SWARM]["rounds_present"] == [R0, R0 + 2]
    assert report["arms"][ARM_CONTROL]["rounds_present"] == [R0 + 1, R0 + 3]


def test_the_missing_rounds_zero_gets_are_out_of_the_medians(report):
    """The medians are over PRESENT rounds, which is the whole point.

    Both missing rounds have an (empty) `get_rpc` response, so folding them in
    would give the swarm arm a median of 100 and the control arm 200 — a ratio
    of 0.5 reached by averaging in two rounds no reader took part in.
    """
    assert _round(report, R0 + 4)["gets"]["total"] == 0
    assert report["arms"][ARM_SWARM]["served_gets_median"] == 220.0
    assert report["arms"][ARM_CONTROL]["served_gets_median"] == 420.0
    assert report["arms"][ARM_SWARM]["total_gets_median"] == 275.0
    assert report["arms"][ARM_CONTROL]["total_gets_median"] == 525.0


def test_an_off_parity_row_is_counted_and_excluded(report):
    """A `round_parity: all` cohort reads on the other arm's rounds too.

    Those reads are the other arm's measurement, so they are in neither arm's
    figures — but a reader has to know they happened, or the control arm's
    numbers read as a cohort that was alone when it was not.
    """
    assert _round(report, R0 + 1)["off_parity_rows"] == 1
    assert sum(r["off_parity_rows"] for r in report["rounds"]) == 1
    # The control round's own figures are the control cohort's four readers.
    assert _round(report, R0 + 1)["native"]["attempted"] == 4
    assert _round(report, R0 + 1)["native"]["rows_with_peers"] == 0
    # And the swarm arm's summary is its 8 even-round rows, not 9.
    assert report["arms"][ARM_SWARM]["native"]["attempted"] == 8


def test_rows_of_arm_keeps_only_that_arms_parity():
    rows = [{"round_id": R0}, {"round_id": R0 + 1}, {"round_id": R0 + 2}]
    assert [r["round_id"] for r in rows_of_arm(rows, ARM_SWARM)] == [R0, R0 + 2]
    assert [r["round_id"] for r in rows_of_arm(rows, ARM_CONTROL)] == [R0 + 1]


# ---------------------------------------------------------------------------
# Per round
# ---------------------------------------------------------------------------


def test_round_r0_is_four_readers_and_two_browsers_all_with_peers(report):
    entry = _round(report, R0)
    assert entry["arm"] == ARM_SWARM
    assert entry["boundary"] == "2026-10-18T09:00:00+00:00"
    assert entry["hosts"] == 3
    native = entry["native"]
    assert (native["attempted"], native["success"], native["failure"]) == (4, 4, 0)
    assert native["completion"] == 1.0
    assert native["rows_with_peers"] == 4
    assert native["peer_share"] == 1.0
    assert native["p50_ms"] == 13000.0
    assert native["p95_ms"] == 15700.0
    assert native["sums"] == {
        "records_from_peers": 130,
        "bytes_from_peers": 13_000_000,
        "records_served": 40,
        "bytes_served": 4_000_000,
    }
    assert native["medians"] == {
        "records_from_peers": 30.0,
        "bytes_from_peers": 3_000_000.0,
        "records_served": 7.5,
        "bytes_served": 750_000.0,
    }
    browser = entry["browser"]
    assert (browser["attempted"], browser["success"], browser["peer_share"]) == (2, 2, 1.0)
    assert browser["chunk_sources"]["sums"]["chunks_from_readers"] == 350


def test_round_r0_plus_2_has_the_timeout_and_the_peerless_reader(report):
    entry = _round(report, R0 + 2)
    native = entry["native"]
    assert (native["attempted"], native["success"], native["failure"]) == (4, 3, 1)
    assert native["completion"] == 0.75
    assert native["error_kinds"] == {"timeout": 1}
    # The failure attempted a read and took nothing from a peer, so it is in the
    # denominator: 3 of 4, not 3 of 3.
    assert native["rows_with_peers"] == 3
    assert native["peer_share"] == 0.75
    assert native["p50_ms"] == 13000.0
    assert native["p95_ms"] == 14800.0
    assert (native["verify_mismatch"], native["verify_ok"], native["verify_unchecked"]) == (1, 1, 1)
    # The skipped browser row is excluded from the denominator and counted.
    browser = entry["browser"]
    assert (browser["rows"], browser["attempted"], browser["skipped"]) == (2, 1, 1)
    assert browser["completion"] == 1.0
    assert browser["peer_share"] == 1.0


def test_a_control_round_reports_a_released_binarys_zeros_as_zeros(report):
    entry = _round(report, R0 + 3)
    native = entry["native"]
    assert (native["attempted"], native["success"], native["failure"]) == (4, 3, 1)
    assert native["error_kinds"] == {"connection": 1}
    assert native["peer_share"] == 0.0
    assert native["swarm_present"] == {"true": 0, "false": 4, "null": 0}
    assert entry["browser"]["rows"] == 0


# ---------------------------------------------------------------------------
# Per arm
# ---------------------------------------------------------------------------


def test_swarm_arm_native_summary(report):
    native = report["arms"][ARM_SWARM]["native"]
    assert (native["attempted"], native["success"], native["failure"], native["skipped"]) == (
        8,
        7,
        1,
        0,
    )
    assert native["completion"] == 0.875
    assert native["rows_with_peers"] == 7
    assert native["peer_share"] == 0.875
    assert native["p50_ms"] == 13000.0
    assert native["p95_ms"] == 15700.0
    assert native["error_kinds"] == {"timeout": 1}
    assert (native["verify_mismatch"], native["verify_ok"], native["verify_unchecked"]) == (1, 5, 1)
    assert native["swarm_present"] == {"true": 8, "false": 0, "null": 0}
    assert native["sums"] == {
        "records_from_peers": 220,
        "bytes_from_peers": 22_000_000,
        "records_served": 60,
        "bytes_served": 6_000_000,
    }
    assert native["medians"] == {
        "records_from_peers": 25.0,
        "bytes_from_peers": 2_500_000.0,
        "records_served": 7.5,
        "bytes_served": 750_000.0,
    }
    assert native["discovery"] == {
        "discovery_announces": 16,
        "discovery_announces_refused": 1,
        "discovery_announces_timed_out": 1,
        "discovery_offers_made": 22,
        "discovery_offers_delivered": 21,
        "discovery_links_proven": 21,
        "discovery_links_failed": 1,
    }


def test_swarm_arm_browser_summary(report):
    browser = report["arms"][ARM_SWARM]["browser"]
    assert (browser["rows"], browser["attempted"], browser["success"], browser["skipped"]) == (
        4,
        3,
        3,
        1,
    )
    assert browser["completion"] == 1.0
    assert browser["peer_share"] == 1.0
    assert browser["p50_ms"] == 22000.0
    assert browser["p95_ms"] == 23800.0
    # The skipped row carries no flag at all, which is neither true nor false.
    assert browser["swarm_present"] == {"true": 3, "false": 0, "null": 1}
    assert browser["sums"] == {
        "records_from_peers": 240,
        "bytes_from_peers": 24_000_000,
        "records_served": 120,
        "bytes_served": 12_000_000,
    }
    assert browser["medians"]["records_from_peers"] == 80.0
    assert browser["medians"]["records_served"] == 40.0
    assert browser["chunk_sources"]["sums"] == {
        "chunks_total": 900,
        "chunks_from_nodes": 370,
        "chunks_from_cache": 0,
        "chunks_from_readers": 530,
    }
    assert browser["chunk_sources"]["medians"]["chunks_from_readers"] == 180.0
    assert browser["discovery"]["discovery_offers_delivered"] == 6


def test_control_arm_summary_and_that_nothing_claims_a_swarm(report):
    arm = report["arms"][ARM_CONTROL]
    native = arm["native"]
    assert native["completion"] == 0.875
    assert native["peer_share"] == 0.0
    assert native["p50_ms"] == 33000.0
    assert native["p95_ms"] == 35700.0
    assert native["swarm_present"] == {"true": 0, "false": 8, "null": 0}
    assert set(native["sums"].values()) == {0}
    assert set(native["discovery"].values()) == {0}
    assert arm["swarm_expected"] is False
    assert arm["unexpected_swarm_rows"] == 0
    assert report["arms"][ARM_SWARM]["swarm_expected"] is True
    # No browser cohort on the control arm at all.
    assert arm["browser"]["rows"] == 0
    assert arm["browser"]["completion"] is None


def test_a_control_row_claiming_a_swarm_is_reported():
    """A released control that reports a swarm is a finding, not a figure.

    It means the control extension is not the released build — which would make
    the whole ratio a comparison of one binary against itself — so the report
    says how many rows say so rather than averaging them in silently.
    """
    native = _rows("native-rows.json")
    for row in native:
        if row["extension"] == "control":
            row["swarm_present"] = True
            row["records_from_peers"] = 5
    rows = rounds_in_window(native, PERIOD, WINDOW_START, WINDOW_END)
    control = [r for r in rows if r["extension"] == "control"]
    docs = _trace_docs()
    swarm_rounds = [
        r
        for r in window_round_ids(PERIOD, WINDOW_START, WINDOW_END)
        if arm_of_round(r) == ARM_SWARM
    ]
    report = build_report(
        deployment="DEV-03",
        deployment_id=702,
        swarm_extension="swarm",
        control_extension="control",
        swarm_native=[r for r in rows if r["extension"] == "swarm"],
        swarm_browser=[
            r
            for r in rounds_in_window(_rows("browser-rows.json"), PERIOD, WINDOW_START, WINDOW_END)
            if r["extension"] == "swarm"
        ],
        control_native=control,
        ignored_rows=0,
        config_entries=_json("hot-file-config.json"),
        browser_config_entries=_json("browser-config.json"),
        hot_files=_json("hot-files.json"),
        hot_files_source="fixture",
        chunk_set=chunk_set_from_traces(docs, swarm_rounds),
        gets_by_round=_gets(),
        trace_docs=docs,
        period_secs=PERIOD,
        period_source="fixture",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        **PASS_THRESHOLDS,
    )
    assert report["arms"][ARM_CONTROL]["unexpected_swarm_rows"] == 8
    assert "8 row(s) say otherwise" in render_markdown(report)


# ---------------------------------------------------------------------------
# The chunk set and the browsers' own GETs
# ---------------------------------------------------------------------------


def test_the_chunk_set_is_the_swarm_rounds_distinct_targets(report):
    assert report["chunk_set"] == {
        "chunks": ["chunk-a", "chunk-b", "chunk-c", "chunk-d"],
        "count": 4,
        "from_rounds": [R0, R0 + 2],
    }


def test_a_control_rounds_trace_record_is_not_in_the_chunk_set():
    """`chunk-z` is read on round R0+1 and must not become the file's record.

    The chunk set is what the fleet's GETs are filtered on, on both arms, so a
    stray address from the other arm's round would put GETs for a different file
    into the comparison.
    """
    docs = _trace_docs()
    assert any(d["target"] == "chunk-z" for d in docs)
    both = chunk_set_from_traces(docs, [R0, R0 + 1, R0 + 2])
    assert "chunk-z" in both["chunks"]
    swarm_only = chunk_set_from_traces(docs, [R0, R0 + 2])
    assert "chunk-z" not in swarm_only["chunks"]


def test_a_discovery_record_is_not_a_chunk_get():
    """Only an ``rpc`` with a non-empty target is a GET of a chunk."""
    docs = [
        {"operation": "discovery", "phase": "finish", "target": "chunk-x", "round_id": R0},
        {"operation": "rpc", "phase": "start", "target": "", "round_id": R0},
    ]
    assert chunk_set_from_traces(docs, [R0])["count"] == 0


def test_browser_gets_count_found_and_not_found_finishes(report):
    assert report["browser_gets"]["by_round"] == [
        {
            "round_id": R0,
            "downloads": 2,
            "found": 6,
            "not_found": 2,
            "found_per_download": 3.0,
            "not_found_per_download": 1.0,
        },
        {
            "round_id": R0 + 2,
            "downloads": 1,
            "found": 2,
            "not_found": 0,
            "found_per_download": 2.0,
            "not_found_per_download": 0.0,
        },
    ]
    totals = report["browser_gets"]
    assert (totals["downloads"], totals["found"], totals["not_found"]) == (3, 8, 2)
    assert totals["found_per_download"] == pytest.approx(8 / 3)
    assert totals["not_found_per_download"] == pytest.approx(2 / 3)


def test_browser_gets_are_empty_outside_the_swarm_rounds():
    assert summarise_browser_gets(_trace_docs(), [])["by_round"] == []
    assert summarise_browser_gets(_trace_docs(), [])["found_per_download"] is None


# ---------------------------------------------------------------------------
# The node GETs
# ---------------------------------------------------------------------------


def test_node_gets_per_round_and_the_busiest_hosts(report):
    by_round = {entry["round_id"]: entry for entry in report["node_gets"]["by_round"]}
    assert len(by_round) == 6
    assert by_round[R0]["arm"] == ARM_SWARM
    assert by_round[R0]["total"] == 250
    assert by_round[R0]["served"] == 200
    assert by_round[R0]["by_request"] == {"get": 200, "get_or_closer": 50}
    assert by_round[R0]["by_storage_outcome"] == {"success": 200, "not_found": 50}
    assert by_round[R0]["served_p50_ms"] == 12.0
    assert by_round[R0]["served_p95_ms"] == 48.0
    assert by_round[R0 + 1]["arm"] == ARM_CONTROL
    assert by_round[R0 + 1]["served"] == 400
    assert by_round[R0 + 4]["total"] == 0
    assert by_round[R0 + 4]["served_p50_ms"] is None
    assert report["node_gets"]["top_hosts"] == [
        {"host": "DEV-03-NODE-1", "gets": 820},
        {"host": "DEV-03-NODE-2", "gets": 480},
        {"host": "DEV-03-NODE-3", "gets": 300},
    ]


def test_get_rpc_query_is_filtered_on_the_target_the_event_and_the_chunk_set():
    body = get_rpc_query_body(["cc", "aa", "bb"], "2026-10-18T09:00:00Z", "2026-10-18T09:10:00Z")
    filters = body["query"]["bool"]["filter"]
    assert {"term": {"tag.target.keyword": "ant_node::storage::rpc_latency"}} in filters
    assert {"match_phrase": {"tail.message": GET_RPC_EVENT}} in filters
    # Sorted, so two identical queries are byte-identical.
    assert {"terms": {CHUNK_ADDRESS_FIELD: ["aa", "bb", "cc"]}} in filters
    # Half-open: a document stamped on the next boundary is the next round's.
    assert {
        "range": {"@timestamp": {"gte": "2026-10-18T09:00:00Z", "lt": "2026-10-18T09:10:00Z"}}
    } in filters
    assert body["size"] == 0
    assert body["track_total_hits"] is True
    aggs = body["aggs"]
    assert aggs["requests"]["terms"]["field"] == REQUEST_FIELD
    assert aggs["storage_outcomes"]["terms"]["field"] == STORAGE_OUTCOME_FIELD
    assert aggs["storage_outcomes"]["aggs"]["latency"]["percentiles"]["field"] == "tail.duration_ms"
    assert aggs["hosts"]["terms"]["size"] == HOST_TERMS_SIZE


def test_parse_get_rpc_fills_the_request_kinds_and_keeps_a_null_latency():
    """A request kind that never occurred is 0; a latency that was not measured
    stays None. A zero latency for a GET that never happened would read as
    instantaneous."""
    parsed = parse_get_rpc_response(
        {
            "hits": {"total": {"value": 7}},
            "aggregations": {
                "requests": {"buckets": [{"key": "get", "doc_count": 7}]},
                "storage_outcomes": {
                    "buckets": [
                        {"key": "not_found", "doc_count": 7, "latency": {"values": {"50.0": None}}}
                    ]
                },
                "hosts": {"buckets": []},
            },
        }
    )
    assert parsed["total"] == 7
    assert parsed["by_request"] == {"get": 7, "get_or_closer": 0}
    assert parsed["served"] == 0
    assert parsed["served_p50_ms"] is None
    assert parsed["by_host"] == {}


def test_top_hosts_sums_across_rounds_and_takes_the_busiest():
    rounds = [
        {"by_host": {"A": 5, "B": 1}},
        {"by_host": {"A": 2, "C": 9}},
    ]
    assert top_hosts(rounds, limit=2) == [
        {"host": "C", "gets": 9},
        {"host": "A", "gets": 7},
    ]


def test_round_bounds_are_the_boundary_and_the_next_boundary():
    assert as_epoch_bounds(R0, PERIOD) == ("2026-10-18T09:00:00Z", "2026-10-18T09:10:00Z")


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


def _gate(report: dict, prefix: str) -> dict:
    return next(g for g in report["gates"] if g["name"].startswith(prefix))


def test_every_gate_passes_on_the_fixture(report):
    assert [g["status"] for g in report["gates"]] == [GATE_PASS] * 4
    assert report["verdict"] == GATE_PASS
    assert _gate(report, "G1")["figure"] == (
        "swarm native 87.50%, control native 87.50%, swarm browser 100.00%"
    )
    assert _gate(report, "G2")["figure"] == f"worst round {R0 + 2} native at 75.0%"
    assert _gate(report, "G3")["figure"] == "0.524 (swarm 220.0 / control 420.0)"
    assert _gate(report, "G4")["figure"] == "swarm 13,000 ms vs control 33,000 ms (-60.6%)"


def test_g1_fails_at_the_commands_default_floor():
    """87.5% is below the default 0.95, on both arms' native readers."""
    report = _report(min_completion=0.95)
    gate = _gate(report, "G1")
    assert gate["status"] == GATE_FAIL
    assert "swarm native 87.50%" in gate["detail"]
    assert "control native 87.50%" in gate["detail"]
    assert "swarm browser" not in gate["detail"]
    assert report["verdict"] == GATE_FAIL


def test_g2_fails_on_the_one_round_whose_swarm_was_short():
    """Per round, so round R0+2's 75% fails even though the window is 87.5%."""
    report = _report(min_peer_share=0.80)
    gate = _gate(report, "G2")
    assert gate["status"] == GATE_FAIL
    assert f"round {R0 + 2} native 75.0%" in gate["detail"]
    # The other three figures passed, which is why the window-wide share hides it.
    assert "4 figure(s) assessed; 1 below the floor" in gate["detail"]
    assert report["verdict"] == GATE_FAIL


def test_g3_fails_when_the_relief_is_smaller_than_asked_for():
    report = _report(max_get_ratio=0.50)
    gate = _gate(report, "G3")
    assert gate["status"] == GATE_FAIL
    assert gate["figure"] == "0.524 (swarm 220.0 / control 420.0)"
    assert gate["threshold"] == "<= 0.50 x the control arm"
    assert report["verdict"] == GATE_FAIL


def test_g3_is_not_assessed_with_one_control_round():
    """A median of one round is that round, so the ratio is not a comparison."""
    report = _report(window_end=NARROW_END)
    assert report["arms"][ARM_SWARM]["rounds_present"] == [R0, R0 + 2]
    assert report["arms"][ARM_CONTROL]["rounds_present"] == [R0 + 1]
    gate = _gate(report, "G3")
    assert gate["status"] == GATE_NOT_ASSESSED
    assert gate["figure"] == "2 swarm round(s), 1 control round(s)"
    # NOT ASSESSED never fails the command.
    assert report["verdict"] == GATE_PASS


def test_g3_is_not_assessed_when_the_control_arm_served_nothing():
    """A ratio over a zero denominator is a measurement that did not happen.

    Reporting it as 0 would certify relief the report never saw — and the likely
    cause is the control readers never running or the node forwarder being down,
    which is what the detail has to say.
    """
    arms = {
        ARM_SWARM: _arm(served=100.0, rounds=[R0, R0 + 2], p50=10.0),
        ARM_CONTROL: _arm(served=0.0, rounds=[R0 + 1, R0 + 3], p50=20.0),
    }
    gate = next(g for g in evaluate_gates(arms, [], **_thresholds()) if g["name"].startswith("G3"))
    assert gate["status"] == GATE_NOT_ASSESSED
    assert "served no GET for the file" in gate["detail"]


def test_g4_warns_when_the_swarm_is_slower_and_never_fails_the_verdict():
    arms = {
        ARM_SWARM: _arm(served=100.0, rounds=[R0, R0 + 2], p50=40000.0),
        ARM_CONTROL: _arm(served=400.0, rounds=[R0 + 1, R0 + 3], p50=20000.0),
    }
    gates = evaluate_gates(arms, [], **_thresholds())
    g4 = next(g for g in gates if g["name"].startswith("G4"))
    assert g4["status"] == GATE_WARN
    assert g4["figure"] == "swarm 40,000 ms vs control 20,000 ms (+100.0%)"
    assert verdict_of(gates) == GATE_PASS


def test_g4_is_excluded_from_the_verdict_by_name_not_by_position():
    gates = [
        {"name": "G1 completion", "status": GATE_PASS},
        {"name": "G4 no slower", "status": GATE_FAIL},
    ]
    assert verdict_of(gates) == GATE_PASS
    assert verdict_of([{"name": "G2 swarms form", "status": GATE_FAIL}]) == GATE_FAIL


def test_g1_and_g2_are_not_assessed_when_nothing_was_attempted():
    arms = {
        ARM_SWARM: _arm(served=None, rounds=[], p50=None),
        ARM_CONTROL: _arm(served=None, rounds=[], p50=None),
    }
    gates = evaluate_gates(arms, [], **_thresholds())
    assert [g["status"] for g in gates] == [GATE_NOT_ASSESSED] * 4
    assert verdict_of(gates) == GATE_PASS


def _thresholds():
    return {"min_completion": 0.95, "min_peer_share": 0.80, "max_get_ratio": 0.80}


def _arm(*, served, rounds, p50):
    """An arm summary with only the fields the gates read."""
    cohort = {
        "completion": None if p50 is None else 1.0,
        "p50_ms": p50,
        "peer_share": None,
        "rows": 0,
    }
    return {
        "rounds_present": rounds,
        "served_gets_median": served,
        "native": cohort,
        "browser": dict(cohort, completion=None),
    }


# ---------------------------------------------------------------------------
# The parity check and the discovery vocabulary
# ---------------------------------------------------------------------------


def test_a_swarm_host_on_odd_parity_is_refused_by_name():
    entries = _json("hot-file-config.json")
    entries[1]["round_parity"] = "odd"
    with pytest.raises(SwarmReadsError) as e:
        check_parities(entries, "swarm", "control")
    assert "DEV-03-swarm-ant-client-hot-file-read-nyc3-1 (swarm) is round_parity=odd" in str(
        e.value
    )


def test_a_control_host_on_even_parity_is_refused_by_name():
    entries = _json("hot-file-config.json")
    entries[2]["round_parity"] = "even"
    with pytest.raises(SwarmReadsError) as e:
        check_parities(entries, "swarm", "control")
    assert "DEV-03-control-ant-client-hot-file-read-lon1-1 (control) is round_parity=even" in str(
        e.value
    )


def test_every_wrong_parity_is_named_in_one_refusal():
    entries = _json("hot-file-config.json")
    entries[0]["round_parity"] = "odd"
    entries[3]["round_parity"] = "even"
    with pytest.raises(SwarmReadsError) as e:
        check_parities(entries, "swarm", "control")
    assert str(e.value).count("round_parity=") == 2


def test_parity_all_is_allowed_because_its_rows_are_excluded():
    entries = _json("hot-file-config.json")
    for entry in entries:
        entry["round_parity"] = "all"
    check_parities(entries, "swarm", "control")
    check_parities(_json("hot-file-config.json"), "swarm", "control")


def test_the_reported_counters_are_real_columns_on_both_records():
    """A rename must break here, not leave the report reading None forever.

    The report names seven discovery counters and four peer counters out of the
    twenty-five on the row; if one is renamed the dict accessor would quietly
    yield None and every total would read 0.
    """
    for column in DISCOVERY_TOTAL_COLUMNS:
        assert column in SWARM_COUNTER_COLUMNS
        assert hasattr(DeploymentHotFileDownloadRecord, column)
        assert hasattr(DeploymentBrowserDownloadRecord, column)


# ---------------------------------------------------------------------------
# The markdown
# ---------------------------------------------------------------------------


def test_the_markdown_carries_every_section_and_is_reproducible(report):
    markdown = render_markdown(report)
    assert markdown == render_markdown(report)
    for heading in (
        "# Swarm read report — DEV-03",
        "## 1. The file, the cohorts and the window",
        "## 2. Per round",
        "## 3. Per arm",
        "## 4. The fleet's GETs for the file",
        "## 5. The browsers' own node GETs (swarm rounds)",
        "## 6. Gates",
    ):
        assert heading in markdown
    assert "**PASS** — 6 round(s) in the window (2 missing)" in markdown
    # The hot file, with the sha256 that only the selection carries.
    assert "ab" * 32 in markdown
    assert "**Chunk set: 4 distinct record(s)**" in markdown
    # A missing round is named as missing rather than rendered as zeros.
    assert f"| {R0 + 4} | 2026-10-18T09:40:00+00:00 | swarm | **missing**" in markdown
    assert "4 row(s) ignored" in markdown
    assert "1 off-parity row(s) excluded" in markdown
    # The discovery counters in DECLARED order, so the two arms line up by eye.
    assert (
        "native discovery: announces=16, announces_refused=1, "
        "announces_timed_out=1, offers_made=22, offers_delivered=21, "
        "links_proven=21, links_failed=1" in markdown
    )
    assert "no swarm**, so every counter above is expected to be 0" in markdown
    assert "| DEV-03-NODE-1 | 820 |" in markdown
    assert "G4 is reported and never fails the command" in markdown


# ---------------------------------------------------------------------------
# The command: loading, refusals and exit codes
# ---------------------------------------------------------------------------


def _args(**extra):
    args = SimpleNamespace(
        name="DEV-03",
        id=702,
        window_start="2026-10-18T09:00:00Z",
        window_end="2026-10-18T10:00:00Z",
        swarm_extension="swarm",
        control_extension="control",
        min_completion=0.85,
        min_peer_share=0.70,
        max_get_ratio=0.80,
        markdown_out=None,
        print_markdown=False,
        json_out=None,
    )
    for key, value in extra.items():
        setattr(args, key, value)
    return args


def _deploy_args():
    return {
        "kind": "testnet",
        "hot_file_readers": _json("hot-file-config.json"),
        "browser_downloaders": _json("browser-config.json"),
        "browser_hot_files": _json("hot-files.json"),
    }


class _Repo:
    def __init__(self, native=None, browser=None, deploy_args=None):
        self.native = _rows("native-rows.json") if native is None else native
        self.browser = _rows("browser-rows.json") if browser is None else browser
        self.deployment = SimpleNamespace(
            id=702,
            name="DEV-03",
            deploy_args=_deploy_args() if deploy_args is None else deploy_args,
        )

    def get(self, deployment_id):
        return self.deployment if deployment_id == 702 else None

    def get_active_by_name(self, name):
        return self.deployment if name == "DEV-03" else None

    def get_by_name(self, name):
        return self.get_active_by_name(name)

    def list_hot_file_download_records_for_deployment(self, deployment_id):
        return self.native

    def list_browser_download_records_for_deployment(self, deployment_id):
        return self.browser


class _FakeEs:
    """The run's Elasticsearch, serving both indices from the fixtures.

    Deliberately a fake ``search`` rather than patched fetch helpers: the trace
    query's paging and sort, and the GET query's chunk-set filter and round
    range, are the parts most likely to be silently wrong, and a stub returning
    the answers directly would exercise neither.
    """

    def __init__(self, docs, gets, error=None, get_error=None):
        self.docs = docs
        self.gets = gets
        self.error = error
        self.get_error = get_error
        self.get_queries = []

    def client_index(self):
        return "telegraf-ant-clients-dev-03*"

    def node_index(self):
        return "telegraf-ant-nodes-dev-03-*"

    def search(self, body, index=None):
        if index == self.node_index():
            return self._node_search(body)
        if self.error is not None:
            raise self.error
        assert index == self.client_index()
        return self._trace_search(body)

    def _node_search(self, body):
        if self.get_error is not None:
            raise self.get_error
        filters = body["query"]["bool"]["filter"]
        chunks = next(f["terms"][CHUNK_ADDRESS_FIELD] for f in filters if "terms" in f)
        start = next(f["range"]["@timestamp"]["gte"] for f in filters if "range" in f)
        self.get_queries.append((tuple(chunks), start))
        round_id = int(
            datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            // PERIOD
        )
        return self.gets[str(round_id)]

    def _trace_search(self, body):
        filters = body["query"]["bool"]["filter"]
        terms = {
            next(iter(f["terms"])): next(iter(f["terms"].values())) for f in filters if "terms" in f
        }
        wanted_rounds = set(terms["browser_trace.round_id"])
        wanted_ops = set(terms["tag.operation.keyword"])
        hits = []
        for doc in self.docs:
            trace, tags = doc["browser_trace"], doc["tag"]
            if trace["round_id"] not in wanted_rounds or tags["operation"] not in wanted_ops:
                continue
            hits.append(
                {
                    "_source": doc,
                    "sort": [trace["round_id"], tags["host"], trace["instance"], trace["seq"]],
                }
            )
        hits.sort(key=lambda h: h["sort"])
        after = body.get("search_after")
        if after:
            hits = [h for h in hits if h["sort"] > after]
        # A page is what the body ASKED for, as Elasticsearch returns: a fake
        # that returned fewer would end the paging loop early, which is exactly
        # the bug a paging test is meant to catch.
        return {"hits": {"hits": hits[: body["size"]]}}


def _run(args, repo=None, docs=None, gets=None, es=None, **es_kwargs):
    fake = es or _FakeEs(
        _json("trace-docs.json") if docs is None else docs,
        _json("get-rpc-responses.json") if gets is None else gets,
        **es_kwargs,
    )
    with (
        patch(
            "saorsa_deploy.analysis.swarm_reads.get_repository",
            return_value=repo if repo is not None else _Repo(),
        ),
        patch("saorsa_deploy.analysis.swarm_reads.EsRun", return_value=fake),
        patch("saorsa_deploy.analysis.swarm_reads.quiet_tls"),
    ):
        return cmd_swarm_reads(args), fake


def test_the_command_writes_the_report_and_exits_zero_on_pass(tmp_path):
    markdown = tmp_path / "report.md"
    payload = tmp_path / "report.json"
    code, fake = _run(_args(markdown_out=str(markdown), json_out=str(payload)))
    assert code == 0
    document = json.loads(payload.read_text())
    assert document["verdict"] == GATE_PASS
    assert document["chunk_set"]["count"] == 4
    # 20 of the fixture's 22 documents: the trace query names the SWARM rounds,
    # so the two `chunk-z` records on a control round are never fetched.
    assert document["totals"]["trace_records"] == 20
    assert document["totals"]["ignored_rows"] == 4
    assert document["round_period_source"] == "deploy_args.hot_file_readers"
    assert document["hot_files"]["source"] == "deploy_args (hot-file selection snapshot)"
    assert "# Swarm read report — DEV-03" in markdown.read_text()
    # One GET query per round in the window, every one filtered to the chunk set.
    assert len(fake.get_queries) == 6
    assert {chunks for chunks, _ in fake.get_queries} == {
        ("chunk-a", "chunk-b", "chunk-c", "chunk-d")
    }
    assert [start for _, start in fake.get_queries] == [
        "2026-10-18T09:00:00Z",
        "2026-10-18T09:10:00Z",
        "2026-10-18T09:20:00Z",
        "2026-10-18T09:30:00Z",
        "2026-10-18T09:40:00Z",
        "2026-10-18T09:50:00Z",
    ]


def test_the_command_exits_one_when_a_gate_fails():
    code, _ = _run(_args(min_completion=0.95))
    assert code == EXIT_GATE_FAIL


def test_the_command_exits_two_with_no_rows_for_the_swarm_extension():
    code, _ = _run(_args(swarm_extension="nope"))
    assert code == EXIT_ERROR


def test_the_command_exits_two_with_no_rows_for_the_control_extension():
    """Without the control arm there is nothing for the swarm arm to be
    compared against, so there is no report rather than a one-armed one."""
    code, _ = _run(_args(control_extension="nope"))
    assert code == EXIT_ERROR


def test_the_command_exits_two_when_the_two_arms_name_one_extension():
    code, _ = _run(_args(control_extension="swarm"))
    assert code == EXIT_ERROR


def test_the_command_exits_two_on_a_parity_that_contradicts_its_arm():
    deploy_args = _deploy_args()
    deploy_args["hot_file_readers"][0]["round_parity"] = "odd"
    code, _ = _run(_args(), repo=_Repo(deploy_args=deploy_args))
    assert code == EXIT_ERROR


def test_the_command_exits_two_when_the_cohorts_recorded_different_periods():
    """Two periods are two clocks: the round ids do not line up at all."""
    deploy_args = _deploy_args()
    for entry in deploy_args["hot_file_readers"]:
        if entry["extension"] == "control":
            entry["round_period_secs"] = 900
    code, _ = _run(_args(), repo=_Repo(deploy_args=deploy_args))
    assert code == EXIT_ERROR


def test_the_command_exits_two_when_one_cohorts_vms_disagree_on_the_period():
    """A cohort's readers and browsers must share the round clock.

    They derive their round from the wall clock, so VMs on different periods are
    not one cohort at all — and the browser half's period silently winning would
    make "the even rounds" mean two things inside one arm.
    """
    deploy_args = _deploy_args()
    deploy_args["browser_downloaders"][0]["round_period_secs"] = 900
    code, _ = _run(_args(), repo=_Repo(deploy_args=deploy_args))
    assert code == EXIT_ERROR


def test_the_command_exits_two_with_too_small_a_chunk_set():
    """Two addresses do not describe a 300 MiB file, and dividing one stray
    address by another is not a measurement."""
    docs = [d for d in _json("trace-docs.json") if d["browser_trace"]["target"] == "chunk-a"]
    code, _ = _run(_args(), docs=docs)
    assert code == EXIT_ERROR


def test_the_command_exits_two_with_no_trace_documents_at_all():
    code, _ = _run(_args(), docs=[])
    assert code == EXIT_ERROR


def test_the_command_exits_two_when_elasticsearch_cannot_be_read():
    code, _ = _run(_args(), error=RuntimeError("connection refused"))
    assert code == EXIT_ERROR


def test_the_command_exits_two_when_a_rounds_gets_cannot_be_counted():
    """A partial GET answer is not a report: that count is G3's numerator."""
    code, _ = _run(_args(), get_error=RuntimeError("503"))
    assert code == EXIT_ERROR


def test_the_command_exits_two_without_a_database():
    with patch("saorsa_deploy.analysis.swarm_reads.get_repository", return_value=None):
        assert cmd_swarm_reads(_args()) == EXIT_ERROR


def test_the_command_exits_two_on_a_bad_window():
    code, _ = _run(_args(window_start="not-a-date"))
    assert code == EXIT_ERROR
    code, _ = _run(_args(window_end="2026-10-18T08:00:00Z"))
    assert code == EXIT_ERROR


def test_the_command_exits_two_on_an_unknown_id():
    code, _ = _run(_args(id=999))
    assert code == EXIT_ERROR


def test_the_command_falls_back_to_the_period_in_the_rows():
    """A cohort provisioned with no DATABASE_URL records no config at all.

    `started_at` is the reader's scheduled start, so with no stagger it is the
    boundary and `boundary / round_id` is the period exactly — which is worth
    having, because the window selection is defined in terms of the period.
    """
    deploy_args = _deploy_args()
    deploy_args["hot_file_readers"] = []
    deploy_args["browser_downloaders"] = []
    payload = {}
    with patch(
        "saorsa_deploy.analysis.swarm_reads.emit_json",
        side_effect=lambda obj, *a, **k: payload.update(obj),
    ):
        code, _ = _run(_args(), repo=_Repo(deploy_args=deploy_args))
    assert code == 0
    assert payload["round_period_source"] == "derived from the recorded boundaries"
    assert payload["round_period_secs"] == PERIOD


def test_the_command_reads_the_live_selection_when_none_was_snapshotted():
    deploy_args = _deploy_args()
    deploy_args.pop("browser_hot_files")
    with patch(
        "saorsa_deploy.analysis.swarm_reads.load_hot_files",
        return_value=_json("hot-files.json"),
    ):
        code, _ = _run(_args(), repo=_Repo(deploy_args=deploy_args))
    assert code == 0


def test_the_command_exits_two_when_the_selection_is_gone_everywhere():
    """The S3 object dies with the deployment's own `clients destroy`, so a
    report with no snapshot and no object cannot say what was read."""
    deploy_args = _deploy_args()
    deploy_args.pop("browser_hot_files")
    with patch("saorsa_deploy.analysis.swarm_reads.load_hot_files", return_value=None):
        code, _ = _run(_args(), repo=_Repo(deploy_args=deploy_args))
    assert code == EXIT_ERROR
