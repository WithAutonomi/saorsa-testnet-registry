"""Watch the WebRTC connection-establishment WARN rates against stable controls.

Four targets log WARN on the path that establishes a WebRTC-direct connection,
and two of them (`webrtc_ice::agent::agent_internal`, `webrtc::mux`) roughly
doubled on the 2026-09-29 release while ten of twelve measured targets fell
(V2-1409). The other two (`dtls::handshake::handshake_message_client_hello`,
`webrtc::peer_connection::peer_connection_internal`) sit on the same path and
are measured alongside them.

A bare per-target count cannot answer the question, because these four swing
36.9-47.5% day to day on their own. So every rate here is divided by the mean
rate of **three stable controls** -- `saorsa_core::dht_network_manager`,
`saorsa_core::transport_handle` and `saorsa_transport::happy_eyeballs`, chosen
for being high-volume (0.7M-1.2M/day), fleet-wide (73 hosts, 774 services) and
under 8% day-to-day CV. The resulting control-normalised ratio

    cnr(target, arm) = rate(target, arm) / mean(rate(c, arm) for c in controls)

cancels fleet-wide volume drift: a target doubling while the controls double is
not a finding, a target doubling while the controls hold still is. Note this
makes the arm's service count cancel out of the ratio, which is deliberate --
the normaliser carries the same denominator as the thing it normalises.

`saorsa_transport::p2p_endpoint` is deliberately **not** a control despite being
fourth by WARN volume: it swings 3.99x across six days (CV 47.7%), worse than
the targets it would be controlling for. A fourth control has to be justified on
stability, not volume.

This is a **standing** check: Phase A of every stable rollout watch runs it at
A2, A3, A4 and A5, so nothing here encodes one release's expectations.

Usage:
    uv run scripts/rollout-watch/webrtc_handshake_warns.py [--window 24h]
        [--to-version 0.22.0] [--baseline-cnr agent_internal=0.63 ...]
        [--json-out out.json]
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
    build_composite,
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

TO_VERSION = "0.22.0"

#: The four WebRTC connection-establishment WARN targets, by short key. These
#: strings are copied verbatim from live documents and must not be
#: reconstructed from a pattern: `client_hello` sits under the bare `dtls::`
#: root (a prefix query on `webrtc` misses it entirely), `mux` is two segments
#: rather than three, and `peer_connection` carries an `_internal` suffix.
MEASURED_TARGETS: dict[str, str] = {
    "client_hello": "dtls::handshake::handshake_message_client_hello",
    "agent_internal": "webrtc_ice::agent::agent_internal",
    "mux": "webrtc::mux",
    "peer_connection": "webrtc::peer_connection::peer_connection_internal",
}

#: The normaliser. High-volume, fleet-wide, and under 8% day-to-day CV.
CONTROL_TARGETS: tuple[str, ...] = (
    "saorsa_core::dht_network_manager",
    "saorsa_core::transport_handle",
    "saorsa_transport::happy_eyeballs",
)

ALL_TARGETS: tuple[str, ...] = (*MEASURED_TARGETS.values(), *CONTROL_TARGETS)

WARN_LEVEL = "WARN"

#: How far above the comparison a target's normalised share may run before the
#: increase is judged real. Matches `sctp_association_errors.py`.
RATIO_LIMIT = 1.5

TOP_HOSTS = 10
TOP_MESSAGES = 5

ARMS = ("new", "old")


# -- rates -----------------------------------------------------------------
@dataclass
class ArmRate:
    """One target's WARN volume on one arm of the rollout."""

    target: str
    arm: str
    services: int
    docs: int
    hosts: int
    emitting_services: int
    hours: float

    @property
    def per_1k_service_hours(self) -> float:
        if self.services <= 0 or self.hours <= 0:
            return 0.0
        return self.docs / (self.services * self.hours) * 1000.0

    @property
    def daily_equivalent(self) -> float:
        """The window's volume scaled to 24h, the unit V2-1409's bands are in."""
        if self.hours <= 0:
            return 0.0
        return self.docs / self.hours * 24.0

    def as_dict(self) -> dict:
        return {
            "target": self.target,
            "arm": self.arm,
            "services": self.services,
            "docs": self.docs,
            "hosts": self.hosts,
            "emitting_services": self.emitting_services,
            "hours": round(self.hours, 4),
            "per_1k_service_hours": round(self.per_1k_service_hours, 4),
            "daily_equivalent": round(self.daily_equivalent, 1),
        }


