"""Subtree-audit counts and commitment coverage for the V2-1363 pointer A/B.

Two signals the A/B is decided on, neither countable before V2-1413:

* the auditor-side `failed subtree audit ({reason:?})` WARN, now five catalogue
  patterns in one group so the group total is the true failure total; and
* how many commitment rotations reported a *non-zero* pointer leaf count, which
  is the only info-level evidence in ant-node v0.21.0 that audits were opening
  pointer leaves at all — without it a clean candidate arm cannot be told apart
  from a run whose audits never touched a pointer.

The ES client is mocked; what is under test is the query shapes, the arithmetic
and the phrase discrimination, each of which fails silently rather than loudly
when it is wrong.
"""

from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest
from rich.console import Console

from saorsa_deploy.analysis import pointer_commitments as pc
from saorsa_deploy.analysis.es import EsRun
from saorsa_deploy.log_patterns import load_pattern_set

# The complete `AuditFailureReason` variant set at ant-node v0.21.0
# (`src/replication/types.rs:356-367`), in declaration order.
AUDIT_FAILURE_REASONS = (
    "Timeout",
    "MalformedResponse",
    "DigestMismatch",
    "KeyAbsent",
    "Rejected",
)

#: `warn!("Audit: {challenged_peer} failed subtree audit ({reason:?})")`
#: — `src/replication/storage_commitment_audit.rs:1184`.
PEER = "12D3KooWH3uVF6wv47WnArKHk5p6cvgCJEb74UTmxztmQDc298t3"


def audit_warn_message(reason: str) -> str:
    return f"Audit: {PEER} failed subtree audit ({reason})"


def standard_tokens(text: str) -> list[str]:
    """Approximate Elasticsearch's standard analyzer: lower-case word tokens.

    `tail.message` is analysed, so `match_phrase` compares TOKEN SEQUENCES, not
    substrings — which is exactly why the parenthesised reason still
    discriminates even though the punctuation around it is discarded. Every
    token that matters here is a run of letters or digits, so splitting on
    non-alphanumerics is faithful for these strings.
    """
    return re.findall(r"[0-9a-z]+", text.lower())


def phrase_matches(phrase: str, message: str) -> bool:
    """True when `match_phrase(phrase)` would match `message`."""
    needle, haystack = standard_tokens(phrase), standard_tokens(message)
    if not needle:
        return False
    return any(
        haystack[i : i + len(needle)] == needle for i in range(len(haystack) - len(needle) + 1)
    )


def _response(count: int, hosts: int = 0, top_hosts: tuple = ()) -> dict:
    return {
        "hits": {"total": {"value": count, "relation": "eq"}},
        "aggregations": {
            "hosts": {"value": hosts},
            "top_hosts": {"buckets": [{"key": h, "doc_count": c} for h, c in top_hosts]},
        },
    }


@pytest.fixture()
def run():
    r = EsRun("DEV-06")
    r._elastic = {
        "elasticsearch_url": "https://es.example:9200/",
        "elasticsearch_api_key": "k",
        "elasticsearch_index_pattern": "telegraf-ant-nodes-dev-06-%Y.%m.%d",
    }
    return r


# ---------------------------------------------------------------------------
# The three queries
# ---------------------------------------------------------------------------


def test_the_three_queries_carry_the_exact_phrase_and_the_two_wildcards(run):
    """The phrase goes on the ANALYSED field and the two discriminators on the
    keyword subfield: `match_phrase` cannot express a wildcard, which is the
    whole reason this command exists rather than another catalogue pattern."""
    bodies = []

    def fake_search(body, index=None):
        bodies.append(body)
        return _response(0)

    with patch.object(run, "search", side_effect=fake_search):
        pc.collect(run, "2026-10-03T00:00:00Z", "2026-10-03T04:00:00Z")

    assert len(bodies) == 4
    phrase_clause = {"match_phrase": {"tail.message": "Storage commitment rotated"}}
    for body in bodies:
        assert phrase_clause in body["query"]["bool"]["filter"]
        assert body["track_total_hits"] is True
        assert body["size"] == 0

    pointers = {"wildcard": {"tail.message.keyword": "*pointers=*"}}
    zero = {"wildcard": {"tail.message.keyword": "*pointers=0"}}
    assert pointers not in bodies[0]["query"]["bool"]["filter"]
    assert pointers in bodies[1]["query"]["bool"]["filter"]
    assert zero in bodies[2]["query"]["bool"]["filter"]
    # The fourth query is the non-zero HOST SPREAD, which no subtraction of two
    # cardinalities can give: a host appears in both.
    assert pointers in bodies[3]["query"]["bool"]["filter"]
    assert bodies[3]["query"]["bool"]["must_not"] == [zero]


