"""`analysis browser-reads`: every section and all four gates, against fixtures.

The two fixtures are a cohort whose first reads are SPREAD and one whose reads
all go to one holder (nearest-first). Their expected figures are worked out on
paper in `fixtures/browser-reads/README.md`, and the numbers asserted here are
those — not whatever the code happened to produce, which is the only way a test
over arithmetic this interlinked says anything.

The gate the whole report exists for is G3, the busiest holder's share of a
chunk's FIRST reads: that is the quantity ADR-0023 (hot-record read spreading)
moves, and a report that got it wrong would read as a clean result.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.analysis.browser_reads import cmd_browser_reads
from saorsa_deploy.browser_reads import (
    BOOTSTRAP_SESSION_CEILING,
    GATE_FAIL,
    GATE_PASS,
    MIN_FIRST_READS_FOR_GATE,
    build_report,
    derive_period_secs,
    render_markdown,
    rounds_in_window,
    summarise_chunks,
)

FIXTURES = Path(__file__).parent / "fixtures" / "browser-reads"

PERIOD = 600
R0 = 2987190  # boundary 2026-10-18T09:00:00Z
WINDOW_START = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)
#: Takes rounds R0..R0+3 and leaves R0-1 (08:50Z) out.
WINDOW_END = datetime(2026, 10, 18, 9, 40, tzinfo=timezone.utc)


def _rows(name: str) -> list[dict]:
    raw = json.loads((FIXTURES / name).read_text())
    for row in raw:
        row["started_at"] = datetime.fromisoformat(row["started_at"])
    return raw


def _hot_files() -> dict:
    return json.loads((FIXTURES / "hot-files.json").read_text())


def _report(
    name: str,
    *,
    window_end: datetime = WINDOW_END,
    expected_instances: int | None = 8,
    min_completion: float = 0.99,
    max_holder_share: float = 0.25,
    max_refused: int = 0,
) -> dict:
    rows = rounds_in_window(_rows(name), PERIOD, WINDOW_START, window_end)
    return build_report(
        deployment="DEV-03",
        deployment_id=701,
        rows=rows,
        hot_files=_hot_files(),
        period_secs=PERIOD,
        period_source="deploy_args.browser_downloaders",
        expected_instances=expected_instances,
        window_start=WINDOW_START,
        window_end=window_end,
        min_completion=min_completion,
        max_holder_share=max_holder_share,
        max_refused=max_refused,
    )


@pytest.fixture
def spread():
    return _report("spread-rows.json")


@pytest.fixture
def nearest():
    return _report(
        "nearest-first-rows.json",
        window_end=datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc),
        expected_instances=4,
    )


# ---------------------------------------------------------------------------
# Window -> rounds
# ---------------------------------------------------------------------------


def test_the_window_takes_whole_rounds_by_boundary():
    """A round is in or out whole: its boundary is `round_id x period`, which
    every instance of it agrees on."""
    rows = rounds_in_window(_rows("spread-rows.json"), PERIOD, WINDOW_START, WINDOW_END)
    assert sorted({r["round_id"] for r in rows}) == [R0, R0 + 1, R0 + 2, R0 + 3]
    assert len(rows) == 32


def test_the_window_is_half_open_at_the_end():
    """So two adjacent windows partition a run's rounds exactly once between
    them -- a round counted in both would be counted twice in any phase-by-phase
    comparison."""
    end_on_a_boundary = datetime(2026, 10, 18, 9, 30, tzinfo=timezone.utc)
    rows = rounds_in_window(_rows("spread-rows.json"), PERIOD, WINDOW_START, end_on_a_boundary)
    assert sorted({r["round_id"] for r in rows}) == [R0, R0 + 1, R0 + 2]


def test_a_round_before_the_window_is_excluded_gates_and_all(spread):
    """The fixture's 08:50Z round is 8 `not_found` failures carrying 5 refused
    dials each. If the selection leaked it in, G2 and G4 would fail -- so this
    is the test that the window bounds the gates and not just the tables."""
    assert all(r["round_id"] >= R0 for r in spread["rounds"])
    assert spread["refusals"]["total"] == 0
    assert spread["totals"]["failure"] == 0


def test_the_period_is_derivable_from_the_recorded_boundaries():
    """The fallback when no role config was recorded: `started_at` IS the
    boundary, so `started_at / round_id` is the period exactly."""
    assert derive_period_secs(_rows("spread-rows.json")) == PERIOD


def test_a_boundary_that_is_not_a_multiple_yields_no_period():
    """Unknown rather than a guess: the whole window selection is defined in
    terms of the period, so a wrong one silently reports the wrong rounds."""
    rows = _rows("spread-rows.json")
    rows[0]["started_at"] = rows[0]["started_at"].replace(second=7)
    assert derive_period_secs(rows) is None


# ---------------------------------------------------------------------------
# Section 1 — hot files
# ---------------------------------------------------------------------------


def test_the_hot_files_section_names_the_rounds_that_read_each_file(spread):
    files = spread["hot_files"]["files"]
    assert [f["index"] for f in files] == [0, 1]
    # Round r reads files[r mod 2] and R0 is even, but R0's rows are all
    # skipped and a skip carries NO index (it never chose a file) -- so the only
    # round that read file 0 is R0+2.
    assert files[0]["rounds"] == [R0 + 2]
    assert files[1]["rounds"] == [R0 + 1, R0 + 3]
    assert files[0]["bytes"] == 52428800


def test_a_selected_file_nothing_read_shows_as_a_file_with_no_rounds():
    """Driven by the selection document rather than by the rows, because a file
    the cohort never cycled to is a finding, not an absence."""
    hot = _hot_files()
    hot["files"].append(
        {"index": 2, "address": "c2" * 32, "bytes": 1, "source_key": "k", "line": 1}
    )
    rows = rounds_in_window(_rows("spread-rows.json"), PERIOD, WINDOW_START, WINDOW_END)
    report = build_report(
        deployment="DEV-03",
        deployment_id=701,
        rows=rows,
        hot_files=hot,
        period_secs=PERIOD,
        period_source="test",
        expected_instances=8,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        min_completion=0.99,
        max_holder_share=0.25,
        max_refused=0,
    )
    assert report["hot_files"]["files"][2]["rounds"] == []


# ---------------------------------------------------------------------------
# Section 2 — per round
# ---------------------------------------------------------------------------


def test_the_per_round_section_counts_instances_against_the_role_config(spread):
    assert [r["round_id"] for r in spread["rounds"]] == [R0, R0 + 1, R0 + 2, R0 + 3]
    for entry in spread["rounds"]:
        assert entry["instances_reporting"] == 8
        assert entry["instances_expected"] == 8
        assert entry["instances_missing"] == 0
        assert entry["hosts"] == 2


def test_instances_expected_is_unknown_without_a_role_config():
    """None, never 0: a report that compared 8 reporting instances against a
    zero expectation would flag every round of every pre-config deployment."""
    report = _report("spread-rows.json", expected_instances=None)
    assert report["rounds"][0]["instances_expected"] is None
    assert report["rounds"][0]["instances_missing"] is None


def test_a_round_of_only_skips_has_no_completion_rate(spread):
    """The rounds before the operator selects a hot file are part of the series.
    Reporting them as 0% would fail the gate for a cohort that was never asked
    to do anything."""
    first = spread["rounds"][0]
    assert (first["success"], first["failure"], first["skipped"]) == (0, 0, 8)
    assert first["completion"] is None
    assert first["error_kinds"] == {}


def test_a_full_round_reports_completion_and_percentiles(spread):
    entry = spread["rounds"][1]
    assert (entry["success"], entry["failure"], entry["skipped"]) == (8, 0, 0)
    assert entry["completion"] == 1.0
    # total_ms is 8000 + 100n for n = 0..7 in this round. p50 interpolates
    # between 8300 and 8400; p95 sits at 0.95 x 7 = 6.65, so 35% of 8600 plus
    # 65% of 8700.
    assert entry["p50_ms"] == pytest.approx(8350.0)
    assert entry["p95_ms"] == pytest.approx(8665.0)


def test_a_desynchronised_round_is_flagged(spread):
    """The measurement is "the same file at the same moment", so a cohort spread
    over more than 10 s is not reading simultaneously. The SPREAD, not the max:
    a cohort that all started 40 s late is still simultaneous."""
    flagged = [r for r in spread["rounds"] if r["desynchronised"]]
    assert [r["round_id"] for r in flagged] == [R0 + 2]
    assert flagged[0]["started_spread_ms"] == 11500
    assert flagged[0]["started_late_ms_max"] == 12000
    # Every other round is flat.
    assert all(r["started_spread_ms"] == 0 for r in spread["rounds"] if r["round_id"] != R0 + 2)


# ---------------------------------------------------------------------------
# Section 3 — per region
# ---------------------------------------------------------------------------


def test_the_per_region_section_aggregates_the_window(spread):
    regions = {r["region"]: r for r in spread["regions"]}
    assert sorted(regions) == ["lon1", "nyc3"]
    for entry in regions.values():
        assert entry["hosts"] == 1
        assert entry["rounds"] == 4
        # 4 skipped (the all-skip round) + 12 successes.
        assert (entry["success"], entry["failure"], entry["skipped"]) == (12, 0, 4)
        assert entry["completion"] == 1.0


# ---------------------------------------------------------------------------
# Section 4 — per chunk, and the gate the report exists for
# ---------------------------------------------------------------------------


def test_the_per_chunk_section_measures_the_busiest_holders_share(spread):
    chunks = {c["chunk"]: c for c in spread["chunks"]}
    # 24 first reads dealt round-robin over 6 holders: 4 each.
    assert chunks["chunk-a"]["reads"] == 24
    assert chunks["chunk-a"]["distinct_holders"] == 6
    assert chunks["chunk-a"]["busiest_holder_reads"] == 4
    assert chunks["chunk-a"]["busiest_holder_share"] == pytest.approx(4 / 24)
    # 24 over 4 holders: 6 each, which is 25.0% exactly -- the gate boundary.
    assert chunks["chunk-b"]["reads"] == 24
    assert chunks["chunk-b"]["busiest_holder_share"] == 0.25


def test_chunks_are_sorted_by_share_descending(spread):
    """So the worst-spread chunk is the first line of the table."""
    shares = [c["busiest_holder_share"] for c in spread["chunks"]]
    assert shares == sorted(shares, reverse=True)
    assert spread["chunks"][0]["chunk"] == "chunk-c"


def test_known_holders_keeps_null_as_its_own_bucket(spread):
    """`null` means no discovery for that chunk reported a count at all, which
    is not "the client knew of no holders" -- see V2-1617's amended definition."""
    chunks = {c["chunk"]: c for c in spread["chunks"]}
    assert chunks["chunk-a"]["known_holders"] == {"7": 24}
    assert chunks["chunk-b"]["known_holders"] == {"null": 24}