@dataclass
class Measurement:
    """Every target's volume on both arms, plus the per-host breakdown."""

    rates: dict[tuple[str, str], ArmRate]
    #: {target: {host: docs}}, summed across both arms -- V2-1409's first open
    #: question is whether a rise is fleet-wide or carried by a few hosts.
    host_docs: dict[str, dict[str, int]] = field(default_factory=dict)

    def rate(self, target: str, arm: str) -> ArmRate:
        return self.rates[(target, arm)]

    def control_mean_rate(self, arm: str) -> float:
        rates = [self.rate(c, arm).per_1k_service_hours for c in CONTROL_TARGETS]
        return sum(rates) / len(rates)

    def cnr(self, target: str, arm: str) -> float | None:
        """The control-normalised ratio, or None when the normaliser is zero."""
        mean = self.control_mean_rate(arm)
        if mean <= 0:
            return None
        return self.rate(target, arm).per_1k_service_hours / mean


def _arm_filter(to_version: str, arm: str) -> dict:
    """Documents written by one arm, by the version tag on the document itself."""
    term = {"term": {VERSION_FIELD: to_version}}
    return term if arm == "new" else {"bool": {"must_not": [term]}}


def _base_filters(start: datetime, end: datetime, to_version: str, arm: str) -> list:
    return [
        range_filter(start, end),
        {"term": {LEVEL_FIELD: WARN_LEVEL}},
        {"terms": {TARGET_FIELD: list(ALL_TARGETS)}},
        _arm_filter(to_version, arm),
    ]


def build_volume_query(
    start: datetime,
    end: datetime,
    to_version: str,
    arm: str,
    after: dict | None = None,
) -> dict:
    """Per (host, service), the WARN document count for each of the targets.

    A composite over (host, service) is what makes the service column mean
    anything: a service is the *pair*, and `tag.service_number` on its own has
    only 15 distinct values (a per-host index), so a cardinality on it would
    report 15 services for the whole fleet.
    """
    return {
        "size": 0,
        "track_total_hits": False,
        "query": {"bool": {"filter": _base_filters(start, end, to_version, arm)}},
        "aggs": build_composite(
            after,
            {"by_target": {"terms": {"field": TARGET_FIELD, "size": len(ALL_TARGETS)}}},
        ),
    }


def measure(
    es: EsClient, start: datetime, end: datetime, to_version: str, arms: Arms
) -> Measurement:
    hours = (end - start).total_seconds() / 3600.0
    services = {"new": len(arms.new), "old": len(arms.old)}
    docs: dict[tuple[str, str], int] = {}
    hosts: dict[tuple[str, str], set[str]] = {}
    emitters: dict[tuple[str, str], set[str]] = {}
    host_docs: dict[str, dict[str, int]] = {t: {} for t in MEASURED_TARGETS.values()}

    for arm in ARMS:
        pages = es.composite_pages(
            lambda after, arm=arm: build_volume_query(
                start, end, to_version, arm, after
            )
        )
        for page in pages:
            for bucket in page["aggregations"]["services"]["buckets"]:
                host = bucket["key"]["host"]
                key = service_key(host, bucket["key"]["service"])
                for sub in bucket["by_target"]["buckets"]:
                    target, count = sub["key"], sub["doc_count"]
                    if not count:
                        continue
                    docs[(target, arm)] = docs.get((target, arm), 0) + count
                    hosts.setdefault((target, arm), set()).add(host)
                    emitters.setdefault((target, arm), set()).add(key)
                    if target in host_docs:
                        per_host = host_docs[target]
                        per_host[host] = per_host.get(host, 0) + count

    rates = {
        (target, arm): ArmRate(
            target=target,
            arm=arm,
            services=services[arm],
            docs=docs.get((target, arm), 0),
            hosts=len(hosts.get((target, arm), ())),
            emitting_services=len(emitters.get((target, arm), ())),
            hours=hours,
        )
        for target in ALL_TARGETS
        for arm in ARMS
    }
    return Measurement(rates=rates, host_docs=host_docs)


