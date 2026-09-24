"""Verify V2-834's traffic-accounting instrumentation is present on ant-prod-01.

V2-834 lands across three crates -- ant-node#230 (chunk-rpc, upgrade and EVM-rpc
attribution), saorsa-core#165 (failed sends, rejected/dropped inbound frames)
and saorsa-transport#169 (socket-boundary ground truth) -- and this script asks
only whether the fleet is emitting all of it, in both stores. Whether the
numbers *reconcile* is a separate question, answered by
`scripts/v2_834_reconcile.py` against the same window.

Three things are checked:

* **Per-target coverage (ES).** Each of the three new ant-node traffic targets
  is emitted by at least 95% of the new-version services that have been up long
  enough to have emitted anything. The first summary lands five minutes after
  start, so a service younger than ten minutes is excluded rather than counted
  as missing -- otherwise a checkpoint taken just after a restart wave reads as
  a fleet-wide failure.
* **Field and message presence (ES).** The new cumulative fields and summary
  lines exist on the new arm, and -- just as important -- are emitted by *no*
  old-arm service. An old build that somehow produced them would mean the arm
  split is wrong, which would silently invalidate every other measure here.
* **InfluxDB arrival.** The three targets reach the `tail` measurement on as
  many hosts as run a new-version service. No Telegraf change is expected:
  V2-1275 rolled out a `*::traffic` glob tagpass precisely so a new target
  needs no config edit. A zero here while ES holds the lines therefore points at
  the forwarder, and the report says so.

Usage:
    uv run scripts/rollout-watch/traffic_targets.py [--window 1h]
        [--to-version 0.20.0] [--json-out out.json]

Example:
    uv run scripts/rollout-watch/traffic_targets.py --window 6h
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    FAIL,
    INFLUX_BUCKET,
    MESSAGE_FIELD,
    NOT_APPLICABLE,
    PASS,
    START_MESSAGE,
    TARGET_FIELD,
    Arms,
    Check,
    Control,
    EsClient,
    InfluxClient,
    RolloutCheckError,
    add_common_args,
    apply_control,
    build_composite,
    collect_latest,
    emit,
    iso,
    load_arms,
    markdown_table,
    not_applicable,
    parse_window,
    quiet_tls,
    range_filter,
    render_arms,
    render_checks,
    service_key,
    window_label,
)

TO_VERSION = "0.20.0"

#: The three traffic targets ant-node#230 adds.
NEW_TARGETS = (
    "ant_node::storage::traffic",
    "ant_node::upgrade::traffic",
    "ant_node::payment::traffic",
)

#: Present on every version, so a zero here means the query or the fleet is
#: broken rather than that the release is missing.
CONTROL_TARGET = "ant_node::replication::traffic"

#: Each new cumulative field with the summary line it lives on. Pinning the
#: field to its message keeps a same-named field on another line (the relay
#: server's per-session lines, V2-1202) out of the count.
NEW_FIELDS = (
    ("tail.sock_tx_bytes", "socket traffic summary (cumulative)"),
    ("tail.sock_rx_bytes", "socket traffic summary (cumulative)"),
    ("tail.wire_failed_tx_bytes", "wire traffic summary (cumulative)"),
    ("tail.wire_rejected_rx_bytes", "wire traffic summary (cumulative)"),
    ("tail.wire_dropped_rx_bytes", "wire traffic summary (cumulative)"),
)

#: The three ant-node summary lines #230 adds.
NEW_MESSAGES = (
    "chunk rpc traffic summary (cumulative)",
    "upgrade traffic summary (cumulative)",
    "evm rpc summary (cumulative)",
)

#: A service's first traffic summary lands five minutes after start; ten minutes
#: leaves room for one missed emission before it counts against coverage.
MIN_UPTIME = timedelta(minutes=10)
TARGET_COVERAGE_FLOOR = 95.0


@dataclass
class Signal:
    """One thing V2-834 should be emitting, and who emitted it."""

    name: str
    kind: str  # "target" | "field" | "message"
    emitters: frozenset[str]

    def in_arm(self, keys: set[str]) -> list[str]:
        return sorted(self.emitters & keys)


def build_emitters_query(
    filters: list[dict], start: datetime, end: datetime, after=None
) -> dict:
    """The (host, service) pairs matching `filters` in the window."""
    return {
        "size": 0,
        "track_total_hits": False,
        "query": {"bool": {"filter": [range_filter(start, end), *filters]}},
        "aggs": build_composite(after),
    }


def collect_emitters(
    es: EsClient, filters: list[dict], start: datetime, end: datetime
) -> frozenset[str]:
    keys: set[str] = set()
    pages = es.composite_pages(
        lambda after: build_emitters_query(filters, start, end, after)
    )
    for page in pages:
        for bucket in page["aggregations"]["services"]["buckets"]:
            keys.add(service_key(bucket["key"]["host"], bucket["key"]["service"]))
    return frozenset(keys)


def target_filters(target: str) -> list[dict]:
    return [{"term": {TARGET_FIELD: target}}]


def field_filters(field: str, message: str) -> list[dict]:
    return [{"exists": {"field": field}}, {"match_phrase": {MESSAGE_FIELD: message}}]


def message_filters(message: str) -> list[dict]:
    return [{"match_phrase": {MESSAGE_FIELD: message}}]


def services_up_long_enough(
    services: list[str],
    starts: dict[str, datetime],
    end: datetime,
    minimum: timedelta = MIN_UPTIME,
) -> list[str]:
    """The services old enough to have emitted a traffic summary.

    A service with no start in the window started before it, so it is up by
    definition; one that restarted inside the last `minimum` is excluded.
    """
    cutoff = end - minimum
    return sorted(k for k in services if starts.get(k) is None or starts[k] <= cutoff)


def evaluate_target_coverage(
    signals: list[Signal], eligible: list[str], arms: Arms
) -> Check:
    """Each new target is emitted by ≥95% of the eligible new-version services."""
    if not arms.new:
        return Check(
            name="target coverage", status=NOT_APPLICABLE, detail=not_applicable(arms)
        )
    if not eligible:
        return Check(
            name="target coverage",
            status=NOT_APPLICABLE,
            detail=(
                f"not applicable: all {len(arms.new)} new-version service(s) have been up "
                f"under {int(MIN_UPTIME.total_seconds() // 60)} minutes"
            ),
        )
    keys = set(eligible)
    shares = {s.name: len(s.in_arm(keys)) / len(keys) * 100.0 for s in signals}
    short = {
        name: share for name, share in shares.items() if share < TARGET_COVERAGE_FLOOR
    }
    detail = ", ".join(
        f"{name.split('::')[-2]} {share:.2f}%" for name, share in shares.items()
    )
    if short:
        return Check(
            name="target coverage",
            status=FAIL,
            detail=f"below {TARGET_COVERAGE_FLOOR}% of {len(keys)} eligible service(s): {detail}",
            data={"shares": shares},
        )
    return Check(
        name="target coverage",
        status=PASS,
        detail=f"{len(keys)} eligible service(s): {detail}",
        data={"shares": shares},
    )


def evaluate_presence(signals: list[Signal], arms: Arms) -> Check:
    """Every new field and summary line is emitted somewhere on the new arm."""
    if not arms.new:
        return Check(
            name="field/message presence",
            status=NOT_APPLICABLE,
            detail=not_applicable(arms),
        )
    keys = set(arms.new)
    missing = [s.name for s in signals if not s.in_arm(keys)]
    if missing:
        return Check(
            name="field/message presence",
            status=FAIL,
            detail=f"absent from every new-version service: {', '.join(missing)}",
            data={"missing": missing},
        )
    return Check(
        name="field/message presence",
        status=PASS,
        detail=f"all {len(signals)} field(s)/message(s) present on the new arm",
    )


def evaluate_old_arm_purity(signals: list[Signal], arms: Arms) -> Check:
    """No old-version service emits any of it.

    A pass here rests on a zero, so it is only trusted when the control is
    non-zero: if the queries were wrong, every one of them would read zero too.
    """
    keys = set(arms.old)
    offenders = {s.name: s.in_arm(keys) for s in signals if s.in_arm(keys)}
    if offenders:
        named = ", ".join(f"{name} ({len(svc)} svc)" for name, svc in offenders.items())
        return Check(
            name="old-arm purity",
            status=FAIL,
            detail=f"emitted by old-version services: {named}",
            data={"offenders": offenders},
        )
    return Check(
        name="old-arm purity",
        status=PASS,
        detail=f"none of {len(signals)} signal(s) emitted by the {len(keys)} old-arm service(s)",
        passed_on_zero=True,
    )


def build_influx_query(
    bucket: str, start: datetime, end: datetime, targets=NEW_TARGETS
) -> str:
    """Distinct hosts writing the new traffic targets into the `tail` measurement."""
    predicate = " or ".join(f'r.target == "{t}"' for t in targets)
    return f"""
