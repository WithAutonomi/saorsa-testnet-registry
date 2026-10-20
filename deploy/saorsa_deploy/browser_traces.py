"""The browser cohort's trace records in Elasticsearch, and the per-chunk
arithmetic over them (V2-1650).

The harness appends one JSON object per client event to
``/var/log/ant-client-forward/browser-trace.jsonl``, telegraf ships it to the
run's own Elasticsearch, and this module reads it back. That replaced
``clients fetch-browser-traces``, which serially ``scp``-ed a file per download
off every VM: 2 h 47 min for 2,676 files on DEV-01 (id 685), long enough to push
that run past its ceiling and leave the node fleet filling its disks.

**Why the raw records and not the derived ``first_reads`` column.** The harness
derives one holder per chunk per download on the VM, and it records the peer the
first GET was *sent to*. On id 685 that was a bootstrap seed answering
``NotFound`` for 1,260 of 1,260 downloads of the file-address chunk, and for
96 % of the content-chunk first GETs — so the gate was measuring where
speculative misses landed, not where reads were served. Counted against the peer
that answered ``found``, one holder served over 25 % of first reads on 41 of 51
chunks (median 34 %, worst 90 %). That is the quantity ADR-0023 section 1 is
about, and it can only be computed with every record in hand.

Four facts about the documents, each of which would produce a confident zero if
got wrong:

* **Telegraf nests a metric's fields under its measurement name.** The trace
  input sets ``name_override = "browser_trace"``, so the fields are
  ``browser_trace.target`` and so on — never bare ``target``, which is the same
  trap as ``tail.message`` on the node index.
* **``operation`` and ``phase`` are TAGS**, so they live under ``tag.*`` and are
  analysed: a ``term`` query needs ``.keyword``.
* **A chunk GET is an ``rpc`` with a non-empty ``target``.** The word "GET"
  appears in no record, and the ``start`` detail is empty — the outcome is on
  the matching ``finish`` (``found N bytes`` / ``NotFound { .. }``).
* **``seq`` is unique within a download and is the page key.** Paging on
  ``[round_id, host, instance, seq]`` is only safe because of that; the harness
  gives the header record 0 and the events 1..N for exactly this reason.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from saorsa_deploy.es_log_counts import HOST_FIELD

#: The measurement, and so the object the fields nest under in Elasticsearch.
TRACE_MEASUREMENT = "browser_trace"
#: Telegraf tags, under `tag.*` and analysed -- hence `.keyword`.
ROLE_FIELD = "tag.role.keyword"
STREAM_FIELD = "tag.stream.keyword"
OPERATION_FIELD = "tag.operation.keyword"

TRACE_ROLE = "browser_downloader"
TRACE_STREAM = "trace"

OP_RPC = "rpc"
OP_DISCOVERY = "discovery"
PHASE_START = "start"
PHASE_FINISH = "finish"

#: A `found N bytes` finish proves the peer holds the chunk; a `NotFound { .. }`
#: finish is the close group saying it does not. Matched on the prefix, in
#: Python: the detail is free text, `NotFound` carries a 32-element array that
#: takes it past any keyword `ignore_above`, and ES cannot express "starts with"
#: on an analysed field without a wildcard.
DETAIL_FOUND = "found"
DETAIL_NOT_FOUND = "NotFound"

#: Documents per page. `search_after` rather than a scroll, as
#: :meth:`EsRun.hits` does, so a caller that dies leaves no server-side context.
#: 5,000 keeps a 600k-document window to ~120 round trips while staying inside
#: the default `index.max_result_window`.
TRACE_PAGE_SIZE = 5000

#: The fields the report needs, and no others: a trace document also carries the
#: seed list and the file address on every line, which at ~600 bytes each would
#: be most of the bytes transferred for nothing.
TRACE_SOURCE_FIELDS = (
    f"{TRACE_MEASUREMENT}.round_id",
    f"{TRACE_MEASUREMENT}.instance",
    f"{TRACE_MEASUREMENT}.target",
    f"{TRACE_MEASUREMENT}.peer",
    f"{TRACE_MEASUREMENT}.detail",
    f"{TRACE_MEASUREMENT}.seq",
    "tag.host",
    "tag.operation",
    "tag.phase",
)

TRACE_SORT = [
    {f"{TRACE_MEASUREMENT}.round_id": "asc"},
    {HOST_FIELD: "asc"},
    {f"{TRACE_MEASUREMENT}.instance": "asc"},
    {f"{TRACE_MEASUREMENT}.seq": "asc"},
]


class BrowserTraceError(Exception):
    """The trace records cannot be read, so the per-chunk report cannot exist."""


def trace_query_body(
    round_ids: list[int],
    *,
    page_size: int = TRACE_PAGE_SIZE,
    search_after: list | None = None,
) -> dict:
    """The search body for one page of a window's ``rpc`` and ``discovery`` records.

    **Both operations, not ``rpc`` alone.** ``decided_before_discovery`` asks
    whether the first read that reached a holder was sent before discovery
    finished for that chunk, and the only record carrying that moment is the
    ``discovery`` ``finish``. Querying ``rpc`` alone would make that column
    read "every read was early" on every run, which is also the answer id 685
    actually gave — indistinguishable from the column being broken.

    The window is expressed as the round ids themselves rather than as a time
    range: a round's identity is its boundary, the rows in the window are what
    define it, and a document's own ``@timestamp`` is when telegraf read the
    line — which on a clock-skewed VM is not when the round ran.
    """
    body: dict[str, Any] = {
        "size": page_size,
        "track_total_hits": False,
        "query": {
            "bool": {
                "filter": [
                    {"term": {ROLE_FIELD: TRACE_ROLE}},
                    {"term": {STREAM_FIELD: TRACE_STREAM}},
                    {"terms": {OPERATION_FIELD: [OP_RPC, OP_DISCOVERY]}},
                    {"terms": {f"{TRACE_MEASUREMENT}.round_id": sorted(round_ids)}},
                ]
            }
        },
        "_source": list(TRACE_SOURCE_FIELDS),
        "sort": TRACE_SORT,
    }
    if search_after:
        body["search_after"] = search_after
    return body


def normalise_trace_doc(source: dict) -> dict | None:
    """One ES ``_source`` as a flat record, or ``None`` if it is not usable.

    Returns None rather than raising on a document missing any of the four keys
    that identify a record's place in a download: ``index.mapping.ignore_malformed``
    is on for this index, so a field whose value did not fit its mapping is
    DROPPED and the document still arrives. A record with no ``seq`` cannot be
    ordered and a record with no round cannot be attributed, so it is counted as
    unusable and reported rather than silently treated as seq 0 of round 0.
    """
    trace = source.get(TRACE_MEASUREMENT) or {}
    tags = source.get("tag") or {}
    round_id, instance, seq = trace.get("round_id"), trace.get("instance"), trace.get("seq")
    host = tags.get("host")
    if round_id is None or instance is None or seq is None or not host:
        return None
    return {
        "round_id": int(round_id),
        "instance": int(instance),
        "seq": int(seq),
        "host": host,
        "operation": tags.get("operation") or "",
        "phase": tags.get("phase") or "",
        "target": trace.get("target") or "",
        "peer": trace.get("peer") or "",
        "detail": str(trace.get("detail") or ""),
    }


def fetch_trace_docs(
    run,
    round_ids: list[int],
    *,
    page_size: int = TRACE_PAGE_SIZE,
    index: str | None = None,
) -> tuple[list[dict], int]:
    """Every ``rpc``/``discovery`` record for the window, and the unusable count.

    ``run`` is an :class:`~saorsa_deploy.analysis.es.EsRun`; only ``search`` and
    ``client_index`` are used, so the arithmetic above stays testable with a
    stub.
    """
    if not round_ids:
        return [], 0
    target_index = index or run.client_index()
    out: list[dict] = []
    dropped = 0
    search_after: list | None = None
    while True:
        body = trace_query_body(round_ids, page_size=page_size, search_after=search_after)
        page = run.search(body, target_index)["hits"]["hits"]
        if not page:
            break
        for hit in page:
            record = normalise_trace_doc(hit.get("_source") or {})
            if record is None:
                dropped += 1
                continue
            out.append(record)
        search_after = page[-1].get("sort")
        if len(page) < page_size or not search_after:
            break
    return out, dropped


def _is_chunk_read(doc: dict) -> bool:
    """A chunk GET: an ``rpc`` with a non-empty ``target``.

    The target is filled with the chunk hex only for a ``GetRequest``; every
    other ``rpc`` leaves it empty. Matching the detail finds nothing — it is
    empty on ``start``.
    """
    return doc["operation"] == OP_RPC and bool(doc["target"])


def _download_key(doc: dict) -> tuple[int, str, int]:
    return (doc["round_id"], doc["host"], doc["instance"])


def holders_by_chunk(docs: list[dict]) -> dict[str, set[str]]:
    """For each chunk, every peer that answered ``found`` for it in the window.

    Over the WHOLE window, not per download: a peer that served the chunk to one
    browser holds it for all of them, and a per-download holder set would make
    the first read of an early download unattributable.
    """
    holders: dict[str, set[str]] = defaultdict(set)
    for doc in docs:
        if (
            _is_chunk_read(doc)
            and doc["phase"] == PHASE_FINISH
            and doc["detail"].startswith(DETAIL_FOUND)
            and doc["peer"]
        ):
            holders[doc["target"]].add(doc["peer"])
    return dict(holders)


def _per_download(docs: list[dict]) -> dict[tuple[int, str, int], dict[str, dict]]:
    """Per download, per chunk: the GETs, the NotFounds and discovery's finish."""
    out: dict[tuple[int, str, int], dict[str, dict]] = defaultdict(dict)
    for doc in sorted(docs, key=lambda d: (_download_key(d), d["seq"])):
        target = doc["target"]
        if not target:
            continue
        if doc["operation"] == OP_DISCOVERY:
            if doc["phase"] != PHASE_FINISH:
                continue
            entry = out[_download_key(doc)].setdefault(target, _blank_entry())
            if entry["discovery_seq"] is None:
                entry["discovery_seq"] = doc["seq"]
            continue
        if not _is_chunk_read(doc):
            continue
        entry = out[_download_key(doc)].setdefault(target, _blank_entry())
        if doc["phase"] == PHASE_START:
            entry["gets"] += 1
            entry["starts"].append((doc["seq"], doc["peer"]))
        elif doc["phase"] == PHASE_FINISH and doc["detail"].startswith(DETAIL_NOT_FOUND):
            entry["not_found"] += 1
    return out