def collect_control_docs(es: EsClient, start: datetime, end: datetime) -> int:
    """The control: WARN documents on the three control targets, both arms.

    Queried in its own right rather than summed out of the composite, because a
    control has to be the simplest statement that the fleet is being read at
    all.
    """
    return es.count(
        [
            {"term": {LEVEL_FIELD: WARN_LEVEL}},
            {"terms": {TARGET_FIELD: list(CONTROL_TARGETS)}},
        ],
        start,
        end,
    )


def collect_messages(
    es: EsClient, start: datetime, end: datetime, to_version: str
) -> dict[str, dict[str, dict[str, int]]]:
    """{target: {arm: {message: count}}} for the measured targets."""
    out: dict[str, dict[str, dict[str, int]]] = {
        t: {arm: {} for arm in ARMS} for t in MEASURED_TARGETS.values()
    }
    for arm in ARMS:
        body = {
            "size": 0,
            "track_total_hits": False,
            "query": {"bool": {"filter": _base_filters(start, end, to_version, arm)}},
            "aggs": {
                "by_target": {
                    "terms": {"field": TARGET_FIELD, "size": len(ALL_TARGETS)},
                    "aggs": {
                        "msg": {
                            "terms": {
                                "field": MESSAGE_KEYWORD_FIELD,
                                "size": TOP_MESSAGES,
                            }
                        }
                    },
                }
            },
        }
        response = es.search(body)
        for bucket in response["aggregations"]["by_target"]["buckets"]:
            target = bucket["key"]
            if target not in out:
                continue
            out[target][arm] = {
                m["key"]: m["doc_count"] for m in bucket["msg"]["buckets"]
            }
    return out


# -- concentration ---------------------------------------------------------
def summarise_concentration(host_docs: dict[str, int]) -> dict:
    """The top `TOP_HOSTS` hosts for one target, and the share they carry."""
    ranked = sorted(host_docs.items(), key=lambda kv: (-kv[1], kv[0]))
    total = sum(host_docs.values())
    top = ranked[:TOP_HOSTS]
    top_docs = sum(count for _, count in top)
    return {
        "hosts": len(ranked),
        "total": total,
        "top": [{"host": host, "docs": count} for host, count in top],
        "top_docs": top_docs,
        "top_share": (top_docs / total * 100.0) if total else 0.0,
    }


