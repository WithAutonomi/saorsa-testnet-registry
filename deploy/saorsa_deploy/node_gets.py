"""The node fleet's own chunk GETs for a set of chunks, from Elasticsearch.

Every native-path chunk GET on every node emits one INFO event ``get_rpc`` on
the ``ant_node::storage::rpc_latency`` target, carrying the ``request``
(``get`` | ``get_or_closer``), the ``chunk_address``, the ``storage_outcome``
and the ``duration_ms``. Telegraf's tail input lifts those structured fields
into ``tail.*``, so the event name is ``tail.message`` and the rest are
``tail.request``, ``tail.chunk_address``, ``tail.storage_outcome`` and
``tail.duration_ms``.

This is the node side of the ADR-0026 swarm question: a swarm that forms means
the readers serve each other and ask the nodes for less, so the figure that
answers it is how many GETs the FLEET served for the hot file's chunks in a
round. `analysis swarm-reads` takes that per round and compares the swarm arm's
median against the control arm's.

Three facts the query shape rests on, each of which would otherwise produce a
confident zero:

* **``tag.target`` is filtered as well as the message**, exactly as
  :func:`es_log_counts.build_rpc_latency_query` does, so a future log line that
  happens to mention ``get_rpc`` in prose cannot be counted as one.
* **``tag.*`` is analysed and ``tail.*`` is dynamically mapped**, so a terms
  filter or aggregation needs ``.keyword`` on both. ``tail.duration_ms`` is a
  number and does not.
* **The range is half-open (``lt``)**, because a round's window is
  ``[round_id x period, (round_id + 1) x period)`` and a GET stamped exactly on
  a boundary belongs to one round, not to two.

**The browser listener emits no per-GET event of its own** (it only counts
``get_service_count`` in its 300 s summary), so these documents are native-path
GETs: each cohort's native readers plus the deployment's own native downloaders,
which draw random files and almost never touch the hot file. That is what makes
the per-round count a like-for-like figure between a swarm arm's native readers
and a control arm's.

Node documents are stamped by each node's own clock. The staging profile skews
one node VM's clock, whose documents fall outside their true round and are
simply lost to the per-round count — a known, constant error that applies to
both arms alike.
"""

from __future__ import annotations

from saorsa_deploy.es_log_counts import (
    HOST_FIELD,
    MESSAGE_FIELD,
    RPC_LATENCY_TARGET,
    TARGET_FIELD,
    TIMESTAMP_FIELD,
    check_search_complete,
)

#: The event name, as it lands in ``tail.message``.
GET_RPC_EVENT = "get_rpc"

#: ``tail.*`` fields the event carries, with ``.keyword`` on the strings.
CHUNK_ADDRESS_FIELD = "tail.chunk_address.keyword"
REQUEST_FIELD = "tail.request.keyword"
STORAGE_OUTCOME_FIELD = "tail.storage_outcome.keyword"
DURATION_FIELD = "tail.duration_ms"

#: The outcome that means the node actually served the record. The gate's
#: numerator, because a `not_found` costs the node a lookup and the reader a
#: round trip but serves no bytes — and a swarm cannot relieve a node of a GET
#: it answered with nothing.
STORAGE_OUTCOME_SUCCESS = "success"

#: The two request kinds ant-node distinguishes. Listed so one that never
#: occurred is still reported as zero: a column that vanishes makes two rounds'
#: tables incomparable by eye.
GET_REQUESTS = ("get", "get_or_closer")

#: Percentiles on the served GETs' own `duration_ms`.
GET_LATENCY_PERCENTILES = (50.0, 95.0)

#: Host buckets per round. The per-host figures are summed across the rounds in
#: Python rather than re-queried over the window, so this has to be larger than
#: the fleet rather than the ten the report prints — a truncated bucket list
#: would silently under-count a host.
HOST_TERMS_SIZE = 500

#: Request/outcome bucket sizes. Both vocabularies are closed and tiny; the
#: headroom is for a variant a future ant-node adds, which must appear rather
#: than be dropped.
REQUEST_TERMS_SIZE = 10
OUTCOME_TERMS_SIZE = 20