def test_the_zero_wildcard_is_anchored_at_the_end():
    """`*pointers=0*` would also match any count whose first digit is a zero."""
    assert pc.ZERO_POINTERS_WILDCARD == "*pointers=0"
    assert not pc.ZERO_POINTERS_WILDCARD.endswith("0*")


def test_aggregations_use_the_keyword_host_subfield(run):
    """`tag.host` is analysed: a cardinality on the bare field errors outright."""
    body = pc.build_commitment_query("S", "E")
    assert body["aggs"]["hosts"] == {"cardinality": {"field": "tag.host.keyword"}}
    assert body["aggs"]["top_hosts"]["terms"]["field"] == "tag.host.keyword"


def test_a_host_filter_matches_both_spellings(run):
    """ES upper-cases `tag.host` while the inventory holds it lower-cased."""
    body = pc.build_commitment_query("S", "E", hosts=["dev-06-ant-node-de1-1"])
    terms = next(
        f["terms"]["tag.host.keyword"] for f in body["query"]["bool"]["filter"] if "terms" in f
    )
    assert "dev-06-ant-node-de1-1" in terms
    assert "DEV-06-ANT-NODE-DE1-1" in terms


def test_the_query_is_run_against_the_deployments_own_node_index(run):
    """The difference from the prod rollout-watch check is the index and the
    absence of a version arm — a testnet run is single-version."""
    with patch.object(run, "search", return_value=_response(0)) as search:
        pc.collect(run, "S", "E")
    # `index=None` means EsRun.search uses the deployment's node index.
    assert all(call.args[1:] == () for call in search.call_args_list)
    assert run.node_index() == "telegraf-ant-nodes-dev-06-*"


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def test_non_zero_is_with_pointers_minus_zero_pointers(run):
    responses = [
        _response(1200, hosts=20),
        _response(1200, hosts=20),
        _response(300, hosts=20),
        _response(900, hosts=18, top_hosts=(("DEV-06-ANT-NODE-DE1-1", 60),)),
    ]
    with patch.object(run, "search", side_effect=responses):
        result = pc.collect(run, "S", "E")
    assert result["total"] == 1200
    assert result["with_pointers"] == 1200
    assert result["zero_pointers"] == 300
    assert result["non_zero"] == 900
    assert result["hosts"] == 20
    assert result["hosts_with_non_zero"] == 18
    assert result["top_hosts_non_zero"] == {"DEV-06-ANT-NODE-DE1-1": 60}
    assert result["status"] == pc.STATUS_PASS
    assert result["pointer_coverage"] == 1.0
    assert result["non_zero_share"] == pytest.approx(0.75)


def test_no_rotations_is_not_applicable_rather_than_a_divide(run):
    """A window with no rotation says nothing about pointer coverage — and the
    coverage ratio has no denominator, so it must not be computed at all."""
    with patch.object(run, "search", return_value=_response(0)):
        result = pc.collect(run, "S", "E")
    assert result["total"] == 0
    assert result["status"] == pc.STATUS_NOT_APPLICABLE
    assert result["pointer_coverage"] is None
    assert result["non_zero_share"] is None
    assert "not applicable" in result["detail"]
    assert "widen the window" in result["detail"]


def test_a_pre_pointer_service_on_the_fleet_fails(run):
    """Some service is emitting the line without `pointers=`, so the fleet is
    not uniformly pointer-capable and the audit counts describe two builds."""
    responses = [_response(1000, 20), _response(940, 19), _response(40, 12), _response(900, 18)]
    with patch.object(run, "search", side_effect=responses):
        result = pc.collect(run, "S", "E")
    assert result["status"] == pc.STATUS_FAIL
    assert "pre-pointer string" in result["detail"]
    assert result["pointer_coverage"] == pytest.approx(0.94)


def test_every_rotation_reporting_zero_pointers_fails(run):
    """The unfalsifiable case the command exists for: audits ran, commitments
    rotated, and not one of them ever covered a pointer leaf."""
    responses = [_response(800, 20), _response(800, 20), _response(800, 20), _response(0, 0)]
    with patch.object(run, "search", side_effect=responses):
        result = pc.collect(run, "S", "E")
    assert result["non_zero"] == 0
    assert result["status"] == pc.STATUS_FAIL
    assert "pointers=0" in result["detail"]