# -- the pass rule ---------------------------------------------------------
def evaluate(
    key: str,
    target: str,
    measurement: Measurement,
    arms: Arms,
    baseline_cnr: float | None,
) -> Check:
    """One measured target's verdict, on its control-normalised ratio."""
    name = f"`{target}` WARN share not elevated on the new arm"
    new, old = measurement.rate(target, "new"), measurement.rate(target, "old")
    new_cnr = measurement.cnr(target, "new")
    old_cnr = measurement.cnr(target, "old")
    data = {
        "key": key,
        "target": target,
        "new": new.as_dict(),
        "old": old.as_dict(),
        "new_cnr": new_cnr,
        "old_cnr": old_cnr,
        "baseline_cnr": baseline_cnr,
    }

    # 0 new-version services is the expected state of a pre-rollout baseline
    # run, and must not read as success.
    if not arms.new:
        return Check(
            name=name,
            status=NOT_APPLICABLE,
            detail=not_applicable(arms),
            data=data,
        )

    if new_cnr is None:
        return Check(
            name=name,
            status=NOT_APPLICABLE,
            detail=(
                "not applicable: the three control targets logged nothing on the "
                "new arm, so the normaliser is undefined"
            ),
            data=data,
        )

    shown = (
        f"new arm cnr {new_cnr:.4f} "
        f"({new.per_1k_service_hours:.2f}/1k service-hours, {new.docs:,} doc(s), "
        f"{new.hosts} host(s), {new.services} service(s))"
    )
    new_share, old_share = arms.share("new"), arms.share("old")

    if min(new_share, old_share) >= SUSPEND_BELOW_SHARE:
        # An undefined old-arm normaliser and a target silent on the old arm
        # are unrelated states, and only the second one is a comparison. The
        # first takes a telemetry fault -- all three controls are emitted by
        # every service at 0.7-1.2M/day -- so treating it as "the target
        # logged none" would FAIL every high-volume target on an artefact.
        if old_cnr is None:
            return Check(
                name=name,
                status=NOT_APPLICABLE,
                detail=(
                    f"{shown}; the three control targets logged nothing on the old "
                    "arm, so that arm has no normaliser and no comparison is "
                    "possible. That is a telemetry fault, not a quiet fleet."
                ),
                data=data,
            )
        if not old_cnr:
            status = FAIL if new.docs else PASS
            return Check(
                name=name,
                status=status,
                detail=f"{shown}; old arm logged none, so no ratio is defined",
                passed_on_zero=(status == PASS),
                data=data,
            )
        ratio = new_cnr / old_cnr
        status = PASS if ratio <= RATIO_LIMIT else FAIL
        return Check(
            name=name,
            status=status,
            detail=(
                f"{shown}; old arm cnr {old_cnr:.4f} "
                f"({old.per_1k_service_hours:.2f}/1k service-hours, "
                f"{old.docs:,} doc(s), {old.hosts} host(s)); "
                f"ratio {ratio:.2f}x against a {RATIO_LIMIT}x limit"
            ),
            passed_on_zero=(status == PASS and new.docs == 0),
            data={**data, "ratio": ratio, "compared_against": "old arm"},
        )

    # `> 0` is only a division guard; `parse_baseline_cnr` refuses a
    # non-positive ratio at the CLI rather than letting it fall through here.
    if baseline_cnr is not None and baseline_cnr > 0:
        ratio = new_cnr / baseline_cnr
        status = PASS if ratio <= RATIO_LIMIT else FAIL
        return Check(
            name=name,
            status=status,
            detail=(
                f"{shown}; baseline cnr {baseline_cnr:.4f}; "
                f"ratio {ratio:.2f}x against a {RATIO_LIMIT}x limit "
                f"(arms are {new_share:.2f}% new / {old_share:.2f}% old, so the "
                "baseline is used)"
            ),
            passed_on_zero=(status == PASS and new.docs == 0),
            data={**data, "ratio": ratio, "compared_against": "baseline"},
        )

    return Check(
        name=name,
        status=NOT_APPLICABLE,
        detail=(
            f"{shown}; arms are {new_share:.2f}% new / {old_share:.2f}% old, so one "
            f"is under {SUSPEND_BELOW_SHARE}% and no --baseline-cnr was given for "
            f"`{key}`. Record the cnr above and pass it as "
            f"--baseline-cnr {key}={new_cnr:.4f} on later runs."
        ),
        data=data,
    )


