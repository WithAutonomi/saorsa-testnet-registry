"""`analysis recovery` — per-service routing-table recovery timing after an outage.

The V2-1085 gate is "a node that started with no network joins once the
network is back, and it is the re-bootstrap fallback that joined it". That is
a per-service ordering question over four `info` lines saorsa-core emits:

* ``Auto re-bootstrap: no connected peers — re-dialing N configured bootstrap peer(s)``
  -- the fallback fired;
* ``Auto re-bootstrap: all N configured bootstrap peer(s) unreachable``
  -- it fired and found nothing (the rate-limit evidence while the cut holds);
* ``Auto re-bootstrap discovered D peers, routing table size now R``
  -- the repair completed; ``R >= 3`` is the threshold cleared;
* ``Added peer <id> to DHT routing table`` -- an admission.

Recovery is the first completion line with ``R >= 3``; when a service has
admissions but never logs one (the tick after the cut lifts can race the
admissions it caused), the third admission stands in and is flagged as such.
Attribution holds when no admission precedes the first ``re-dialing`` line
after ``--since``: nothing but the fallback can have reconnected a node that
nobody knew about.

Everything is computed from exported documents so the same rows can be
re-scored offline; the ES query is one filtered export per phrase.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from saorsa_deploy.analysis import hosts
from saorsa_deploy.analysis.es import EsRun, hosts_filter
from saorsa_deploy.cmd.analysis import add_deployment_args, add_host_selection_args
from saorsa_deploy.es_log_counts import MESSAGE_FIELD, TIMESTAMP_FIELD

HOST_SRC = "tag.host"
SERVICE_SRC = "tag.service_number"

REDIAL_PHRASE = "no connected peers"  # "Auto re-bootstrap: no connected peers — re-dialing …"
UNREACHABLE_PHRASE = "configured bootstrap peer(s) unreachable"
DISCOVERED_PHRASE = "Auto re-bootstrap discovered"
ADDED_PHRASE = "to DHT routing table"

PHRASES = {
    "redial": REDIAL_PHRASE,
    "unreachable": UNREACHABLE_PHRASE,
    "discovered": DISCOVERED_PHRASE,
    "added": ADDED_PHRASE,
}

RT_RE = re.compile(r"routing table size now (\d+)")
THRESHOLD = 3
ADMISSIONS_FOR_RECOVERY = 3
COOLDOWN_S = 300.0


def _get(src: dict, dotted: str):
    if dotted in src:
        return src[dotted]
    cur = src
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def parse_ts(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(p * (len(ordered) - 1))))
    return ordered[idx]


def score_service(events: list[tuple[datetime, str, str]], since: datetime) -> dict:
    """Score one service's (ts, kind, message) events, all at or after ``since``."""
    events = sorted(events, key=lambda e: e[0])
    first_redial = next((ts for ts, kind, _ in events if kind == "redial"), None)
    unreachable = [ts for ts, kind, _ in events if kind == "unreachable"]
    added = [ts for ts, kind, _ in events if kind == "added"]
    recovered_via = None
    recovered_at = None
    rt_after = None
    for ts, kind, msg in events:
        if kind != "discovered":
            continue
        m = RT_RE.search(msg or "")
        if m and int(m.group(1)) >= THRESHOLD:
            recovered_at, recovered_via, rt_after = ts, "discovered", int(m.group(1))
            break
    if recovered_at is None and len(added) >= ADMISSIONS_FOR_RECOVERY:
        recovered_at, recovered_via = added[ADMISSIONS_FOR_RECOVERY - 1], "admissions"
    gaps = [(b - a).total_seconds() for a, b in zip(unreachable, unreachable[1:], strict=False)]
    return {
        "first_redial_utc": _iso(first_redial),
        "redial_count": sum(1 for _, kind, _ in events if kind == "redial"),
        "unreachable_count": len(unreachable),
        "min_attempt_gap_s": round(min(gaps), 1) if gaps else None,
        "added_count": len(added),
        "recovered": recovered_at is not None,
        "recovered_utc": _iso(recovered_at),
        "recovered_via": recovered_via,
        "rt_after": rt_after,
        "time_to_recovery_s": (
            round((recovered_at - since).total_seconds(), 1) if recovered_at else None
        ),
        "attributed": (
            first_redial is not None and all(ts >= first_redial for ts in added)
            if added or first_redial
            else None
        ),
    }


def score_docs(docs: dict[str, list[dict]], since: datetime) -> dict[tuple, dict]:
    """Group exported documents (by phrase kind) into per-(host, service) scores."""
    per_service: dict[tuple, list] = defaultdict(list)
    for kind, rows in docs.items():
        for src in rows:
            ts_text = _get(src, TIMESTAMP_FIELD)
            if not ts_text:
                continue
            key = (str(_get(src, HOST_SRC)).upper(), str(_get(src, SERVICE_SRC)))
            per_service[key].append((parse_ts(ts_text), kind, _get(src, MESSAGE_FIELD) or ""))
    return {key: score_service(events, since) for key, events in sorted(per_service.items())}


