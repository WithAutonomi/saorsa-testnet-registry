"""Connection-error log-string counting and its gate (V2-842).

Three things are pinned here, each of which produces a confidently wrong number
rather than an error if it breaks:

* the Elasticsearch query shape -- `.keyword` for tags, `tail.message` for the
  text, and an explicit `track_total_hits` (its default caps a reported total at
  10,000, which would silently turn every large count into exactly 10,000);
* the group aggregation -- `max` for one event logged at two layers, because
  summing it reports one occurrence twice;
* absence being NOT ASSESSED and never zero, since no run predating this
  measurement has counts and none can be backfilled.
"""
import re
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from saorsa_deploy.es_log_counts import (
    HOST_FIELD,
    LEVEL_FIELD,
    MESSAGE_FIELD,
    build_count_query,
    build_total_query,
    collapse_index_date_suffix,
    node_index_pattern,
    parse_count_response,
    rate_per_hour,
    summarise_counts,
)
from saorsa_deploy.gate_comparison import (
    FAIL,
    NOT_ASSESSED,
    PASS,
    RunMetrics,
    compare_runs,
    log_count_warnings,
    log_event_group_values,
)
from saorsa_deploy.gate_config import load_gate_config
from saorsa_deploy.gate_report import render_report
from saorsa_deploy.log_patterns import (
    LogPatternError,
    get_default_patterns_path,
    load_pattern_set,
    parse_pattern_set,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


@pytest.fixture
def config():
    return load_gate_config()


@pytest.fixture
def patterns():
    return load_pattern_set()


# ---------------------------------------------------------------------------
# The shipped catalogue
# ---------------------------------------------------------------------------


#: The connection groups V2-842 defined. Named so the assertion below can be
#: scoped to them: the catalogue is shared and has since grown a pointer section
#: (V2-1339), so an equality against every key in the file would fail whenever an
#: unrelated group is added — which says nothing about whether the connection
#: strings are still right.
_CONNECTION_GROUPS = (
    "connection_failure",
    "dial_failure",
    "identity_announce_failure",
)


def test_shipped_catalogue_covers_every_string_from_the_issue(patterns):
    """The strings V2-842 named that can still fire, under stable phrases.

    Four of the original six. The other two were retired at catalogue v2 —
    see the test below.
    """
    by_key = {p.key: p for p in patterns.patterns}
    connection_keys = {
        p.key for p in patterns.patterns if p.group in _CONNECTION_GROUPS
    }
    assert connection_keys == {
        "connection_attempt_failed",
        "happy_eyeballs_failed",
        "dial_failed_all_candidates",
        "identity_announce_failed",
    }
    assert by_key["connection_attempt_failed"].phrase == "Connection attempt failed"
    assert by_key["happy_eyeballs_failed"].phrase == "connect_happy_eyeballs failed"
    assert by_key["dial_failed_all_candidates"].phrase == "dial failed for all"
    assert (
        by_key["identity_announce_failed"].phrase == "Failed to send identity announce"
    )


def test_the_two_unreachable_patterns_stay_retired(patterns):
    """V2-1091: both counted zero on every run because their emit cannot run.

    `accept_stream_connection_not_found` watched a handshake/cleanup race that
    saorsa-transport 0.36.0 designed out via `accept_with_connection()`; from
    that version the accept stream yields only `Ok`, so saorsa-core's `Err(e)`
    arm is unreachable. `quic_disconnect` sits on an arm that never could run:
    `P2pEndpoint::disconnect()` returns only `Ok(())` or `PeerNotFound`, and
    both are consumed by the arms above it.

    Pinned as a test because both look like obvious coverage gaps, and the
    reason they are absent lives in a comment that a re-adder may not read.
    Widening the accept phrase does not help either — the emit is unreachable
    whatever phrase is matched against it.
    """
    keys = {p.key for p in patterns.patterns}
    assert "accept_stream_connection_not_found" not in keys
    assert "quic_disconnect" not in keys

    # The groups must go with them: a group with no patterns is a gate that can
    # never be measured, and the loader rejects one.
    group_keys = {g.key for g in patterns.groups}
    assert "accept_stream_failure" not in group_keys
    assert "quic_disconnect" not in group_keys


def test_the_connection_failure_modules_are_not_transposed(patterns):
    """V2-1091: until v2 these two carried each other's module.

    `Connection attempt failed` is emitted by saorsa-transport's happy-eyeballs
    racer (`src/happy_eyeballs.rs`), once per address attempt;
    `connect_happy_eyeballs failed` is emitted by its caller in saorsa-core
    (`src/transport_handle.rs`), once per overall failure. The catalogue had
    them the other way round, which sends anyone verifying a zero into the
    wrong crate — it is how V2-1091's own investigation lost time.
    """
    by_key = {p.key: p for p in patterns.patterns}
    assert by_key["connection_attempt_failed"].module == (
        "saorsa_transport::happy_eyeballs"
    )
    assert by_key["happy_eyeballs_failed"].module == "saorsa_core::transport_handle"


def test_no_module_field_is_a_file_line(patterns):
    """A file:line drifts on every edit to the file and cannot document anything.

    `quic_disconnect` carried `saorsa_transport_adapter.rs:626` (by V2-1091 it
    was line 643), and that was the tell that the entry had been transcribed by
    reading code rather than from an observed log line — which is why nobody
    noticed it had never matched.
    """
    for pattern in patterns.patterns:
        assert not re.search(r"\.rs:\d+$", pattern.module), (
            f"{pattern.key} has a file:line module ({pattern.module}); use a "
            "module path, which survives edits to the file"
        )


def test_dial_failure_phrase_avoids_the_interpolated_count(patterns):
    """The emit interpolates a peer, an address and a count.

    `[STEP 1b] {} -> {}: dial failed for all {} candidate address(es)` — any
    phrase spanning an interpolated value matches nothing, forever, and reports
    zero rather than failing.
    """
    phrase = {p.key: p.phrase for p in patterns.patterns}["dial_failed_all_candidates"]
    assert phrase == "dial failed for all"
    assert "candidate address" not in phrase
    assert "STEP 1b" not in phrase


def test_the_two_layers_of_one_connection_failure_share_a_group(patterns):
    """V2-842: they are one phenomenon, so they must be one gate."""
    group = patterns.group("connection_failure")
    assert group is not None
    assert group.aggregate == "max"
    assert {p.key for p in patterns.patterns_in_group("connection_failure")} == {
        "connection_attempt_failed",
        "happy_eyeballs_failed",
    }


def test_every_pattern_fits_the_registry_columns(patterns):
    for pattern in patterns.patterns:
        assert len(pattern.key) <= 64
        assert len(pattern.phrase) <= 512
    for group in patterns.groups:
        assert len(group.key) <= 64


def test_catalogue_yaml_is_the_one_that_ships(patterns):
    assert get_default_patterns_path().is_file()
    assert patterns.source_path == str(get_default_patterns_path())
    assert patterns.name == "connection-errors"


# ---------------------------------------------------------------------------
# Catalogue validation -- loud failures, never a silently dropped pattern
# ---------------------------------------------------------------------------


def _catalogue(**overrides) -> dict:
    data = {
        "version": 1,
        "index_scope": "nodes",
        "groups": [{"key": "g", "label": "G", "aggregate": "sum"}],
        "patterns": [{"key": "p", "group": "g", "phrase": "boom"}],
    }
    data.update(overrides)
    return data


def test_pattern_naming_an_undefined_group_is_refused():
    """Its count would be recorded and then never gated on."""
    data = _catalogue(patterns=[{"key": "p", "group": "nope", "phrase": "boom"}])
    with pytest.raises(LogPatternError, match="not defined"):
        parse_pattern_set(data, "cat.yaml", "cat")


def test_group_with_no_patterns_is_refused():
    """An empty group produces a gate that can never be measured."""
    data = _catalogue(
        groups=[
            {"key": "g", "label": "G", "aggregate": "sum"},
            {"key": "empty", "label": "E", "aggregate": "sum"},
        ]
    )
    with pytest.raises(LogPatternError, match="empty"):
        parse_pattern_set(data, "cat.yaml", "cat")


def test_duplicate_pattern_key_is_refused():
    """Counts are one row per key, so the second would overwrite the first."""
    data = _catalogue(
        patterns=[
            {"key": "p", "group": "g", "phrase": "one"},
            {"key": "p", "group": "g", "phrase": "two"},
        ]
    )
    with pytest.raises(LogPatternError, match="duplicate"):
        parse_pattern_set(data, "cat.yaml", "cat")


def test_unknown_aggregate_is_refused():
    data = _catalogue(groups=[{"key": "g", "label": "G", "aggregate": "mean"}])
    with pytest.raises(LogPatternError, match="aggregate"):
        parse_pattern_set(data, "cat.yaml", "cat")


def test_missing_version_is_refused():
    data = _catalogue()
    del data["version"]
    with pytest.raises(LogPatternError, match="version"):
        parse_pattern_set(data, "cat.yaml", "cat")


def test_empty_phrase_is_refused():
    """A blank phrase would match every document and record the whole index."""
    data = _catalogue(patterns=[{"key": "p", "group": "g", "phrase": "  "}])
    with pytest.raises(LogPatternError, match="phrase"):
        parse_pattern_set(data, "cat.yaml", "cat")


def test_shipped_catalogue_parses_from_disk():
    data = yaml.safe_load(get_default_patterns_path().read_text())
    parsed = parse_pattern_set(data, str(get_default_patterns_path()), "connection-errors")
    assert parsed.version >= 1
    assert parsed.index_scope == "nodes"


# ---------------------------------------------------------------------------
# Query construction -- the four ways an ES count goes silently wrong
# ---------------------------------------------------------------------------


def test_count_query_matches_the_message_field_as_a_phrase():
    """Fields nest under the measurement name `tail`, not `fields`.

    The index template declares a top-level `message` property, but no real
    document has one, so querying it returns zero forever.
    """
    body = build_count_query("Connection attempt failed")
    clause = body["query"]["bool"]["filter"][0]
    assert clause == {"match_phrase": {"tail.message": "Connection attempt failed"}}
    assert MESSAGE_FIELD == "tail.message"


def test_count_query_tracks_total_hits():
    """Without it the reported total silently caps at 10,000."""
    body = build_count_query("boom")
    assert body["track_total_hits"] is True
    assert body["size"] == 0


def test_aggregations_use_keyword_subfields():
    """`tag.*` is analysed: a term on the bare field matches nothing, and a
    cardinality on it errors outright."""
    aggs = build_count_query("boom")["aggs"]
    assert aggs["host_count"]["cardinality"]["field"] == HOST_FIELD
    assert aggs["top_hosts"]["terms"]["field"] == HOST_FIELD
    assert aggs["levels"]["terms"]["field"] == LEVEL_FIELD
    assert HOST_FIELD.endswith(".keyword")
    assert LEVEL_FIELD.endswith(".keyword")


def test_unwindowed_query_has_no_range_filter():
    """The default counts the whole index.

    A time filter is applied against each host's own clock, and the testnet
    profile skews those by up to ±3h in a per-host random direction.
    """
    body = build_count_query("boom")
    assert len(body["query"]["bool"]["filter"]) == 1


def test_windowed_query_is_half_open_on_timestamp():
    body = build_count_query("boom", T0, T0 + timedelta(hours=3))
    range_clause = body["query"]["bool"]["filter"][1]["range"]["@timestamp"]
    assert range_clause["gte"] == "2026-08-11T12:00:00Z"
    assert range_clause["lt"] == "2026-08-11T15:00:00Z"


def test_naive_window_bounds_are_treated_as_utc():
    body = build_count_query("boom", datetime(2026, 8, 11, 12, 0), None)
    assert body["query"]["bool"]["filter"][1]["range"]["@timestamp"]["gte"] == (
        "2026-08-11T12:00:00Z"
    )


def test_total_query_counts_everything_when_unwindowed():
    assert build_total_query()["query"] == {"match_all": {}}
    assert build_total_query()["track_total_hits"] is True


def test_index_pattern_is_lowercased_and_globbed():
    assert node_index_pattern("STG-01") == "telegraf-ant-nodes-stg-01-*"


def test_strftime_index_pattern_from_state_is_collapsed_to_a_glob():
    """The stored pattern is what telegraf writes WITH, and matches no index.

    Collapsed to `*` rather than to today's date so a run straddling midnight
    UTC still matches every day it wrote.
    """
    assert (
        collapse_index_date_suffix("telegraf-ant-nodes-stg-01-%Y.%m.%d")
        == "telegraf-ant-nodes-stg-01-*"
    )
    assert (
        collapse_index_date_suffix("telegraf-ant-nodes-stg-01-*")
        == "telegraf-ant-nodes-stg-01-*"
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _response(count: int, hosts: int = 3, levels=None, top=None, span=True) -> dict:
    aggs = {
        "host_count": {"value": hosts},
        "top_hosts": {
            "buckets": [
                {"key": key, "doc_count": value}
                for key, value in (top or {"STG-01-ANT-NODE-A": count}).items()
            ]
        },
        "levels": {
            "buckets": [
                {"key": key, "doc_count": value}
                for key, value in (levels or {"WARN": count}).items()
            ]
        },
        "first_seen": {"value": T0.timestamp() * 1000 if span else None},
        "last_seen": {
            "value": (T0 + timedelta(hours=4)).timestamp() * 1000 if span else None
        },
    }
    return {"hits": {"total": {"value": count, "relation": "eq"}}, "aggregations": aggs}


def test_parse_reads_count_hosts_levels_and_span():
    parsed = parse_count_response(_response(71309, hosts=68, levels={"WARN": 71309}))
    assert parsed["count"] == 71309
    assert parsed["hosts"] == 68
    assert parsed["by_level"] == {"WARN": 71309}
    assert parsed["first_seen"] == T0
    assert parsed["last_seen"] == T0 + timedelta(hours=4)


def test_parse_accepts_a_bare_integer_total():
    """Very old Elasticsearch returns `hits.total` as an integer.

    Worth tolerating rather than crashing at the end of a five-hour run.
    """
    parsed = parse_count_response({"hits": {"total": 42}, "aggregations": {}})
    assert parsed["count"] == 42


def test_zero_count_parses_with_no_span():
    """A pattern that matched nothing has no first/last timestamp.

    A zero is a real measurement and must remain storable.
    """
    parsed = parse_count_response(_response(0, hosts=0, span=False))
    assert parsed["count"] == 0
    assert parsed["first_seen"] is None
    assert parsed["last_seen"] is None


def test_rate_per_hour_is_none_without_a_span():
    assert rate_per_hour(100, None, None) is None
    assert rate_per_hour(100, T0, T0) is None
    assert rate_per_hour(400, T0, T0 + timedelta(hours=4)) == 100.0


# ---------------------------------------------------------------------------
# Grouping -- max, not sum, for one event logged twice
# ---------------------------------------------------------------------------


def test_summarise_takes_the_max_for_a_two_layer_group(patterns):
    """Summing 71,309 and 71,160 would report ~142k failures where ~71k occurred."""
    results = {
        "connection_attempt_failed": {"count": 71309, "hosts": 68},
        "happy_eyeballs_failed": {"count": 71160, "hosts": 67},
    }
    summary = {row["group"]: row for row in summarise_counts(results, patterns)}
    assert summary["connection_failure"]["count"] == 71309
    assert summary["connection_failure"]["aggregate"] == "max"
    assert summary["connection_failure"]["members"] == {
        "connection_attempt_failed": 71309,
        "happy_eyeballs_failed": 71160,
    }


def test_summarise_omits_groups_with_no_measured_member(patterns):
    """Never queried and occurred zero times are different facts."""
    summary = summarise_counts(
        {"connection_attempt_failed": {"count": 5, "hosts": 1}}, patterns
    )
    assert {row["group"] for row in summary} == {"connection_failure"}


def test_group_values_returns_none_for_a_run_with_no_rows():
    assert log_event_group_values([], "connection_failure", "max") is None


def test_group_values_computes_rate_and_share():
    rows = [
        _row("connection_attempt_failed", "connection_failure", 3600, hosts=40),
        _row("happy_eyeballs_failed", "connection_failure", 3500, hosts=39),
    ]
    values = log_event_group_values(rows, "connection_failure", "max")
    assert values["value"] == 3600.0
    assert values["hosts"] == 40
    assert values["rate_per_hour"] == pytest.approx(900.0)
    assert values["total_documents"] == 1_000_000


def test_sum_group_adds_its_members():
    rows = [
        _row("a", "g", 10),
        _row("b", "g", 5),
    ]
    assert log_event_group_values(rows, "g", "sum")["value"] == 15.0
    assert log_event_group_values(rows, "g", "max")["value"] == 10.0


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _row(
    key: str,
    group: str,
    count: int,
    hosts: int = 10,
    hours: float = 4.0,
    version: int = 1,
    total: int = 1_000_000,
) -> dict:
    return {
        "event_key": key,
        "event_group": group,
        "pattern": "phrase",
        "pattern_set": "connection-errors",
        "pattern_version": version,
        "count": count,
        "hosts": hosts,
        "total_documents": total,
        "observed_start": T0,
        "observed_end": T0 + timedelta(hours=hours),
    }


def _run(deployment_id: int, name: str, rows: list[dict]) -> RunMetrics:
    return RunMetrics(
        deployment_id=deployment_id,
        name=name,
        window_label="full-run",
        window_start=T0,
        window_end=T0 + timedelta(hours=4),
        log_event_counts=rows,
    )


def _connection_gates(verdict: dict) -> dict:
    return {
        gate["gate"]: gate
        for gate in verdict["gates"]
        if gate["gate"].startswith("Connection errors:")
    }


def test_a_rise_above_the_threshold_fails(config, patterns):
    candidate = _run(2, "STG-01", [_row("dial_failed_all_candidates", "dial_failure", 1200)])
    baseline = _run(1, "STG-01", [_row("dial_failed_all_candidates", "dial_failure", 1000)])
    verdict = compare_runs(candidate, baseline, config, pattern_set=patterns)
    gate = _connection_gates(verdict)["Connection errors: Dial failures (all candidates)"]
    assert gate["verdict"] == FAIL
    assert gate["delta_percent"] == pytest.approx(20.0)
    assert gate["kind"] == "soft"
    assert gate["unit"] == "count"


def test_a_rise_within_the_threshold_passes(config, patterns):
    candidate = _run(2, "STG-01", [_row("dial_failed_all_candidates", "dial_failure", 1050)])
    baseline = _run(1, "STG-01", [_row("dial_failed_all_candidates", "dial_failure", 1000)])
    verdict = compare_runs(candidate, baseline, config, pattern_set=patterns)
    assert (
        _connection_gates(verdict)["Connection errors: Dial failures (all candidates)"]["verdict"]
        == PASS
    )


def test_a_fall_passes(config, patterns):
    """One-sided, like every other soft gate: an improvement is not a regression."""
    candidate = _run(2, "STG-01", [_row("dial_failed_all_candidates", "dial_failure", 10)])
    baseline = _run(1, "STG-01", [_row("dial_failed_all_candidates", "dial_failure", 1000)])
    verdict = compare_runs(candidate, baseline, config, pattern_set=patterns)
    assert (
        _connection_gates(verdict)["Connection errors: Dial failures (all candidates)"]["verdict"]
        == PASS
    )


def test_the_two_layer_group_produces_one_gate_not_two(config, patterns):
    rows = [
        _row("connection_attempt_failed", "connection_failure", 71309),
        _row("happy_eyeballs_failed", "connection_failure", 71160),
    ]
    base = [
        _row("connection_attempt_failed", "connection_failure", 1415),
        _row("happy_eyeballs_failed", "connection_failure", 1415),
    ]
    verdict = compare_runs(
        _run(2, "STG-01", rows), _run(1, "STG-01", base), config, pattern_set=patterns
    )
    gates = _connection_gates(verdict)
    matching = [g for g in gates if "Connection failures" in g]
    assert len(matching) == 1
    gate = gates[matching[0]]
    # max, not sum: 71,309 rather than 142,469.
    assert gate["candidate"] == 71309.0
    assert gate["baseline"] == 1415.0
    assert gate["verdict"] == FAIL
    assert "max, not sum" in gate["detail"]


def test_a_baseline_with_no_counts_is_not_assessed_never_a_regression(config, patterns):
    """Every run predating this measurement has no counts and none can be
    backfilled — the run's Elasticsearch dies at teardown. Treating absence as
    zero would show the candidate's real counts against nothing."""
    candidate = _run(
        2, "STG-01", [_row("dial_failed_all_candidates", "dial_failure", 50_000)]
    )
    baseline = _run(1, "STG-01", [])
    verdict = compare_runs(candidate, baseline, config, pattern_set=patterns)
    for gate in _connection_gates(verdict).values():
        assert gate["verdict"] == NOT_ASSESSED
        assert gate["baseline"] is None
    assert verdict["overall"] != "SOFT FAIL"


def test_not_assessed_names_the_missing_side_and_the_remedy(config, patterns):
    verdict = compare_runs(
        _run(2, "STG-01", []),
        _run(1, "STG-01", [_row("dial_failed_all_candidates", "dial_failure", 10)]),
        config,
        pattern_set=patterns,
    )
    detail = _connection_gates(verdict)[
        "Connection errors: Dial failures (all candidates)"
    ]["detail"]
    assert "candidate" in detail
    assert "record-log-counts" in detail
    assert "backfilled" in detail


def test_every_catalogue_group_gets_a_gate(config, patterns):
    """Including ones neither run recorded, so a missing measurement is visible."""
    verdict = compare_runs(
        _run(2, "STG-01", []), _run(1, "STG-01", []), config, pattern_set=patterns
    )
    assert len(_connection_gates(verdict)) == len(patterns.groups)


def test_a_group_absent_from_the_catalogue_is_still_evaluated(config):
    """The counts were taken deliberately; dropping them would be a silent loss."""
    verdict = compare_runs(
        _run(2, "STG-01", [_row("x", "mystery", 200)]),
        _run(1, "STG-01", [_row("x", "mystery", 100)]),
        config,
        pattern_set=None,
    )
    gate = _connection_gates(verdict)["Connection errors: mystery"]
    assert gate["verdict"] == FAIL


def test_appearing_where_the_baseline_had_none_fails(config, patterns):
    """No percentage can express a rise from zero, but it is still a regression.

    NOT ASSESSED would read as "could not be measured" for the clearest signal
    this gate can see.
    """
    verdict = compare_runs(
        _run(2, "STG-01", [_row("dial_failed_all_candidates", "dial_failure", 50_000)]),
        _run(1, "STG-01", [_row("dial_failed_all_candidates", "dial_failure", 0)]),
        config,
        pattern_set=patterns,
    )
    gate = _connection_gates(verdict)["Connection errors: Dial failures (all candidates)"]
    assert gate["verdict"] == FAIL
    assert gate["delta_percent"] is None
    assert "undefined" in gate["detail"]


def test_zero_on_both_runs_passes_rather_than_reading_as_unmeasurable(
    config, patterns
):
    """A permanently eliminated string is a measurement, not a missing one."""
    verdict = compare_runs(
        _run(2, "STG-01", [_row("dial_failed_all_candidates", "dial_failure", 0)]),
        _run(1, "STG-01", [_row("dial_failed_all_candidates", "dial_failure", 0)]),
        config,
        pattern_set=patterns,
    )
    gate = _connection_gates(verdict)["Connection errors: Dial failures (all candidates)"]
    assert gate["verdict"] == PASS
    assert gate["detail"] == "neither run logged this at all"


def test_gate_detail_carries_the_rate_and_host_spread(config, patterns):
    """A raw count cannot distinguish a fleet-wide regression from one sick node."""
    verdict = compare_runs(
        _run(
            2,
            "STG-01",
            [_row("dial_failed_all_candidates", "dial_failure", 4000, hosts=68)],
        ),
        _run(
            1,
            "STG-01",
            [_row("dial_failed_all_candidates", "dial_failure", 4000, hosts=2)],
        ),
        config,
        pattern_set=patterns,
    )
    detail = _connection_gates(verdict)[
        "Connection errors: Dial failures (all candidates)"
    ]["detail"]
    assert "68 hosts" in detail
    assert "2 hosts" in detail
    assert "/h" in detail
    assert "of lines" in detail


# ---------------------------------------------------------------------------
# Warnings a count comparison cannot infer for itself
# ---------------------------------------------------------------------------


def test_differing_pattern_versions_warn():
    warnings = log_count_warnings(
        _run(2, "STG-01", [_row("a", "g", 10, version=2)]),
        _run(1, "STG-01", [_row("a", "g", 10, version=1)]),
    )
    assert any("catalogue versions differ" in w for w in warnings)


def test_materially_different_log_spans_warn():
    """Counts scale with how long a run logged for."""
    warnings = log_count_warnings(
        _run(2, "STG-01", [_row("a", "g", 10, hours=5.0)]),
        _run(1, "STG-01", [_row("a", "g", 10, hours=3.0)]),
    )
    assert any("per-hour rate" in w for w in warnings)


def test_equal_spans_and_versions_produce_no_warning():
    assert (
        log_count_warnings(
            _run(2, "STG-01", [_row("a", "g", 10)]),
            _run(1, "STG-01", [_row("a", "g", 12)]),
        )
        == []
    )


def test_no_warning_when_a_run_has_no_counts_at_all():
    """The NOT ASSESSED gates already say so; a span warning would be noise."""
    assert log_count_warnings(_run(2, "STG-01", []), _run(1, "STG-01", [])) == []


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_report_renders_counts_with_thousands_separators(config, patterns):
    verdict = compare_runs(
        _run(2, "STG-01", [_row("dial_failed_all_candidates", "dial_failure", 71309)]),
        _run(1, "STG-01", [_row("dial_failed_all_candidates", "dial_failure", 1415)]),
        config,
        pattern_set=patterns,
    )
    report = render_report(verdict)
    assert "71,309" in report
    assert "1,415" in report
    assert "Connection errors: Dial failures (all candidates)" in report


def test_report_is_byte_identical_for_the_same_inputs(config, patterns):
    def build():
        return render_report(
            compare_runs(
                _run(2, "STG-01", [_row("dial_failed_all_candidates", "dial_failure", 71309)]),
                _run(1, "STG-01", [_row("dial_failed_all_candidates", "dial_failure", 1415)]),
                config,
                pattern_set=patterns,
            )
        )

    assert build() == build()


def test_threshold_config_declares_the_connection_gate(config):
    assert config.connection_error_increase_percent == 10.0


# ---------------------------------------------------------------------------
# Round trip through the registry
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo():
    from saorsa_db.db import Base
    from saorsa_db.repositories import SqlAlchemyDeploymentRepository
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    yield SqlAlchemyDeploymentRepository(session)
    session.close()
    engine.dispose()


def _store(repo, deployment_id, key, group, count, label="full-run"):
    return repo.upsert_log_event_count(
        deployment_id=deployment_id,
        event_key=key,
        event_group=group,
        pattern="phrase",
        pattern_set="connection-errors",
        pattern_version=1,
        window_label=label,
        count=count,
        hosts=40,
        total_documents=1_000_000,
        observed_start=T0,
        observed_end=T0 + timedelta(hours=4),
    )


def test_re_recording_replaces_rather_than_accumulates(repo):
    """Two rival counts for one window would make the comparison
    non-deterministic — the same rule the derived metrics follow."""
    deployment = repo.create(name="STG-01", deploy_args={})
    _store(repo, deployment.id, "dial_failed_all_candidates", "dial_failure", 100)
    _store(repo, deployment.id, "dial_failed_all_candidates", "dial_failure", 250)

    rows = repo.list_log_event_counts_for_deployment(deployment.id)
    assert len(rows) == 1
    assert rows[0].count == 250


def test_counts_are_kept_per_window_label(repo):
    deployment = repo.create(name="STG-01", deploy_args={})
    _store(repo, deployment.id, "dial_failed_all_candidates", "dial_failure", 100)
    _store(
        repo,
        deployment.id,
        "dial_failed_all_candidates",
        "dial_failure",
        30,
        label="steady-state",
    )

    assert len(repo.list_log_event_counts_for_deployment(deployment.id)) == 2
    windowed = repo.list_log_event_counts_for_deployment(
        deployment.id, window_label="steady-state"
    )
    assert [row.count for row in windowed] == [30]


def test_a_large_count_survives_the_round_trip(repo):
    """The column is a BigInteger: a staging fleet's log volume makes a
    32-bit count reachable."""
    deployment = repo.create(name="STG-01", deploy_args={})
    _store(repo, deployment.id, "dial_failed_all_candidates", "dial_failure", 4_000_000_000)
    assert (
        repo.list_log_event_counts_for_deployment(deployment.id)[0].count
        == 4_000_000_000
    )


def test_stored_rows_drive_the_gate_end_to_end(repo, config, patterns):
    """The rows the recorder writes are the rows the comparison reads."""
    candidate_row = repo.create(name="STG-01", deploy_args={})
    baseline_row = repo.create(name="STG-01", deploy_args={})
    _store(repo, candidate_row.id, "dial_failed_all_candidates", "dial_failure", 1200)
    _store(repo, baseline_row.id, "dial_failed_all_candidates", "dial_failure", 1000)

    def as_metrics(deployment):
        return _run(
            deployment.id,
            deployment.name,
            [
                {
                    "event_key": row.event_key,
                    "event_group": row.event_group,
                    "pattern": row.pattern,
                    "pattern_set": row.pattern_set,
                    "pattern_version": row.pattern_version,
                    "count": row.count,
                    "hosts": row.hosts,
                    "total_documents": row.total_documents,
                    "observed_start": row.observed_start,
                    "observed_end": row.observed_end,
                }
                for row in repo.list_log_event_counts_for_deployment(
                    deployment.id, window_label="full-run"
                )
            ],
        )

    verdict = compare_runs(
        as_metrics(candidate_row),
        as_metrics(baseline_row),
        config,
        pattern_set=patterns,
    )
    gate = _connection_gates(verdict)["Connection errors: Dial failures (all candidates)"]
    assert gate["verdict"] == FAIL
    assert gate["delta_percent"] == pytest.approx(20.0)


def test_deleting_counts_is_scoped_to_a_window(repo):
    deployment = repo.create(name="STG-01", deploy_args={})
    _store(repo, deployment.id, "dial_failed_all_candidates", "dial_failure", 100)
    _store(
        repo,
        deployment.id,
        "dial_failed_all_candidates",
        "dial_failure",
        30,
        label="steady-state",
    )
    assert repo.delete_log_event_counts(deployment.id, window_label="steady-state") == 1
    remaining = repo.list_log_event_counts_for_deployment(deployment.id)
    assert [row.window_label for row in remaining] == ["full-run"]


# ---------------------------------------------------------------------------
# Advisory (environmental) groups -- V2-1441
#
# The payment-RPC group measures the weather the run happened in, not the
# release: the public Sepolia endpoint rate-limiting payment verification. It
# is recorded and rendered like any other group, but it never decides the
# verdict, and a rise in it is repeated as a WARNING naming the gates it casts
# doubt on.
# ---------------------------------------------------------------------------


def test_advisory_defaults_to_false():
    parsed = parse_pattern_set(_catalogue(), "cat.yaml", "cat")
    assert parsed.group("g").advisory is False
    assert parsed.group("g").confounds == ""


def test_non_boolean_advisory_is_refused():
    """'yes' would be a silent choice about whether a group decides the verdict."""
    data = _catalogue(
        groups=[{"key": "g", "label": "G", "aggregate": "sum", "advisory": "yes"}]
    )
    with pytest.raises(LogPatternError, match="advisory"):
        parse_pattern_set(data, "cat.yaml", "cat")


def test_confounds_on_a_counted_group_is_refused():
    """A counted group IS one of the gates; it cannot also cast doubt on them."""
    data = _catalogue(
        groups=[{"key": "g", "label": "G", "aggregate": "sum", "confounds": "x"}]
    )
    with pytest.raises(LogPatternError, match="confounds"):
        parse_pattern_set(data, "cat.yaml", "cat")


def test_bundled_catalogue_marks_the_payment_rpc_group_advisory(patterns):
    """The bundled v5 group: two phrases, advisory, and it names what it confounds."""
    assert patterns.version >= 5
    group = patterns.group("payment_rpc_failure")
    assert group is not None
    assert group.advisory is True
    assert group.aggregate == "max"
    assert "upload" in group.confounds
    keys = {p.key for p in patterns.patterns_in_group("payment_rpc_failure")}
    assert keys == {"payment_rpc_http_429", "completed_payments_lookup_failed"}
    # Every other group still counts: this flag must not leak.
    assert all(
        not g.advisory for g in patterns.groups if g.key != "payment_rpc_failure"
    )


def _advisory_patterns():
    return parse_pattern_set(
        _catalogue(
            groups=[
                {"key": "g", "label": "G", "aggregate": "sum"},
                {
                    "key": "env",
                    "label": "Env",
                    "aggregate": "max",
                    "advisory": True,
                    "confounds": "the upload-duration gates",
                },
            ],
            patterns=[
                {"key": "p", "group": "g", "phrase": "boom"},
                {"key": "e", "group": "env", "phrase": "429"},
            ],
        ),
        "cat.yaml",
        "cat",
    )


def test_an_advisory_group_fail_is_reported_but_never_decides_the_verdict(config):
    verdict = compare_runs(
        _run(2, "STG-01", [_row("e", "env", 5000)]),
        _run(1, "STG-01", [_row("e", "env", 100)]),
        config,
        pattern_set=_advisory_patterns(),
    )
    gate = _connection_gates(verdict)["Connection errors: Env"]
    assert gate["verdict"] == FAIL
    assert gate["advisory"] is True
    assert "advisory, not blocking" in gate["detail"]
    assert verdict["overall"] != "SOFT FAIL"


def test_a_counted_group_fail_still_decides_the_verdict(config):
    """The control for the test above: same rise on a counted group."""
    verdict = compare_runs(
        _run(2, "STG-01", [_row("p", "g", 5000)]),
        _run(1, "STG-01", [_row("p", "g", 100)]),
        config,
        pattern_set=_advisory_patterns(),
    )
    gate = _connection_gates(verdict)["Connection errors: G"]
    assert gate["verdict"] == FAIL
    assert gate["advisory"] is False
    assert verdict["overall"] == "SOFT FAIL"


def test_an_advisory_group_absent_on_one_side_is_marked_advisory_too(config):
    verdict = compare_runs(
        _run(2, "STG-01", [_row("e", "env", 5000)]),
        _run(1, "STG-01", []),
        config,
        pattern_set=_advisory_patterns(),
    )
    gate = _connection_gates(verdict)["Connection errors: Env"]
    assert gate["verdict"] == NOT_ASSESSED
    assert gate["advisory"] is True


def test_a_rise_in_an_advisory_group_warns_about_what_it_confounds():
    warnings = log_count_warnings(
        _run(2, "STG-01", [_row("e", "env", 5000)]),
        _run(1, "STG-01", [_row("e", "env", 100)]),
        _advisory_patterns(),
    )
    hits = [w for w in warnings if w.startswith("Env:")]
    assert len(hits) == 1
    assert "1,250/h on the candidate vs 25/h on the baseline" in hits[0]
    assert "the upload-duration gates" in hits[0]


def test_a_rise_below_the_confounder_factor_does_not_warn():
    """1.5x is weather too, but a warning that fires every run gets ignored."""
    warnings = log_count_warnings(
        _run(2, "STG-01", [_row("e", "env", 150)]),
        _run(1, "STG-01", [_row("e", "env", 100)]),
        _advisory_patterns(),
    )
    assert not any(w.startswith("Env:") for w in warnings)


def test_a_counted_group_rise_is_a_gate_not_a_confounder():
    warnings = log_count_warnings(
        _run(2, "STG-01", [_row("p", "g", 5000)]),
        _run(1, "STG-01", [_row("p", "g", 100)]),
        _advisory_patterns(),
    )
    assert not any("confounds" in w for w in warnings)


def test_an_advisory_group_the_baseline_never_recorded_still_warns():
    """Runs before v5 have no payment-RPC counts; the candidate's figure still matters."""
    warnings = log_count_warnings(
        _run(2, "STG-01", [_row("e", "env", 5000)]),
        _run(1, "STG-01", [_row("p", "g", 10)]),
        _advisory_patterns(),
    )
    hits = [w for w in warnings if w.startswith("Env:")]
    assert len(hits) == 1
    assert "baseline recorded no such counts" in hits[0]


def test_compare_carries_the_confounder_warning_into_the_verdict(config):
    verdict = compare_runs(
        _run(2, "STG-01", [_row("e", "env", 5000)]),
        _run(1, "STG-01", [_row("e", "env", 100)]),
        config,
        pattern_set=_advisory_patterns(),
    )
    assert any(w.startswith("Env:") for w in verdict["warnings"])