# -- CLI -------------------------------------------------------------------
def parse_baseline_cnr(values: list[str] | None) -> dict[str, float]:
    """`--baseline-cnr agent_internal=0.63` -> {"agent_internal": 0.63}.

    An unknown key is an error rather than a silent no-op: a typo there would
    quietly withdraw the baseline and turn a comparison into NOT APPLICABLE. A
    non-positive ratio is refused for the same reason -- it cannot be a
    denominator, so it would be dropped just as silently.
    """
    out: dict[str, float] = {}
    for raw in values or []:
        if "=" not in raw:
            raise RolloutCheckError(
                f"--baseline-cnr must look like target=1.23. Got {raw!r}."
            )
        key, _, number = raw.partition("=")
        key = key.strip()
        if key not in MEASURED_TARGETS:
            known = ", ".join(MEASURED_TARGETS)
            raise RolloutCheckError(
                f"--baseline-cnr key {key!r} is not a measured target. "
                f"Known keys: {known}."
            )
        try:
            ratio = float(number.strip())
        except ValueError:
            raise RolloutCheckError(
                f"--baseline-cnr {key} must be a number. Got {number.strip()!r}."
            ) from None
        if ratio <= 0:
            raise RolloutCheckError(
                f"--baseline-cnr {key} must be greater than zero, since it is the "
                f"denominator of a ratio. Got {ratio}."
            )
        out[key] = ratio
    return out


# -- rendering -------------------------------------------------------------
def _rate_row(rate: ArmRate, cnr: float | None) -> list[str]:
    return [
        f"`{rate.target}`",
        rate.arm,
        f"{rate.docs:,}",
        f"{rate.hosts}",
        f"{rate.emitting_services}",
        f"{rate.per_1k_service_hours:.2f}",
        f"{rate.daily_equivalent:,.0f}",
        "n/a" if cnr is None else f"{cnr:.4f}",
    ]


_RATE_HEADERS = [
    "target",
    "arm",
    "docs",
    "hosts",
    "services",
    "per 1k service-hours",
    "daily equivalent",
    "cnr",
]


def render_rates(measurement: Measurement) -> str:
    rows = [
        _rate_row(measurement.rate(target, arm), measurement.cnr(target, arm))
        for target in MEASURED_TARGETS.values()
        for arm in ARMS
    ]
    return markdown_table(_RATE_HEADERS, rows, align="llrrrrrr")


def render_controls(measurement: Measurement) -> str:
    """The controls, plus the mean that every cnr above is divided by."""
    rows = [
        _rate_row(measurement.rate(target, arm), measurement.cnr(target, arm))
        for target in CONTROL_TARGETS
        for arm in ARMS
    ]
    for arm in ARMS:
        rows.append(
            [
                "**mean of the three controls**",
                arm,
                "—",
                "—",
                "—",
                f"{measurement.control_mean_rate(arm):.2f}",
                "—",
                "—",
            ]
        )
    return markdown_table(_RATE_HEADERS, rows, align="llrrrrrr")


def render_concentration(target: str, summary: dict) -> str:
    if not summary["total"]:
        return f"`{target}`: _no WARN documents in this window._"
    rows = [
        [
            f"`{entry['host']}`",
            f"{entry['docs']:,}",
            f"{entry['docs'] / summary['total'] * 100:.2f}%",
        ]
        for entry in summary["top"]
    ]
    head = (
        f"`{target}`: top {len(summary['top'])} of {summary['hosts']} hosts carry "
        f"{summary['top_share']:.1f}% of {summary['total']:,} document(s)."
    )
    return head + "\n\n" + markdown_table(["host", "docs", "share"], rows, align="lrr")


def render_messages(target: str, per_arm: dict[str, dict[str, int]]) -> str:
    new, old = per_arm.get("new", {}), per_arm.get("old", {})
    keys = list(new)
    keys += [k for k in old if k not in new]
    if not keys:
        return f"`{target}`: _no WARN documents in this window._"
    rows = [[f"`{k}`", f"{new.get(k, 0):,}", f"{old.get(k, 0):,}"] for k in keys]
    return f"`{target}`:\n\n" + markdown_table(
        ["message", "new arm", "old arm"], rows, align="lrr"
    )


