"""Count named log strings in a testnet's Elasticsearch (V2-842).

Pure functions only: this module builds the query bodies and reads the
responses. The HTTP call belongs to the caller, which is what makes the traps
below testable without an Elasticsearch to hand -- and every one of them was
hit for real, each producing a confidently wrong number rather than an error.

The traps
---------

**Aggregate and term-match on ``.keyword``.** ``tag.*`` fields are analysed, so
``{"term": {"tag.level": "WARN"}}`` silently matches nothing and a
``cardinality`` on bare ``tag.host`` errors outright.

**Fields nest under the measurement name.** Telegraf writes node log documents
with the fields under ``tail``, not ``fields`` -- so the message is
``tail.message``. The index template declares a top-level ``message`` property,
but no real document has one, and querying it returns zero forever.

**Never use ``_search``'s hit count.** It defaults to 10 hits and, worse,
``hits.total`` defaults to being tracked only up to 10,000 -- so a string
occurring 71,309 times reports 10,000. ``track_total_hits`` is set explicitly.

**A count alone answers the wrong question.** Every query also takes the host
cardinality, the top hosts and the level breakdown, because a rise concentrated
on two hosts of seventy-two is a sick node (or a service restart), not a
fleet-wide regression -- and a string emitted only at DEBUG may be absent from
Elasticsearch entirely because the node-tier forwarder drops DEBUG and TRACE by
default, which is indistinguishable from the string not occurring.
"""
from __future__ import annotations

from datetime import datetime, timezone

# The forwarder's index for node logs. The state file holds telegraf's strftime
# form (`telegraf-ant-nodes-<name>-%Y.%m.%d`), which matches no index -- the
# date suffix has to be collapsed to a glob, and to a glob rather than to
# today's date so a run straddling midnight UTC still matches.
NODE_INDEX_TEMPLATE = "telegraf-ant-nodes-{name}-*"

MESSAGE_FIELD = "tail.message"
HOST_FIELD = "tag.host.keyword"
LEVEL_FIELD = "tag.level.keyword"
TIMESTAMP_FIELD = "@timestamp"

# How many hosts to name in the stored breakdown. Enough to show whether a
# count is concentrated or spread, without storing a row per host on a
# ~70-host fleet.
TOP_HOSTS = 10


def node_index_pattern(name: str) -> str:
    """The node-log index glob for a deployment name."""
    return NODE_INDEX_TEMPLATE.format(name=name.lower())


def collapse_index_date_suffix(index_pattern: str) -> str:
    """Turn telegraf's strftime index pattern into a glob that matches indices.

    The elastic S3 state records the pattern telegraf writes WITH, which
    contains ``%Y.%m.%d`` and therefore matches nothing when used as a search
    target.
    """
    if "%Y" in index_pattern:
        return index_pattern.replace("-%Y.%m.%d", "-*")
    return index_pattern


def _range_filter(
    start: datetime | None, end: datetime | None
) -> list[dict]:
    if start is None and end is None:
        return []
    bounds: dict[str, str] = {}
    if start is not None:
        bounds["gte"] = _iso(start)
    if end is not None:
        bounds["lt"] = _iso(end)
    return [{"range": {TIMESTAMP_FIELD: bounds}}]


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_count_query(
    phrase: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    """The search body counting one phrase, with its host and level breakdown.

    ``size: 0`` because the documents are not wanted -- only the count is
    recorded, and a staging run's matches run to hundreds of thousands.
    ``track_total_hits: true`` because the default caps the reported total at
    10,000, which would silently turn every large count into exactly 10,000.
    """
    return {
        "size": 0,
        "track_total_hits": True,
        "query": {
            "bool": {
                "filter": [
                    # match_phrase, not match: `Connection attempt failed` as a
                    # bag of words would also match a line saying a connection
                    # attempt succeeded after a failed something-else.
                    {"match_phrase": {MESSAGE_FIELD: phrase}},
                    *_range_filter(start, end),
                ]
            }
        },
        "aggs": {
            "host_count": {"cardinality": {"field": HOST_FIELD}},
            "top_hosts": {"terms": {"field": HOST_FIELD, "size": TOP_HOSTS}},
            "levels": {"terms": {"field": LEVEL_FIELD, "size": 10}},
            "first_seen": {"min": {"field": TIMESTAMP_FIELD}},
            "last_seen": {"max": {"field": TIMESTAMP_FIELD}},
        },
    }


def build_total_query(
    start: datetime | None = None, end: datetime | None = None
) -> dict:
    """The search body counting every forwarded node log line in the same scope.

    Recorded alongside each pattern so "more errors" can be told apart from
    "more logging". A run that logged twice as much of everything has not
    regressed on connections, and a raw count comparison cannot see the
    difference.
    """
    filters = _range_filter(start, end)
    return {
        "size": 0,
        "track_total_hits": True,
        "query": (
            {"bool": {"filter": filters}} if filters else {"match_all": {}}
        ),
        "aggs": {
            "first_seen": {"min": {"field": TIMESTAMP_FIELD}},
            "last_seen": {"max": {"field": TIMESTAMP_FIELD}},
        },
    }


def _parse_timestamp(agg: dict | None) -> datetime | None:
    """Read a min/max date aggregation.

    Elasticsearch returns the epoch-millis in ``value`` and, usually, a string
    in ``value_as_string``. The numeric field is used because it is always
    present and needs no format guessing; ``None`` when the aggregation matched
    nothing, which is what a zero-count pattern produces.
    """
    if not isinstance(agg, dict):
        return None
    value = agg.get("value")
    if value is None:
        return None
    return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)