def test_json_out_holds_the_same_shape_as_the_table(run, tmp_path):
    out = tmp_path / "commitments.json"
    args = type(
        "Args",
        (),
        {
            "name": "DEV-06",
            "start": "2026-10-03T00:00:00Z",
            "end": "2026-10-03T04:00:00Z",
            "hosts": None,
            "json_out": str(out),
            "min_rotations": None,
            "min_hosts": None,
        },
    )()
    responses = [_response(10, 4), _response(10, 4), _response(2, 3), _response(8, 4)]
    with (
        patch.object(EsRun, "search", side_effect=responses),
        patch.object(EsRun, "elastic", return_value=dict(run.elastic())),
    ):
        assert pc.cmd_pointer_commitments(args) == 0
    payload = json.loads(out.read_text())
    assert payload["total"] == 10
    assert payload["non_zero"] == 8
    assert payload["name"] == "DEV-06"
    assert payload["index"] == "telegraf-ant-nodes-dev-06-*"
    assert payload["start"] == "2026-10-03T00:00:00Z"
    assert payload["status"] == pc.STATUS_PASS


def test_the_rendered_table_shows_n_a_rather_than_a_percentage_of_nothing():
    result = pc.summarise(0, 0, 0, 0, 0, {})
    console = Console(record=True, width=120)
    console.print(pc.render({**result, "name": "DEV-06", "start": "S", "end": "E"}))
    text = console.export_text()
    assert "n/a" in text
    assert pc.STATUS_NOT_APPLICABLE in text


# ---------------------------------------------------------------------------
# Sufficiency: enough rotations to conclude anything (V2-1450)
# ---------------------------------------------------------------------------


def test_the_run_647_shape_is_insufficient_not_pass(run):
    """One rotation, one host, non-zero, against the run issue's own criterion
    of >=20 rotations on >=5 hosts. The compositional verdict is clean and the
    sensitivity criterion was missed by more than an order of magnitude, which
    is exactly the reading `PASS` must not be given to."""
    responses = [
        _response(1, 1),
        _response(1, 1),
        _response(0, 0),
        _response(1, 1, (("DEV-06-ANT-NODE-DE1-1", 1),)),
    ]
    with patch.object(run, "search", side_effect=responses):
        result = pc.collect(run, "S", "E", min_rotations=20, min_hosts=5)
    assert result["status"] == pc.STATUS_INSUFFICIENT
    assert result["non_zero"] == 1
    assert result["pointer_coverage"] == 1.0


def test_the_insufficient_detail_names_both_observed_values_and_both_floors():
    result = pc.summarise(3, 3, 0, 2, 2, {}, min_rotations=20, min_hosts=5)
    assert result["status"] == pc.STATUS_INSUFFICIENT
    for fragment in ("3 rotation(s)", "floor of 20", "2 rotating host(s)", "floor of 5"):
        assert fragment in result["detail"]


def test_either_floor_alone_is_enough_to_be_short():
    """Volume and spread are independent: 40 rotations all on one host does not
    demonstrate spread, and 4 rotations across 9 hosts does not demonstrate
    volume."""
    spread = pc.summarise(40, 40, 10, 1, 1, {}, min_hosts=5)
    assert spread["status"] == pc.STATUS_INSUFFICIENT
    assert "floor of 5" in spread["detail"]
    assert "floor of 20" not in spread["detail"]

    volume = pc.summarise(4, 4, 1, 9, 9, {}, min_rotations=20)
    assert volume["status"] == pc.STATUS_INSUFFICIENT
    assert "floor of 20" in volume["detail"]


def test_floors_that_are_met_pass_with_no_caveat():
    result = pc.summarise(40, 40, 10, 8, 7, {}, min_rotations=20, min_hosts=5)
    assert result["status"] == pc.STATUS_PASS
    assert "NOT assessed" not in result["detail"]
    assert result["min_rotations"] == 20 and result["min_hosts"] == 5


def test_no_floors_keeps_the_compositional_verdict_but_says_it_is_not_one():
    """Acceptance 4: with nothing to measure volume against, `PASS` may not read
    as a sensitivity verdict."""
    result = pc.summarise(1, 1, 0, 1, 1, {})
    assert result["status"] == pc.STATUS_PASS
    assert "volume and host spread NOT assessed" in result["detail"]
    assert result["min_rotations"] is None and result["min_hosts"] is None


