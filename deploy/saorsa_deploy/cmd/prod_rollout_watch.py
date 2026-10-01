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
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

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
# ant-node's `node.rs` logs the canonical success line and the check-phase
# error under `ant_node::node`, which matches neither of the other two values —
# so until this was added those documents were filtered out of the query
# entirely and every rollout under-counted its own successes.
TARGET_NODE_MODULE = "ant_node::node"
TARGET_UPGRADE_PREFIX = "ant_node::upgrade"

# The upgrade-lifecycle messages. The first two are whole messages; the rest
# are prefixes because the node interpolates versions and error detail into the
# tail. Each matcher is a message that fires exactly once per event, so a
# document count IS an event count and no per-service dedup is needed.
#
# A start logs two lines back to back — `Starting auto-apply upgrade...`
# (`node.rs`) and `Starting auto-apply upgrade from {cur} to {new}`
# (`upgrade/apply.rs`) — so only the second is matched; the common prefix would
# count every attempt twice. A success logs `Upgrade to {} successful,
# initiating graceful shutdown` (`node.rs`) exactly once per upgraded service
# on every path. The one service per host that actually replaces the shared
# binary additionally logs `Successfully upgraded to version {}! Restarting...`
# and every other service on that host logs `Binary already upgraded to ...`
# instead, so those two are details of a success rather than successes of their
# own — replaced + reused = succeeded.
EVENT_MESSAGES = (
    "New version detected, staged rollout delay calculated",
    "Staged rollout delay elapsed, ready to upgrade",
)
ATTEMPT_PREFIX = "Starting auto-apply upgrade from"
SUCCESS_PREFIX = "Upgrade to "
SUCCESS_MARKER = "successful, initiating graceful shutdown"
REPLACED_PREFIX = "Successfully upgraded to version"
REUSED_PREFIX = "Binary already upgraded to"
# A non-zero backoff count against an empty failures table means a failure the
# matchers missed; that is the only reason this row exists.
BACKOFF_PREFIX = "Upgrade rollout delay elapsed but previous apply did not succeed"
EVENT_PREFIXES = (
    ATTEMPT_PREFIX,
    SUCCESS_PREFIX,
    REPLACED_PREFIX,
    REUSED_PREFIX,
    BACKOFF_PREFIX,
)
EVENT_LABELS = {
    ATTEMPT_PREFIX: "upgrade attempts",
    SUCCESS_PREFIX: "upgrades succeeded",
    REPLACED_PREFIX: "— of which binary replaced",
    REUSED_PREFIX: "— of which shared binary reused",
    BACKOFF_PREFIX: "retry backoff",
}
FAILURE_PREFIXES = (
    "Download failed:",
    "Signature download failed:",
    "Signature verification failed:",
    "Binary replacement failed:",
    "Backup creation failed:",
    "Failed to copy from cache",
    "CRITICAL: Replacement failed",
)

UPGRADE_ERROR_PREFIX = "Error during upgrade process:"
# The check-phase sources of `Error during upgrade process:` — ant-node
# `src/upgrade/monitor.rs` (the GitHub release check) and
# `src/upgrade/release_cache.rs` (the release cache lock). Nothing was
# attempted when one of these is logged, so they are informational and never
# feed the gate. Everything else, including a variant a future ant-node adds,
# is a failure: downgrading one is an explicit addition to this list.
TRANSIENT_ERROR_MARKERS = (
    "network error: GitHub API request failed:",
    "network error: GitHub API returned status:",
    "network error: Failed to parse releases:",
    "network error: Failed to read releases:",
    "upgrade error: Cache lock task failed:",
    "upgrade error: Failed to create release cache lock:",
    "upgrade error: Failed to acquire release cache lock:",
)
SPAWN_FAILURE_MARKER = "Failed to spawn new binary"
SPAWN_FAILURE_NOTE = "binary already replaced on disk; old version still running"

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
                {"term": {TARGET_FIELD: TARGET_NODE_MODULE}},
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
    should += [
        {"prefix": {MESSAGE_KEYWORD_FIELD: p}}
        for p in EVENT_PREFIXES + FAILURE_PREFIXES + (UPGRADE_ERROR_PREFIX,)
    ]
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


