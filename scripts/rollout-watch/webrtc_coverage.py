"""Verify ant-node #220's WebRTC-Direct listener fleet-side on ant-prod-01.

Three measures, in increasing order of how much they prove:

1. **Listening coverage** -- every service on the new version logged
   `ADR-0015 WebRTC Direct listening` after its most recent `ant-node starting`.
   This is the cheap fleet-wide check and it only proves the listener bound.
2. **WARN families** -- none of the ten WebRTC/browser WARN messages touches
   more than 5% of the new-version services. A listener that bound and then
   failed on every endpoint update passes (1) and fails here.
3. **Advertised address** -- a sampled service's persisted
   `webrtc-direct.multiaddr` names the host's real public IP, and an ICE probe
   from this machine gets an answer on the port in that file. This is the only
   measure that proves a browser could actually reach the node: the probe is a
   real ICE connectivity check, because `saorsa-transport`'s admission filter
   answers an unauthenticated STUN binding with silence, so a plain binding
   request cannot tell a firewalled port from a healthy one.

Node services take a *random* WebRTC port (unlike the seven bootstraps, which
V2-1307 pins), so the port is read out of the multiaddr file every time and can
never be assumed.

Usage:
    uv run scripts/rollout-watch/webrtc_coverage.py [--window 1h]
        [--to-version 0.20.0] [--sample 10] [--json-out out.json]

Example:
    uv run scripts/rollout-watch/webrtc_coverage.py --window 6h --sample 10
"""

from __future__ import annotations

import argparse
import dataclasses
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    DEPLOYMENT_NAME,
    FAIL,
    HOST_FIELD,
    MESSAGE_FIELD,
    MESSAGE_KEYWORD_FIELD,
    NOT_APPLICABLE,
    PASS,
    SERVICE_FIELD,
    START_MESSAGE,
    Arms,
    Check,
    Control,
    EsClient,
    RolloutCheckError,
    add_common_args,
    apply_control,
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
from saorsa_deploy.analysis import hosts  # noqa: E402
from saorsa_deploy.provisioning.volume import MOUNT_POINT  # noqa: E402
from saorsa_deploy.webrtc_addresses import (  # noqa: E402
    MULTIADDR_FILENAME,
    address_host,
    parse_multiaddr,
)
from saorsa_deploy.webrtc_probe import probe  # noqa: E402

TO_VERSION = "0.20.0"
LISTENING_MESSAGE = "ADR-0015 WebRTC Direct listening"

#: How long after `ant-node starting` a missing listening line is still
#: *pending* rather than a miss. 0.20.0 opens the listener only after the node
#: has joined: measured 7m10s-11m55s on six fleet services at the 2026-09-24 A2
#: checkpoint (1m43s-3m29s+ on the bootstraps). Without a grace window, every
#: service upgraded in the last ten minutes fails the check while healthy.
LISTEN_GRACE = timedelta(minutes=15)

#: The WARN messages ant-node #220 and saorsa-transport can emit around the
#: WebRTC-Direct path. Matched as phrases, not exact terms: most interpolate an
#: error or an endpoint into the tail of the line.
WARN_FAMILIES = (
    "Failed to update WebRTC Direct endpoint",
    "WebRTC Direct connection task failed",
    "WebRTC Direct listener error",
    "WebRTC Direct connection tasks did not drain before shutdown deadline",
    "WebRtcDirect task shutdown failed",
    "Browser operation failed",
    "Browser payment verification failed",
    "Browser protocol operation failed",
    "Unrecognized browser protocol error",
    "WebRTC Direct UDP listener failed",
)

#: A family affecting more than this share of the new arm is a regression, not
#: one sick host.
WARN_FAMILY_SHARE_LIMIT = 5.0

#: Providers the sample must span. A WebRTC port reachable on Hetzner and
#: nowhere else is a firewall finding, and only a spread sample shows it.
SAMPLE_PROVIDERS = ("hetzner", "ovh", "contabo")
MIN_PER_PROVIDER = 3
PROBE_TIMEOUT_SECS = 5.0
PROBE_ATTEMPTS = 3
SSH_TIMEOUT_SECS = 60


# -- listening coverage ----------------------------------------------------
@dataclass
class Coverage:
    """Per-service outcome of the ADR-0015 listening check."""

    covered: list[str] = field(default_factory=list)
    misses: list[tuple[str, str]] = field(default_factory=list)
    unassessed: list[str] = field(default_factory=list)
    pending: list[tuple[str, str]] = field(default_factory=list)

    @property
    def assessed(self) -> int:
        return len(self.covered) + len(self.misses)

    @property
    def share(self) -> float:
        return 100.0 if not self.assessed else len(self.covered) / self.assessed * 100.0