from(bucket: "{bucket}")
  |> range(start: {iso(start)}, stop: {iso(end)})
  |> filter(fn: (r) => r._measurement == "tail")
  |> filter(fn: (r) => {predicate})
  |> keep(columns: ["host", "target"])
  |> group(columns: ["host", "target"])
  |> distinct(column: "host")
"""


def summarise_influx(rows: list[dict]) -> dict[str, set[str]]:
    """{target: {host, ...}} from the distinct-host rows."""
    out: dict[str, set[str]] = {}
    for row in rows:
        target, host = row.get("target"), row.get("_value") or row.get("host")
        if not target or not host:
            continue
        out.setdefault(target, set()).add(str(host).lower())
    return out


def evaluate_influx(
    per_target: dict[str, set[str]], arms: Arms, es_has_lines: bool
) -> Check:
    """Influx must see the targets on as many hosts as run a new-version service."""
    if not arms.new:
        return Check(
            name="influx arrival", status=NOT_APPLICABLE, detail=not_applicable(arms)
        )
    expected = {h.lower() for h in arms.hosts("new")}
    seen = set().union(*per_target.values()) if per_target else set()
    if not seen and es_has_lines:
        return Check(
            name="influx arrival",
            status=FAIL,
            detail=(
                f"0 of {len(expected)} host(s) in `{INFLUX_BUCKET}` while Elasticsearch holds "
                "the lines — the V2-1275 `*::traffic` tagpass should cover these targets "
                "without a Telegraf change, so look at the forwarder, not the release"
            ),
            data={"expected_hosts": sorted(expected)},
        )
    missing = sorted(expected - seen)
    if missing:
        return Check(
            name="influx arrival",
            status=FAIL,
            detail=f"{len(seen)}/{len(expected)} host(s); missing {', '.join(missing[:10])}",
            data={"missing_hosts": missing},
        )
    return Check(
        name="influx arrival",
        status=PASS,
        detail=f"{len(seen)}/{len(expected)} host(s) writing the new targets",
        passed_on_zero=not seen,
    )


# -- report ----------------------------------------------------------------
def build_report(
    es: EsClient, influx: InfluxClient, args: argparse.Namespace, now: datetime
) -> dict:
    window = parse_window(args.window)
    start, end = now - window, now
    arms = load_arms(es, args.to_version, end)

    control = Control(
        name=f"`{CONTROL_TARGET}` documents",
        count=es.count(target_filters(CONTROL_TARGET), start, end),
    )

    target_signals = [
        Signal(
            name=t,
            kind="target",
            emitters=collect_emitters(es, target_filters(t), start, end),
        )
        for t in NEW_TARGETS
    ]
    presence_signals = [
        Signal(
            name=field,
            kind="field",
            emitters=collect_emitters(es, field_filters(field, message), start, end),
        )
        for field, message in NEW_FIELDS
    ] + [
        Signal(
            name=message,
            kind="message",
            emitters=collect_emitters(es, message_filters(message), start, end),
        )
        for message in NEW_MESSAGES
    ]

    starts = collect_latest(es, START_MESSAGE, start, end)
    eligible = services_up_long_enough(arms.new, starts, end)

    per_target = summarise_influx(
        influx.query(build_influx_query(influx.bucket, start, end))
    )
    es_has_lines = any(s.emitters for s in target_signals)

    checks = apply_control(
        [
            evaluate_target_coverage(target_signals, eligible, arms),
            evaluate_presence(presence_signals, arms),
            evaluate_old_arm_purity(target_signals + presence_signals, arms),
            evaluate_influx(per_target, arms, es_has_lines),
        ],
        control,
    )
    signals = target_signals + presence_signals
    return {
        "window": window_label(window),
        "window_start": iso(start),
        "window_end": iso(end),
        "to_version": args.to_version,
        "running_services": arms.running,
        "new_arm": len(arms.new),
        "old_arm": len(arms.old),
        "eligible_new_services": len(eligible),
        "version_counts": arms.version_counts(),
        "control": {"name": control.name, "count": control.count, "ok": control.ok},
        "signals": [
            {"name": s.name, "kind": s.kind, "emitters": len(s.emitters)}
            for s in signals
        ],
        "influx_hosts": {t: sorted(h) for t, h in per_target.items()},
        "checks": [c.__dict__ for c in checks],
        "passed": not any(c.failed for c in checks),
        "_arms": arms,
        "_checks": checks,
        "_control": control,
        "_signals": signals,
        "_eligible": eligible,
        "_per_target": per_target,
    }


def render_report(result: dict) -> str:
    arms: Arms = result["_arms"]
    lines = [
        f"### V2-834 traffic targets — ant-prod-01, to {result['to_version']}",
        "",
        f"Window {result['window']} ({result['window_start']} → {result['window_end']}). "
        f"{result['running_services']} running service(s): {result['new_arm']} new arm "
        f"({result['eligible_new_services']} up over "
        f"{int(MIN_UPTIME.total_seconds() // 60)} min), {result['old_arm']} old arm.",
        "",
        render_arms(arms),
        "",
        render_checks(result["_checks"], result["_control"]),
        "",
        "Emitters per signal:",
        "",
        markdown_table(
            ["signal", "kind", "new arm", "old arm"],
            [
                [
                    f"`{s.name}`",
                    s.kind,
                    f"{len(s.in_arm(set(arms.new)))}",
                    f"{len(s.in_arm(set(arms.old)))}",
                ]
                for s in result["_signals"]
            ],
            align="llrr",
        ),
        "",
    ]
    per_target: dict[str, set[str]] = result["_per_target"]
    if per_target:
        lines += [
            "InfluxDB `tail` hosts per target:",
            "",
            markdown_table(
                ["target", "hosts"],
                [[f"`{t}`", f"{len(h)}"] for t, h in sorted(per_target.items())],
                align="lr",
            ),
            "",
        ]
    else:
        lines += [
            f"No `tail` points for the new targets in `{INFLUX_BUCKET}` in the window.",
            "",
        ]
    lines.append(
        "Coverage excludes new-version services up under "
        f"{int(MIN_UPTIME.total_seconds() // 60)} minutes: the first traffic summary is "
        "emitted five minutes after start. Old-arm purity is a pass on a zero, so it is "
        "only trusted while the control is non-zero."
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
        result = build_report(
            EsClient.from_env(),
            InfluxClient.from_env(),
            args,
            datetime.now(timezone.utc),
        )
    except RolloutCheckError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    payload = {k: v for k, v in result.items() if not k.startswith("_")}
    emit(payload, render_report(result), args.json_out)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