def classify_message(message: str) -> tuple[str | None, str | None, str | None]:
    """Fold one message into the lifecycle category it reports.

    Returns the kind (`event`, `failure`, `transient` or None), the label the
    kind is grouped under, and an annotation for the rows that need one.
    """
    for prefix in FAILURE_PREFIXES:
        if message.startswith(prefix):
            return "failure", prefix, None
    # Ahead of the event prefixes: the generic error line is emitted from three
    # sites and only the check-phase one is harmless.
    if message.startswith(UPGRADE_ERROR_PREFIX):
        detail = message[len(UPGRADE_ERROR_PREFIX) :].strip()
        for marker in TRANSIENT_ERROR_MARKERS:
            if detail.startswith(marker):
                return "transient", marker, None
        note = SPAWN_FAILURE_NOTE if SPAWN_FAILURE_MARKER in detail else None
        return "failure", UPGRADE_ERROR_PREFIX, note
    for exact in EVENT_MESSAGES:
        if message == exact:
            return "event", exact, None
    if message.startswith(SUCCESS_PREFIX):
        # Only the graceful-shutdown form is the canonical per-service success;
        # any other `Upgrade to ...` line is deliberately left uncounted rather
        # than inflating the success total.
        if SUCCESS_MARKER in message:
            return "event", SUCCESS_PREFIX, None
        return None, None, None
    for prefix in EVENT_PREFIXES:
        if message.startswith(prefix):
            return "event", prefix, None
    return None, None, None