def evaluate_listening_coverage(
    services: list[str],
    starts: dict[str, datetime],
    listens: dict[str, datetime],
    now: datetime | None = None,
    grace: timedelta = LISTEN_GRACE,
) -> Coverage:
    """Which new-version services logged the listening line since their last start.

    A service whose window holds neither a start nor a listening line is
    *unassessed*, not a miss: the window simply does not cover its start, and
    counting it as a failure would turn every quiet service into a regression
    once the rollout has been done for a while.

    With ``now``, a service that started less than ``grace`` ago and has not
    listened yet is *pending*, not a miss: the listener opens minutes after
    start (see ``LISTEN_GRACE``). Past the grace window it is a miss as before.
    """
    result = Coverage()
    for key in sorted(services):
        started, listened = starts.get(key), listens.get(key)
        if listened is not None and (started is None or listened >= started):
            result.covered.append(key)
        elif started is not None and now is not None and now - started < grace:
            result.pending.append(
                (key, f"restarted {iso(started)}, within the {grace} listen grace")
            )
        elif started is not None:
            reason = (
                f"restarted {iso(started)}, last listening line {iso(listened)}"
                if listened is not None
                else f"restarted {iso(started)}, no listening line since"
            )
            result.misses.append((key, reason))
        else:
            result.unassessed.append(key)
    return result


# -- WARN families ---------------------------------------------------------
@dataclass
class Family:
    """One WARN message's spread in the window."""

    phrase: str
    count: int
    services: list[str]

    def share(self, denominator: int) -> float:
        return 0.0 if not denominator else len(self.services) / denominator * 100.0


def build_family_query(phrase: str, start: datetime, end: datetime) -> dict:
    return {
        "size": 0,
        "track_total_hits": True,
        "query": {
            "bool": {
                "filter": [
                    range_filter(start, end),
                    {"match_phrase": {MESSAGE_FIELD: phrase}},
                ]
            }
        },
        "aggs": {
            "hosts": {
                "terms": {"field": HOST_FIELD, "size": 200},
                "aggs": {"services": {"terms": {"field": SERVICE_FIELD, "size": 50}}},
            }
        },
    }


def summarise_family(phrase: str, response: dict) -> Family:
    services: list[str] = []
    for host in response["aggregations"]["hosts"]["buckets"]:
        for service in host["services"]["buckets"]:
            services.append(service_key(host["key"], service["key"]))
    return Family(
        phrase=phrase,
        count=response["hits"]["total"]["value"],
        services=sorted(services),
    )


def evaluate_warn_families(families: list[Family], arms: Arms) -> Check:
    """Fail when any family touches more than 5% of the new-version services."""
    denominator = len(arms.new)
    if not denominator:
        return Check(
            name="WARN families",
            status=NOT_APPLICABLE,
            detail=not_applicable(arms),
            data={"families": [f.__dict__ for f in families]},
        )
    new_arm = set(arms.new)
    breaches = []
    for family in families:
        affected = [s for s in family.services if s in new_arm]
        share = len(affected) / denominator * 100.0
        if share > WARN_FAMILY_SHARE_LIMIT:
            breaches.append((family.phrase, len(affected), share))
    total = sum(f.count for f in families)
    if breaches:
        worst = ", ".join(f"{p} ({n} svc, {s:.2f}%)" for p, n, s in breaches)
        return Check(
            name="WARN families",
            status=FAIL,
            detail=f"over {WARN_FAMILY_SHARE_LIMIT}% of the new arm: {worst}",
            data={"breaches": breaches},
        )
    return Check(
        name="WARN families",
        status=PASS,
        detail=(
            f"{total:,} document(s) across {len(WARN_FAMILIES)} families, "
            f"none over {WARN_FAMILY_SHARE_LIMIT}% of {denominator} new-version services"
        ),
        passed_on_zero=total == 0,
        data={"total": total},
    )


# -- advertised address sample --------------------------------------------
@dataclass
class Advertised:
    """One sampled service's advertised address and whether it answers."""

    host: str
    ip: str
    provider: str
    service: str
    multiaddr: str | None = None
    port: int | None = None
    advertised_ip: str | None = None
    probed: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.advertised_ip == self.ip and self.probed