def test_a_chunk_below_the_read_floor_is_listed_but_not_gated(spread):
    """chunk-c is served entirely by one holder -- 100% -- on 6 reads. One
    holder serving 2 of 3 reads is 67% and says nothing, so below 20 reads a
    share is noise and the chunk is reported without gating."""
    chunks = {c["chunk"]: c for c in spread["chunks"]}
    assert chunks["chunk-c"]["reads"] == 6
    assert chunks["chunk-c"]["busiest_holder_share"] == 1.0
    assert chunks["chunk-c"]["gated"] is False
    assert chunks["chunk-a"]["gated"] is True
    assert MIN_FIRST_READS_FOR_GATE == 20


def test_a_row_with_no_first_reads_contributes_no_chunk():
    """A bundle built without `test-utils` reports none at all, and that must
    read as a missing instrument rather than as a chunk read by nobody."""
    assert summarise_chunks([{"first_reads": None}, {"first_reads": []}]) == []


# ---------------------------------------------------------------------------
# Section 5 — stalls
# ---------------------------------------------------------------------------


def test_the_stalls_section_reports_the_sum_and_the_distribution(spread):
    stalls = spread["stalls"]
    # 6 of the 24 successful downloads stalled once; the other 26 in-window rows
    # (18 successes + 8 skips) carry an explicit 0.
    assert stalls["total"] == 6
    assert stalls["downloads_with_counts"] == 32
    assert stalls["downloads_with_a_stall"] == 6
    assert stalls["share_with_a_stall"] == pytest.approx(6 / 32)
    assert stalls["distribution"] == {"0": 26, "1": 6}
    assert [e["round_id"] for e in stalls["by_round"]] == [R0, R0 + 1, R0 + 2, R0 + 3]
    assert sum(e["stalls"] for e in stalls["by_round"]) == 6