def test_the_all_zero_fail_also_carries_the_caveat_when_unassessed():
    """`every rotation reports pointers=0` is a claim about ABSENCE, so it rests
    on volume the same way `PASS` does."""
    result = pc.summarise(800, 800, 800, 20, 0, {})
    assert result["status"] == pc.STATUS_FAIL
    assert "volume and host spread NOT assessed" in result["detail"]


def test_a_shortfall_outranks_the_all_zero_fail(run):
    """One rotation reporting `pointers=0` is not evidence that no commitment
    ever covered a pointer leaf -- it is evidence of one rotation. The
    well-formedness FAIL below is the opposite case and still wins."""
    result = pc.summarise(1, 1, 1, 1, 0, {}, min_rotations=20, min_hosts=5)
    assert result["status"] == pc.STATUS_INSUFFICIENT


def test_a_rotation_missing_pointers_fails_at_any_volume():
    """A statement about the LINES, not about how many of them there were: one
    mis-shaped rotation proves a non-pointer-capable service is on the fleet."""
    result = pc.summarise(1, 0, 0, 1, 0, {}, min_rotations=20, min_hosts=5)
    assert result["status"] == pc.STATUS_FAIL
    assert "pre-pointer string" in result["detail"]


def test_no_rotations_stays_not_applicable_under_a_floor():
    """`total == 0` is not a shortfall to widen past -- it is the pre-existing
    NOT APPLICABLE case, whose own advice is already to widen the window."""
    result = pc.summarise(0, 0, 0, 0, 0, {}, min_rotations=20, min_hosts=5)
    assert result["status"] == pc.STATUS_NOT_APPLICABLE
    assert "widen the window" in result["detail"]


def test_an_unknown_host_cardinality_cannot_demonstrate_spread():
    """`hosts` is None when the aggregation is missing; a supplied floor is
    unmet rather than quietly satisfied."""
    result = pc.summarise(40, 40, 10, None, None, {}, min_hosts=5)
    assert result["status"] == pc.STATUS_INSUFFICIENT
    assert "unknown rotating host(s)" in result["detail"]


def test_the_floors_are_rendered_only_when_supplied():
    """A `-` row on every other run would imply the tool had an opinion about
    volume, which without a floor it does not."""
    base = {"name": "DEV-06", "start": "S", "end": "E"}
    gated = Console(record=True, width=120)
    gated.print(pc.render({**pc.summarise(1, 1, 0, 1, 1, {}, 20, 5), **base}))
    text = gated.export_text()
    assert "min rotations" in text and "min hosts" in text
    assert pc.STATUS_INSUFFICIENT in text

    plain = Console(record=True, width=120)
    plain.print(pc.render({**pc.summarise(1, 1, 0, 1, 1, {}), **base}))
    assert "min rotations" not in plain.export_text()


def test_the_floors_travel_into_the_json(run, tmp_path):
    """The JSON a run issue archives has to say what the verdict was measured
    against, or the status cannot be re-read later."""
    out = tmp_path / "commitments.json"
    args = type(
        "Args",
        (),
        {
            "name": "DEV-06",
            "start": "S",
            "end": "E",
            "hosts": None,
            "json_out": str(out),
            "min_rotations": 20,
            "min_hosts": 5,
        },
    )()
    responses = [_response(1, 1), _response(1, 1), _response(0, 0), _response(1, 1)]
    with (
        patch.object(EsRun, "search", side_effect=responses),
        patch.object(EsRun, "elastic", return_value=dict(run.elastic())),
    ):
        assert pc.cmd_pointer_commitments(args) == 0
    payload = json.loads(out.read_text())
    assert payload["status"] == pc.STATUS_INSUFFICIENT
    assert payload["min_rotations"] == 20 and payload["min_hosts"] == 5


def test_json_out_to_stdout_suppresses_the_table_and_the_detail(run, capsys):
    """Shares the `--json-out -` contract with every other analysis command."""
    args = type(
        "Args",
        (),
        {
            "name": "DEV-06",
            "start": "S",
            "end": "E",
            "hosts": None,
            "json_out": "-",
            "min_rotations": None,
            "min_hosts": None,
        },
    )()
    responses = [
        _response(10, 4),
        _response(10, 4),
        _response(2, 3),
        _response(8, 4, (("DEV-06-ANT-NODE-DE1-1", 5),)),
    ]
    with (
        patch.object(EsRun, "search", side_effect=responses),
        patch.object(EsRun, "elastic", return_value=dict(run.elastic())),
    ):
        assert pc.cmd_pointer_commitments(args) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["non_zero"] == 8
    assert "commitment rotations" not in out
    assert "top hosts by non-zero rotation" not in out


