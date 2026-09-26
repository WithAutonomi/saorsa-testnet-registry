"""The node-side pointer signal: `pointer_put_rpc` / `pointer_get_rpc` from ES.

These two INFO events are the only quantitative node-side pointer measurement
that is not DEBUG — everything about stale refusals and quorum shortfalls is
DEBUG, and the node-tier forwarder drops DEBUG at `log_level: info`, which is
what staging runs at. So they are what lets "the client saw a stale state" be
told apart from "a node answered stale", and that distinction is the reason the
counts are taken at all.

Document shape, verified against ant-node branch `feat/pointers-immutable-owner`
(`src/storage/handler.rs`) and the node-log index template: telegraf's tail input
captures every non-numeric JSON field, so the event name lands in `tail.message`
and the structured fields as `tail.outcome` / `tail.duration_ms`.
"""

from types import SimpleNamespace

import pytest

from saorsa_deploy.es_log_counts import (
    POINTER_RPC_EVENTS,
    RPC_LATENCY_TARGET,
    build_rpc_latency_query,
    parse_rpc_latency_response,
)
from saorsa_deploy.log_patterns import load_pattern_set


def _agg_response(total, buckets, overall=(12.0, 88.0)):
    return {
        "hits": {"total": {"value": total, "relation": "eq"}},
        "aggregations": {
            "host_count": {"value": 40},
            "first_seen": {"value": 1786000000000},
            "last_seen": {"value": 1786014400000},
            "latency": {"values": {"50.0": overall[0], "95.0": overall[1]}},
            "outcomes": {
                "buckets": [
                    {
                        "key": key,
                        "doc_count": count,
                        "latency": {"values": {"50.0": p50, "95.0": p95}},
                    }
                    for key, count, p50, p95 in buckets
                ]
            },
        },
    }


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------


def test_the_query_pins_the_tracing_target_as_well_as_the_message():
    """Filtering on the message alone would count a future log line that merely
    mentions `pointer_put_rpc` in prose."""
    query = build_rpc_latency_query("pointer_put_rpc")
    filters = query["query"]["bool"]["filter"]
    assert {"term": {"tag.target.keyword": RPC_LATENCY_TARGET}} in filters
    assert {"match_phrase": {"tail.message": "pointer_put_rpc"}} in filters


def test_the_query_aggregates_and_never_scrolls_documents():
    """A staging run's node index takes millions of documents; only the counts
    and the percentiles are wanted."""
    query = build_rpc_latency_query("pointer_get_rpc")
    assert query["size"] == 0
    # The default caps the reported total at 10,000, which would silently turn
    # every large count into exactly 10,000.
    assert query["track_total_hits"] is True


def test_the_outcome_field_is_aggregated_on_its_keyword_subfield():
    """`tail.*` is dynamically mapped, so the analysed field matches nothing in a
    terms aggregation — the trap `es_log_counts` documents for `tag.*`."""
    query = build_rpc_latency_query("pointer_put_rpc")
    assert query["aggs"]["outcomes"]["terms"]["field"] == "tail.outcome.keyword"


def test_latency_is_taken_per_outcome_as_well_as_overall():
    """A rise in the overall p95 driven entirely by the `error` outcome is a
    different finding from one spread across successes."""
    query = build_rpc_latency_query("pointer_put_rpc")
    assert query["aggs"]["latency"]["percentiles"]["field"] == "tail.duration_ms"
    assert "latency" in query["aggs"]["outcomes"]["aggs"]


# ---------------------------------------------------------------------------
# The response
# ---------------------------------------------------------------------------


def test_every_outcome_the_event_can_report_is_present_even_at_zero():
    """ "No `stale` answers" is a finding, and a column that vanishes makes two
    runs' tables incomparable by eye."""
    parsed = parse_rpc_latency_response(
        _agg_response(8, [("success", 8, 10.0, 40.0)]), "pointer_put_rpc"
    )
    assert set(parsed["by_outcome"]) == set(POINTER_RPC_EVENTS["pointer_put_rpc"])
    assert parsed["by_outcome"]["stale"]["count"] == 0
    assert parsed["by_outcome"]["success"]["latency"][50.0] == 10.0


def test_an_outcome_this_build_does_not_know_is_kept():
    """Dropping it would leave the per-outcome counts not summing to the total,
    and the discrepancy would be invisible."""
    parsed = parse_rpc_latency_response(
        _agg_response(10, [("success", 8, 10.0, 40.0), ("novel", 2, 5.0, 6.0)]),
        "pointer_put_rpc",
    )
    assert parsed["by_outcome"]["novel"]["count"] == 2
    assert sum(v["count"] for v in parsed["by_outcome"].values()) == parsed["count"]


def test_a_percentile_over_no_documents_is_none_not_zero():
    """A zero latency for an outcome that never occurred would read as
    instantaneous."""
    parsed = parse_rpc_latency_response(
        _agg_response(1, [("success", 1, None, None)]), "pointer_get_rpc"
    )
    assert parsed["by_outcome"]["success"]["latency"][50.0] is None


def test_the_get_event_has_its_own_outcome_set():
    """A GET cannot be `stale` or `payment_required`; a PUT cannot be
    `not_found`. Listing the wrong set would print columns that can never fill."""
    assert "not_found" in POINTER_RPC_EVENTS["pointer_get_rpc"]
    assert "not_found" not in POINTER_RPC_EVENTS["pointer_put_rpc"]
    assert "payment_required" in POINTER_RPC_EVENTS["pointer_put_rpc"]
    assert "payment_required" not in POINTER_RPC_EVENTS["pointer_get_rpc"]


