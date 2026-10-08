"""Watch the SCTP association error rate across the rollout (V2-1364 Phase B).

`webrtc_sctp::association::association_internal` logs ERROR only, with just two
distinct messages -- `[] stream 0 not found)` and `[] stream 2 not found)` -- so
it counts exactly. It was **already climbing before this train**, measured on
2026-09-30:

    2026-09-25  1,035 errors / 38 hosts
    2026-09-26  1,804 / 42
    2026-09-27  1,831 / 45
    2026-09-28  1,303 / 44
    2026-09-29  2,934 / 60
    2026-09-30  2,856 / 51   (partial day)

Nothing on this train fixes it, so this script is not asking whether it is
fixed. It asks whether the new release makes it *worse*, which matters because
pointers newly cross the WebRTC-direct boundary -- a pointer GET on the RPC lane
and a paid pointer PUT on the data lane -- so this release adds traffic to
exactly the SCTP path that is erroring.

Why this is not read off `prod rollout-watch` §4: that report's per-target rows
cover only the top `TOP_TARGETS = 15` targets, and this target sits right at the
edge of that cut (15th by volume in a 24h sample), so it can silently drop out
of any given run. §4 is also suspended whenever either arm is under 5% of
running services, which is the state at A4.

There is no pre-0.20.0 baseline to be had: the production index retains about
seven days, so every document in reach is already 0.20.0. That is convenient
rather than limiting -- before-and-after and old-arm-versus-new-arm are the same
comparison here -- but it does mean the arm comparison must be taken while the
fleet is still mixed, at A3.

Usage:
    uv run scripts/rollout-watch/sctp_association_errors.py [--window 6h]
        [--to-version 0.21.0] [--baseline-rate N] [--json-out out.json]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    FAIL,
    HOST_FIELD,
    LEVEL_FIELD,
    MESSAGE_KEYWORD_FIELD,
    NOT_APPLICABLE,
    PASS,
    SUSPEND_BELOW_SHARE,
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

SCTP_TARGET = "webrtc_sctp::association::association_internal"
#: A chunk rpc traffic summary is emitted by every service every 300s, so it is
#: non-zero whenever the fleet is alive -- which is what a control has to be.
CONTROL_TARGET = "ant_node::storage::traffic"
CONTROL_MESSAGE = "chunk rpc traffic summary"

#: How far above the old arm the new arm may run before the increase is judged
#: real. Matches the threshold agreed for this train's Phase B.
RATIO_LIMIT = 1.5


@dataclass
class ArmRate:
    arm: str
    services: int
    errors: int
    hosts: int
    hours: float

    @property
    def per_1k_service_hours(self) -> float:
        if self.services <= 0 or self.hours <= 0:
            return 0.0
        return self.errors / (self.services * self.hours) * 1000.0

    def as_dict(self) -> dict:
        return {
            "arm": self.arm,
            "services": self.services,
            "errors": self.errors,
            "hosts": self.hosts,
            "hours": round(self.hours, 4),
            "per_1k_service_hours": round(self.per_1k_service_hours, 4),
        }


def _arm_filter(to_version: str, arm: str) -> dict:
    """Documents written by one arm, by the version tag on the document itself."""
    term = {"term": {VERSION_FIELD: to_version}}
    return term if arm == "new" else {"bool": {"must_not": [term]}}


def measure_arm(
    es: EsClient,
    start: datetime,
    end: datetime,
    to_version: str,
    arm: str,
    services: int,
) -> ArmRate:
    body = {
        "size": 0,
        "track_total_hits": True,
        "query": {
            "bool": {
                "filter": [
                    {"range": {"@timestamp": {"gte": iso(start), "lte": iso(end)}}},
                    {"term": {TARGET_FIELD: SCTP_TARGET}},
                    {"term": {LEVEL_FIELD: "ERROR"}},
                    _arm_filter(to_version, arm),
                ]
            }
        },
        "aggs": {"hosts": {"cardinality": {"field": HOST_FIELD}}},
    }
    response = es.search(body)
    return ArmRate(
        arm=arm,
        services=services,
        errors=response["hits"]["total"]["value"],
        hosts=int(response["aggregations"]["hosts"]["value"]),
        hours=(end - start).total_seconds() / 3600.0,
    )


def collect_messages(es: EsClient, start: datetime, end: datetime) -> dict[str, int]:
    body = {
        "size": 0,
        "track_total_hits": False,
        "query": {
            "bool": {
                "filter": [
                    {"range": {"@timestamp": {"gte": iso(start), "lte": iso(end)}}},
                    {"term": {TARGET_FIELD: SCTP_TARGET}},
                    {"term": {LEVEL_FIELD: "ERROR"}},
                ]
            }
        },
        "aggs": {"msg": {"terms": {"field": MESSAGE_KEYWORD_FIELD, "size": 20}}},
    }
    response = es.search(body)
    return {
        b["key"]: b["doc_count"] for b in response["aggregations"]["msg"]["buckets"]
    }


def evaluate(
    rates: dict[str, ArmRate], arms: Arms, baseline_rate: float | None
) -> Check:
    name = "SCTP association errors not elevated on the new arm"
    new, old = rates["new"], rates["old"]
    if not arms.new:
        return Check(
            name=name,
            status=NOT_APPLICABLE,
            detail=f"not applicable: {len(arms.new)} new-version services",
        )
    shown = (
        f"new arm {new.per_1k_service_hours:.2f}/1k service-hours "
        f"({new.errors:,} error(s), {new.hosts} host(s), {new.services} service(s))"
    )
    new_share, old_share = arms.share("new"), arms.share("old")

    if min(new_share, old_share) >= SUSPEND_BELOW_SHARE:
        if old.per_1k_service_hours == 0:
            status = FAIL if new.errors else PASS
            return Check(
                name=name,
                status=status,
                detail=f"{shown}; old arm logged none, so no ratio is defined",
                passed_on_zero=(status == PASS),
                data={"new": new.as_dict(), "old": old.as_dict()},
            )
        ratio = new.per_1k_service_hours / old.per_1k_service_hours
        status = PASS if ratio <= RATIO_LIMIT else FAIL
        return Check(
            name=name,
            status=status,
            detail=(
                f"{shown}; old arm {old.per_1k_service_hours:.2f}/1k service-hours "
                f"({old.errors:,} error(s), {old.hosts} host(s)); "
                f"ratio {ratio:.2f}x against a {RATIO_LIMIT}x limit"
            ),
            data={"new": new.as_dict(), "old": old.as_dict(), "ratio": ratio},
        )

    if baseline_rate is not None and baseline_rate > 0:
        ratio = new.per_1k_service_hours / baseline_rate
        status = PASS if ratio <= RATIO_LIMIT else FAIL
        return Check(
            name=name,
            status=status,
            detail=(
                f"{shown}; baseline {baseline_rate:.2f}/1k service-hours; "
                f"ratio {ratio:.2f}x against a {RATIO_LIMIT}x limit "
                f"(old arm {old_share:.2f}% of running, so the baseline is used)"
            ),
            data={"new": new.as_dict(), "baseline_rate": baseline_rate, "ratio": ratio},
        )

    return Check(
        name=name,
        status=NOT_APPLICABLE,
        detail=(
            f"{shown}; arms are {new_share:.2f}% new / {old_share:.2f}% old, so one "
            f"is under {SUSPEND_BELOW_SHARE}% and no --baseline-rate was given. "
            "Record the rate above and pass it as --baseline-rate on later runs."
        ),
        data={"new": new.as_dict(), "old": old.as_dict()},
    )


def render_rates(rates: dict[str, ArmRate]) -> str:
    rows = [
        [
            arm,
            f"{r.services:,}",
            f"{r.errors:,}",
            f"{r.hosts}",
            f"{r.per_1k_service_hours:.2f}",
        ]
        for arm, r in rates.items()
    ]
    return markdown_table(
        ["arm", "services", "ERROR docs", "hosts", "per 1k service-hours"],
        rows,
        align="lrrrr",
    )


def render_messages(messages: dict[str, int]) -> str:
    if not messages:
        return "_No SCTP association errors in this window._"
    rows = [[f"`{k}`", f"{v:,}"] for k, v in messages.items()]
    return markdown_table(["message (both arms)", "count"], rows, align="lr")


def build_report(es: EsClient, args: argparse.Namespace, now: datetime) -> dict:
    window = parse_window(args.window)
    start, end = now - window, now
    arms = load_arms(es, args.to_version, end)

    control = Control(
        name=f"`{CONTROL_MESSAGE}` documents (both arms)",
        count=es.count(
            [
                {"term": {TARGET_FIELD: CONTROL_TARGET}},
                {"match_phrase": {"tail.message": CONTROL_MESSAGE}},
            ],
            start,
            end,
        ),
    )

    rates = {
        "new": measure_arm(es, start, end, args.to_version, "new", len(arms.new)),
        "old": measure_arm(es, start, end, args.to_version, "old", len(arms.old)),
    }
    messages = collect_messages(es, start, end)
    checks = apply_control([evaluate(rates, arms, args.baseline_rate)], control)
    return {
        "window": window_label(window),
        "window_start": iso(start),
        "window_end": iso(end),
        "to_version": args.to_version,
        "baseline_rate": args.baseline_rate,
        "running_services": arms.running,
        "new_arm": len(arms.new),
        "old_arm": len(arms.old),
        "version_counts": arms.version_counts(),
        "control": {"name": control.name, "count": control.count, "ok": control.ok},
        "rates": {arm: r.as_dict() for arm, r in rates.items()},
        "messages": messages,
        "checks": [c.__dict__ for c in checks],
        "passed": not any(c.failed for c in checks),
        "_arms": arms,
        "_checks": checks,
        "_control": control,
        "_rates": rates,
        "_messages": messages,
    }


def render_report(result: dict) -> str:
    arms: Arms = result["_arms"]
    lines = [
        f"### SCTP association errors — ant-prod-01, to {result['to_version']}",
        "",
        f"Window {result['window']} ({result['window_start']} → {result['window_end']}). "
        f"{result['running_services']} running service(s): {result['new_arm']} new arm, "
        f"{result['old_arm']} old arm.",
        "",
        render_arms(arms),
        "",
        render_checks(result["_checks"], result["_control"]),
        "",
        f"`{SCTP_TARGET}` ERROR rate by arm:",
        "",
        render_rates(result["_rates"]),
        "",
        render_messages(result["_messages"]),
        "",
        "This signal predates the train and nothing on the train fixes it, so an "
        "elevated rate is filed as its own investigation rather than treated as a "
        "release regression. It is measured because pointers newly cross the "
        "WebRTC-direct boundary, adding traffic to the erroring path. Service-hours "
        "are the arm's running-service count times the window, which is the same "
        "simplification on both arms and so does not bias the ratio.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(parser, to_version=TO_VERSION)
    parser.add_argument(
        "--baseline-rate",
        type=float,
        default=None,
        help=(
            "ERROR rate per 1k service-hours measured before the rollout. Used only "
            f"once one arm is under {SUSPEND_BELOW_SHARE}% and no arm comparison is "
            "possible."
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
