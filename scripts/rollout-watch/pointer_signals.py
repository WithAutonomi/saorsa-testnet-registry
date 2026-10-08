"""Verify the pointer feature is live and clean on ant-prod-01 (ant-node #231).

ADR-0016's pointers ship with very little that production can see. Production
nodes run at `info` and two independent filters drop DEBUG and TRACE (telegraf's
`tagdrop`, and an Elasticsearch `final_pipeline` that `?pipeline=_none` cannot
bypass), and **the entire success path of pointer replication is `debug!`** --
"Fresh pointer {} offered to {} peers" and "Stored fresh pointer {} from
{source}" both. The per-variant replication byte counters were never wired into
`log_traffic_summary()` either: the enum went from 17 to 23 variants but only
0-16 are emitted, so pointer replication traffic is counted in memory and never
logged.

So this script does not try to prove replication. It proves the three things
that *are* visible at `info`:

1. **The new instrumentation is live.** The chunk rpc traffic summary gains
   `pointer_get_rx_count` / `pointer_put_rx_count` fields on 0.21.0 and has them
   on no earlier build, so their presence on a service is proof that service is
   running the new code -- independent of whether any pointer exists.
2. **Pointer replication is wired.** `"Replication engine started with N
   background tasks"` rises by exactly 2 (the verification loop and the fresh
   drainer). A 0.20.0 fleet reports 16; this release must report 18.
3. **The commitment carries pointer leaves.** `"Storage commitment rotated"`
   gains a `pointers=` field. Note the whole line is one unparsed
   `tail.message` string -- `hash=` and `key_count=` are not fields either -- so
   this is matched on the message, not on a field.

Plus the set of warnings that must never appear at all. Pointer and chunk
addresses derive from different BLAKE3 modes, so a cross-kind collision takes a
cross-mode preimage break; any occurrence is a genuine alarm, not noise.

Replication is instead proved out of band, by a client read: a read returns a
state only when two peers name it, so a successful `ant pointer get` from a
clean client is itself evidence that more than one node holds the record.

Usage:
    uv run scripts/rollout-watch/pointer_signals.py [--window 6h]
        [--to-version 0.21.0] [--expected-tasks 18] [--json-out out.json]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    FAIL,
    LEVEL_FIELD,
    MESSAGE_FIELD,
    MESSAGE_KEYWORD_FIELD,
    NOT_APPLICABLE,
    PASS,
    TARGET_FIELD,
    VERSION_FIELD,
    Arms,
    Check,
    Control,
    EsClient,
    RolloutCheckError,
    add_common_args,
    apply_control,
    emit,
    iso,
    load_arms,
    markdown_table,
    parse_window,
    quiet_tls,
    render_arms,
    render_checks,
    window_label,
)

TO_VERSION = "0.21.0"

STORAGE_TRAFFIC_TARGET = "ant_node::storage::traffic"
#: As with the audit summary, the group number alone does not identify a
#: summary on these targets -- always pin the message too.
CHUNK_TRAFFIC_MESSAGE = "chunk rpc traffic summary"
#: Present on 0.21.0's group 1 line and on no earlier build.
POINTER_RX_FIELD = "tail.pointer_get_rx_count"

RPC_LATENCY_TARGET = "ant_node::storage::rpc_latency"
POINTER_RPC_EVENTS = ("pointer_put_rpc", "pointer_get_rpc")

BACKGROUND_TASKS_PHRASE = "background tasks"
#: 16 on 0.20.0; pointers add the verification loop and the fresh drainer.
EXPECTED_BACKGROUND_TASKS = 18

COMMITMENT_PHRASE = "Storage commitment rotated"
POINTER_STORE_TARGET = "ant_node::pointer::store"

#: Every one of these means something that cannot happen has happened.
#: `ant_node::pointer::store` has no info-level line at all, so any WARN or
#: ERROR on that target belongs here too and is counted separately below.
FORBIDDEN_PHRASES = (
    "a pointer already occupies that address",
    "a chunk already occupies that address",
    "Subtree audit: committed pointer",
    "Commitment retention: corrupt pointer leaves",
)


def _new_arm(to_version: str) -> dict:
    """Documents from the new arm, by their own version tag.

    Cheaper and exactly equivalent to filtering on 774 (host, service) pairs:
    every document carries the version of the node that wrote it.
    """
    return {"term": {VERSION_FIELD: to_version}}


def count_new(es: EsClient, filters: list[dict], start, end, to_version: str) -> int:
    return es.count([_new_arm(to_version), *filters], start, end)


def evaluate_instrumentation(
    es: EsClient, start, end, to_version: str, arms: Arms
) -> Check:
    """The pointer traffic fields exist on the new arm's summary line."""
    name = "pointer traffic fields present on the new arm"
    base = [
        {"term": {TARGET_FIELD: STORAGE_TRAFFIC_TARGET}},
        {"match_phrase": {MESSAGE_FIELD: CHUNK_TRAFFIC_MESSAGE}},
    ]
    summaries = count_new(es, base, start, end, to_version)
    if not arms.new or summaries == 0:
        return Check(
            name=name,
            status=NOT_APPLICABLE,
            detail=(
                f"not applicable: {len(arms.new)} new-version service(s), "
                f"{summaries} chunk rpc traffic summary document(s) on the new arm"
            ),
        )
    with_fields = count_new(
        es, [*base, {"exists": {"field": POINTER_RX_FIELD}}], start, end, to_version
    )
    if with_fields == summaries:
        return Check(
            name=name,
            status=PASS,
            detail=(
                f"all {summaries:,} new-arm summary document(s) carry "
                f"`{POINTER_RX_FIELD}`"
            ),
        )
    return Check(
        name=name,
        status=FAIL,
        detail=(
            f"{with_fields:,} of {summaries:,} new-arm summary document(s) carry "
            f"`{POINTER_RX_FIELD}` -- a service tagged {to_version} is emitting the "
            "pre-pointer summary line"
        ),
        data={"with_fields": with_fields, "summaries": summaries},
    )


