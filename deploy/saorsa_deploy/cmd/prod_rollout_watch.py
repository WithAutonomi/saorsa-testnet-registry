"""`prod rollout-watch` — an Elasticsearch-only view of a production rollout.

Stateless by construction. Every run asks Elasticsearch what is running right
now: there is no SQLite database, no cursor to back-date and no `init` step to
forget. That is the whole reason this command exists. Its predecessor,
`track-upgrades`, had to *infer* a node's running version from the sequence of
upgrade log messages, so it needed durable per-node state, and a cursor seeded
after the rollout had started blinded it permanently.

ant-node now stamps `node_version` and `node_commit` on every JSON log line
(WithAutonomi/ant-node#225) and telegraf promotes both to tags, so a service's
version is simply the tag on its most recent document. Nothing has to be
remembered between runs.

Two things this is careful about, both of which produce a confidently wrong
number rather than an error:

* **`tag.*` fields are analysed**, so every `terms`, `composite`, `prefix` and
  `cardinality` uses the `.keyword` subfield. `tag.host.keyword` values are
  upper-cased by the forwarder.
* **Only `size: 0` aggregations, never a document scroll.** The production
  node index takes ~13.7M documents every two hours; paging documents out of it
  would take longer than the rollout.

The `unknown` bucket is labelled `unknown (unstamped, pre-<to-version>)`
because builds older than #225 do not stamp a version at all. The rule is
applied ONLY to the literal value `unknown` (and to a missing tag, which means
the same thing): once the fleet is past this first stamped rollout, every old
arm is a real version and appears under its own value.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

from saorsa_deploy.analysis.es import EsRun
from saorsa_deploy.es_log_counts import (
    HOST_FIELD,
    LEVEL_FIELD,
    MESSAGE_FIELD,
    TIMESTAMP_FIELD,
)

SERVICE_FIELD = "tag.service_number.keyword"
TARGET_FIELD = "tag.target.keyword"
VERSION_FIELD = "tag.node_version.keyword"
ROLE_FIELD = "tag.host_role.keyword"
MESSAGE_KEYWORD_FIELD = "tail.message.keyword"
WIRE_TX_FIELD = "tail.wire_tx_bytes"

# A service is "running" if it logged in the last 15 minutes. Shorter than that
# and a quiet service drops out of the denominator; longer and a service that
# died an hour ago is still counted as running.
ADOPTION_LOOKBACK = timedelta(minutes=15)
# Service-hours are counted in 15-minute buckets: a service that logged at all
# in a bucket is credited with the whole quarter hour.
HEALTH_BUCKET_MINUTES = 15
COMPOSITE_PAGE_SIZE = 1000
# ~73 hosts x ~15 services is a little over 1000 buckets, so two pages is the
# normal case. The cap only exists so a misbehaving server cannot loop forever.
MAX_COMPOSITE_PAGES = 50
TOP_TARGETS = 15
# Below this the old arm is too small to compare against: a handful of services
# produce rates dominated by whichever host happens to be sick.
SUSPEND_BELOW_SHARE = 5.0

BOOTSTRAP_ROLE = "BOOTSTRAP"
UNKNOWN_VERSION = "unknown"
START_MESSAGE = "ant-node starting"
CONNECTION_ERROR_PHRASE = "Connection attempt failed"
WIRE_TRAFFIC_MESSAGE = "wire traffic summary (cumulative)"
HEALTH_LEVELS = ("ERROR", "WARN")

TARGET_NODE = "ant_node"
TARGET_UPGRADE_PREFIX = "ant_node::upgrade"

# The upgrade-lifecycle messages, exactly as the state machine `track-upgrades`
# used matched them. The first two are whole messages; the rest are prefixes
# because the node interpolates versions and error detail into the tail.
EVENT_MESSAGES = (
    "New version detected, staged rollout delay calculated",
    "Staged rollout delay elapsed, ready to upgrade",
)
EVENT_PREFIXES = (
    "Starting auto-apply upgrade",
    "Successfully upgraded to version",
)
FAILURE_PREFIXES = (
    "Download failed:",
    "Signature download failed:",
    "Signature verification failed:",
    "Binary replacement failed:",
    "Backup creation failed:",
    "Failed to copy from cache",
    "CRITICAL: Replacement failed",
)

_WINDOW_RE = re.compile(r"^(\d+)(m|h)$")


class RolloutWatchError(RuntimeError):
    pass


# -- small helpers ---------------------------------------------------------
def parse_window(value: str) -> timedelta:
    """`15m`, `1h`, `6h`, `24h` -> a timedelta."""
    m = _WINDOW_RE.match(value.strip())
    if not m:
        raise RolloutWatchError(f"--window must look like 15m, 1h, 6h or 24h. Got {value!r}.")
    amount = int(m.group(1))
    if amount <= 0:
        raise RolloutWatchError("--window must be greater than zero.")
    return timedelta(minutes=amount) if m.group(2) == "m" else timedelta(hours=amount)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def checkpoint_band(share: float) -> str:
    """The rollout checkpoint the to-version share has reached."""
    if share >= 99.0:
        return "complete"
    if share >= 50.0:
        return "50–99%"
    if share >= 20.0:
        return "20–50%"
    if share >= 10.0:
        return "10–20%"
    return "none"


def version_label(version: str, to_version: str) -> str:
    if version == UNKNOWN_VERSION:
        return f"unknown (unstamped, pre-{to_version})"
    return version


def service_key(host: str, service: str) -> str:
    return f"{host}/{service}"


def _range(start: datetime, end: datetime) -> dict:
    return {"range": {TIMESTAMP_FIELD: {"gte": iso(start), "lte": iso(end)}}}


def _upgrade_target_filter() -> dict:
    return {
        "bool": {
            "should": [
                {"term": {TARGET_FIELD: TARGET_NODE}},
                {"prefix": {TARGET_FIELD: TARGET_UPGRADE_PREFIX}},
            ],
            "minimum_should_match": 1,
        }
    }


def _service_sources() -> list[dict]:
    return [
        {"host": {"terms": {"field": HOST_FIELD}}},
        {"service": {"terms": {"field": SERVICE_FIELD}}},
    ]


def _composite(after: dict | None, aggs: dict) -> dict:
    composite: dict = {"sources": _service_sources(), "size": COMPOSITE_PAGE_SIZE}
    if after:
        composite["after"] = after
    return {"services": {"composite": composite, "aggs": aggs}}


# -- query builders --------------------------------------------------------
def build_adoption_query(start: datetime, end: datetime, after: dict | None = None) -> dict:
    """Every running service with the tags on its most recent document."""
    return {
        "size": 0,
        "track_total_hits": True,
        "query": {"bool": {"filter": [_range(start, end)]}},
        "aggs": _composite(
            after,
            {
                "latest": {
                    "top_hits": {
                        "size": 1,
                        "sort": [{TIMESTAMP_FIELD: "desc"}],
                        "_source": [
                            "tag.node_version",
                            "tag.node_commit",
                            "tag.host_role",
                        ],
                    }
                }
            },
        ),
    }


def build_upgrade_events_query(start: datetime, end: datetime) -> dict:
    """The upgrade-lifecycle messages only, with their (host, service) spread.

    Restricting the query to the known messages is what keeps the aggregation
    small: `tag.target = ant_node` on its own covers most of the fleet's log
    volume.
    """
    should: list[dict] = [{"term": {MESSAGE_KEYWORD_FIELD: m}} for m in EVENT_MESSAGES]
    should += [{"prefix": {MESSAGE_KEYWORD_FIELD: p}} for p in EVENT_PREFIXES + FAILURE_PREFIXES]
    return {
        "size": 0,
        "track_total_hits": True,
        "query": {
            "bool": {
                "filter": [
                    _range(start, end),
                    _upgrade_target_filter(),
                    {"bool": {"should": should, "minimum_should_match": 1}},
                ]
            }
        },
        "aggs": {
            "messages": {
                "terms": {"field": MESSAGE_KEYWORD_FIELD, "size": 200},
                "aggs": {
                    "hosts": {
                        "terms": {"field": HOST_FIELD, "size": 200},
                        "aggs": {"services": {"terms": {"field": SERVICE_FIELD, "size": 50}}},
                    }
                },
            }
        },
    }


def build_restarts_query(start: datetime, end: datetime, after: dict | None = None) -> dict:
    return {
        "size": 0,
        "track_total_hits": True,
        "query": {
            "bool": {
                "filter": [
                    _range(start, end),
                    {"term": {TARGET_FIELD: TARGET_NODE}},
                    {"term": {MESSAGE_KEYWORD_FIELD: START_MESSAGE}},
                ]
            }
        },
        "aggs": _composite(after, {"versions": {"terms": {"field": VERSION_FIELD, "size": 10}}}),
    }


def build_service_hours_query(start: datetime, end: datetime, after: dict | None = None) -> dict:
    """Which 15-minute buckets each service logged in, at any level."""
    return {
        "size": 0,
        "track_total_hits": False,
        "query": {"bool": {"filter": [_range(start, end)]}},
        "aggs": _composite(
            after,
            {
                "active": {
                    "date_histogram": {
                        "field": TIMESTAMP_FIELD,
                        "fixed_interval": f"{HEALTH_BUCKET_MINUTES}m",
                        "min_doc_count": 1,
                    }
                }
            },
        ),
    }


def build_target_health_query(start: datetime, end: datetime) -> dict:
    """ERROR/WARN counts per target, broken down far enough to split by arm.

    Elasticsearch has no idea which arm a service is in, so the split has to
    happen here: the breakdown goes down to (target, host, service, level) and
    the arms are applied in Python.
    """
    return {
        "size": 0,
        "track_total_hits": True,
        "query": {
            "bool": {
                "filter": [
                    _range(start, end),
                    {"terms": {LEVEL_FIELD: list(HEALTH_LEVELS)}},
                ]
            }
        },
        "aggs": {
            "targets": {
                "terms": {"field": TARGET_FIELD, "size": TOP_TARGETS},
                "aggs": {
                    "hosts": {
                        "terms": {"field": HOST_FIELD, "size": 200},
                        "aggs": {
                            "services": {
                                "terms": {"field": SERVICE_FIELD, "size": 50},
                                "aggs": {"levels": {"terms": {"field": LEVEL_FIELD, "size": 5}}},
                            }
                        },
                    }
                },
            }
        },
    }


def build_phrase_query(phrase: str, start: datetime, end: datetime) -> dict:
    return {
        "size": 0,
        "track_total_hits": True,
        "query": {
            "bool": {"filter": [_range(start, end), {"match_phrase": {MESSAGE_FIELD: phrase}}]}
        },
        "aggs": {
            "hosts": {
                "terms": {"field": HOST_FIELD, "size": 200},
                "aggs": {"services": {"terms": {"field": SERVICE_FIELD, "size": 50}}},
            }
        },
    }


def build_wire_tx_query(start: datetime, end: datetime) -> dict:
    """Per-service min/max of the cumulative wire counter.

    `wire_tx_bytes` and not `udp_tx_bytes`: saorsa-transport#169 changed what
    the latter counts on the 2026-09-23 train, so across a mixed fleet the two
    arms would not be measuring the same thing.
    """
    return {
        "size": 0,
        "track_total_hits": False,
        "query": {
            "bool": {
                "filter": [
                    _range(start, end),
                    {"term": {MESSAGE_KEYWORD_FIELD: WIRE_TRAFFIC_MESSAGE}},
                    {"exists": {"field": WIRE_TX_FIELD}},
                ]
            }
        },
        "aggs": {
            "hosts": {
                "terms": {"field": HOST_FIELD, "size": 200},
                "aggs": {
                    "services": {
                        "terms": {"field": SERVICE_FIELD, "size": 50},
                        "aggs": {
                            "lo": {"min": {"field": WIRE_TX_FIELD}},
                            "hi": {"max": {"field": WIRE_TX_FIELD}},
                        },
                    }
                },
            }
        },
    }


def build_missing_version_query(start: datetime, end: datetime) -> dict:
    return {
        "size": 0,
        "track_total_hits": True,
        "query": {
            "bool": {
                "filter": [_range(start, end)],
                "must_not": [{"exists": {"field": "tag.node_version"}}],
            }
        },
        "aggs": {
            "hosts": {
                "terms": {"field": HOST_FIELD, "size": 200},
                "aggs": {"services": {"terms": {"field": SERVICE_FIELD, "size": 50}}},
            }
        },
    }


# -- response summarisers --------------------------------------------------
def _service_pairs(bucket: dict) -> list[str]:
    pairs = []
    for hb in bucket["hosts"]["buckets"]:
        host = str(hb["key"])
        for sb in hb["services"]["buckets"]:
            pairs.append(service_key(host, str(sb["key"])))
    return sorted(pairs)


def _host_service_counts(response: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for hb in response["aggregations"]["hosts"]["buckets"]:
        host = str(hb["key"])
        for sb in hb["services"]["buckets"]:
            out[service_key(host, str(sb["key"]))] = sb["doc_count"]
    return out


def summarise_adoption(pages: list[dict], to_version: str) -> dict:
    services: dict[str, dict] = {}
    for page in pages:
        for bucket in page["aggregations"]["services"]["buckets"]:
            host = str(bucket["key"]["host"])
            number = str(bucket["key"]["service"])
            hits = bucket["latest"]["hits"]["hits"]
            tag = (hits[0]["_source"].get("tag") if hits else None) or {}
            # A missing tag means the same thing as the literal `unknown` the
            # pre-#225 builds reported: this build does not stamp its version.
            version = str(tag.get("node_version") or UNKNOWN_VERSION)
            services[service_key(host, number)] = {
                "host": host,
                "service": number,
                "version": version,
                "commit": tag.get("node_commit"),
                "role": tag.get("host_role"),
            }

    total = len(services)
    grouped: dict[str, dict] = {}
    for entry in services.values():
        row = grouped.setdefault(entry["version"], {"services": 0, "hosts": set(), "bootstraps": 0})
        row["services"] += 1
        row["hosts"].add(entry["host"])
        if entry["role"] == BOOTSTRAP_ROLE:
            row["bootstraps"] += 1

    rows = [
        {
            "version": version,
            "label": version_label(version, to_version),
            "services": row["services"],
            "share": round(100.0 * row["services"] / total, 1) if total else 0.0,
            "hosts": len(row["hosts"]),
            "bootstraps": row["bootstraps"],
        }
        for version, row in grouped.items()
    ]
    rows.sort(key=lambda r: (r["version"] != to_version, -r["services"], r["version"]))

    on_target = grouped.get(to_version, {}).get("services", 0)
    share = 100.0 * on_target / total if total else 0.0
    return {
        "running_services": total,
        "running_hosts": len({e["host"] for e in services.values()}),
        "bootstrap_services": sum(1 for e in services.values() if e["role"] == BOOTSTRAP_ROLE),
        "versions": rows,
        "to_version": to_version,
        "to_version_services": on_target,
        "to_version_share": round(share, 1),
        "band": checkpoint_band(share),
        "services": services,
    }


def classify_message(message: str) -> tuple[str | None, str | None]:
    """Fold one message into the lifecycle category it reports."""
    for prefix in FAILURE_PREFIXES:
        if message.startswith(prefix):
            return "failure", prefix
    for exact in EVENT_MESSAGES:
        if message == exact:
            return "event", exact
    for prefix in EVENT_PREFIXES:
        if message.startswith(prefix):
            return "event", prefix
    return None, None


def summarise_upgrade_events(response: dict) -> dict:
    events: dict[str, int] = {}
    failures: dict[str, dict] = {}
    for bucket in response["aggregations"]["messages"]["buckets"]:
        kind, label = classify_message(str(bucket["key"]))
        if kind is None or label is None:
            continue
        if kind == "event":
            events[label] = events.get(label, 0) + bucket["doc_count"]
            continue
        entry = failures.setdefault(label, {"message": label, "count": 0, "services": []})
        entry["count"] += bucket["doc_count"]
        for pair in _service_pairs(bucket):
            if pair not in entry["services"]:
                entry["services"].append(pair)

    for entry in failures.values():
        entry["services"].sort()
    return {
        # Every category is listed even at zero: a zero here is a measurement
        # ("no node reported this"), not an absence of information.
        "events": [
            {"message": message, "count": events.get(message, 0)}
            for message in EVENT_MESSAGES + EVENT_PREFIXES
        ],
        "failures": [failures[p] for p in FAILURE_PREFIXES if p in failures],
    }


def summarise_restarts(pages: list[dict]) -> list[dict]:
    """Services that started more than once in the window.

    An upgrade restarts the service exactly once, so two starts in a window
    spanning it is the expected shape; anything above that is a loop.
    """
    out: list[dict] = []
    for page in pages:
        for bucket in page["aggregations"]["services"]["buckets"]:
            starts = bucket["doc_count"]
            if starts <= 1:
                continue
            out.append(
                {
                    "service": service_key(
                        str(bucket["key"]["host"]), str(bucket["key"]["service"])
                    ),
                    "starts": starts,
                    "versions": [str(v["key"]) for v in bucket["versions"]["buckets"]],
                    "restart_loop": starts > 2,
                }
            )
    out.sort(key=lambda r: (-r["starts"], r["service"]))
    return out


def summarise_service_hours(pages: list[dict]) -> dict[str, float]:
    hours = HEALTH_BUCKET_MINUTES / 60.0
    out: dict[str, float] = {}
    for page in pages:
        for bucket in page["aggregations"]["services"]["buckets"]:
            key = service_key(str(bucket["key"]["host"]), str(bucket["key"]["service"]))
            out[key] = len(bucket["active"]["buckets"]) * hours
    return out


def arms_for(services: dict[str, dict], to_version: str) -> dict[str, str]:
    return {
        key: ("new" if entry["version"] == to_version else "old") for key, entry in services.items()
    }


def _arm_totals(counts: dict[str, float], arms: dict[str, str]) -> dict[str, float]:
    totals = {"new": 0.0, "old": 0.0}
    for key, value in counts.items():
        arm = arms.get(key)
        # A service with no arm stopped logging before the adoption window, so
        # it is not part of either arm's denominator either.
        if arm is not None:
            totals[arm] += value
    return totals


def _per_1k(count: float, hours: float) -> float | None:
    return None if hours <= 0 else count / hours * 1000.0


def _ratio(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return new / old


def summarise_target_health(response: dict, arms: dict[str, str]) -> list[dict]:
    rows = []
    for tb in response["aggregations"]["targets"]["buckets"]:
        counts = {arm: dict.fromkeys(HEALTH_LEVELS, 0) for arm in ("new", "old")}
        for hb in tb["hosts"]["buckets"]:
            host = str(hb["key"])
            for sb in hb["services"]["buckets"]:
                arm = arms.get(service_key(host, str(sb["key"])))
                if arm is None:
                    continue
                for lb in sb["levels"]["buckets"]:
                    level = str(lb["key"])
                    if level in counts[arm]:
                        counts[arm][level] += lb["doc_count"]
        rows.append({"target": str(tb["key"]), "docs": tb["doc_count"], "counts": counts})
    return rows


def summarise_wire_tx(response: dict, arms: dict[str, str]) -> dict[str, float]:
    """Per-arm total of each service's max-minus-min cumulative wire counter.

    Taken per service and then summed: the counter is cumulative since process
    start, so a fleet-wide max minus min is meaningless across services with
    different uptimes.
    """
    deltas: dict[str, float] = {}
    for hb in response["aggregations"]["hosts"]["buckets"]:
        host = str(hb["key"])
        for sb in hb["services"]["buckets"]:
            lo, hi = sb["lo"]["value"], sb["hi"]["value"]
            if lo is None or hi is None:
                continue
            deltas[service_key(host, str(sb["key"]))] = max(hi - lo, 0.0)
    return _arm_totals(deltas, arms)


def build_per_arm(
    *,
    arms: dict[str, str],
    hours_by_service: dict[str, float],
    target_response: dict,
    phrase_response: dict,
    wire_response: dict,
) -> dict:
    hours = _arm_totals({k: float(v) for k, v in hours_by_service.items()}, arms)
    rows: list[dict] = []
    for entry in summarise_target_health(target_response, arms):
        for level in HEALTH_LEVELS:
            new = _per_1k(entry["counts"]["new"][level], hours["new"])
            old = _per_1k(entry["counts"]["old"][level], hours["old"])
            rows.append(
                {
                    "metric": f"{entry['target']} {level}/1k service-hours",
                    "new": new,
                    "old": old,
                    "ratio": _ratio(new, old),
                }
            )

    phrase = _arm_totals(
        {k: float(v) for k, v in _host_service_counts(phrase_response).items()}, arms
    )
    phrase_new = _per_1k(phrase["new"], hours["new"])
    phrase_old = _per_1k(phrase["old"], hours["old"])
    rows.append(
        {
            "metric": f"{CONNECTION_ERROR_PHRASE}/1k service-hours",
            "new": phrase_new,
            "old": phrase_old,
            "ratio": _ratio(phrase_new, phrase_old),
        }
    )

    wire = summarise_wire_tx(wire_response, arms)
    wire_new = None if hours["new"] <= 0 else wire["new"] / hours["new"]
    wire_old = None if hours["old"] <= 0 else wire["old"] / hours["old"]
    rows.append(
        {
            "metric": "wire_tx_bytes/service-hour",
            "new": wire_new,
            "old": wire_old,
            "ratio": _ratio(wire_new, wire_old),
        }
    )
    return {
        "suspended": False,
        "service_counts": {
            "new": sum(1 for a in arms.values() if a == "new"),
            "old": sum(1 for a in arms.values() if a == "old"),
        },
        "service_hours": {arm: round(value, 2) for arm, value in hours.items()},
        "rows": rows,
    }


def arm_counts(arms: dict[str, str]) -> dict[str, int]:
    return {
        "new": sum(1 for a in arms.values() if a == "new"),
        "old": sum(1 for a in arms.values() if a == "old"),
    }


def short_arm(arms: dict[str, str]) -> tuple[str, int, float]:
    """The smaller arm, with its size and share of running services.

    Whichever arm is short, the comparison says nothing: a handful of services
    produce rates dominated by whichever host happens to be sick, and an arm of
    zero produces no rate at all. Before the rollout starts that is the NEW
    arm; by the end it is the old one.
    """
    counts = arm_counts(arms)
    running = len(arms)
    arm = min(counts, key=lambda a: counts[a])
    share = 100.0 * counts[arm] / running if running else 0.0
    return arm, counts[arm], share


def suspended_per_arm(arms: dict[str, str]) -> dict:
    arm, count, share = short_arm(arms)
    return {
        "suspended": True,
        "short_arm": arm,
        "reason": (f"{arm} arm is {count} services (< {SUSPEND_BELOW_SHARE:g}% of running)"),
        "service_counts": arm_counts(arms),
        "short_arm_share": round(share, 1),
        "rows": [],
    }


# -- orchestration ---------------------------------------------------------
def _composite_pages(search, build) -> list[dict]:
    pages: list[dict] = []
    after: dict | None = None
    for _ in range(MAX_COMPOSITE_PAGES):
        response = search(build(after))
        pages.append(response)
        agg = response["aggregations"]["services"]
        after = agg.get("after_key")
        if not after or not agg.get("buckets"):
            break
    return pages


def build_report(search, *, name: str, to_version: str, window: timedelta, now: datetime) -> dict:
    start, end = now - window, now
    adoption_start = now - ADOPTION_LOOKBACK

    adoption = summarise_adoption(
        _composite_pages(search, lambda after: build_adoption_query(adoption_start, end, after)),
        to_version,
    )
    services = adoption.pop("services")
    arms = arms_for(services, to_version)

    upgrade_events = summarise_upgrade_events(search(build_upgrade_events_query(start, end)))
    restarts = summarise_restarts(
        _composite_pages(search, lambda after: build_restarts_query(start, end, after))
    )

    if short_arm(arms)[2] < SUSPEND_BELOW_SHARE:
        # Nothing to compare against, so the four health queries are not even
        # issued — they are the expensive half of the report.
        per_arm = suspended_per_arm(arms)
    else:
        per_arm = build_per_arm(
            arms=arms,
            hours_by_service=summarise_service_hours(
                _composite_pages(search, lambda after: build_service_hours_query(start, end, after))
            ),
            target_response=search(build_target_health_query(start, end)),
            phrase_response=search(build_phrase_query(CONNECTION_ERROR_PHRASE, start, end)),
            wire_response=search(build_wire_tx_query(start, end)),
        )

    missing = _arm_totals(
        {
            k: float(v)
            for k, v in _host_service_counts(
                search(build_missing_version_query(adoption_start, end))
            ).items()
        },
        arms,
    )
    commits = sorted(
        {
            str(entry["commit"])
            for key, entry in services.items()
            if arms[key] == "new" and entry.get("commit")
        }
    )
    return {
        "name": name,
        "to_version": to_version,
        "window": _window_label(window),
        "window_start": iso(start),
        "window_end": iso(end),
        "adoption_since": iso(adoption_start),
        "adoption": adoption,
        "upgrade_events": upgrade_events,
        "restarts": restarts,
        "per_arm": per_arm,
        "version_telemetry": {
            "new_arm_commits": commits,
            "missing_node_version_docs": int(missing["new"]),
        },
        "services": services,
    }


def _window_label(window: timedelta) -> str:
    seconds = int(window.total_seconds())
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


# -- rendering -------------------------------------------------------------
def _num(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:.2f}"


def render_report(result: dict) -> str:
    adoption = result["adoption"]
    lines = [
        f"# Rollout watch — {result['name']} → {result['to_version']}",
        "",
        f"Window {result['window']} ({result['window_start']} → "
        f"{result['window_end']}). Adoption measured since "
        f"{result['adoption_since']}.",
        "",
        "## 1. Adoption",
        "",
        "| version | services | share | hosts | bootstraps |",
        "| -- | -- | -- | -- | -- |",
    ]
    bootstraps_total = adoption["bootstrap_services"]
    for row in adoption["versions"]:
        lines.append(
            f"| {row['label']} | {row['services']} | {row['share']:.1f}% | "
            f"{row['hosts']} | {row['bootstraps']}/{bootstraps_total} |"
        )
    lines += [
        "",
        f"Running services: {adoption['running_services']} across "
        f"{adoption['running_hosts']} hosts "
        f"({bootstraps_total} bootstrap{'' if bootstraps_total == 1 else 's'}).",
        "",
        f"Checkpoint band: {adoption['band']}",
        "",
        "## 2. Upgrade events in the window",
        "",
        "| message | count |",
        "| -- | -- |",
    ]
    for event in result["upgrade_events"]["events"]:
        lines.append(f"| {event['message']} | {event['count']} |")
    lines.append("")
    failures = result["upgrade_events"]["failures"]
    if not failures:
        lines.append("Failures: none.")
    else:
        lines += [
            "Failures:",
            "",
            "| failure | count | services |",
            "| -- | -- | -- |",
        ]
        for failure in failures:
            lines.append(
                f"| {failure['message']} | {failure['count']} | {', '.join(failure['services'])} |"
            )
    lines += ["", "## 3. Restarts in the window", ""]
    if not result["restarts"]:
        lines.append("No service started more than once.")
    else:
        lines += [
            "| service | starts | versions seen | flag |",
            "| -- | -- | -- | -- |",
        ]
        for restart in result["restarts"]:
            flag = "restart-loop" if restart["restart_loop"] else ""
            lines.append(
                f"| {restart['service']} | {restart['starts']} | "
                f"{', '.join(restart['versions']) or '—'} | {flag} |"
            )

    per_arm = result["per_arm"]
    lines += ["", "## 4. Per-arm health", ""]
    if per_arm["suspended"]:
        lines.append(f"Per-arm comparison suspended: {per_arm['reason']}.")
    else:
        counts, hours = per_arm["service_counts"], per_arm["service_hours"]
        lines += [
            f"new arm: {counts['new']} services, {hours['new']:.2f} service-hours. "
            f"old arm: {counts['old']} services, {hours['old']:.2f} service-hours.",
            "",
            "| metric | new | old | ratio (new/old) |",
            "| -- | -- | -- | -- |",
        ]
        for row in per_arm["rows"]:
            lines.append(
                f"| {row['metric']} | {_num(row['new'])} | {_num(row['old'])} | "
                f"{_num(row['ratio'])} |"
            )

    telemetry = result["version_telemetry"]
    commits = telemetry["new_arm_commits"]
    lines += [
        "",
        "## 5. Version telemetry",
        "",
        f"- node_commit values on the new arm: {len(commits)}"
        + (f" ({', '.join(commits)})" if commits else ""),
        f"- new-arm documents since {result['adoption_since']} with "
        f"tag.node_version missing: {telemetry['missing_node_version_docs']}",
        "",
    ]
    return "\n".join(lines)


# -- command ---------------------------------------------------------------
def cmd_prod_rollout_watch(args) -> int:
    requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
    try:
        window = parse_window(args.window)
    except RolloutWatchError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    run = EsRun(args.name)
    result = build_report(
        run.search,
        name=args.name,
        to_version=args.to_version,
        window=window,
        now=datetime.now(timezone.utc),
    )
    print(render_report(result))
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(result, fh, indent=2)
        print(f"wrote {args.json_out}", file=sys.stderr)
    return 0