def parse_count_response(response: dict) -> dict:
    """Read a count query's response into the fields recorded per pattern."""
    aggs = response.get("aggregations") or {}
    total = ((response.get("hits") or {}).get("total") or {})
    # `hits.total` is an object in modern Elasticsearch and a bare integer in
    # very old ones. Accept both rather than crashing on an unexpected shape at
    # the end of a five-hour run.
    count = total.get("value") if isinstance(total, dict) else total

    by_level = {
        bucket["key"]: bucket["doc_count"]
        for bucket in ((aggs.get("levels") or {}).get("buckets") or [])
    }
    by_host = {
        bucket["key"]: bucket["doc_count"]
        for bucket in ((aggs.get("top_hosts") or {}).get("buckets") or [])
    }
    host_count = (aggs.get("host_count") or {}).get("value")

    return {
        "count": int(count or 0),
        "hosts": int(host_count) if host_count is not None else None,
        "first_seen": _parse_timestamp(aggs.get("first_seen")),
        "last_seen": _parse_timestamp(aggs.get("last_seen")),
        "by_level": by_level,
        "top_hosts": by_host,
    }


# ---------------------------------------------------------------------------
# Pointer RPC latency events (V2-1339)
# ---------------------------------------------------------------------------
#
# ant-node emits one INFO event per pointer PUT and GET on a dedicated tracing
# target, carrying the outcome and the duration. They are the only quantitative
# node-side pointer signal that is not DEBUG, and they answer a question the
# client-side records cannot: whether "the client saw a stale state" is the same
# event as "a node answered stale".
#
# Telegraf's tail input captures every non-numeric JSON field, so the event name
# lands in `tail.message` and the structured fields beside it as `tail.outcome`
# and `tail.duration_ms`. `tail.*` is dynamically mapped, which gives a string a
# `.keyword` subfield -- so the outcome is aggregatable and the duration is a
# number.

#: Tracing target both events are emitted on. Filtered on as well as the message
#: so a future log line that happens to mention `pointer_put_rpc` in prose
#: cannot be counted as one.
RPC_LATENCY_TARGET = "ant_node::storage::rpc_latency"
TARGET_FIELD = "tag.target.keyword"
OUTCOME_FIELD = "tail.outcome.keyword"
DURATION_FIELD = "tail.duration_ms"

#: The two events, and the outcomes each can report. Listed so an outcome that
#: never occurred is still printed as zero -- a column that vanishes makes two
#: runs' tables incomparable by eye, and "no `stale` answers" is a finding.
POINTER_RPC_EVENTS = {
    "pointer_put_rpc": ("success", "unchanged", "stale", "payment_required", "error"),
    "pointer_get_rpc": ("success", "not_found", "error"),
}

#: Percentiles taken on the event's own `duration_ms`, matching what the client
#: side reports so the two latencies can be put beside each other.
RPC_LATENCY_PERCENTILES = (50.0, 95.0)