def evaluate_background_tasks(
    es: EsClient, start, end, to_version: str, arms: Arms, expected: int
) -> Check:
    """Pointer replication adds exactly two background tasks.

    The line is emitted once at start-up, so the window has to span each
    service's upgrade restart; a window that does not is NOT APPLICABLE rather
    than a failure.
    """
    name = f"replication engine reports {expected} background tasks"
    base = [{"match_phrase": {MESSAGE_FIELD: BACKGROUND_TASKS_PHRASE}}]
    total = count_new(es, base, start, end, to_version)
    if not arms.new or total == 0:
        return Check(
            name=name,
            status=NOT_APPLICABLE,
            detail=(
                f"not applicable: {total} start-up line(s) on the new arm in this "
                "window. The line is logged once at start-up, so widen --window to "
                "span the upgrade restarts."
            ),
        )
    matching = count_new(
        es,
        [
            *base,
            {
                "wildcard": {
                    MESSAGE_KEYWORD_FIELD: f"*with {expected} background tasks*"
                }
            },
        ],
        start,
        end,
        to_version,
    )
    if matching == total:
        return Check(
            name=name,
            status=PASS,
            detail=f"all {total} new-arm start-up line(s) report {expected} tasks",
        )
    return Check(
        name=name,
        status=FAIL,
        detail=(
            f"{matching} of {total} new-arm start-up line(s) report {expected} "
            "tasks -- the other(s) report a different count, so pointer "
            "replication may not be wired on every service"
        ),
        data={"matching": matching, "total": total},
    )