def select_sample(
    vms: list[hosts.Vm],
    sample: int,
    providers: tuple[str, ...] = SAMPLE_PROVIDERS,
    min_per_provider: int = MIN_PER_PROVIDER,
) -> list[hosts.Vm]:
    """`sample` VMs with at least `min_per_provider` from each provider.

    Deterministic (inventory order), so two runs sample the same hosts and a
    failure can be chased on the box. A provider short of its minimum is taken
    as far as it goes rather than refused -- the fleet's composition is not this
    script's business.
    """
    by_provider: dict[str, list[hosts.Vm]] = {p: [] for p in providers}
    others: list[hosts.Vm] = []
    for vm in vms:
        if not vm.services:
            continue
        by_provider.get(vm.provider, others).append(vm)
    chosen: list[hosts.Vm] = []
    for provider in providers:
        chosen.extend(by_provider[provider][:min_per_provider])
    remaining = [vm for vm in vms if vm not in chosen and vm.services]
    for vm in remaining:
        if len(chosen) >= sample:
            break
        chosen.append(vm)
    return chosen[:sample]


def new_arm_services(vm: hosts.Vm, new_arm: set[str]) -> tuple[str, ...]:
    """The VM's services that are on the new version, in inventory order.

    Arm keys are ``<HOST>/<n>`` (ES upper-cases the host and tags the service
    number); inventory services are ``ant-node-<n>``.
    """
    host = vm.name.upper()
    return tuple(
        service
        for service in vm.services
        if f"{host}/{service.rsplit('-', 1)[-1]}" in new_arm
    )


def new_arm_vms(vms: list[hosts.Vm], new_arm: set[str]) -> list[hosts.Vm]:
    """``vms`` restricted to their new-version services.

    Mid-rollout most services are still on the old version, which never writes
    ``webrtc-direct.multiaddr``. Sampling a VM's first service regardless of
    version failed 9/10 healthy samples at the 2026-09-24 A2 checkpoint. VMs with
    no new-version service end up with no services, which ``select_sample``
    already skips.
    """
    return [
        dataclasses.replace(vm, services=new_arm_services(vm, new_arm)) for vm in vms
    ]


def multiaddr_path(service: str) -> str:
    return f"{MOUNT_POINT}/data/{service}/{MULTIADDR_FILENAME}"


def read_advertised(vm: hosts.Vm, service: str) -> Advertised:
    """Read one service's persisted browser address, then probe its port."""
    record = Advertised(host=vm.name, ip=vm.ip, provider=vm.provider, service=service)
    command = [*vm.ssh_args(), vm.target, f"cat {multiaddr_path(service)}"]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=SSH_TIMEOUT_SECS
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        record.error = f"ssh failed: {exc}"
        return record
    if result.returncode != 0:
        record.error = f"no {MULTIADDR_FILENAME}: {result.stderr.strip()[:120]}"
        return record
    try:
        record.multiaddr = parse_multiaddr(result.stdout)
        record.advertised_ip = address_host(record.multiaddr)
        record.port = int(record.multiaddr.split("/")[4])
    except (ValueError, IndexError) as exc:
        record.error = f"unusable multiaddr: {exc}"
        return record
    if record.advertised_ip != vm.ip:
        record.error = f"advertises {record.advertised_ip}, inventory says {vm.ip}"
        return record
    record.probed = probe(
        vm.ip, record.port, timeout=PROBE_TIMEOUT_SECS, attempts=PROBE_ATTEMPTS
    )
    if not record.probed:
        record.error = f"no ICE answer on udp/{record.port}"
    return record


def evaluate_advertised(records: list[Advertised], arms: Arms, sample: int) -> Check:
    if not sample:
        return Check(
            name="advertised address sample",
            status=NOT_APPLICABLE,
            detail="skipped (--sample 0)",
        )
    if not arms.new:
        return Check(
            name="advertised address sample",
            status=NOT_APPLICABLE,
            detail=not_applicable(arms),
        )
    failures = [r for r in records if not r.ok]
    detail = (
        f"{len(records) - len(failures)}/{len(records)} sampled service(s) reachable"
    )
    if failures:
        named = ", ".join(f"{r.host}/{r.service}: {r.error}" for r in failures[:10])
        return Check(
            name="advertised address sample",
            status=FAIL,
            detail=f"{detail}; {named}",
            data={"failures": [r.__dict__ for r in failures]},
        )
    return Check(name="advertised address sample", status=PASS, detail=detail)