def test_the_stall_share_is_over_the_rows_that_carry_a_count():
    """A row written before the field existed cannot be said to have had no
    stall, so it is not in the denominator either."""
    report = build_report(
        deployment="X",
        deployment_id=1,
        rows=[
            {"round_id": R0, "status": "success", "stalls": 2, "started_at": WINDOW_START},
            {"round_id": R0, "status": "success", "stalls": None, "started_at": WINDOW_START},
        ],
        hot_files=_hot_files(),
        period_secs=PERIOD,
        period_source="test",
        expected_instances=None,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        min_completion=0.99,
        max_holder_share=0.25,
        max_refused=0,
    )
    assert report["stalls"]["downloads_with_counts"] == 1
    assert report["stalls"]["share_with_a_stall"] == 1.0


# ---------------------------------------------------------------------------
# Section 6 — refusals
# ---------------------------------------------------------------------------


def test_the_refusals_section_names_the_rows_carrying_them(nearest):
    """A refusal usually means the VM exceeded the per-IP WebRTC session quota,
    so a count that cannot be traced to a VM reads as the fleet refusing us."""
    refusals = nearest["refusals"]
    assert refusals["total"] == 2
    assert len(refusals["rows"]) == 1
    assert refusals["rows"][0]["refused_dials"] == 2
    assert refusals["rows"][0]["round_id"] == R0 + 5
    assert refusals["rows"][0]["instance"] == 4


