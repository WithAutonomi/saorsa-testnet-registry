"""Watch the subtree-audit protocol id move to v2 (ant-node #231, V2-1277).

ADR-0016 commits a node's responsible pointers as leaves of the same signed
storage commitment as its chunks, and round 2 proves a pointer by its whole
signed record. That is a new slice item, so the subtree audit's protocol id
moves from `autonomi.ant.replication.subtree-audit.v1` to `...v2`. A v1 auditor
would reject a pointer leaf in round 1 as a content-address mismatch and
penalise an honest holder, so the two ids are deliberately incompatible and
**nodes on different ids do not audit each other across the upgrade**.

The receiving side of a mismatch logs nothing at any level -- `match_replication
_protocol` fails to match, `replication_payload_from_event` returns `None`, and
the handler simply continues. So the pause is only visible from the *auditor*
side, and the clean signal is the INFO cumulative summary every 300s:

    target  ant_node::replication::traffic
    group   4
    message "audit outcome summary (cumulative)"

During the mixed window `subtree_audit_pass` flatlines and
`subtree_audit_fail_timeout` climbs, **on both arms**, because each cohort is
failing to audit the other. That is expected, carries no trust penalty
(`handle_subtree_failed_audit` returns early for `Timeout`) and is not a
failure here.

What is *not* expected, at any point, is `subtree_audit_fail_digest_mismatch`
or `subtree_audit_fail_key_absent` on the new arm. Those mean a node failed an
audit on a committed pointer it genuinely holds -- mis-priced, mis-judged
storage that compounds silently. They are the reason this script exists.

The fields are **cumulative per service**, so every figure here is a windowed
delta (last minus first), and a service that restarted mid-window has its
counter reset to zero -- when the last value is below the first, the last value
*is* the delta. Without that, every upgrade restart would read as a huge
negative or a spurious spike.

A uniform 0.20.0 fleet already runs a non-zero timeout count (about 96 timeouts
against 601 passes per service), so "zero timeouts" was never the pass rule and
is not used as one.

Usage:
    uv run scripts/rollout-watch/audit_protocol_v2.py [--window 6h]
        [--to-version 0.21.0] [--json-out out.json]

Example:
    uv run scripts/rollout-watch/audit_protocol_v2.py --window 6h --to-version 0.21.0
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    FAIL,
    MESSAGE_FIELD,
    NOT_APPLICABLE,
    PASS,
    SUSPEND_BELOW_SHARE,
    TARGET_FIELD,
    TIMESTAMP_FIELD,
    Arms,
    Check,
    Control,
    EsClient,
    RolloutCheckError,
    add_common_args,
    apply_control,
    build_composite,
    emit,
    iso,
    load_arms,
    markdown_table,
    parse_window,
    quiet_tls,
    render_arms,
    render_checks,
    service_key,
    window_label,
)

#: The version this script was written for. Always pass `--to-version`: the
#: default measures the wrong arm on any later rollout.
TO_VERSION = "0.21.0"

TRAFFIC_TARGET = "ant_node::replication::traffic"
#: `group` does NOT uniquely identify a summary. Group 4 carries BOTH
#: "audit outcome summary (cumulative)" and "replication responder admission
#: summary (cumulative)", emitted at the *same millisecond*, and only the former
#: has the `subtree_audit_*` fields. Filtering on the group alone lets a sort by
#: `@timestamp` land on the admission summary, where every audit field reads as
#: missing -- which a delta then treats as a counter reset from zero and reports
#: the service's entire lifetime total as one window's activity. Both filters are
#: required.
AUDIT_GROUP = 4
AUDIT_MESSAGE = "audit outcome summary"

#: Present on the audit summary and on nothing else in group 4, so it is the
#: marker that a document really is the one being read.
MARKER_FIELD = "subtree_audit_pass"

#: The subtree-audit outcome counters, in report order. `pass` first because it
#: is the one that must resume once the fleet is uniform.
SUBTREE_FIELDS = (
    "subtree_audit_pass",
    "subtree_audit_fail_timeout",
    "subtree_audit_fail_digest_mismatch",
    "subtree_audit_fail_key_absent",
    "subtree_audit_fail_malformed",
    "subtree_audit_fail_rejected",
)

#: A committed key the node cannot produce. Measured at zero on a uniform
#: 0.20.0 fleet over both 1h and 24h, and ADR-0016 calls it a confirmed failure,
#: so this one is zero-tolerance.
#:
#: Do NOT substitute the `KeyAbsent` log phrase for this counter: that phrase
#: occurs ~1,800 times a week on the *responsible* audit path, which rides the
#: unchanged protocol id, and conflating the two makes a quiet subtree path look
#: broken.
ABSENT_FIELD = "subtree_audit_fail_key_absent"

#: NOT zero-tolerance. A uniform 0.20.0 fleet -- which holds no pointers at all,
#: so every one of these is a *chunk* digest mismatch -- already runs ~56 of
#: these a day fleet-wide against ~135,000 passes, and the rate roughly tripled
#: between 2026-09-27 and 2026-09-30 (host breadth 1-6 -> 7-14). So the measure
#: is a rate *against the old arm in the same window*: both arms carry the same
#: chunk background, and only the new arm can add a pointer-caused mismatch.
MISMATCH_FIELD = "subtree_audit_fail_digest_mismatch"

#: How much above the old arm's per-service rate the new arm may run before the
#: pointer contribution is judged real rather than noise.
MISMATCH_RATIO_LIMIT = 1.5

BUG_FIELDS = (MISMATCH_FIELD, ABSENT_FIELD)

#: Reported beside the subtree counters for context: the responsible-key audit
#: runs on the unchanged protocol id, so it should NOT pause. If it pauses too,
#: the cause is not the protocol id change.
RESPONSIBLE_FIELDS = (
    "responsible_audit_pass",
    "responsible_audit_fail_timeout",
)

ALL_FIELDS = SUBTREE_FIELDS + RESPONSIBLE_FIELDS


@dataclass
class ArmTotals:
    """Windowed deltas for one arm of the rollout."""

    services: int = 0
    #: Services that emitted at least two summaries, so a delta could be taken.
    measured: int = 0
    resets: int = 0
    totals: dict[str, int] = field(default_factory=lambda: dict.fromkeys(ALL_FIELDS, 0))
    #: service_key -> {field: delta}, only for services with a non-zero bug field.
    offenders: dict[str, dict[str, int]] = field(default_factory=dict)

    def bug_total(self) -> int:
        return sum(self.totals[f] for f in BUG_FIELDS)

    def as_dict(self) -> dict:
        return {
            "services": self.services,
            "measured": self.measured,
            "counter_resets": self.resets,
            "totals": dict(self.totals),
            "offenders": self.offenders,
        }


def build_summary_query(
    start: datetime, end: datetime, after: dict | None = None
) -> dict:
    """Per (host, service), the first and last audit outcome summary in the window.

    The counters are cumulative since service start, so a delta needs the two
    ends of the window rather than a sum or a max.
    """
    source = [f"tail.{name}" for name in ALL_FIELDS]
    return {
        "size": 0,
        "track_total_hits": False,
        "query": {
            "bool": {
                "filter": [
                    {"range": {TIMESTAMP_FIELD: {"gte": iso(start), "lte": iso(end)}}},
                    {"term": {TARGET_FIELD: TRAFFIC_TARGET}},
                    {"term": {"tail.group": AUDIT_GROUP}},
                    {"match_phrase": {MESSAGE_FIELD: AUDIT_MESSAGE}},
                ]
            }
        },
        "aggs": build_composite(
            after,
            {
                "first": {
                    "top_hits": {
                        "size": 1,
                        "sort": [{TIMESTAMP_FIELD: "asc"}],
                        "_source": source,
                    }
                },
                "last": {
                    "top_hits": {
                        "size": 1,
                        "sort": [{TIMESTAMP_FIELD: "desc"}],
                        "_source": source,
                    }
                },
            },
        ),
    }


def _tail_values(hit: dict) -> dict[str, int] | None:
    """The audit counters on one document, or None if it is not an audit summary.

    Returning None rather than a dict of zeros matters: a zero here is
    indistinguishable from a counter that really is at zero, and treating the
    wrong document as all-zeros is what made a delta report a service's whole
    lifetime as one window.
    """
    tail = (hit.get("_source") or {}).get("tail") or {}
    if MARKER_FIELD not in tail:
        return None
    out: dict[str, int] = {}
    for name in ALL_FIELDS:
        value = tail.get(name)
        out[name] = int(value) if isinstance(value, (int, float)) else 0
    return out


def summarise(pages: list[dict], arms: Arms) -> dict[str, ArmTotals]:
    """Windowed deltas per arm, handling a counter reset at an upgrade restart."""
    summaries = {"new": ArmTotals(), "old": ArmTotals()}
    for arm, totals in summaries.items():
        totals.services = len(arms.new if arm == "new" else arms.old)

    for page in pages:
        for bucket in page["aggregations"]["services"]["buckets"]:
            key = service_key(bucket["key"]["host"], bucket["key"]["service"])
            arm = arms.arms.get(key)
            if arm not in summaries:
                # A service that logged a summary but is not in the running set
                # (it went quiet before the adoption lookback) tells us nothing
                # about either arm.
                continue
            first_hits = bucket["first"]["hits"]["hits"]
            last_hits = bucket["last"]["hits"]["hits"]
            if not first_hits or not last_hits:
                continue
            first = _tail_values(first_hits[0])
            last = _tail_values(last_hits[0])
            if first is None or last is None:
                continue
            summary = summaries[arm]
            summary.measured += 1
            # A restart zeroes every counter, so the last value IS the delta.
            # Decided per field, never per service: one field going backwards --
            # a restart caught mid-field, out-of-order timestamps on a skewed
            # clock -- must not make the other seven fall back to their absolute
            # cumulative totals, which would turn a flat counter into a delta of
            # everything that service has ever counted.
            deltas: dict[str, int] = {}
            reset_fields = 0
            for name in ALL_FIELDS:
                if last[name] < first[name]:
                    reset_fields += 1
                    delta = last[name]
                else:
                    delta = last[name] - first[name]
                deltas[name] = max(0, delta)
                summary.totals[name] += deltas[name]
            if reset_fields:
                summary.resets += 1
            if any(deltas[name] for name in BUG_FIELDS):
                summary.offenders[key] = deltas
    return summaries


def _unmeasured(name: str, arms: Arms, new: ArmTotals) -> Check | None:
    """The shared "there is nothing to measure yet" guard."""
    if not arms.new:
        return Check(
            name=name,
            status=NOT_APPLICABLE,
            detail=f"not applicable: {len(arms.new)} new-version services",
        )
    if new.measured == 0:
        return Check(
            name=name,
            status=NOT_APPLICABLE,
            detail=(
                f"not applicable: none of {len(arms.new)} new-arm service(s) emitted "
                "an audit outcome summary in the window (the summary is every 300s, "
                "so widen --window)"
            ),
        )
    return None


def evaluate_key_absent(summaries: dict[str, ArmTotals], arms: Arms) -> Check:
    """The breach check: a node committed a pointer it cannot produce."""
    name = "no committed key absent on the new arm"
    new = summaries["new"]
    guard = _unmeasured(name, arms, new)
    if guard:
        return guard
    absent = new.totals[ABSENT_FIELD]
    if absent:
        offenders = {k: v for k, v in new.offenders.items() if v.get(ABSENT_FIELD)}
        return Check(
            name=name,
            status=FAIL,
            detail=(
                f"`{ABSENT_FIELD}` = {absent} on the new arm across "
                f"{len(offenders)} service(s) -- a node failed a subtree audit on a "
                "committed key it could not produce. This is NOT the version pause "
                "and NOT the chunk-mismatch background."
            ),
            data={"offenders": offenders},
        )
    return Check(
        name=name,
        status=PASS,
        detail=(
            f"`{ABSENT_FIELD}` = 0 over {new.measured} measured new-arm service(s)"
        ),
        passed_on_zero=True,
    )


def _per_service_hour(total: int, measured: int, hours: float) -> float:
    if measured <= 0 or hours <= 0:
        return 0.0
    return total / measured / hours


def evaluate_digest_mismatch(
    summaries: dict[str, ArmTotals],
    arms: Arms,
    hours: float,
    baseline_rate: float | None,
) -> Check:
    """Rate comparison, not zero-tolerance -- the chunk background is non-zero.

    Prefers the old arm in the same window, because that cancels the chunk
    mismatch background exactly. Falls back to an operator-supplied baseline
    (what this script reported before T0) once the old arm has drained.
    """
    name = "digest mismatches not elevated on the new arm"
    new, old = summaries["new"], summaries["old"]
    guard = _unmeasured(name, arms, new)
    if guard:
        return guard

    new_rate = _per_service_hour(new.totals[MISMATCH_FIELD], new.measured, hours)
    old_share = arms.share("old")
    shown = f"new arm {new_rate:.4f}/service-hour ({new.totals[MISMATCH_FIELD]} over {new.measured} service(s), {hours:.2f}h)"

    if old_share >= SUSPEND_BELOW_SHARE and old.measured:
        old_rate = _per_service_hour(old.totals[MISMATCH_FIELD], old.measured, hours)
        if old_rate == 0:
            # Nothing to divide by. A non-zero new arm against a silent old arm
            # is still worth reporting, but the ratio would be meaningless.
            status = FAIL if new_rate > 0 else PASS
            return Check(
                name=name,
                status=status,
                detail=(f"{shown}; old arm 0/service-hour, so no ratio is defined"),
                passed_on_zero=(status == PASS),
                data={"new_rate": new_rate, "old_rate": old_rate},
            )
        ratio = new_rate / old_rate
        status = PASS if ratio <= MISMATCH_RATIO_LIMIT else FAIL
        return Check(
            name=name,
            status=status,
            detail=(
                f"{shown}; old arm {old_rate:.4f}/service-hour; "
                f"ratio {ratio:.2f}x against a {MISMATCH_RATIO_LIMIT}x limit "
                f"(old arm {old_share:.2f}% of running)"
            ),
            data={"new_rate": new_rate, "old_rate": old_rate, "ratio": ratio},
        )

    if baseline_rate is not None:
        if baseline_rate <= 0:
            status = FAIL if new_rate > 0 else PASS
            return Check(
                name=name,
                status=status,
                detail=f"{shown}; --digest-baseline is {baseline_rate}, so no ratio is defined",
                passed_on_zero=(status == PASS),
                data={"new_rate": new_rate, "baseline_rate": baseline_rate},
            )
        ratio = new_rate / baseline_rate
        status = PASS if ratio <= MISMATCH_RATIO_LIMIT else FAIL
        return Check(
            name=name,
            status=status,
            detail=(
                f"{shown}; baseline {baseline_rate:.4f}/service-hour; "
                f"ratio {ratio:.2f}x against a {MISMATCH_RATIO_LIMIT}x limit "
                f"(old arm {old_share:.2f}% of running, so the baseline is used)"
            ),
            data={"new_rate": new_rate, "baseline_rate": baseline_rate, "ratio": ratio},
        )

    return Check(
        name=name,
        status=NOT_APPLICABLE,
        detail=(
            f"{shown}; old arm is {old_share:.2f}% of running "
            f"(< {SUSPEND_BELOW_SHARE}%) and no --digest-baseline was given, so "
            "there is nothing to compare against. Record the rate above and pass "
            "it as --digest-baseline on later runs."
        ),
        data={"new_rate": new_rate},
    )


def evaluate_recovery(summaries: dict[str, ArmTotals], arms: Arms) -> Check:
    """Once the fleet is uniform, subtree audits must be flowing again.

    While both arms are live the pause is expected, so this claims nothing. It
    only bites after the old arm has drained, which is where a protocol id that
    nobody can audit would otherwise sit unnoticed.
    """
    new = summaries["new"]
    old_share = arms.share("old")
    if not arms.new or new.measured == 0:
        return Check(
            name="subtree audits flowing on the new arm",
            status=NOT_APPLICABLE,
            detail=f"not applicable: {new.measured} measured new-arm service(s)",
        )
    if old_share >= SUSPEND_BELOW_SHARE:
        return Check(
            name="subtree audits flowing on the new arm",
            status=NOT_APPLICABLE,
            detail=(
                f"not applicable while the fleet is mixed: old arm is "
                f"{old_share:.2f}% of running services (>= {SUSPEND_BELOW_SHARE}%). "
                "Cross-version audits pause by design; re-run once the old arm has "
                "drained."
            ),
        )
    passes = new.totals["subtree_audit_pass"]
    if passes == 0:
        return Check(
            name="subtree audits flowing on the new arm",
            status=FAIL,
            detail=(
                f"0 subtree_audit_pass on the new arm over {new.measured} service(s) "
                f"with the old arm at {old_share:.2f}% -- the fleet is uniform, so "
                "audits should have resumed."
            ),
        )
    return Check(
        name="subtree audits flowing on the new arm",
        status=PASS,
        detail=(
            f"{passes:,} subtree_audit_pass on the new arm "
            f"(old arm {old_share:.2f}% of running)"
        ),
    )


def render_totals(summaries: dict[str, ArmTotals]) -> str:
    rows = []
    for name in SUBTREE_FIELDS + RESPONSIBLE_FIELDS:
        new = summaries["new"].totals[name]
        old = summaries["old"].totals[name]
        rows.append([f"`{name}`", f"{new:,}", f"{old:,}"])
    return markdown_table(
        ["counter (delta over window)", "new arm", "old arm"], rows, align="lrr"
    )


def build_report(es: EsClient, args: argparse.Namespace, now: datetime) -> dict:
    window = parse_window(args.window)
    start, end = now - window, now
    arms = load_arms(es, args.to_version, end)

    control = Control(
        name=f"`{TRAFFIC_TARGET}` group {AUDIT_GROUP} documents",
        count=es.count(
            [
                {"term": {TARGET_FIELD: TRAFFIC_TARGET}},
                {"term": {"tail.group": AUDIT_GROUP}},
                {"match_phrase": {MESSAGE_FIELD: AUDIT_MESSAGE}},
            ],
            start,
            end,
        ),
    )

    pages = list(
        es.composite_pages(lambda after: build_summary_query(start, end, after))
    )
    summaries = summarise(pages, arms)
    hours = window.total_seconds() / 3600.0
    checks = apply_control(
        [
            evaluate_key_absent(summaries, arms),
            evaluate_digest_mismatch(summaries, arms, hours, args.digest_baseline),
            evaluate_recovery(summaries, arms),
        ],
        control,
    )
    return {
        "window": window_label(window),
        "window_hours": round(hours, 4),
        "digest_baseline": args.digest_baseline,
        "window_start": iso(start),
        "window_end": iso(end),
        "to_version": args.to_version,
        "running_services": arms.running,
        "new_arm": len(arms.new),
        "old_arm": len(arms.old),
        "old_arm_share": round(arms.share("old"), 2),
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
    summaries: dict[str, ArmTotals] = result["_summaries"]
    new = summaries["new"]
    lines = [
        "### Subtree-audit protocol v2 (ant-node #231) — ant-prod-01, to "
        f"{result['to_version']}",
        "",
        f"Window {result['window']} ({result['window_start']} → {result['window_end']}). "
        f"{result['running_services']} running service(s): {result['new_arm']} new arm, "
        f"{result['old_arm']} old arm ({result['old_arm_share']:.2f}%).",
        "",
        render_arms(arms),
        "",
        render_checks(result["_checks"], result["_control"]),
        "",
        "Cumulative counters, as a delta over the window "
        f"({new.measured} new-arm and {summaries['old'].measured} old-arm service(s) "
        f"measured; {new.resets} new-arm counter reset(s) treated as a restart):",
        "",
        render_totals(summaries),
        "",
    ]
    if new.offenders:
        lines += ["Services with a real pointer-audit failure:", ""]
        for key, deltas in sorted(new.offenders.items())[:20]:
            detail = ", ".join(f"{n}={deltas[n]}" for n in BUG_FIELDS if deltas[n])
            lines.append(f"- `{key}`: {detail}")
        lines.append("")
    lines.append(
        "`subtree_audit_fail_timeout` climbing on BOTH arms is the expected "
        "v1/v2 pause: the two ids cannot audit each other, the timeout is fully "
        "graced and no trust penalty is applied. It is reported, not gated. "
        f"`{ABSENT_FIELD}` is zero-tolerance and a breach -- it means a node "
        "committed a key it could not produce. "
        f"`{MISMATCH_FIELD}` is NOT zero-tolerance: a pointer-free 0.20.0 fleet "
        "already runs a non-zero chunk-mismatch background, rising over the week "
        "to 2026-09-30, so it is judged as a rate against the old arm in the same "
        "window (which cancels that background) and is filed, not breached. "
        "`responsible_audit_*` rides the unchanged protocol id and should not "
        "pause -- if it does, the cause is not this release's id change."
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(parser, to_version=TO_VERSION)
    parser.add_argument(
        "--digest-baseline",
        type=float,
        default=None,
        help=(
            f"Fleet-wide `{MISMATCH_FIELD}` rate per service-hour, measured before "
            "the rollout. Used only once the old arm has drained below "
            f"{SUSPEND_BELOW_SHARE}% and an arm comparison is no longer possible."
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