def evaluate_commitment(es: EsClient, start, end, to_version: str, arms: Arms) -> Check:
    """Rotated commitments on the new arm carry the `pointers=` field."""
    name = "commitment rotation reports pointer leaves"
    base = [{"match_phrase": {MESSAGE_FIELD: COMMITMENT_PHRASE}}]
    total = count_new(es, base, start, end, to_version)
    if not arms.new or total == 0:
        return Check(
            name=name,
            status=NOT_APPLICABLE,
            detail=(
                f"not applicable: {total} commitment rotation(s) on the new arm in "
                "this window"
            ),
        )
    with_pointers = count_new(
        es,
        [*base, {"wildcard": {MESSAGE_KEYWORD_FIELD: "*pointers=*"}}],
        start,
        end,
        to_version,
    )
    zero_pointers = count_new(
        es,
        [*base, {"wildcard": {MESSAGE_KEYWORD_FIELD: "*pointers=0"}}],
        start,
        end,
        to_version,
    )
    non_zero = with_pointers - zero_pointers
    if with_pointers == total:
        return Check(
            name=name,
            status=PASS,
            detail=(
                f"all {total:,} new-arm rotation(s) carry `pointers=`; "
                f"{non_zero:,} report a non-zero count"
            ),
            data={"total": total, "non_zero": non_zero},
        )
    return Check(
        name=name,
        status=FAIL,
        detail=(
            f"{with_pointers:,} of {total:,} new-arm rotation(s) carry `pointers=` "
            "-- the rest are emitting the pre-pointer string"
        ),
        data={"with_pointers": with_pointers, "total": total, "non_zero": non_zero},
    )


def evaluate_forbidden(
    es: EsClient, start, end, to_version: str, arms: Arms
) -> tuple[Check, dict[str, int]]:
    """Warnings that must never appear, on either arm."""
    name = "no impossible-state warnings"
    counts: dict[str, int] = {}
    for phrase in FORBIDDEN_PHRASES:
        counts[phrase] = es.count(
            [{"match_phrase": {MESSAGE_FIELD: phrase}}], start, end
        )
    counts[f"{POINTER_STORE_TARGET} WARN/ERROR"] = es.count(
        [
            {"term": {TARGET_FIELD: POINTER_STORE_TARGET}},
            {"terms": {LEVEL_FIELD: ["WARN", "ERROR"]}},
        ],
        start,
        end,
    )
    total = sum(counts.values())
    if total == 0:
        return (
            Check(
                name=name,
                status=PASS,
                detail=f"all {len(counts)} forbidden signal(s) at zero",
                passed_on_zero=True,
            ),
            counts,
        )
    offending = ", ".join(f"`{k}`={v}" for k, v in counts.items() if v)
    return (
        Check(
            name=name,
            status=FAIL,
            detail=f"{total} occurrence(s): {offending}",
            data=counts,
        ),
        counts,
    )


def collect_rpc_outcomes(es: EsClient, start, end, to_version: str) -> dict:
    """Pointer RPC counts by event and outcome, from the new arm."""
    body = {
        "size": 0,
        "track_total_hits": True,
        "query": {
            "bool": {
                "filter": [
                    {"range": {"@timestamp": {"gte": iso(start), "lte": iso(end)}}},
                    _new_arm(to_version),
                    {"term": {TARGET_FIELD: RPC_LATENCY_TARGET}},
                    {"terms": {MESSAGE_KEYWORD_FIELD: list(POINTER_RPC_EVENTS)}},
                ]
            }
        },
        "aggs": {
            "event": {
                "terms": {"field": MESSAGE_KEYWORD_FIELD, "size": 10},
                "aggs": {
                    "outcome": {"terms": {"field": "tail.outcome.keyword", "size": 20}}
                },
            }
        },
    }
    response = es.search(body)
    out: dict[str, dict[str, int]] = {}
    for bucket in response["aggregations"]["event"]["buckets"]:
        out[bucket["key"]] = {
            o["key"]: o["doc_count"] for o in bucket["outcome"]["buckets"]
        }
    return {"total": response["hits"]["total"]["value"], "events": out}


def evaluate_rpc_errors(rpc: dict) -> Check:
    name = "no pointer RPC errors on the new arm"
    if rpc["total"] == 0:
        return Check(
            name=name,
            status=NOT_APPLICABLE,
            detail=(
                "not applicable: no pointer RPC on the new arm in this window. "
                "Production has no pointer client, so this stays empty until a "
                "pointer is written by hand."
            ),
        )
    errors = sum(outcomes.get("error", 0) for outcomes in rpc["events"].values())
    if errors:
        return Check(
            name=name,
            status=FAIL,
            detail=f"{errors} `outcome=error` of {rpc['total']} pointer RPC",
            data=rpc,
        )
    return Check(
        name=name,
        status=PASS,
        detail=f"0 `outcome=error` of {rpc['total']} pointer RPC",
    )