# ---------------------------------------------------------------------------
# Section 7 — bootstrap load
# ---------------------------------------------------------------------------


def test_the_bootstrap_load_is_the_downloads_in_a_round(spread):
    """Every download dials every seed, so a round's download count is the
    session count ONE bootstrap sees at its start. A skip launches no browser
    and dials nothing."""
    load = spread["bootstrap_load"]
    assert [e["sessions_per_bootstrap"] for e in load["by_round"]] == [0, 8, 8, 8]
    assert load["peak_sessions_per_bootstrap"] == 8
    assert load["ceiling"] == BOOTSTRAP_SESSION_CEILING == 256
    assert load["headroom"] == 248
    assert load["seed_count"] == 2


# ---------------------------------------------------------------------------
# Section 8 — the gates
# ---------------------------------------------------------------------------


def _gates(report: dict) -> dict:
    return {g["name"]: g["status"] for g in report["gates"]}


def test_a_spread_cohort_passes_every_gate(spread):
    assert _gates(spread) == {
        "G1 per-round completion": GATE_PASS,
        "G2 no not_found": GATE_PASS,
        "G3 busiest holder share": GATE_PASS,
        "G4 refused dials": GATE_PASS,
    }
    assert spread["verdict"] == GATE_PASS


def test_a_nearest_first_cohort_fails_every_gate(nearest):
    """chunk-a's 23 first reads all went to one holder -- 100% -- which is the
    reading the spreading change is supposed to remove."""
    assert _gates(nearest) == {
        "G1 per-round completion": GATE_FAIL,
        "G2 no not_found": GATE_FAIL,
        "G3 busiest holder share": GATE_FAIL,
        "G4 refused dials": GATE_FAIL,
    }
    assert nearest["verdict"] == GATE_FAIL
    chunks = {c["chunk"]: c for c in nearest["chunks"]}
    assert chunks["chunk-a"]["reads"] == 23
    assert chunks["chunk-a"]["busiest_holder_share"] == 1.0
    assert chunks["chunk-a"]["gated"] is True


