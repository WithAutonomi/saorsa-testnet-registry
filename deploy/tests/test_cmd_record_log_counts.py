"""Unit tests for `deployments record-log-counts` (V2-842).

The command's job is to get the counts out of a run's Elasticsearch before that
Elasticsearch is destroyed. What is pinned here is what it refuses to record: a
count that would be silently wrong is worse than no count, because the gate
would then compare against it as though it were a measurement.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.record_log_counts import cmd_record_log_counts
from saorsa_deploy.log_patterns import load_pattern_set


def _args(**overrides):
    defaults = {
        "id": None,
        "name": "STG-01",
        "elastic_name": None,
        "patterns": None,
        "label": "full-run",
        "window_start": None,
        "window_end": None,
        "index_pattern": None,
        "es_url": "http://es.invalid:9200",
        "es_username": None,
        "es_password": None,
        "es_api_key": "key",
        "timeout": 30,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class FakeRepo:
    def __init__(self):
        self.rows = []

    def get_active_by_name(self, name):
        return SimpleNamespace(id=474, name=name)

    def get_by_name(self, name):  # pragma: no cover - the active lookup wins
        return SimpleNamespace(id=474, name=name)

    def get(self, deployment_id):
        return SimpleNamespace(id=deployment_id, name="STG-01")

    def upsert_log_event_count(self, **kwargs):
        self.rows.append(kwargs)
        return SimpleNamespace(**kwargs)


def _es_response(count: int) -> dict:
    return {
        "hits": {"total": {"value": count, "relation": "eq"}},
        "aggregations": {
            "host_count": {"value": 40},
            "top_hosts": {"buckets": [{"key": "STG-01-ANT-NODE-A", "doc_count": count}]},
            "levels": {"buckets": [{"key": "WARN", "doc_count": count}]},
            "first_seen": {"value": 1786000000000},
            "last_seen": {"value": 1786014400000},
        },
    }


#: Patterns in the shipped catalogue. Derived, not hard-coded: the catalogue is
#: config and grows (the pointer groups were added at v3), and a test that has to
#: be edited every time one does would eventually be edited without being read.
_PATTERN_COUNT = len(load_pattern_set().patterns)


def _run(args, repo, responses):
    """Drive the command with a scripted sequence of Elasticsearch responses.

    Responses past the end of the list repeat the last one. The command issues
    one query per pattern plus one per pointer RPC event, and a test that cares
    about the first few must not have to enumerate the rest -- that coupling is
    what broke every test here when the catalogue gained a group.
    """
    calls = list(responses)
    index = {"n": 0}

    class FakeClient:
        def __init__(self, *_, **__):
            pass

        def search(self, index_pattern, body):
            self.last_index = index_pattern
            position = min(index["n"], len(calls) - 1)
            index["n"] += 1
            return calls[position]

    with patch("saorsa_deploy.cmd.record_log_counts.get_repository", return_value=repo):
        with patch("saorsa_deploy.cmd.record_log_counts.ESClient", FakeClient):
            cmd_record_log_counts(args)


def test_records_one_row_per_pattern():
    repo = FakeRepo()
    # First response is the total-documents query, then one per pattern.
    _run(_args(), repo, [_es_response(1_000_000)] + [_es_response(500)] * 4)

    # One row per catalogue pattern, plus one per pointer RPC event outcome —
    # those are recorded in the same table, keyed `<event>:<outcome>`.
    assert len(repo.rows) >= _PATTERN_COUNT
    keys = {row["event_key"] for row in repo.rows}
    assert "connection_attempt_failed" in keys
    assert "dial_failed_all_candidates" in keys


def test_recorded_row_carries_everything_the_gate_and_a_reader_need():
    repo = FakeRepo()
    _run(_args(), repo, [_es_response(1_000_000)] + [_es_response(500)] * 4)

    row = next(r for r in repo.rows if r["event_key"] == "dial_failed_all_candidates")
    assert row["deployment_id"] == 474
    assert row["count"] == 500
    assert row["hosts"] == 40
    assert row["total_documents"] == 1_000_000
    assert row["event_group"] == "dial_failure"
    assert row["pattern"] == "dial failed for all"
    assert row["pattern_set"] == "connection-errors"
    assert row["pattern_version"] >= 1
    assert row["window_label"] == "full-run"
    assert row["source"] == "elasticsearch"
    # The level breakdown is what shows a pattern is only ever emitted at a
    # level the node forwarder drops by default.
    assert row["details"]["by_level"] == {"WARN": 500}
    assert row["details"]["index_pattern"] == "telegraf-ant-nodes-stg-01-*"


def test_an_empty_index_records_nothing_and_exits_nonzero():
    """Zeros from an empty index would read as a total elimination of every
    error on the next comparison — which is exactly what a misdirected query or
    a run with log forwarding disabled looks like."""
    repo = FakeRepo()
    with pytest.raises(SystemExit) as exc:
        _run(_args(), repo, [_es_response(0)])
    assert exc.value.code == 1
    assert repo.rows == []


def test_a_zero_count_against_a_populated_index_is_still_recorded():
    """That zero IS a measurement: the string did not appear in logs that exist.

    A pattern matching nothing has no first/last timestamp either, so the row
    has to store without a span.
    """
    empty = {
        "hits": {"total": {"value": 0, "relation": "eq"}},
        "aggregations": {
            "host_count": {"value": 0},
            "top_hosts": {"buckets": []},
            "levels": {"buckets": []},
            "first_seen": {"value": None},
            "last_seen": {"value": None},
        },
    }
    repo = FakeRepo()
    _run(_args(), repo, [_es_response(1_000_000)] + [empty] * 4)
    assert len(repo.rows) == _PATTERN_COUNT
    assert all(row["count"] == 0 for row in repo.rows)
    assert all(row["observed_start"] is None for row in repo.rows)
    assert all(row["total_documents"] == 1_000_000 for row in repo.rows)


def test_a_narrowed_window_may_not_claim_the_full_run_label():
    """`full-run` names everything the index holds, so a slice under that label
    would overwrite the whole-run count with a partial one."""
    repo = FakeRepo()
    with pytest.raises(SystemExit) as exc:
        _run(
            _args(window_start="2026-08-11T13:00:00", label="full-run"),
            repo,
            [_es_response(1_000_000)],
        )
    assert exc.value.code == 1
    assert repo.rows == []


def test_a_labelled_window_is_recorded_under_that_label():
    repo = FakeRepo()
    _run(
        _args(
            window_start="2026-08-11T13:00:00",
            window_end="2026-08-11T16:00:00",
            label="steady-state",
        ),
        repo,
        [_es_response(1_000_000)] + [_es_response(10)] * 4,
    )
    assert {row["window_label"] for row in repo.rows} == {"steady-state"}
    assert repo.rows[0]["details"]["scope_start"] == "2026-08-11T13:00:00+00:00"


def test_a_malformed_window_bound_is_rejected():
    repo = FakeRepo()
    with pytest.raises(SystemExit):
        _run(
            _args(window_start="yesterday", label="steady-state"),
            repo,
            [_es_response(1_000_000)],
        )
    assert repo.rows == []


def test_no_database_url_refuses():
    with patch("saorsa_deploy.cmd.record_log_counts.get_repository", return_value=None):
        with pytest.raises(SystemExit) as exc:
            cmd_record_log_counts(_args())
    assert exc.value.code == 1


def test_the_v6_catalogue_counts_the_three_pointer_finality_phrases():
    """The node-side half of the handover measurement (V2-1533, for V2-1354).

    What the client saw is in `deployment_pointer_records`; these are what the
    NODES did about it, and the two are deliberately separate — "a handover did
    not complete" and "a node refused a final state" are different facts.

    All three are emitted on EVENT only, so a zero means no handover reached the
    fleet as much as it means none was refused. That is only readable beside the
    client-side `transfer` counts, which is why the group is recorded rather than
    gated on in isolation.
    """
    repo = FakeRepo()
    _run(_args(), repo, [_es_response(1_000_000)] + [_es_response(7)] * 4)

    rows = {row["event_key"]: row for row in repo.rows}
    for key, phrase, module in (
        ("pointer_final_refused", "Refusing final pointer state", "ant_node::pointer::service"),
        (
            "pointer_fresh_final_refused",
            "Refusing fresh final pointer state",
            "ant_node::replication::pointer",
        ),
        ("pointer_forked", "is forked: peer", "ant_node::replication::pointer"),
    ):
        row = rows[key]
        assert row["event_group"] == "pointer_finality", key
        assert row["pattern"] == phrase, key
        assert row["count"] == 7, key
        # Every one interpolates at least one hex address, so a phrase spanning
        # it would match nothing forever and report a confident zero.
        assert "{" not in row["pattern"], key
        pattern = next(p for p in load_pattern_set().patterns if p.key == key)
        assert pattern.module == module, key

    assert load_pattern_set().version >= 6