# ---------------------------------------------------------------------------
# Recording, via record-log-counts
# ---------------------------------------------------------------------------


class _Repo:
    def __init__(self):
        self.rows = []

    def upsert_log_event_count(self, **kwargs):
        self.rows.append(kwargs)


def _run_rpc(repo, responses):
    from rich.console import Console

    from saorsa_deploy.cmd.record_log_counts import _record_pointer_rpc_events

    calls = list(responses)
    index = {"n": 0}

    class FakeClient:
        def search(self, index_pattern, body):
            position = min(index["n"], len(calls) - 1)
            index["n"] += 1
            return calls[position]

    return _record_pointer_rpc_events(
        repo,
        FakeClient(),
        SimpleNamespace(id=7, name="PTR-01"),
        "telegraf-ant-nodes-ptr-01-*",
        load_pattern_set(),
        SimpleNamespace(label="full-run"),
        None,
        None,
        1_000_000,
        Console(),
    )


def test_one_row_per_event_and_outcome():
    """Keyed `<event>:<outcome>` in `deployment_log_event_counts`, so this needed
    no migration and the counts sit where the runner already looks for counts."""
    repo = _Repo()
    _run_rpc(repo, [_agg_response(10, [("success", 10, 10.0, 40.0)])])
    keys = {row["event_key"] for row in repo.rows}
    assert "pointer_put_rpc:success" in keys
    assert "pointer_put_rpc:stale" in keys
    assert "pointer_get_rpc:not_found" in keys
    row = next(r for r in repo.rows if r["event_key"] == "pointer_put_rpc:success")
    assert row["event_group"] == "pointer_put_rpc"
    assert row["count"] == 10
    assert row["details"]["latency_ms"]["50.0"] == 10.0
    assert row["details"]["event_total"] == 10


def test_an_event_with_no_documents_records_nothing():
    """A zero here is what a run with no pointer clients looks like, not a
    finding. Storing zeros would read on the next comparison as a pointer tier
    that stopped working."""
    repo = _Repo()
    assert _run_rpc(repo, [_agg_response(0, [])]) == {}
    assert repo.rows == []


def test_a_query_error_warns_and_leaves_the_phrase_counts_alone():
    """Best-effort by design, unlike the phrase counts: a node build predating
    pointers has no such tracing target at all."""
    from rich.console import Console

    from saorsa_deploy.cmd.record_log_counts import _record_pointer_rpc_events
    from saorsa_deploy.es_client import ESError

    repo = _Repo()

    class Failing:
        def search(self, *_):
            raise ESError("no such field")

    result = _record_pointer_rpc_events(
        repo,
        Failing(),
        SimpleNamespace(id=7, name="PTR-01"),
        "idx",
        load_pattern_set(),
        SimpleNamespace(label="full-run"),
        None,
        None,
        10,
        Console(),
    )
    assert result == {}
    assert repo.rows == []


# ---------------------------------------------------------------------------
# The pointer log-pattern group
# ---------------------------------------------------------------------------


def test_the_catalogue_carries_the_pointer_groups():
    patterns = load_pattern_set()
    groups = {g.key for g in patterns.groups}
    assert {
        "pointer_store_failure",
        "pointer_address_collision",
        "pointer_replication_shortfall",
        "pointer_audit_failure",
        "pointer_prune",
    } <= groups
    # Version bumped, because the catalogue version is stored on every count and
    # is what says whether two runs counted the same thing.
    assert patterns.version >= 3


@pytest.mark.parametrize(
    "key, phrase",
    [
        ("pointer_replicated_store_failed", "Could not store replicated pointer"),
        ("pointer_fresh_store_failed", "Could not store fresh pointer"),
        ("pointer_commit_failed", "Pointer commit failed"),
        ("pointer_offer_not_held", "does not hold pointer"),
        ("pointer_prune_failed", "Could not prune pointer"),
        ("pointer_audit_not_held", "committed pointer"),
    ],
)
def test_each_pointer_phrase_is_the_stable_substring(key, phrase):
    """Every one of these emits interpolates a hex address, and most an error
    too, so a phrase spanning them would match nothing forever and report zero.
    Read off the pinned ant-node branch on 2026-09-26."""
    patterns = {p.key: p for p in load_pattern_set().patterns}
    assert patterns[key].phrase == phrase


def test_the_address_collision_phrases_avoid_the_interpolated_address():
    """ "Refusing pointer at {}: a chunk already occupies that address" puts the
    address in the middle, so only the tail is usable."""
    patterns = {p.key: p for p in load_pattern_set().patterns}
    assert (
        patterns["pointer_refused_chunk_holds_address"].phrase
        == "a chunk already occupies that address"
    )
    assert (
        patterns["chunk_refused_pointer_holds_address"].phrase
        == "a pointer already occupies that address"
    )


def test_the_prune_phrase_starts_after_the_interpolated_count():
    patterns = {p.key: p for p in load_pattern_set().patterns}
    phrase = patterns["pointer_records_pruned"].phrase
    assert phrase == "pointer records this node is no longer responsible for"
    assert "{" not in phrase and not phrase[0].isdigit()


def test_the_connection_groups_are_untouched():
    """Their counts must stay comparable across the version bump: no phrase
    changed, so v2 and v3 counts of them ARE comparable."""
    patterns = {p.key: p for p in load_pattern_set().patterns}
    assert patterns["connection_attempt_failed"].phrase == "Connection attempt failed"
    assert patterns["dial_failed_all_candidates"].phrase == "dial failed for all"