def test_g1_fails_a_window_in_which_everything_was_skipped():
    """A PASS over nothing attempted is the reading this gate exists to prevent.
    The individual all-skip round is not gated; a window of only such rounds
    has no completion rate at all, and that is reported as a failure with the
    reason rather than as a clean result."""
    report = _report(
        "spread-rows.json", window_end=datetime(2026, 10, 18, 9, 10, tzinfo=timezone.utc)
    )
    assert report["totals"]["skipped"] == 8
    assert report["totals"]["completion"] is None
    gate = next(g for g in report["gates"] if g["name"] == "G1 per-round completion")
    assert gate["status"] == GATE_FAIL
    assert "every round in the window was skipped" in gate["detail"]


def test_g1_is_per_round_not_over_the_window(nearest):
    """Round R0+5 is 3 ok / 1 fail = 75% while the other five are at 100%. Over
    the window that averages to 95.8%, which a per-round gate must not forgive:
    a cohort that failed every read for one round and recovered has still had an
    outage."""
    rounds = {r["round_id"]: r for r in nearest["rounds"]}
    assert rounds[R0 + 5]["completion"] == 0.75
    assert all(rounds[R0 + i]["completion"] == 1.0 for i in range(5))
    assert nearest["totals"]["completion"] == pytest.approx(23 / 24)
    gate = next(g for g in nearest["gates"] if g["name"] == "G1 per-round completion")
    assert gate["status"] == GATE_FAIL
    assert f"round {R0 + 5}" in gate["figure"]


def test_g3_boundary_is_inclusive(spread):
    """chunk-b sits at exactly 25.0%, so a `<` comparison would fail a cohort
    that met the threshold."""
    gate = next(g for g in spread["gates"] if g["name"] == "G3 busiest holder share")
    assert gate["status"] == GATE_PASS
    tightened = _report("spread-rows.json", max_holder_share=0.2)
    tightened_gate = next(g for g in tightened["gates"] if g["name"] == "G3 busiest holder share")
    assert tightened_gate["status"] == GATE_FAIL


def test_g2_counts_only_not_found_failures(spread):
    gate = next(g for g in spread["gates"] if g["name"] == "G2 no not_found")
    assert gate["status"] == GATE_PASS
    assert gate["figure"] == "0 row(s)"


def test_g4_honours_a_raised_ceiling(nearest):
    relaxed = _report(
        "nearest-first-rows.json",
        window_end=datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc),
        expected_instances=4,
        max_refused=2,
    )
    assert _gates(relaxed)["G4 refused dials"] == GATE_PASS


def test_every_gate_carries_its_figure_and_threshold(spread):
    for gate in spread["gates"]:
        assert gate["figure"]
        assert gate["threshold"]
        assert gate["detail"]


# ---------------------------------------------------------------------------
# Skips, totals and rendering
# ---------------------------------------------------------------------------


def test_skips_are_listed_separately_and_in_no_denominator(spread):
    assert spread["totals"]["skipped"] == 8
    assert spread["totals"]["completion"] == 1.0
    assert len(spread["skipped_rows"]) == 8
    assert {r["error_kind"] for r in spread["skipped_rows"]} == {"no_hot_files"}


def test_the_totals_carry_the_archived_trace_count(spread):
    """24 of the 32 in-window rows were archived (the 8 skips were not), which
    is how an operator sees that the evidence came off the VMs."""
    assert spread["totals"]["traces_archived"] == 24


def test_the_markdown_is_fixed_format_and_reproducible(spread):
    first = render_markdown(spread)
    assert first == render_markdown(spread)
    assert first.startswith("# Browser hot-read report — DEV-03")
    for heading in (
        "## 1. Hot files",
        "## 2. Per round",
        "## 3. Per region",
        "## 4. Per chunk",
        "## 5. Stalls",
        "## 6. Refusals",
        "## 7. Bootstrap load",
        "## 8. Gates",
    ):
        assert heading in first
    assert "**PASS**" in first
    assert "⚠ desync" in first


def test_the_markdown_marks_an_ungated_chunk(spread):
    assert "not gated" in render_markdown(spread)


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


def _args(**extra):
    args = SimpleNamespace(
        name="DEV-03",
        id=701,
        window_start="2026-10-18T09:00:00Z",
        window_end="2026-10-18T09:40:00Z",
        min_completion=0.99,
        max_holder_share=0.25,
        max_refused=0,
        markdown_out=None,
        print_markdown=False,
        json_out=None,
    )
    for key, value in extra.items():
        setattr(args, key, value)
    return args