def render_sample(records: list[Advertised]) -> str:
    rows = [
        [
            r.host,
            r.provider,
            r.service,
            r.ip,
            "-" if r.port is None else str(r.port),
            "yes" if r.probed else "no",
            r.error or "",
        ]
        for r in records
    ]
    return markdown_table(
        ["host", "provider", "service", "ip", "udp port", "ICE answer", "note"], rows
    )


# -- report ----------------------------------------------------------------
# -- listener delay (V2-1331, record-only) ---------------------------------
#: ant-node 0.20.0 was measured opening its WebRTC listener 1m43s to 14m26s
#: after start (V2-1331), which leaves a node unreachable from browsers for
#: that whole time -- and browsers only enter the network through the
#: bootstraps. Nothing on the 2026-09-29 train attempts a fix, so this section
#: exists to accumulate a second rollout's worth of evidence.
#:
#: It is deliberately RECORD-ONLY and contributes NO check: a listener that
#: never opens is already a `listening coverage` miss, which gate 1 breaches on.
#: Adding a pass rule here would double-count that and give gate 1 a second,
#: differently-calibrated trigger.
def summarise_listen_delays(
    new_arm: list[str],
    starts: dict[str, datetime],
    listens: dict[str, datetime],
) -> dict:
    """Seconds from `ant-node starting` to the listening line, per new-arm service.

    Only pairs where the listening line is at or after the start are counted: a
    listening line from a *previous* run of the service (the window caught the
    old listen but not the old start) would otherwise read as a negative delay
    and quietly drag the median down.
    """
    delays: list[tuple[str, float]] = []
    stale = 0
    for key in new_arm:
        began, listening = starts.get(key), listens.get(key)
        if began is None or listening is None:
            continue
        seconds = (listening - began).total_seconds()
        if seconds < 0:
            stale += 1
            continue
        delays.append((key, seconds))
    delays.sort(key=lambda kv: kv[1])
    values = [seconds for _, seconds in delays]
    return {
        "measured": len(values),
        "stale_pairs": stale,
        "min_secs": round(values[0], 1) if values else None,
        "median_secs": round(_percentile(values, 0.50), 1) if values else None,
        "p90_secs": round(_percentile(values, 0.90), 1) if values else None,
        "max_secs": round(values[-1], 1) if values else None,
        "slowest": [[key, round(seconds, 1)] for key, seconds in delays[-10:][::-1]],
        "no_listen_line": sorted(
            key for key in new_arm if key in starts and key not in listens
        ),
    }