def build_report(es: EsClient, args: argparse.Namespace, now: datetime) -> dict:
    window = parse_window(args.window)
    start, end = now - window, now
    baselines = parse_baseline_cnr(args.baseline_cnr)

    arms = load_arms(es, args.to_version, end)
    measurement = measure(es, start, end, args.to_version, arms)
    messages = collect_messages(es, start, end, args.to_version)
    control = Control(
        name="WARN documents on the three control targets (both arms)",
        count=collect_control_docs(es, start, end),
    )
    concentration = {
        target: summarise_concentration(measurement.host_docs[target])
        for target in MEASURED_TARGETS.values()
    }
    checks = apply_control(
        [
            evaluate(key, target, measurement, arms, baselines.get(key))
            for key, target in MEASURED_TARGETS.items()
        ],
        control,
    )
    return {
        "window": window_label(window),
        "window_start": iso(start),
        "window_end": iso(end),
        "to_version": args.to_version,
        "baseline_cnr": baselines,
        "running_services": arms.running,
        "new_arm": len(arms.new),
        "old_arm": len(arms.old),
        "version_counts": arms.version_counts(),
        "control": {"name": control.name, "count": control.count, "ok": control.ok},
        "ratio_limit": RATIO_LIMIT,
        "measured_targets": MEASURED_TARGETS,
        "control_targets": list(CONTROL_TARGETS),
        "rates": [r.as_dict() for r in measurement.rates.values()],
        "control_mean_rate": {
            arm: round(measurement.control_mean_rate(arm), 4) for arm in ARMS
        },
        "cnr": {
            target: {arm: measurement.cnr(target, arm) for arm in ARMS}
            for target in ALL_TARGETS
        },
        "concentration": concentration,
        "messages": messages,
        "checks": [c.__dict__ for c in checks],
        "passed": not any(c.failed for c in checks),
        "_arms": arms,
        "_checks": checks,
        "_control": control,
        "_measurement": measurement,
        "_messages": messages,
        "_concentration": concentration,
    }


def render_report(result: dict) -> str:
    arms: Arms = result["_arms"]
    measurement: Measurement = result["_measurement"]
    lines = [
        f"### WebRTC handshake WARN rates — ant-prod-01, to {result['to_version']}",
        "",
        f"Window {result['window']} ({result['window_start']} → "
        f"{result['window_end']}). {result['running_services']} running "
        f"service(s): {result['new_arm']} new arm, {result['old_arm']} old arm.",
        "",
        render_arms(arms),
        "",
        "Measured targets, by arm. `cnr` is the rate divided by the mean of the "
        "three controls below:",
        "",
        render_rates(measurement),
        "",
        "Controls, and the mean every `cnr` above is divided by:",
        "",
        render_controls(measurement),
        "",
        render_checks(result["_checks"], result["_control"]),
        "",
        "**Host concentration.** A fleet-wide rise and one carried by a handful "
        "of hosts mean different things (V2-1409):",
        "",
    ]
    for target in MEASURED_TARGETS.values():
        lines.append(render_concentration(target, result["_concentration"][target]))
        lines.append("")
    lines += [
        f"**Message bodies**, top {TOP_MESSAGES} per arm. The same message at a "
        "higher rate and a new message appearing are different findings "
        "(V2-1409):",
        "",
    ]
    for target in MEASURED_TARGETS.values():
        lines.append(render_messages(target, result["_messages"][target]))
        lines.append("")
    lines += [
        "The measure is each target's *share* of WARN traffic, not its volume: "
        "the four targets swing 36.9-47.5% day to day on their own, while the "
        "three controls hold under 8%, so dividing by the controls' mean is what "
        "separates a release regression from fleet-wide drift. Service-hours are "
        "the arm's running-service count times the window, the same "
        "simplification on both arms and in both numerator and denominator, so "
        "it cancels out of every `cnr`.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(parser, to_version=TO_VERSION)
    # These four targets are too volatile to read over an hour; 24h is the unit
    # V2-1409's bands are in.
    parser.set_defaults(window="24h")
    parser.add_argument(
        "--baseline-cnr",
        action="append",
        metavar="TARGET=RATIO",
        default=None,
        help=(
            "Control-normalised ratio measured before the rollout, per target "
            f"({', '.join(MEASURED_TARGETS)}). Repeatable. Used only once one arm "
            f"is under {SUSPEND_BELOW_SHARE}% and no arm comparison is possible."
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