class _Repo:
    def __init__(self, rows, deploy_args=None):
        self.rows = rows
        self.deployment = SimpleNamespace(
            id=701,
            name="DEV-03",
            deploy_args=deploy_args
            if deploy_args is not None
            else {
                "kind": "testnet",
                "browser_downloaders": [
                    {"hostname": "h1", "instances": 4, "round_period_secs": PERIOD},
                    {"hostname": "h2", "instances": 4, "round_period_secs": PERIOD},
                ],
            },
        )

    def get(self, deployment_id):
        return self.deployment if deployment_id == 701 else None

    def get_active_by_name(self, name):
        return self.deployment if name == "DEV-03" else None

    def get_by_name(self, name):
        return self.get_active_by_name(name)

    def list_browser_download_records_for_deployment(self, deployment_id):
        return self.rows


def _run(args, rows, deploy_args=None, hot_files="present", s3_error=None):
    """Drive the command offline.

    `hot_files` is what the LIVE S3 object holds ("present", a document, or
    None for "gone" — which is what the deployment's own `clients destroy`
    leaves behind); `s3_error` makes that read raise instead. What the registry
    recorded goes in `deploy_args` under `browser_hot_files`.
    """
    hot = _hot_files() if hot_files == "present" else hot_files
    patched_load = (
        patch(
            "saorsa_deploy.analysis.browser_reads.load_hot_files",
            side_effect=s3_error,
        )
        if s3_error is not None
        else patch("saorsa_deploy.analysis.browser_reads.load_hot_files", return_value=hot)
    )
    with (
        patch(
            "saorsa_deploy.analysis.browser_reads.get_repository",
            return_value=_Repo(rows, deploy_args),
        ),
        patched_load,
    ):
        return cmd_browser_reads(args)


def _deploy_args(**extra):
    args = {
        "kind": "testnet",
        "browser_downloaders": [
            {"hostname": "h1", "instances": 4, "round_period_secs": PERIOD},
            {"hostname": "h2", "instances": 4, "round_period_secs": PERIOD},
        ],
    }
    args.update(extra)
    return args


def test_exit_zero_when_every_gate_passes():
    assert _run(_args(), _rows("spread-rows.json")) == 0


def test_exit_one_when_a_gate_fails():
    """Distinct from 2 on purpose: an unattended runner has to tell "the release
    made the read path worse" from "I measured nothing"."""
    assert (
        _run(
            _args(window_end="2026-10-18T10:00:00Z"),
            _rows("nearest-first-rows.json"),
        )
        == 1
    )


def test_exit_two_when_no_rounds_are_recorded():
    assert _run(_args(), []) == 2


def test_exit_two_when_no_round_falls_in_the_window():
    assert (
        _run(
            _args(
                window_start="2026-10-19T09:00:00Z",
                window_end="2026-10-19T09:40:00Z",
            ),
            _rows("spread-rows.json"),
        )
        == 2
    )


def test_exit_two_when_there_is_no_hot_file_selection():
    """Neither source has it: nothing recorded, and the S3 object gone."""
    assert _run(_args(), _rows("spread-rows.json"), hot_files=None) == 2


def test_the_recorded_snapshot_is_used_after_the_s3_object_is_gone():
    """The case this report exists for. The selection lives under
    `addresses/<deployment>/`, which the deployment's own `clients destroy`
    deletes WHOLE — and the report is run after teardown, so reading S3 first
    would make a torn-down run's primary verdict unobtainable."""
    code = _run(
        _args(json_out=None),
        _rows("spread-rows.json"),
        deploy_args=_deploy_args(browser_hot_files=_hot_files()),
        hot_files=None,
    )
    assert code == 0