def get_rpc_query_body(chunks, start: str, end: str) -> dict:
    """The search body for one round's ``get_rpc`` documents over ``chunks``.

    ``size: 0`` and ``track_total_hits``: the figure is a count, and the hit
    count caps at 10,000 while the tracked total does not.

    The chunk set is a ``terms`` filter rather than a per-chunk aggregation. The
    report's question is how many GETs the fleet served for THE FILE, so the
    file's record set is one filter; at a few hundred addresses it is well
    inside ``index.max_terms_count`` (65,536).
    """
    return {
        "size": 0,
        "track_total_hits": True,
        "query": {
            "bool": {
                "filter": [
                    {"term": {TARGET_FIELD: RPC_LATENCY_TARGET}},
                    {"match_phrase": {MESSAGE_FIELD: GET_RPC_EVENT}},
                    {"terms": {CHUNK_ADDRESS_FIELD: sorted(chunks)}},
                    {"range": {TIMESTAMP_FIELD: {"gte": start, "lt": end}}},
                ]
            }
        },
        "aggs": {
            "requests": {"terms": {"field": REQUEST_FIELD, "size": REQUEST_TERMS_SIZE}},
            "storage_outcomes": {
                "terms": {"field": STORAGE_OUTCOME_FIELD, "size": OUTCOME_TERMS_SIZE},
                "aggs": {
                    "latency": {
                        "percentiles": {
                            "field": DURATION_FIELD,
                            "percents": list(GET_LATENCY_PERCENTILES),
                        }
                    }
                },
            },
            "hosts": {"terms": {"field": HOST_FIELD, "size": HOST_TERMS_SIZE}},
        },
    }


def _buckets(aggs: dict, name: str) -> list[dict]:
    return (aggs.get(name) or {}).get("buckets") or []


def _percentile(agg: dict | None, percent: float) -> float | None:
    """One value out of a ``percentiles`` aggregation, or None.

    Elasticsearch keys the values by their string form ("50.0"), and a
    percentile over no documents comes back as null — which stays None rather
    than becoming zero. A zero latency for a GET that never happened would read
    as instantaneous.
    """
    values = (agg or {}).get("values") or {}
    raw = values.get(str(percent), values.get(percent))
    return float(raw) if isinstance(raw, (int, float)) else None


def parse_get_rpc_response(response: dict) -> dict:
    """One round's ``get_rpc`` response as the report's per-round figures.

    ``served`` is the ``success`` outcome's count, and the latencies are that
    bucket's own percentiles — taken inside the outcome terms aggregation rather
    than overall, because a `not_found` is answered from the index alone and
    folding its latency in would flatter the served figure.

    Every kind in :data:`GET_REQUESTS` appears at zero when it did not occur;
    the outcomes are reported as they came back, since that vocabulary is
    ant-node's and a variant it adds must be visible rather than dropped.

    **A partial answer is refused before anything is read off it.** This count
    is G3's numerator and denominator, so a short one does not look wrong — it
    changes a ratio and the gate's verdict with it. See
    :func:`es_log_counts.check_search_complete`.
    """
    check_search_complete(response, f"`{GET_RPC_EVENT}`")
    aggs = response.get("aggregations") or {}
    total = (response.get("hits") or {}).get("total") or {}
    count = total.get("value") if isinstance(total, dict) else total
    by_request = {kind: 0 for kind in GET_REQUESTS}
    for bucket in _buckets(aggs, "requests"):
        by_request[bucket["key"]] = bucket["doc_count"]
    by_outcome: dict[str, int] = {}
    served_p50 = served_p95 = None
    for bucket in _buckets(aggs, "storage_outcomes"):
        by_outcome[bucket["key"]] = bucket["doc_count"]
        if bucket["key"] == STORAGE_OUTCOME_SUCCESS:
            served_p50 = _percentile(bucket.get("latency"), GET_LATENCY_PERCENTILES[0])
            served_p95 = _percentile(bucket.get("latency"), GET_LATENCY_PERCENTILES[1])
    return {
        "total": int(count or 0),
        "by_request": by_request,
        "by_storage_outcome": by_outcome,
        "served": by_outcome.get(STORAGE_OUTCOME_SUCCESS, 0),
        "served_p50_ms": served_p50,
        "served_p95_ms": served_p95,
        "by_host": {b["key"]: b["doc_count"] for b in _buckets(aggs, "hosts")},
    }


def fetch_round_gets(run, chunks, start: str, end: str, *, index: str | None = None) -> dict:
    """Query and parse one round's ``get_rpc`` figures.

    ``run`` is an :class:`~saorsa_deploy.analysis.es.EsRun`; only ``search`` and
    ``node_index`` are used, so every caller above stays testable against a
    stub.
    """
    body = get_rpc_query_body(chunks, start, end)
    return parse_get_rpc_response(run.search(body, index or run.node_index()))


def top_hosts(per_round: list[dict], limit: int = 10) -> list[dict]:
    """The busiest hosts over every round, summed from the per-round buckets.

    Summed rather than re-queried over the whole window: the per-round buckets
    already carry every host (:data:`HOST_TERMS_SIZE` is well above any fleet),
    so one more query would only be a second chance to disagree with the rounds
    the report prints.
    """
    totals: dict[str, int] = {}
    for entry in per_round:
        for host, count in (entry.get("by_host") or {}).items():
            totals[host] = totals.get(host, 0) + count
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"host": host, "gets": count} for host, count in ranked[:limit]]