def summarise_upgrade_events(response: dict, arms: dict[str, str] | None = None) -> dict:
    """The §2 tables. `arms` marks a transient error's service as recovered."""
    arms = arms or {}
    events: dict[str, int] = {}
    failures: dict[str, dict] = {}
    transient: dict[str, dict] = {}
    for bucket in response["aggregations"]["messages"]["buckets"]:
        kind, label, note = classify_message(str(bucket["key"]))
        if kind is None or label is None:
            continue
        if kind == "event":
            events[label] = events.get(label, 0) + bucket["doc_count"]
            continue
        table = failures if kind == "failure" else transient
        entry = table.setdefault(label, {"message": label, "count": 0, "services": [], "notes": []})
        entry["count"] += bucket["doc_count"]
        for pair in _service_pairs(bucket):
            if pair not in entry["services"]:
                entry["services"].append(pair)
        if note and note not in entry["notes"]:
            entry["notes"].append(note)

    for entry in failures.values():
        entry["services"].sort()
    for entry in transient.values():
        entry["services"].sort()
        # The check phase attempted nothing, so a service that is on the new
        # arm now went on to upgrade; anything else needs a human to look.
        entry["recovered"] = [p for p in entry["services"] if arms.get(p) == "new"]
        entry["follow_up"] = [p for p in entry["services"] if arms.get(p) != "new"]
    return {
        # Every category is listed even at zero: a zero here is a measurement
        # ("no node reported this"), not an absence of information.
        "events": [
            {
                "message": message,
                "label": EVENT_LABELS.get(message, message),
                "count": events.get(message, 0),
            }
            for message in EVENT_MESSAGES + EVENT_PREFIXES
        ],
        "failures": [
            failures[p] for p in FAILURE_PREFIXES + (UPGRADE_ERROR_PREFIX,) if p in failures
        ],
        "transient": [transient[m] for m in TRANSIENT_ERROR_MARKERS if m in transient],
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

    upgrade_events = summarise_upgrade_events(search(build_upgrade_events_query(start, end)), arms)
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
        "| event | count |",
        "| -- | -- |",
    ]
    counts: dict[str, int] = {}
    for event in result["upgrade_events"]["events"]:
        counts[event["message"]] = event["count"]
        lines.append(f"| {event['label']} | {event['count']} |")
    replaced = counts.get(REPLACED_PREFIX, 0)
    reused = counts.get(REUSED_PREFIX, 0)
    succeeded = counts.get(SUCCESS_PREFIX, 0)
    # Every success is either the one service per host that replaced the shared
    # binary or one that reused it, so the two details must add up to it.
    mismatch = "" if replaced + reused == succeeded else " — **does not add up**"
    lines += [
        "",
        f"Detail check: {replaced} replaced + {reused} reused = {replaced + reused}, "
        f"against {succeeded} succeeded{mismatch}.",
        "",
    ]
    failures = result["upgrade_events"]["failures"]
    if not failures:
        lines.append("Failures: none.")
    else:
        lines += [
            "Failures (the only §2 table that breaches the rollout gate):",
            "",
            "| failure | count | services | note |",
            "| -- | -- | -- | -- |",
        ]
        for failure in failures:
            lines.append(
                f"| {failure['message']} | {failure['count']} | "
                f"{', '.join(failure['services'])} | {', '.join(failure['notes']) or '—'} |"
            )
    lines.append("")
    transient = result["upgrade_events"]["transient"]
    if not transient:
        lines.append("Transient check errors: none.")
    else:
        lines += [
            "Transient check errors (the check phase failed, nothing was attempted; "
            "informational, not a gate breach):",
            "",
            "| error | count | services | status |",
            "| -- | -- | -- | -- |",
        ]
        for entry in transient:
            status = (
                "all recovered"
                if not entry["follow_up"]
                else f"follow up: {', '.join(entry['follow_up'])}"
            )
            lines.append(
                f"| {entry['message']} | {entry['count']} | "
                f"{', '.join(entry['services'])} | {status} |"
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


# -- pretty rendering ------------------------------------------------------
# Presentation only. Every number below is read out of the same `result` dict
# the markdown renderer reads, so the two renderings cannot disagree: the
# markdown remains the form that gets pasted into an issue, this one is for
# reading in a terminal.
def _ratio_style(value: float | None) -> str:
    """Highlight a per-arm divergence.

    Deliberately not a verdict: `wire_tx_bytes/service-hour` moving is a
    traffic measurement rather than a fault, so the colour marks how far the
    arms are apart and leaves which direction is good to the reader.
    """
    if value is None:
        return "dim"
    if value >= 1.25:
        return "bold red"
    if value >= 1.05:
        return "yellow"
    if value <= 0.8:
        return "cyan"
    return ""


def _version_style(version: str, to_version: str) -> str:
    if version == to_version:
        return "green"
    if version == UNKNOWN_VERSION:
        return "dim"
    return "yellow"


def _table(*columns: str | tuple[str, str], title: str | None = None) -> Table:
    """Numbers right, text left.

    A column is right-aligned unless it is given as `(name, "left")` — a list
    of service names right-aligned against a count reads as a column of
    ragged text.

    Nothing is `no_wrap`: a §4 metric name runs to 74 characters
    (`webrtc::peer_connection::peer_connection_internal ERROR/1k
    service-hours`), and pinning the first column at its full width on an
    80-column terminal left rich nothing to give the number columns — they
    came out one character wide, so the measurements, which are the point of
    the table, were invisible. Let the names wrap and the numbers always fit.
    """
    table = Table(title=title, title_justify="left", box=box.SQUARE, header_style="bold")
    for index, column in enumerate(columns):
        name, justify = column if isinstance(column, tuple) else (column, "right")
        table.add_column(name, justify="left" if index == 0 else justify)
    return table


def _esc(value) -> str:
    """Escape a value that came out of Elasticsearch.

    Rich reads `[...]` in a cell as markup, and every message, target and
    version in this report is node log text: an upgrade error carrying
    `[2/3]`, or any other square bracket, would otherwise be swallowed as a
    style tag or raise outright, losing exactly the detail the failures table
    exists to show.
    """
    return escape(str(value))


def _section(console: Console, heading: str) -> None:
    console.print()
    console.print(Rule(f"[bold]{heading}[/bold]", align="left", style="dim"))
    console.print()


def render_report_pretty(result: dict, console: Console | None = None) -> None:
    """Print the report as terminal tables instead of markdown.

    Reads the same `result` dict `render_report` reads and recomputes nothing,
    so `--pretty` can only ever change how the report looks.
    """
    console = console or Console()
    adoption = result["adoption"]
    to_version = result["to_version"]

    console.print()
    console.print(
        Panel(
            f"[bold]{_esc(result['name'])}[/bold] → "
            f"[bold cyan]{_esc(to_version)}[/bold cyan]\n"
            f"Window {result['window']}  "
            f"[dim]{result['window_start']} → {result['window_end']}[/dim]\n"
            f"Adoption since [dim]{result['adoption_since']}[/dim]",
            title="[bold]rollout watch[/bold]",
            title_align="left",
            expand=False,
            padding=(0, 2),
        )
    )

    _section(console, "1. Adoption")
    bootstraps_total = adoption["bootstrap_services"]
    table = _table("version", "services", "share", "hosts", "bootstraps")
    for row in adoption["versions"]:
        style = _version_style(str(row["version"]), to_version)
        table.add_row(
            _esc(row["label"]),
            str(row["services"]),
            f"{row['share']:.1f}%",
            str(row["hosts"]),
            f"{row['bootstraps']}/{bootstraps_total}",
            style=style,
        )
    console.print(table)
    plural = "" if bootstraps_total == 1 else "s"
    console.print(
        f"Running services: [bold]{adoption['running_services']}[/bold] across "
        f"{adoption['running_hosts']} hosts ({bootstraps_total} bootstrap{plural})."
    )
    console.print(f"Checkpoint band: [bold]{_esc(adoption['band'])}[/bold]")

    _section(console, "2. Upgrade events in the window")
    counts: dict[str, int] = {}
    table = _table("event", "count")
    for event in result["upgrade_events"]["events"]:
        counts[event["message"]] = event["count"]
        table.add_row(_esc(event["label"]), str(event["count"]))
    console.print(table)

    replaced = counts.get(REPLACED_PREFIX, 0)
    reused = counts.get(REUSED_PREFIX, 0)
    succeeded = counts.get(SUCCESS_PREFIX, 0)
    adds_up = replaced + reused == succeeded
    verdict = "[green]ok[/green]" if adds_up else "[bold red]DOES NOT ADD UP[/bold red]"
    console.print(
        f"Detail check: {replaced} replaced + {reused} reused = {replaced + reused}, "
        f"against {succeeded} succeeded — {verdict}"
    )

    console.print()
    failures = result["upgrade_events"]["failures"]
    if not failures:
        console.print("Failures: [green]none[/green].")
    else:
        table = _table(
            "failure",
            "count",
            ("services", "left"),
            ("note", "left"),
            title=(
                "[bold red]Failures[/bold red] [dim](the only §2 table that breaches "
                "the rollout gate)[/dim]"
            ),
        )
        for failure in failures:
            table.add_row(
                _esc(failure["message"]),
                str(failure["count"]),
                _esc(", ".join(failure["services"])),
                _esc(", ".join(failure["notes"])) or "—",
                style="red",
            )
        console.print(table)

    console.print()
    transient = result["upgrade_events"]["transient"]
    if not transient:
        console.print("Transient check errors: [green]none[/green].")
    else:
        table = _table(
            "error",
            "count",
            ("services", "left"),
            ("status", "left"),
            title=(
                "Transient check errors [dim](the check phase failed, nothing was "
                "attempted; informational, not a gate breach)[/dim]"
            ),
        )
        for entry in transient:
            recovered = not entry["follow_up"]
            status = (
                "[green]all recovered[/green]"
                if recovered
                else f"[yellow]follow up: {_esc(', '.join(entry['follow_up']))}[/yellow]"
            )
            table.add_row(
                _esc(entry["message"]),
                str(entry["count"]),
                _esc(", ".join(entry["services"])),
                status,
            )
        console.print(table)

    _section(console, "3. Restarts in the window")
    if not result["restarts"]:
        console.print("No service started more than once.")
    else:
        table = _table("service", "starts", ("versions seen", "left"), ("flag", "left"))
        for restart in result["restarts"]:
            looping = restart["restart_loop"]
            table.add_row(
                _esc(restart["service"]),
                str(restart["starts"]),
                _esc(", ".join(restart["versions"])) or "—",
                "[bold red]restart-loop[/bold red]" if looping else "",
                style="red" if looping else "",
            )
        console.print(table)

    _section(console, "4. Per-arm health")
    per_arm = result["per_arm"]
    if per_arm["suspended"]:
        console.print(f"[yellow]Per-arm comparison suspended:[/yellow] {_esc(per_arm['reason'])}.")
    else:
        arm_counts_, hours = per_arm["service_counts"], per_arm["service_hours"]
        console.print(
            f"new arm: [bold]{arm_counts_['new']}[/bold] services, "
            f"{hours['new']:.2f} service-hours.    "
            f"old arm: [bold]{arm_counts_['old']}[/bold] services, "
            f"{hours['old']:.2f} service-hours."
        )
        console.print()
        table = _table("metric", "new", "old", "ratio (new/old)")
        for row in per_arm["rows"]:
            table.add_row(
                _esc(row["metric"]),
                _num(row["new"]),
                _num(row["old"]),
                f"[{_ratio_style(row['ratio'])}]{_num(row['ratio'])}[/]"
                if _ratio_style(row["ratio"])
                else _num(row["ratio"]),
            )
        console.print(table)

    _section(console, "5. Version telemetry")
    telemetry = result["version_telemetry"]
    commits = telemetry["new_arm_commits"]
    console.print(
        f"node_commit values on the new arm: [bold]{len(commits)}[/bold]"
        + (f" ({_esc(', '.join(commits))})" if commits else "")
    )
    missing = telemetry["missing_node_version_docs"]
    console.print(
        f"new-arm documents since {result['adoption_since']} with tag.node_version "
        f"missing: " + (f"[yellow]{missing}[/yellow]" if missing else "[green]0[/green]")
    )
    console.print()


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
    if getattr(args, "pretty", False):
        render_report_pretty(result)
    else:
        print(render_report(result))
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(result, fh, indent=2)
        print(f"wrote {args.json_out}", file=sys.stderr)
    return 0