# ---------------------------------------------------------------------------
# The five audit phrases
# ---------------------------------------------------------------------------


def test_the_catalogue_covers_every_audit_failure_reason():
    """All five variants get a pattern, so the group sums to the TRUE total and
    no failure is silently uncounted."""
    patterns = load_pattern_set()
    keys = {p.key for p in patterns.patterns_in_group("subtree_audit_failure")}
    assert keys == {
        "subtree_audit_fail_digest_mismatch",
        "subtree_audit_fail_key_absent",
        "subtree_audit_fail_timeout",
        "subtree_audit_fail_rejected",
        "subtree_audit_fail_malformed",
    }
    group = patterns.group("subtree_audit_failure")
    assert group.aggregate == "sum"
    assert len(AUDIT_FAILURE_REASONS) == len(keys)


def test_the_catalogue_carries_the_commitment_group_since_version_four():
    patterns = load_pattern_set()
    # v4 added the commitment group; later versions (v5 added the advisory
    # payment-RPC group) keep it, so this is a floor, not an equality.
    assert patterns.version >= 4
    groups = {g.key for g in patterns.groups}
    assert {"subtree_audit_failure", "storage_commitment_rotated"} <= groups
    commitment = patterns.patterns_in_group("storage_commitment_rotated")
    assert [p.phrase for p in commitment] == [pc.COMMITMENT_PHRASE]
    assert patterns.group("storage_commitment_rotated").aggregate == "sum"


@pytest.mark.parametrize("reason", AUDIT_FAILURE_REASONS)
def test_each_audit_phrase_matches_its_own_message(reason):
    phrases = {p.phrase for p in load_pattern_set().patterns_in_group("subtree_audit_failure")}
    mine = f"failed subtree audit ({reason})"
    assert mine in phrases
    assert phrase_matches(mine, audit_warn_message(reason))


@pytest.mark.parametrize("reason", AUDIT_FAILURE_REASONS)
def test_no_audit_phrase_matches_another_reasons_message(reason):
    """`tail.message` is analysed, so the parentheses are discarded — the reason
    TOKEN is what discriminates, and it is distinct per variant."""
    message = audit_warn_message(reason)
    for other in AUDIT_FAILURE_REASONS:
        if other == reason:
            continue
        assert not phrase_matches(f"failed subtree audit ({other})", message)


def test_the_group_total_is_the_sum_of_the_five_reasons():
    """One message matches exactly one pattern, so summing the group cannot
    double-count a single audit failure."""
    patterns = load_pattern_set().patterns_in_group("subtree_audit_failure")
    for reason in AUDIT_FAILURE_REASONS:
        message = audit_warn_message(reason)
        hits = [p.key for p in patterns if phrase_matches(p.phrase, message)]
        assert len(hits) == 1, (reason, hits)


def test_every_audit_pattern_names_the_auditor_module():
    """A wrong module sends the next person verifying a zero into the wrong
    crate, which is how V2-1091's investigation lost time."""
    for p in load_pattern_set().patterns_in_group("subtree_audit_failure"):
        assert p.module == "ant_node::replication::storage_commitment_audit"


def test_the_pre_existing_pointer_and_connection_keys_are_untouched():
    """v3 and v4 counts of every pre-existing key must stay comparable: only the
    NEW keys have no counts on runs 612/614/615/627."""
    patterns = {p.key: p for p in load_pattern_set().patterns}
    assert patterns["connection_attempt_failed"].phrase == "Connection attempt failed"
    assert patterns["dial_failed_all_candidates"].phrase == "dial failed for all"
    assert patterns["pointer_audit_not_held"].phrase == "committed pointer"
    assert patterns["pointer_commit_failed"].phrase == "Pointer commit failed"


def test_the_audit_phrases_do_not_collide_with_the_existing_audit_pattern():
    """`pointer_audit_not_held` counts a DIFFERENT finding — a committed pointer
    the audit could not read back — so neither must match the other's message."""
    not_held = "Subtree audit: committed pointer 0a1b2c is not held"
    for reason in AUDIT_FAILURE_REASONS:
        assert not phrase_matches(f"failed subtree audit ({reason})", not_held)
    assert not phrase_matches("committed pointer", audit_warn_message("DigestMismatch"))
