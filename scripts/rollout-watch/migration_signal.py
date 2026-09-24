"""Verify saorsa-core #163 (V2-1260) on ant-prod-01's migration signal.

Before #163, a peer that disconnected part-way through a tally was recorded with
`agent = none` -- the observer had no agent string for it, so a live peer that
merely went away mid-count was filed as if nothing was known about it. The fix
does not remove the value: a peer genuinely seen once with no agent still
produces one such document. What it removes is the *persistence*, because the
mid-tally case recurs every report interval for as long as the peer keeps
reconnecting.

So the measure is not "are there any `agent = none` documents" -- that would
fail on a single honest observation -- but "does the same (observer, peer) pair
keep coming back with `agent = none` across two consecutive 15-minute report
intervals". On the fixed arm that count must be 0.

A zero on its own proves nothing, so the check also wants the *old* arm's count
to be non-zero: that is the bug reproducing on the unfixed half of the fleet in
the same window, which is what makes the new arm's zero mean something. Once the
old arm is under 5% of running services the comparison is suspended (a handful
of services produce figures dominated by whichever host happens to be sick) and
the new arm's 0 stands alone.

The observer is identified by its service (`host/service_number`), not by
`tail.observer`: the arm split is per service, and one service is one observing
node, so the two are interchangeable -- but only the service key can be resolved
to a version.

Usage:
    uv run scripts/rollout-watch/migration_signal.py [--window 1h]
        [--to-version 0.20.0] [--json-out out.json]

Example:
    uv run scripts/rollout-watch/migration_signal.py --window 6h
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    FAIL,
    HOST_FIELD,
    NOT_APPLICABLE,
    PASS,
    SERVICE_FIELD,
    SUSPEND_BELOW_SHARE,
    TIMESTAMP_FIELD,
    UNTRUSTED,
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
    range_filter,
    render_arms,
    render_checks,
    service_key,
    window_label,
)

TO_VERSION = "0.20.0"

MIGRATION_EVENT_FIELD = "tail.migration_event.keyword"
AGENT_FIELD = "tail.agent.keyword"
PEER_FIELD = "tail.peer.keyword"
OBSERVER_FIELD = "tail.observer.keyword"

PEER_STATE_EVENT = "peer_state"
#: The value #163 stops a mid-tally disconnect from producing.
MISSING_AGENT = "none"

#: The node reports peer state once per interval; two adjacent intervals is the
#: shortest span that distinguishes a recurrence from a single observation.
REPORT_INTERVAL = timedelta(minutes=15)


@dataclass
class ArmSummary:
    """What one arm of the fleet reported with `agent = none`."""

    arm: str
    documents: int = 0
    observers: set[str] = field(default_factory=set)
    peers: set[str] = field(default_factory=set)
    persistent: list[tuple[str, str, int]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "arm": self.arm,
            "documents": self.documents,
            "observers": len(self.observers),
            "peers": len(self.peers),
            "persistent_pairs": len(self.persistent),
            "persistent": [
                {"observer": o, "peer": p, "consecutive_intervals": n}
                for o, p, n in self.persistent
            ],
        }


def build_pairs_query(
    start: datetime, end: datetime, after: dict | None = None
) -> dict:
    """Every (host, service, peer) that reported `agent = none`, per interval.

    The date histogram is what turns a count into a persistence measure: a pair
    present in two adjacent buckets came back, a pair in one bucket did not.
    """
    composite: dict = {
        "sources": [
            {"host": {"terms": {"field": HOST_FIELD}}},
            {"service": {"terms": {"field": SERVICE_FIELD}}},
            {"peer": {"terms": {"field": PEER_FIELD}}},
        ],
        "size": 1000,
    }
    if after:
        composite["after"] = after
    interval = f"{int(REPORT_INTERVAL.total_seconds() // 60)}m"
    return {
        "size": 0,
        "track_total_hits": True,
        "query": {
            "bool": {
                "filter": [
                    range_filter(start, end),
                    {"term": {MIGRATION_EVENT_FIELD: PEER_STATE_EVENT}},
                    {"term": {AGENT_FIELD: MISSING_AGENT}},
                ]
            }
        },
        "aggs": {
            "services": {
                "composite": composite,
                "aggs": {
                    "intervals": {
                        "date_histogram": {
                            "field": TIMESTAMP_FIELD,
                            "fixed_interval": interval,
                            "min_doc_count": 1,
                        }
                    },
                    "observers": {"terms": {"field": OBSERVER_FIELD, "size": 5}},
                },
            }
        },
    }


def consecutive_runs(buckets: list[int], interval: timedelta = REPORT_INTERVAL) -> int:
    """The longest run of adjacent report intervals in a bucket-start series.

    Buckets come back as epoch milliseconds, already ordered, and empty ones are
    omitted -- so adjacency has to be tested on the timestamps, not on position.
    """
    if not buckets:
        return 0
    step = int(interval.total_seconds() * 1000)
    longest = current = 1
    for previous, nxt in zip(buckets, buckets[1:]):
        current = current + 1 if nxt - previous == step else 1
        longest = max(longest, current)
    return longest


def summarise_pairs(pages: list[dict], arms: Arms) -> dict[str, ArmSummary]:
    """Split the `agent = none` pairs by the observing service's arm."""
    summaries = {arm: ArmSummary(arm=arm) for arm in ("new", "old", "unknown")}
    for page in pages:
        for bucket in page["aggregations"]["services"]["buckets"]:
            key = bucket["key"]
            observer = service_key(key["host"], key["service"])
            peer = key["peer"]
            arm = arms.arms.get(observer, "unknown")
            summary = summaries[arm]
            summary.documents += bucket["doc_count"]
            summary.observers.add(observer)
            summary.peers.add(peer)
            starts = [int(b["key"]) for b in bucket["intervals"]["buckets"]]
            run = consecutive_runs(starts)
            if run >= 2:
                summary.persistent.append((observer, peer, run))
    for summary in summaries.values():
        summary.persistent.sort(key=lambda item: (-item[2], item[0], item[1]))
    return summaries