def _blank_entry() -> dict:
    return {"gets": 0, "not_found": 0, "starts": [], "discovery_seq": None}


def _known_holders_by_chunk(rows) -> dict[str, dict[str, int]]:
    """``known_holders`` per chunk, still from the recorded Postgres rows.

    The trace carries the count only in a ``discovery`` ``finish`` detail, and
    the harness already resolves it per download with the amended V2-1617
    definition (the count for the whole download, ``null`` when none was
    reported). Recomputing it here would be a second definition to keep in step
    with the first.
    """
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for row in rows:
        reads = (
            row.get("first_reads") if isinstance(row, dict) else getattr(row, "first_reads", None)
        )
        for read in reads or []:
            if not isinstance(read, dict):
                continue
            chunk = read.get("chunk")
            if not chunk:
                continue
            known = read.get("known_holders")
            key = "null" if known is None else str(known)
            out[chunk][key] = out[chunk].get(key, 0) + 1
    return dict(out)


def summarise_chunks(docs: list[dict], rows, *, min_first_reads: int) -> dict:
    """Per-chunk spreading, by **the holder that served the read**.

    * A **holder** of chunk ``t`` is a peer that answered ``found`` for ``t``
      anywhere in the window.
    * A download's **first read** of ``t`` is its first ``rpc start`` for ``t``,
      in ``seq`` order, whose peer is a holder. A GET to a non-holder is a
      speculative miss; counting it as the read is what made the old gate
      measure the bootstrap seeds.

    Returns ``{"chunks": [...], "chunks_without_a_holder": [...]}``. The second
    list is chunks every GET of which went to a peer that never answered
    ``found`` — no first read, so nothing to gate, but a chunk no peer in the
    fleet admitted holding is worth seeing rather than silently absent.
    """
    holders = holders_by_chunk(docs)
    per_download = _per_download(docs)

    agg: dict[str, dict] = defaultdict(
        lambda: {
            "first_reads": defaultdict(int),
            "gets": [],
            "not_found": [],
            "first_get_to_holder": 0,
            "downloads": 0,
            "decided_before_discovery": 0,
            "first_read_downloads": 0,
        }
    )
    for chunks in per_download.values():
        for target, entry in chunks.items():
            held = holders.get(target) or set()
            a = agg[target]
            if entry["gets"]:
                a["downloads"] += 1
                a["gets"].append(entry["gets"])
                a["not_found"].append(entry["not_found"])
                if entry["starts"][0][1] in held:
                    a["first_get_to_holder"] += 1
            first = next((s for s in entry["starts"] if s[1] in held), None)
            if first is None:
                continue
            a["first_reads"][first[1]] += 1
            a["first_read_downloads"] += 1
            # No discovery finish for this chunk in this download counts as
            # "decided before discovery": the read did not wait for a result
            # that never came. On id 685 discovery was cancelled for 82% of
            # chunk reads because the early read had already succeeded.
            if entry["discovery_seq"] is None or first[0] < entry["discovery_seq"]:
                a["decided_before_discovery"] += 1

    known = _known_holders_by_chunk(rows)
    chunks: list[dict] = []
    orphans: list[dict] = []
    for target, a in agg.items():
        reads = sum(a["first_reads"].values())
        summary = {
            "chunk": target,
            "downloads": a["downloads"],
            "gets_per_download": statistics.mean(a["gets"]) if a["gets"] else None,
            "not_found_per_download": (statistics.mean(a["not_found"]) if a["not_found"] else None),
            "first_get_to_holder": (
                a["first_get_to_holder"] / a["downloads"] if a["downloads"] else None
            ),
        }
        if not reads:
            # A chunk only ever DISCOVERED, never GET, is neither gated nor an
            # orphan: nothing read it, so there is nothing to say about who
            # served it.
            if a["downloads"]:
                orphans.append(summary)
            continue
        busiest_holder, busiest_reads = max(a["first_reads"].items(), key=lambda kv: (kv[1], kv[0]))
        chunks.append(
            {
                **summary,
                "reads": reads,
                "distinct_holders": len(a["first_reads"]),
                "holders_answering_found": len(holders.get(target) or set()),
                "busiest_holder": busiest_holder,
                "busiest_holder_reads": busiest_reads,
                "busiest_holder_share": busiest_reads / reads,
                "decided_before_discovery": (
                    a["decided_before_discovery"] / a["first_read_downloads"]
                ),
                "known_holders": known.get(target, {}),
                "gated": reads >= min_first_reads,
            }
        )
    chunks.sort(key=lambda c: (-c["busiest_holder_share"], -c["reads"], c["chunk"]))
    orphans.sort(key=lambda c: (-c["downloads"], c["chunk"]))
    return {"chunks": chunks, "chunks_without_a_holder": orphans}