def _percentile(values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile over a pre-sorted list."""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    position = fraction * (len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def render_listen_delays(delays: dict) -> str:
    if not delays["measured"]:
        return (
            "_No new-arm service had both a start and a listening line in the "
            "window, so no delay could be measured._"
        )
    rows = [
        [
            f"{delays['measured']}",
            _fmt_secs(delays["min_secs"]),
            _fmt_secs(delays["median_secs"]),
            _fmt_secs(delays["p90_secs"]),
            _fmt_secs(delays["max_secs"]),
        ]
    ]
    return markdown_table(
        ["services", "min", "median", "p90", "max"], rows, align="rrrrr"
    )


def _fmt_secs(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 60:
        return f"{value:.0f}s"
    return f"{int(value // 60)}m{int(value % 60):02d}s"


def build_report(es: EsClient, args: argparse.Namespace, now: datetime) -> dict:
    window = parse_window(args.window)
    start, end = now - window, now
    arms = load_arms(es, args.to_version, end)

    control = Control(
        name=f"`{START_MESSAGE}` lines",
        count=es.count([{"term": {MESSAGE_KEYWORD_FIELD: START_MESSAGE}}], start, end),
    )

    starts = collect_latest(es, START_MESSAGE, start, end)
    listens = collect_latest(es, LISTENING_MESSAGE, start, end)
    coverage = evaluate_listening_coverage(arms.new, starts, listens, now=end)
    listen_delays = summarise_listen_delays(arms.new, starts, listens)
    if arms.new:
        listening_check = Check(
            name="listening coverage",
            status=PASS if not coverage.misses else FAIL,
            detail=(
                f"{len(coverage.covered)}/{coverage.assessed} assessed service(s) "
                f"({coverage.share:.2f}%), {len(coverage.pending)} pending "
                f"(started < {LISTEN_GRACE} ago), {len(coverage.unassessed)} unassessed "
                "(no start and no listening line in window)"
            ),
            passed_on_zero=coverage.assessed == 0,
            data={
                "misses": coverage.misses,
                "pending": coverage.pending,
                "unassessed": coverage.unassessed,
            },
        )
    else:
        listening_check = Check(
            name="listening coverage",
            status=NOT_APPLICABLE,
            detail=not_applicable(arms),
        )

    families = [
        summarise_family(phrase, es.search(build_family_query(phrase, start, end)))
        for phrase in WARN_FAMILIES
    ]
    warn_check = evaluate_warn_families(families, arms)

    records: list[Advertised] = []
    if args.sample and arms.new:
        vms = new_arm_vms(hosts.node_vms(DEPLOYMENT_NAME, args.inventory), set(arms.new))
        for vm in select_sample(vms, args.sample):
            records.append(read_advertised(vm, vm.services[0]))
    sample_check = evaluate_advertised(records, arms, args.sample)

    checks = apply_control([listening_check, warn_check, sample_check], control)
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
        "listen_delays": listen_delays,
        "coverage": {
            "covered": coverage.covered,
            "misses": coverage.misses,
            "pending": coverage.pending,
            "unassessed": coverage.unassessed,
        },
        "warn_families": [
            {"phrase": f.phrase, "count": f.count, "services": f.services}
            for f in families
        ],
        "sample": [r.__dict__ for r in records],
        "checks": [c.__dict__ for c in checks],
        "passed": not any(c.failed for c in checks),
        "_arms": arms,
        "_checks": checks,
        "_control": control,
        "_families": families,
        "_records": records,
    }


def render_report(result: dict) -> str:
    arms: Arms = result["_arms"]
    lines = [
        f"### WebRTC-Direct coverage — ant-prod-01, to {result['to_version']}",
        "",
        f"Window {result['window']} ({result['window_start']} → {result['window_end']}). "
        f"{result['running_services']} running service(s): "
        f"{result['new_arm']} new arm, {result['old_arm']} old arm.",
        "",
        render_arms(arms),
        "",
        render_checks(result["_checks"], result["_control"]),
        "",
    ]
    coverage = result["coverage"]
    if coverage["misses"]:
        lines += ["Misses:", ""]
        lines += [f"- `{key}` — {reason}" for key, reason in coverage["misses"]]
        lines.append("")
    if coverage["pending"]:
        lines += ["Pending (inside the listen grace window, not counted):", ""]
        lines += [f"- `{key}` — {reason}" for key, reason in coverage["pending"]]
        lines.append("")
    families = [f for f in result["_families"] if f.count]
    if families:
        lines += [
            "WARN families seen in the window:",
            "",
            markdown_table(
                ["message", "documents", "services"],
                [
                    [f"`{f.phrase}`", f"{f.count:,}", f"{len(f.services)}"]
                    for f in families
                ],
                align="lrr",
            ),
            "",
        ]
    else:
        lines += [
            f"No WebRTC/browser WARN lines in the window ({len(WARN_FAMILIES)} families).",
            "",
        ]
    if result["_records"]:
        lines += [
            "Advertised-address sample:",
            "",
            render_sample(result["_records"]),
            "",
        ]
    delays = result["listen_delays"]
    lines += [
        "Listener delay from `ant-node starting` to the listening line "
        "(V2-1331, record-only — no pass rule):",
        "",
        render_listen_delays(delays),
        "",
    ]
    if delays["slowest"]:
        lines += ["Slowest to open a listener:", ""]
        lines += [
            f"- `{key}` — {_fmt_secs(seconds)}" for key, seconds in delays["slowest"]
        ]
        lines.append("")
    if delays["no_listen_line"]:
        lines += [
            f"Started but no listening line in the window "
            f"({len(delays['no_listen_line'])}):",
            "",
        ]
        lines += [f"- `{key}`" for key in delays["no_listen_line"][:20]]
        lines.append("")
    if delays["stale_pairs"]:
        lines += [
            f"{delays['stale_pairs']} service(s) had a listening line older than "
            "their start and were excluded as a previous run's.",
            "",
        ]
    lines.append(
        "Coverage is measured over the new arm only; a service with neither a start nor a "
        "listening line in the window is unassessed rather than a miss, because the window "
        "does not cover its start. The probe is a real ICE connectivity check — "
        "saorsa-transport ignores an unauthenticated STUN binding, so silence there would "
        "not distinguish a firewalled port from a healthy one."
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(parser, to_version=TO_VERSION)
    parser.add_argument(
        "--sample",
        type=int,
        default=10,
        help="Hosts to SSH for the advertised-address check (0 skips it).",
    )
    parser.add_argument(
        "--inventory", default=None, help="Override the ant-prod-01 inventory path."
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