def evaluate_persistence(summaries: dict[str, ArmSummary], arms: Arms) -> Check:
    """The fixed arm must show no recurring `agent = none` pair."""
    new, old = summaries["new"], summaries["old"]
    if not arms.new:
        return Check(
            name="persistent agent=none pairs",
            status=NOT_APPLICABLE,
            detail=(
                f"not applicable: 0 new-version services "
                f"(old arm: {len(old.persistent)} persistent pair(s))"
            ),
            data=old.as_dict(),
        )
    if new.persistent:
        named = ", ".join(f"{o}→{p[:12]} x{n}" for o, p, n in new.persistent[:10])
        return Check(
            name="persistent agent=none pairs",
            status=FAIL,
            detail=f"{len(new.persistent)} pair(s) on the new arm: {named}",
            data=new.as_dict(),
        )
    old_share = arms.share("old")
    if old_share < SUSPEND_BELOW_SHARE:
        return Check(
            name="persistent agent=none pairs",
            status=PASS,
            detail=(
                f"0 pair(s) on the new arm; old-arm comparison suspended "
                f"(old arm is {len(arms.old)} service(s), < {SUSPEND_BELOW_SHARE}% of running)"
            ),
            passed_on_zero=True,
            data=new.as_dict(),
        )
    if not old.persistent:
        return Check(
            name="persistent agent=none pairs",
            status=UNTRUSTED,
            detail=(
                f"0 pair(s) on the new arm, but also 0 on the old arm "
                f"({len(arms.old)} service(s), {old_share:.2f}% of running) — the bug did not "
                "reproduce on the unfixed half in this window, so the new arm's zero shows "
                "nothing. Widen --window or re-run while the fleet is still mixed."
            ),
            data={"new": new.as_dict(), "old": old.as_dict()},
        )
    return Check(
        name="persistent agent=none pairs",
        status=PASS,
        detail=(
            f"0 pair(s) on the new arm against {len(old.persistent)} on the old arm "
            f"({old_share:.2f}% of running)"
        ),
        data={"new": new.as_dict(), "old": old.as_dict()},
    )


def render_arm_table(summaries: dict[str, ArmSummary]) -> str:
    rows = [
        [
            arm,
            f"{s.documents:,}",
            f"{len(s.observers)}",
            f"{len(s.peers)}",
            f"{len(s.persistent)}",
        ]
        for arm, s in summaries.items()
        if s.documents or arm in ("new", "old")
    ]
    return markdown_table(
        ["arm", "documents", "observers", "distinct peers", "persistent pairs"],
        rows,
        align="lrrrr",
    )


# -- report ----------------------------------------------------------------
def build_report(es: EsClient, args: argparse.Namespace, now: datetime) -> dict:
    window = parse_window(args.window)
    start, end = now - window, now
    arms = load_arms(es, args.to_version, end)

    control = Control(
        name=f"`{PEER_STATE_EVENT}` documents",
        count=es.count(
            [{"term": {MIGRATION_EVENT_FIELD: PEER_STATE_EVENT}}], start, end
        ),
    )

    pages = list(es.composite_pages(lambda after: build_pairs_query(start, end, after)))
    summaries = summarise_pairs(pages, arms)
    checks = apply_control([evaluate_persistence(summaries, arms)], control)
    return {
        "window": window_label(window),
        "window_start": iso(start),
        "window_end": iso(end),
        "to_version": args.to_version,
        "running_services": arms.running,
        "new_arm": len(arms.new),
        "old_arm": len(arms.old),
        "version_counts": arms.version_counts(),
        "control": {"name": control.name, "count": control.count, "ok": control.ok},
        "arms": {arm: s.as_dict() for arm, s in summaries.items()},
        "checks": [c.__dict__ for c in checks],
        "passed": not any(c.failed for c in checks),
        "_arms": arms,
        "_checks": checks,
        "_control": control,
        "_summaries": summaries,
    }


def render_report(result: dict) -> str:
    arms: Arms = result["_arms"]
    summaries: dict[str, ArmSummary] = result["_summaries"]
    interval_minutes = int(REPORT_INTERVAL.total_seconds() // 60)
    lines = [
        f"### Migration signal (saorsa-core #163) — ant-prod-01, to {result['to_version']}",
        "",
        f"Window {result['window']} ({result['window_start']} → {result['window_end']}). "
        f"{result['running_services']} running service(s): {result['new_arm']} new arm, "
        f"{result['old_arm']} old arm.",
        "",
        render_arms(arms),
        "",
        render_checks(result["_checks"], result["_control"]),
        "",
        f"`migration_event = peer_state` with `agent = none`, by observer arm "
        f"(persistence measured over {interval_minutes}-minute report intervals):",
        "",
        render_arm_table(summaries),
        "",
    ]
    persistent = summaries["new"].persistent + summaries["old"].persistent
    if persistent:
        lines += ["Recurring pairs:", ""]
        for observer, peer, runs in persistent[:20]:
            arm = arms.arms.get(observer, "unknown")
            lines.append(
                f"- [{arm}] `{observer}` → peer `{peer[:16]}…` in {runs} adjacent intervals"
            )
        lines.append("")
    lines.append(
        "A single `agent = none` observation is expected and is not a failure: #163 removes "
        "the recurrence, not the value. The new arm's zero is only meaningful while the old "
        "arm reproduces the recurrence in the same window, which is why a zero on both arms "
        "is reported as untrusted rather than as a pass."
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(parser, to_version=TO_VERSION)
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