def summarise(scores: dict[tuple, dict], expected_services: int, within_s: float) -> dict:
    ttr = [s["time_to_recovery_s"] for s in scores.values() if s["time_to_recovery_s"] is not None]
    recovered = [s for s in scores.values() if s["recovered"]]
    return {
        "expected_services": expected_services,
        "services_with_events": len(scores),
        "recovered": len(recovered),
        "recovered_within_window": sum(1 for t in ttr if t <= within_s),
        "recovered_via_admissions_only": sum(
            1 for s in recovered if s["recovered_via"] == "admissions"
        ),
        "unattributed": sum(1 for s in recovered if s["attributed"] is False),
        "ttr_p50_s": round(statistics.median(ttr), 1) if ttr else None,
        "ttr_p95_s": round(_pct(ttr, 0.95), 1) if ttr else None,
        "ttr_max_s": round(max(ttr), 1) if ttr else None,
        "min_attempt_gap_s": min(
            (s["min_attempt_gap_s"] for s in scores.values() if s["min_attempt_gap_s"] is not None),
            default=None,
        ),
        "attempt_gap_below_cooldown": sum(
            1
            for s in scores.values()
            if s["min_attempt_gap_s"] is not None and s["min_attempt_gap_s"] < COOLDOWN_S - 5
        ),
    }


def fetch_docs(run: EsRun, host_names: list[str], start: str, end: str) -> dict[str, list[dict]]:
    fields = [TIMESTAMP_FIELD, HOST_SRC, SERVICE_SRC, MESSAGE_FIELD]
    return {
        kind: run.hits(
            [{"match_phrase": {MESSAGE_FIELD: phrase}}, *hosts_filter(host_names)],
            start,
            end,
            fields=fields,
        )
        for kind, phrase in PHRASES.items()
    }


def cmd_recovery(args: argparse.Namespace) -> int:
    console = Console()
    vms = hosts.select_vms(args.name, args.ips, args.inventory)
    since = parse_ts(args.since)
    until = args.until or _iso(datetime.now(timezone.utc))
    run = EsRun(args.name)
    docs = fetch_docs(run, [vm.name for vm in vms], args.since, until)
    scores = score_docs(docs, since)
    expected = sum(len(vm.services) for vm in vms)
    summary = summarise(scores, expected, args.within_seconds)
    summary.update({"since": args.since, "until": until, "ips": [vm.ip for vm in vms]})

    out_path = Path(args.out or f"recovery-{args.tag}.jsonl").expanduser()
    with out_path.open("w") as fh:
        for (host, service), s in scores.items():
            fh.write(
                json.dumps({"host": host, "service": service, **s}, separators=(",", ":")) + "\n"
            )
        fh.write(json.dumps({"summary": summary}, separators=(",", ":")) + "\n")

    table = Table(title=f"recovery since {args.since} ({len(vms)} VMs, {expected} services)")
    for col in ("metric", "value"):
        table.add_column(col)
    for key in (
        "services_with_events",
        "recovered",
        "recovered_within_window",
        "recovered_via_admissions_only",
        "unattributed",
        "ttr_p50_s",
        "ttr_p95_s",
        "ttr_max_s",
        "min_attempt_gap_s",
        "attempt_gap_below_cooldown",
    ):
        table.add_row(key, str(summary[key]))
    console.print(table)
    console.print(f"per-service rows and summary written to {out_path}")
    if summary["services_with_events"] < expected:
        console.print(
            f"[yellow]{expected - summary['services_with_events']} service(s) logged none of the "
            "four phrases in the window — check the cut actually bit and logs are "
            "forwarding[/yellow]"
        )
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "recovery",
        help=(
            "Per-service re-bootstrap recovery timing after an outage lifts: first fallback "
            "re-dial, first completion with routing table >= 3, attribution, attempt spacing."
        ),
    )
    add_deployment_args(p)
    add_host_selection_args(p, require_ips=True)
    p.add_argument(
        "--since", required=True, help="Outage lift (ISO-8601 UTC); TTR is measured from here."
    )
    p.add_argument("--until", default=None, help="Window end, ISO-8601 UTC (default: now).")
    p.add_argument(
        "--within-seconds",
        type=float,
        default=900,
        help="Recovery window the gate counts against (default 900 = 15 min).",
    )
    p.add_argument("--tag", required=True, help="Names the output file.")
    p.add_argument("--out", default=None, help="JSONL output (default recovery-<tag>.jsonl).")
    p.set_defaults(func=cmd_recovery)