def build_rpc_latency_query(
    event: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    """Count one `rpc_latency` event, broken down by outcome, with latencies.

    ``size: 0`` and ``track_total_hits`` for the same reasons the phrase query
    uses them. The percentiles are taken INSIDE the outcome terms aggregation as
    well as overall: a rise in the overall p95 driven entirely by the `error`
    outcome is a different finding from one spread across successes.
    """
    return {
        "size": 0,
        "track_total_hits": True,
        "query": {
            "bool": {
                "filter": [
                    {"term": {TARGET_FIELD: RPC_LATENCY_TARGET}},
                    {"match_phrase": {MESSAGE_FIELD: event}},
                    *_range_filter(start, end),
                ]
            }
        },
        "aggs": {
            "host_count": {"cardinality": {"field": HOST_FIELD}},
            "first_seen": {"min": {"field": TIMESTAMP_FIELD}},
            "last_seen": {"max": {"field": TIMESTAMP_FIELD}},
            "latency": {
                "percentiles": {
                    "field": DURATION_FIELD,
                    "percents": list(RPC_LATENCY_PERCENTILES),
                }
            },
            "outcomes": {
                "terms": {"field": OUTCOME_FIELD, "size": 20},
                "aggs": {
                    "latency": {
                        "percentiles": {
                            "field": DURATION_FIELD,
                            "percents": list(RPC_LATENCY_PERCENTILES),
                        }
                    }
                },
            },
        },
    }


def _percentile_values(agg: dict | None) -> dict:
    """Read a `percentiles` aggregation into {50.0: ms, 95.0: ms}.

    Elasticsearch returns the keys as strings ("50.0"), and a percentile over no
    documents comes back as null -- which stays None rather than becoming zero. A
    zero latency for an event that never occurred would read as instantaneous.
    """
    values = (agg or {}).get("values") or {}
    out = {}
    for percent in RPC_LATENCY_PERCENTILES:
        raw = values.get(str(percent), values.get(percent))
        out[percent] = float(raw) if isinstance(raw, (int, float)) else None
    return out


def parse_rpc_latency_response(response: dict, event: str) -> dict:
    """Read an `rpc_latency` query's response into the stored per-outcome shape.

    Every outcome the event can report appears, at zero if it did not occur.
    """
    aggs = response.get("aggregations") or {}
    total = (response.get("hits") or {}).get("total") or {}
    count = total.get("value") if isinstance(total, dict) else total
    buckets = {
        bucket["key"]: bucket
        for bucket in ((aggs.get("outcomes") or {}).get("buckets") or [])
    }
    by_outcome = {}
    for outcome in POINTER_RPC_EVENTS.get(event, tuple(buckets)):
        bucket = buckets.pop(outcome, None)
        by_outcome[outcome] = {
            "count": int((bucket or {}).get("doc_count") or 0),
            "latency": _percentile_values((bucket or {}).get("latency")),
        }
    # An outcome this build does not know about is kept rather than dropped: it
    # means ant-node grew one, and silently discarding it would leave the
    # per-outcome counts not summing to the total.
    for outcome, bucket in buckets.items():
        by_outcome[outcome] = {
            "count": int(bucket.get("doc_count") or 0),
            "latency": _percentile_values(bucket.get("latency")),
        }
    host_count = (aggs.get("host_count") or {}).get("value")
    return {
        "event": event,
        "count": int(count or 0),
        "hosts": int(host_count) if host_count is not None else None,
        "first_seen": _parse_timestamp(aggs.get("first_seen")),
        "last_seen": _parse_timestamp(aggs.get("last_seen")),
        "latency": _percentile_values(aggs.get("latency")),
        "by_outcome": by_outcome,
    }


def rate_per_hour(count: int, start: datetime | None, end: datetime | None) -> float | None:
    """Occurrences per hour across the span the matches actually cover.

    ``None`` when the span is unknown or zero -- a rate over no elapsed time is
    undefined, and reporting it as the raw count would read as a rate.
    """
    if start is None or end is None:
        return None
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        return None
    return count * 3600.0 / seconds


def summarise_counts(
    results: dict[str, dict],
    pattern_set,
) -> list[dict]:
    """Group per-pattern results into the per-group values the gate uses.

    Applies each group's ``aggregate``: ``max`` when its members are one event
    logged at several layers (summing would double-count it), ``sum`` when they
    are distinct events sharing a gate.

    Groups with no measured member are omitted rather than reported as zero. A
    pattern that was never queried and a pattern that occurred zero times are
    very different facts, and only one of them is a measurement.
    """
    summary = []
    for group in pattern_set.groups:
        members = [
            results[p.key]
            for p in pattern_set.patterns_in_group(group.key)
            if p.key in results
        ]
        if not members:
            continue
        counts = [m["count"] for m in members]
        value = max(counts) if group.aggregate == "max" else sum(counts)
        hosts = [m["hosts"] for m in members if m.get("hosts") is not None]
        summary.append(
            {
                "group": group.key,
                "label": group.label,
                "aggregate": group.aggregate,
                "count": value,
                "hosts": max(hosts) if hosts else None,
                "members": {m_key: results[m_key]["count"] for m_key in (
                    p.key for p in pattern_set.patterns_in_group(group.key)
                    if p.key in results
                )},
            }
        )
    return summary