def test_the_recorded_snapshot_is_preferred_over_the_live_object(tmp_path):
    """Preferred, not merely a fallback: a selection that was `--replace`d since
    the rounds were recorded says nothing about the rounds in hand, and the
    report names which source it read."""
    replaced = _hot_files()
    replaced["files"] = [
        {"index": 0, "address": "ff" * 32, "bytes": 1, "source_key": "k", "line": 1}
    ]
    json_out = tmp_path / "report.json"
    code = _run(
        _args(json_out=str(json_out)),
        _rows("spread-rows.json"),
        deploy_args=_deploy_args(browser_hot_files=_hot_files()),
        hot_files=replaced,
    )
    assert code == 0
    document = json.loads(json_out.read_text())
    assert document["hot_files"]["source"] == "deploy_args.browser_hot_files"
    # The recorded selection's two 50 MiB files, not the replacement's one.
    assert [f["address"] for f in document["hot_files"]["files"]] == [
        f["address"] for f in _hot_files()["files"]
    ]


def test_the_live_object_is_named_as_the_source_when_nothing_was_recorded(tmp_path):
    json_out = tmp_path / "report.json"
    assert _run(_args(json_out=str(json_out)), _rows("spread-rows.json")) == 0
    source = json.loads(json_out.read_text())["hot_files"]["source"]
    assert source.startswith("s3://") and "browser-hot-files.json" in source


def test_a_half_written_snapshot_falls_back_to_s3():
    """A document with no `files` list is not a selection, so it must not shadow
    one that is still readable."""
    assert (
        _run(
            _args(),
            _rows("spread-rows.json"),
            deploy_args=_deploy_args(browser_hot_files={"selected_at": "x"}),
        )
        == 0
    )


def test_an_s3_failure_with_nothing_recorded_exits_two_not_a_traceback():
    assert (
        _run(
            _args(),
            _rows("spread-rows.json"),
            s3_error=RuntimeError("no credentials"),
        )
        == 2
    )


def test_an_s3_failure_is_irrelevant_once_a_snapshot_exists():
    """Which is the whole point of taking one — the report must not depend on
    reaching S3 at all after teardown."""
    assert (
        _run(
            _args(),
            _rows("spread-rows.json"),
            deploy_args=_deploy_args(browser_hot_files=_hot_files()),
            s3_error=RuntimeError("no credentials"),
        )
        == 0
    )


def test_exit_two_when_the_period_is_unknown():
    """No role config and boundaries that do not divide evenly: the window
    selection is defined in terms of the period, so a guess would silently
    report the wrong rounds."""
    rows = _rows("spread-rows.json")
    for row in rows:
        row["started_at"] = row["started_at"].replace(second=7)
    assert _run(_args(), rows, deploy_args={"kind": "testnet"}) == 2


def test_the_period_falls_back_to_the_recorded_boundaries():
    """A deployment provisioned without DATABASE_URL recorded no role config,
    and the report must still be producible from the rows alone."""
    assert _run(_args(), _rows("spread-rows.json"), deploy_args={"kind": "testnet"}) == 0


def test_exit_two_on_a_window_that_ends_before_it_starts():
    assert (
        _run(
            _args(window_start="2026-10-18T09:40:00Z", window_end="2026-10-18T09:00:00Z"),
            _rows("spread-rows.json"),
        )
        == 2
    )


def test_the_json_and_markdown_destinations_are_written(tmp_path):
    json_out = tmp_path / "report.json"
    markdown_out = tmp_path / "report.md"
    code = _run(
        _args(json_out=str(json_out), markdown_out=str(markdown_out)),
        _rows("spread-rows.json"),
    )
    assert code == 0
    document = json.loads(json_out.read_text())
    assert document["verdict"] == GATE_PASS
    # Every table is in the JSON, which is what an unattended runner reads.
    for key in (
        "hot_files",
        "rounds",
        "regions",
        "chunks",
        "stalls",
        "refusals",
        "bootstrap_load",
        "gates",
    ):
        assert key in document
    assert markdown_out.read_text().startswith("# Browser hot-read report")


def test_json_out_dash_writes_the_object_once_and_nothing_else(capsys):
    """`-` means "the JSON IS the output": a table in front of it is what makes
    a caller's `jq` abort, and the `// 0` default then turns the abort into a
    confident zero."""
    code = _run(
        _args(json_out="-", print_markdown=True),
        _rows("spread-rows.json"),
    )
    assert code == 0
    out = capsys.readouterr().out
    assert json.loads(out)["deployment"] == "DEV-03"