def render_rpc(rpc: dict) -> str:
    if not rpc["events"]:
        return "_No pointer RPC on the new arm in this window._"
    rows = []
    for event in POINTER_RPC_EVENTS:
        outcomes = rpc["events"].get(event, {})
        for outcome, count in sorted(outcomes.items(), key=lambda kv: -kv[1]):
            rows.append([f"`{event}`", outcome, f"{count:,}"])
    return markdown_table(["event", "outcome", "count"], rows, align="llr")


def render_forbidden(counts: dict[str, int]) -> str:
    rows = [[f"`{k}`", f"{v:,}"] for k, v in counts.items()]
    return markdown_table(["signal (must be zero)", "count"], rows, align="lr")


def build_report(es: EsClient, args: argparse.Namespace, now: datetime) -> dict:
    window = parse_window(args.window)
    start, end = now - window, now
    arms = load_arms(es, args.to_version, end)

    control = Control(
        name=f"`{CHUNK_TRAFFIC_MESSAGE}` documents (both arms)",
        count=es.count(
            [
                {"term": {TARGET_FIELD: STORAGE_TRAFFIC_TARGET}},
                {"match_phrase": {MESSAGE_FIELD: CHUNK_TRAFFIC_MESSAGE}},
            ],
            start,
            end,
        ),
    )

    rpc = collect_rpc_outcomes(es, start, end, args.to_version)
    forbidden_check, forbidden_counts = evaluate_forbidden(
        es, start, end, args.to_version, arms
    )
    checks = apply_control(
        [
            evaluate_instrumentation(es, start, end, args.to_version, arms),
            evaluate_background_tasks(
                es, start, end, args.to_version, arms, args.expected_tasks
            ),
            evaluate_commitment(es, start, end, args.to_version, arms),
            forbidden_check,
            evaluate_rpc_errors(rpc),
        ],
        control,
    )
    return {
        "window": window_label(window),
        "window_start": iso(start),
        "window_end": iso(end),
        "to_version": args.to_version,
        "expected_tasks": args.expected_tasks,
        "running_services": arms.running,
        "new_arm": len(arms.new),
        "old_arm": len(arms.old),
        "version_counts": arms.version_counts(),
        "control": {"name": control.name, "count": control.count, "ok": control.ok},
        "pointer_rpc": rpc,
        "forbidden": forbidden_counts,
        "checks": [c.__dict__ for c in checks],
        "passed": not any(c.failed for c in checks),
        "_arms": arms,
        "_checks": checks,
        "_control": control,
        "_rpc": rpc,
        "_forbidden": forbidden_counts,
    }


def render_report(result: dict) -> str:
    arms: Arms = result["_arms"]
    lines = [
        f"### Pointer signals (ant-node #231) — ant-prod-01, to {result['to_version']}",
        "",
        f"Window {result['window']} ({result['window_start']} → {result['window_end']}). "
        f"{result['running_services']} running service(s): {result['new_arm']} new arm, "
        f"{result['old_arm']} old arm.",
        "",
        render_arms(arms),
        "",
        render_checks(result["_checks"], result["_control"]),
        "",
        "Pointer RPC on the new arm:",
        "",
        render_rpc(result["_rpc"]),
        "",
        render_forbidden(result["_forbidden"]),
        "",
        "Pointer **replication** is deliberately not measured here: its entire "
        "success path is `debug!`, which production's `info` level and two "
        "forwarder/ingest filters drop, and the per-variant traffic counters were "
        "never wired into the summary. Replication is proved instead by a client "
        "read, which returns a state only when two peers name it.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(parser, to_version=TO_VERSION)
    parser.add_argument(
        "--expected-tasks",
        type=int,
        default=EXPECTED_BACKGROUND_TASKS,
        help=(
            "Background tasks the replication engine should report "
            f"(default {EXPECTED_BACKGROUND_TASKS}; a 0.20.0 fleet reports 16)."
        ),
    )
    args = parser.parse_args(argv)

    quiet_tls()
    try:
        result = build_report(EsClient.from_env(), args, datetime.now(timezone.utc))
    except RolloutCheckError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    payload = {k: v for k, v in result.items() if not k.startswith("_")}
    emit(payload, render_report(result), args.json_out)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
