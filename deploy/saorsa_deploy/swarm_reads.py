"""The ADR-0026 swarm report's arithmetic, over two cohorts of one deployment.

ADR-0026 is the peer swarm: readers of the same file share its records with each
other over WebRTC data channels, with the nodes introducing them and staying the
last seed. The DEV-01 run asks two things about it on a staging-shape fleet — do
swarms form among readers on public cloud VMs, and do the nodes serve fewer GETs
for a hot file when they do — and it asks them with **two `kind: clients`
extensions on one deployment**, reading one hot file in 10-minute rounds:

* the **swarm** arm: native hot-file readers on the branch `ant` plus browsers on
  the branch bundle, on `round_parity: even`;
* the **control** arm: native hot-file readers on a released `ant`, on
  `round_parity: odd`.

So the comparison is between alternate rounds of one deployment against one
file, which is what makes it a comparison at all: the same fleet, the same
chunk set, the same hour. The arm of a round is therefore its **parity** — even
is the swarm's, odd is the control's — and the rows of an arm are the rows
carrying that extension's name.

Pure functions over row-shaped inputs, like :mod:`saorsa_deploy.browser_reads`,
so every section and every gate is testable against a committed fixture with no
database, no S3 and no Elasticsearch. The command around it
(:mod:`saorsa_deploy.analysis.swarm_reads`) does the loading, the rendering and
the exit code.

Three rules run through all of it:

* **A ``skipped`` row is excluded from every denominator and counted
  separately.** A round that ran before the operator selected a hot file, or one
  a reader was still busy for, is part of the series — but it read nothing, so
  counting it would make a cohort's completion a function of how early it was
  provisioned.
* **A round whose own arm produced no row is MISSING, not zero.** It is named in
  the report and excluded from the gates: a round nobody ran is not a round that
  failed, and folding it in as a 0 would drag the arm's medians toward a number
  no reader ever measured.
* **An off-parity row is counted and excluded, never folded in.** A cohort
  configured `round_parity: all` runs every round, so its rows land on the other
  arm's rounds too — and an odd round is the CONTROL's measurement. Including
  the swarm cohort's reads there would contaminate exactly the baseline the
  ratio divides by.
"""

from __future__ import annotations

from datetime import datetime, timezone

from saorsa_deploy.browser_reads import (
    STATUS_FAILURE,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    as_utc,
    boundary_of,
    row_status,
)
from saorsa_deploy.browser_traces import (
    DETAIL_FOUND,
    DETAIL_NOT_FOUND,
    OP_RPC,
    PHASE_FINISH,
)
from saorsa_deploy.client_roles import (
    HOT_FILE_PARITY_EVEN,
    HOT_FILE_PARITY_ODD,
)
from saorsa_deploy.gate_comparison import percentile
from saorsa_deploy.node_gets import top_hosts

ARM_SWARM = "swarm"
ARM_CONTROL = "control"

#: Which parity each arm owns. The swarm cohort runs the even rounds and the
#: control cohort the odd ones, which is what `round_parity` on each tier
#: configures — so these two facts have to agree, and :func:`check_parities`
#: refuses the run when they do not.
ARM_PARITY = {ARM_SWARM: HOT_FILE_PARITY_EVEN, ARM_CONTROL: HOT_FILE_PARITY_ODD}

GATE_PASS = "PASS"
GATE_FAIL = "FAIL"
GATE_WARN = "WARN"
GATE_NOT_ASSESSED = "NOT ASSESSED"

#: Below this many distinct chunk addresses the trace has not described a file.
#: A 300 MiB file is hundreds of records, so three is not a floor anyone meets
#: by accident — it is the line between "the traces describe the file" and "the
#: traces describe nothing", and the report refuses rather than divide a
#: fleet-wide GET count by a chunk set that is really one stray address.
MIN_CHUNK_SET = 3

#: G3 needs a median on each side, and a median of one round is that round. Two
#: is the fewest that makes the comparison a comparison rather than a pair of
#: single observations.
MIN_ROUNDS_FOR_RATIO = 2

#: The four swarm counters the report sums and takes a per-row median of. Named
#: here rather than spelled out at each site so the two cohorts' tables cannot
#: come to carry different columns.
PEER_COUNTER_COLUMNS = (
    "records_from_peers",
    "bytes_from_peers",
    "records_served",
    "bytes_served",
)

#: The discovery counters the report totals, out of the twenty-five on the row.
#: These seven are the ones that say whether the introduction mechanism worked:
#: how many announces were made, refused or timed out, how many offers were made
#: and delivered, and how many links were proven or failed. A test asserts every
#: one is still a column on the record, so a rename cannot leave the report
#: reading None forever.
DISCOVERY_TOTAL_COLUMNS = (
    "discovery_announces",
    "discovery_announces_refused",
    "discovery_announces_timed_out",
    "discovery_offers_made",
    "discovery_offers_delivered",
    "discovery_links_proven",
    "discovery_links_failed",
)

#: Where each chunk of a browser's download came from. Browser rows only — the
#: native reader has no equivalent, since the figure is parsed out of the
#: bundle's own progress messages.
CHUNK_SOURCE_COLUMNS = (
    "chunks_total",
    "chunks_from_nodes",
    "chunks_from_cache",
    "chunks_from_readers",
)

#: `verify_status` as the reader records it: 0 ok, 1 mismatch, 2 nothing to
#: check against. 2 is NOT a failure — a selection line that published no
#: SHA-256 leaves nothing to verify, which is not the same as a mismatch.
VERIFY_OK = 0
VERIFY_MISMATCH = 1
VERIFY_UNCHECKED = 2


class SwarmReadsError(RuntimeError):
    """The report cannot be produced. Carries the operator-facing message."""


def _field(row, name):
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


def arm_of_round(round_id: int) -> str:
    """The arm a round belongs to: even rounds are the swarm's, odd the control's.

    ``abs`` on the remainder for the same reason
    :func:`browser_records.entry_owns_round` uses it — a negative round id (a
    boundary before the epoch) is nonsense here but must not silently swap the
    arms if it ever arrives.
    """
    return ARM_CONTROL if abs(int(round_id) % 2) == 1 else ARM_SWARM


def window_round_ids(period_secs: int, window_start: datetime, window_end: datetime) -> list[int]:
    """Every round id whose boundary lies in ``[start, end)``.

    Enumerated from the window rather than collected from the rows, because a
    round that produced NO row is exactly the one the report has to be able to
    name. Half-open, so two adjacent windows partition a run's rounds exactly
    once between them.
    """
    if period_secs <= 0:
        raise SwarmReadsError(f"the round period must be positive; got {period_secs}.")
    first = -(-int(as_utc(window_start).timestamp()) // period_secs)
    last = -(-int(as_utc(window_end).timestamp()) // period_secs)
    return list(range(first, last))


def check_parities(entries, swarm_extension: str, control_extension: str) -> None:
    """Refuse a cohort whose recorded parity contradicts its arm.

    A swarm host on `odd` or a control host on `even` is not a misconfiguration
    the report can work around: the arm of a round is its parity, so such a host
    reads on the rounds the OTHER arm's figures are taken from and every number
    in the report would describe a blend of the two. Named individually, because
    a cohort is several VMs and the operator has to know which one to fix.

    A host on `all` is allowed — it runs both parities, and its off-parity rows
    are counted and excluded (see the module docstring) rather than silently
    mixed in.
    """
    wrong: list[str] = []
    for entry in entries or []:
        extension = entry.get("extension")
        parity = entry.get("round_parity")
        if extension == swarm_extension and parity == HOT_FILE_PARITY_ODD:
            wrong.append(f"{entry.get('hostname')} ({swarm_extension}) is round_parity=odd")
        elif extension == control_extension and parity == HOT_FILE_PARITY_EVEN:
            wrong.append(f"{entry.get('hostname')} ({control_extension}) is round_parity=even")
    if wrong:
        raise SwarmReadsError(
            "the recorded round parities contradict the arms: the swarm arm is "
            "the EVEN rounds and the control arm the ODD ones, so "
            + "; ".join(sorted(wrong))
            + ". The two cohorts' figures would describe a blend of both arms, "
            "so no report is produced."
        )


def _sum(rows, column: str) -> int:
    return sum(int(_field(r, column) or 0) for r in rows)


def _median(rows, column: str) -> float | None:
    values = [float(_field(r, column)) for r in rows if _field(r, column) is not None]
    return percentile(values, 0.50)


def _duration_percentiles(rows) -> tuple[float | None, float | None]:
    durations = [
        float(_field(r, "total_ms"))
        for r in rows
        if row_status(r) == STATUS_SUCCESS and _field(r, "total_ms") is not None
    ]
    return percentile(durations, 0.50), percentile(durations, 0.95)


def _error_kinds(rows) -> dict[str, int]:
    kinds: dict[str, int] = {}
    for row in rows:
        if row_status(row) != STATUS_FAILURE:
            continue
        kind = _field(row, "error_kind") or "unknown"
        kinds[kind] = kinds.get(kind, 0) + 1
    return kinds


def _completion(success: int, failure: int) -> float | None:
    """Successes over attempts, or None when nothing was attempted.

    None, never 0.0: a round every row of which was skipped has no completion
    rate, and reporting 0% would fail the gate for a cohort that was never asked
    to do anything.
    """
    attempted = success + failure
    if not attempted:
        return None
    return success / attempted


def _swarm_presence(rows) -> dict[str, int]:
    """``swarm_present`` counted by value, with NULL as its own bucket.

    The column exists so a zero can be read: a released `ant` prints no `swarm`
    block at all, so its rows carry zeros throughout, and without the flag that
    is indistinguishable from a swarm that formed and shared nothing. NULL is
    neither — it is a row recorded before the flag, or one whose result object
    could not be read — so it is never collapsed into `false`.
    """
    out = {"true": 0, "false": 0, "null": 0}
    for row in rows:
        value = _field(row, "swarm_present")
        out["null" if value is None else ("true" if value else "false")] += 1
    return out


def summarise_cohort(rows, *, chunk_sources: bool = False) -> dict:
    """One cohort's figures over the rows handed in.

    The rows are already narrowed by the caller — to an arm, and usually to a
    round — so this does no selection of its own beyond excluding the skips from
    the denominators.

    **The peer share's denominator is the ATTEMPTED rows**, not the successful
    ones. A read that failed may still have taken records from a peer, and a
    read that never got going cannot have: so the question "did a swarm form for
    this reader" is asked of every reader that tried.
    """
    success = sum(1 for r in rows if row_status(r) == STATUS_SUCCESS)
    failure = sum(1 for r in rows if row_status(r) == STATUS_FAILURE)
    skipped = sum(1 for r in rows if row_status(r) == STATUS_SKIPPED)
    attempted = [r for r in rows if row_status(r) != STATUS_SKIPPED]
    with_peers = sum(1 for r in attempted if int(_field(r, "records_from_peers") or 0) >= 1)
    p50, p95 = _duration_percentiles(rows)
    out = {
        "rows": len(rows),
        "attempted": len(attempted),
        "success": success,
        "failure": failure,
        "skipped": skipped,
        "completion": _completion(success, failure),
        "error_kinds": _error_kinds(rows),
        "verify_mismatch": sum(1 for r in rows if _field(r, "verify_status") == VERIFY_MISMATCH),
        "verify_ok": sum(1 for r in rows if _field(r, "verify_status") == VERIFY_OK),
        "verify_unchecked": sum(1 for r in rows if _field(r, "verify_status") == VERIFY_UNCHECKED),
        "rows_with_peers": with_peers,
        "peer_share": (with_peers / len(attempted)) if attempted else None,
        "sums": {column: _sum(attempted, column) for column in PEER_COUNTER_COLUMNS},
        "medians": {column: _median(attempted, column) for column in PEER_COUNTER_COLUMNS},
        "discovery": {column: _sum(attempted, column) for column in DISCOVERY_TOTAL_COLUMNS},
        "swarm_present": _swarm_presence(rows),
        "p50_ms": p50,
        "p95_ms": p95,
    }
    if chunk_sources:
        out["chunk_sources"] = {
            "sums": {column: _sum(attempted, column) for column in CHUNK_SOURCE_COLUMNS},
            "medians": {column: _median(attempted, column) for column in CHUNK_SOURCE_COLUMNS},
        }
    return out


def _by_round(rows) -> dict[int, list]:
    out: dict[int, list] = {}
    for row in rows:
        round_id = _field(row, "round_id")
        if round_id is None:
            continue
        out.setdefault(int(round_id), []).append(row)
    return out


def summarise_rounds(
    round_ids: list[int],
    *,
    swarm_native,
    swarm_browser,
    control_native,
    gets_by_round: dict[int, dict],
    period_secs: int,
) -> list[dict]:
    """One entry per round in the window, oldest first.

    Every round id in the window appears, including the ones nobody ran: such a
    round carries ``missing: True`` and is excluded from the gates. The rows
    belonging to the OTHER arm's cohort are counted in ``off_parity`` and are in
    none of the figures.
    """
    swarm_native_by_round = _by_round(swarm_native)
    swarm_browser_by_round = _by_round(swarm_browser)
    control_native_by_round = _by_round(control_native)

    out: list[dict] = []
    for round_id in sorted(round_ids):
        arm = arm_of_round(round_id)
        if arm == ARM_SWARM:
            native = swarm_native_by_round.get(round_id, [])
            browser = swarm_browser_by_round.get(round_id, [])
            off_parity = len(control_native_by_round.get(round_id, []))
        else:
            native = control_native_by_round.get(round_id, [])
            browser = []
            off_parity = len(swarm_native_by_round.get(round_id, [])) + len(
                swarm_browser_by_round.get(round_id, [])
            )
        gets = gets_by_round.get(round_id)
        out.append(
            {
                "round_id": round_id,
                "boundary": boundary_of(round_id, period_secs).isoformat(),
                "arm": arm,
                "missing": not native and not browser,
                "hosts": len({_field(r, "client_hostname") for r in (*native, *browser)}),
                "native": summarise_cohort(native),
                "browser": summarise_cohort(browser, chunk_sources=True),
                "gets": gets,
                "off_parity_rows": off_parity,
            }
        )
    return out


def summarise_arm(
    arm: str,
    round_summaries: list[dict],
    *,
    native,
    browser,
) -> dict:
    """One arm's figures over its own rounds in the window.

    ``native`` and ``browser`` are the arm's rows ALREADY restricted to the
    rounds of its own parity (the caller does that, so the off-parity rows it
    excluded can be reported rather than quietly dropped).

    The served-GET and duration medians are taken over the arm's PRESENT rounds
    only, which is the rule the module docstring states: a round nobody ran is
    not a round that served nothing.
    """
    mine = [r for r in round_summaries if r["arm"] == arm]
    present = [r for r in mine if not r["missing"]]
    served = [r["gets"]["served"] for r in present if r["gets"] is not None]
    totals = [r["gets"]["total"] for r in present if r["gets"] is not None]
    return {
        "arm": arm,
        "parity": ARM_PARITY[arm],
        "rounds": [r["round_id"] for r in mine],
        "rounds_present": [r["round_id"] for r in present],
        "rounds_missing": [r["round_id"] for r in mine if r["missing"]],
        "native": summarise_cohort(native),
        "browser": summarise_cohort(browser, chunk_sources=True),
        "served_gets_median": percentile([float(v) for v in served], 0.50),
        "total_gets_median": percentile([float(v) for v in totals], 0.50),
        "served_gets_by_round": [
            {"round_id": r["round_id"], "served": r["gets"]["served"] if r["gets"] else None}
            for r in present
        ],
        # The control arm runs a released binary with no swarm at all, so every
        # swarm counter on its rows is expected to be 0 with `swarm_present`
        # false — and a non-zero there is a finding, not a figure. Flagged so
        # the renderer can say which it is looking at rather than leaving a
        # reader to infer it from the extension name.
        "swarm_expected": arm == ARM_SWARM,
        "unexpected_swarm_rows": (
            0
            if arm == ARM_SWARM
            else sum(
                1
                for r in native
                if int(_field(r, "records_from_peers") or 0) >= 1 or _field(r, "swarm_present")
            )
        ),
    }


def chunk_set_from_traces(docs, round_ids) -> dict:
    """The hot file's record set, as the browser traces saw it.

    Every ``rpc`` record with a non-empty ``target`` is a chunk GET, and the
    target is the chunk's hex — so the distinct targets over the SWARM rounds are
    the file's records, its root DataMap chunk included. The word "GET" appears
    in no record and the ``start`` detail is empty, so there is nothing else to
    match on.

    Taken from the swarm rounds alone because that is where the browsers are:
    the control arm is native-only and emits no trace at all. The set is the
    file's, not an arm's, so using it to filter the fleet's GETs on both arms is
    exactly like-for-like.
    """
    wanted = {int(r) for r in round_ids}
    chunks: set[str] = set()
    contributing: set[int] = set()
    for doc in docs:
        if doc.get("operation") != OP_RPC or not doc.get("target"):
            continue
        if int(doc["round_id"]) not in wanted:
            continue
        chunks.add(doc["target"])
        contributing.add(int(doc["round_id"]))
    return {
        "chunks": sorted(chunks),
        "count": len(chunks),
        "from_rounds": sorted(contributing),
    }


def summarise_browser_gets(docs, round_ids) -> dict:
    """The browsers' own node GETs: ``found`` and ``NotFound`` finishes.

    The outcome of a browser's GET is on the ``finish`` record, never the
    ``start``: a ``found N bytes`` detail is the peer answering that it holds the
    chunk, a ``NotFound { .. }`` is it saying it does not. Both are counted per
    DOWNLOAD — a round's figure divided by the downloads that produced a trace —
    because the count scales with the cohort and a bare total would move with
    the number of browsers rather than with the read path.

    Swarm rounds only, since the control arm runs no browser.
    """
    wanted = {int(r) for r in round_ids}
    per_round: dict[int, dict] = {}
    for doc in docs:
        round_id = int(doc["round_id"])
        if round_id not in wanted:
            continue
        if doc.get("operation") != OP_RPC or not doc.get("target"):
            continue
        entry = per_round.setdefault(round_id, {"found": 0, "not_found": 0, "downloads": set()})
        entry["downloads"].add((doc["host"], doc["instance"]))
        if doc.get("phase") != PHASE_FINISH:
            continue
        detail = doc.get("detail") or ""
        if detail.startswith(DETAIL_FOUND):
            entry["found"] += 1
        elif detail.startswith(DETAIL_NOT_FOUND):
            entry["not_found"] += 1

    rounds = []
    for round_id in sorted(per_round):
        entry = per_round[round_id]
        downloads = len(entry["downloads"])
        rounds.append(
            {
                "round_id": round_id,
                "downloads": downloads,
                "found": entry["found"],
                "not_found": entry["not_found"],
                "found_per_download": (entry["found"] / downloads) if downloads else None,
                "not_found_per_download": (entry["not_found"] / downloads) if downloads else None,
            }
        )
    downloads = sum(r["downloads"] for r in rounds)
    found = sum(r["found"] for r in rounds)
    not_found = sum(r["not_found"] for r in rounds)
    return {
        "by_round": rounds,
        "downloads": downloads,
        "found": found,
        "not_found": not_found,
        "found_per_download": (found / downloads) if downloads else None,
        "not_found_per_download": (not_found / downloads) if downloads else None,
    }


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def _gate(name: str, status: str, figure: str, threshold: str, detail: str) -> dict:
    return {
        "name": name,
        "status": status,
        "figure": figure,
        "threshold": threshold,
        "detail": detail,
    }


def _g1(arms: dict, min_completion: float) -> dict:
    """G1 — completion, three figures, all of which must pass.

    The two arms' native readers and the swarm arm's browsers. A figure over a
    cohort that attempted nothing is NOT ASSESSED rather than 0%: a deployment
    with no browser tier has no browser completion, and failing it would be the
    report inventing a cohort.
    """
    figures = [
        (f"{ARM_SWARM} native", arms[ARM_SWARM]["native"]["completion"]),
        (f"{ARM_CONTROL} native", arms[ARM_CONTROL]["native"]["completion"]),
        (f"{ARM_SWARM} browser", arms[ARM_SWARM]["browser"]["completion"]),
    ]
    assessed = [(label, value) for label, value in figures if value is not None]
    failing = [(label, value) for label, value in assessed if value < min_completion]
    rendered = ", ".join(
        f"{label} {'n/a' if value is None else f'{value:.2%}'}" for label, value in figures
    )
    if not assessed:
        return _gate(
            "G1 completion",
            GATE_NOT_ASSESSED,
            rendered,
            f">= {min_completion:.2%} in each",
            "no cohort attempted a read in the window",
        )
    return _gate(
        "G1 completion",
        GATE_FAIL if failing else GATE_PASS,
        rendered,
        f">= {min_completion:.2%} in each",
        (
            "below the floor: " + ", ".join(f"{label} {value:.2%}" for label, value in failing)
            if failing
            else f"{len(assessed)} of 3 figure(s) assessed, all at or above the floor"
        ),
    )


def _g2(round_summaries: list[dict], min_peer_share: float) -> dict:
    """G2 — a swarm formed in EVERY swarm round, for natives and for browsers.

    Per round, not over the window: a cohort whose swarm failed to form for one
    round and recovered has still had an outage, and a window-wide share hides
    it. A round of the swarm parity that is missing, or whose cohort attempted
    nothing, has no share and is not assessed.
    """
    breaches: list[str] = []
    assessed = 0
    worst: tuple[float, str] | None = None
    for entry in round_summaries:
        if entry["arm"] != ARM_SWARM or entry["missing"]:
            continue
        for kind in ("native", "browser"):
            share = entry[kind]["peer_share"]
            if share is None:
                continue
            assessed += 1
            label = f"round {entry['round_id']} {kind}"
            if worst is None or share < worst[0]:
                worst = (share, label)
            if share < min_peer_share:
                breaches.append(f"{label} {share:.1%}")
    if not assessed:
        return _gate(
            "G2 swarms form",
            GATE_NOT_ASSESSED,
            "no swarm round had an attempted read",
            f">= {min_peer_share:.1%} of rows with a peer record, every swarm round",
            "the swarm arm produced no assessable round in the window",
        )
    return _gate(
        "G2 swarms form",
        GATE_FAIL if breaches else GATE_PASS,
        f"worst {worst[1]} at {worst[0]:.1%}",
        f">= {min_peer_share:.1%} of rows with a peer record, every swarm round",
        (
            f"{assessed} figure(s) assessed; {len(breaches)} below the floor: "
            + ", ".join(breaches)
            if breaches
            else f"{assessed} figure(s) assessed, all at or above the floor"
        ),
    )


def _g3(arms: dict, max_get_ratio: float) -> dict:
    """G3 — the nodes are relieved: swarm served GETs ÷ control served GETs.

    The whole point of ADR-0026 on the node side, and the one gate that needs
    both arms. NOT ASSESSED below :data:`MIN_ROUNDS_FOR_RATIO` present rounds on
    either side, and NOT ASSESSED when the control arm served none: a ratio over
    a zero denominator is not 0, it is a measurement that did not happen, and
    reporting it as a pass would be the report certifying relief it never saw.
    """
    swarm, control = arms[ARM_SWARM], arms[ARM_CONTROL]
    swarm_rounds = len(swarm["rounds_present"])
    control_rounds = len(control["rounds_present"])
    threshold = f"<= {max_get_ratio:.2f} x the control arm"
    if swarm_rounds < MIN_ROUNDS_FOR_RATIO or control_rounds < MIN_ROUNDS_FOR_RATIO:
        return _gate(
            "G3 nodes relieved",
            GATE_NOT_ASSESSED,
            f"{swarm_rounds} swarm round(s), {control_rounds} control round(s)",
            threshold,
            f"needs at least {MIN_ROUNDS_FOR_RATIO} present round(s) in each arm; "
            "a median of one round is that round",
        )
    swarm_median = swarm["served_gets_median"]
    control_median = control["served_gets_median"]
    if swarm_median is None or control_median is None:
        return _gate(
            "G3 nodes relieved",
            GATE_NOT_ASSESSED,
            "no served-GET median on at least one arm",
            threshold,
            "the fleet's `get_rpc` documents could not be counted for every "
            "round, so there is nothing to divide",
        )
    if not control_median:
        return _gate(
            "G3 nodes relieved",
            GATE_NOT_ASSESSED,
            f"swarm {swarm_median:,.1f}, control {control_median:,.1f}",
            threshold,
            "the control arm served no GET for the file, so there is no load to "
            "be relieved of — check that the control readers ran and that the "
            "node log forwarder was up",
        )
    ratio = swarm_median / control_median
    return _gate(
        "G3 nodes relieved",
        GATE_FAIL if ratio > max_get_ratio else GATE_PASS,
        f"{ratio:.3f} (swarm {swarm_median:,.1f} / control {control_median:,.1f})",
        threshold,
        f"median served `get_rpc` for the file over {swarm_rounds} swarm and "
        f"{control_rounds} control round(s)",
    )


def _g4(arms: dict) -> dict:
    """G4 — the swarm is no slower. Reported, never part of the verdict.

    A swarm that forms and relieves the nodes while making the read slower is
    worth seeing, but it is not what the run is gating on and no threshold for
    it has been calibrated — so this renders PASS or WARN and the verdict
    ignores it.
    """
    swarm = arms[ARM_SWARM]["native"]["p50_ms"]
    control = arms[ARM_CONTROL]["native"]["p50_ms"]
    threshold = "<= the control arm's p50 (advisory)"
    if swarm is None or control is None:
        return _gate(
            "G4 no slower",
            GATE_NOT_ASSESSED,
            f"swarm {'n/a' if swarm is None else f'{swarm:,.0f} ms'}, "
            f"control {'n/a' if control is None else f'{control:,.0f} ms'}",
            threshold,
            "a p50 needs a successful native read in the arm",
        )
    return _gate(
        "G4 no slower",
        GATE_PASS if swarm <= control else GATE_WARN,
        f"swarm {swarm:,.0f} ms vs control {control:,.0f} ms ({(swarm / control - 1) * 100:+.1f}%)",
        threshold,
        "reported only: the verdict ignores G4, and no threshold for it has been calibrated",
    )


def evaluate_gates(
    arms: dict,
    round_summaries: list[dict],
    *,
    min_completion: float,
    min_peer_share: float,
    max_get_ratio: float,
) -> list[dict]:
    """G1–G4 in order, each with its figure, threshold and verdict."""
    return [
        _g1(arms, min_completion),
        _g2(round_summaries, min_peer_share),
        _g3(arms, max_get_ratio),
        _g4(arms),
    ]


def verdict_of(gates: list[dict]) -> str:
    """FAIL when any of G1–G3 failed, else PASS.

    G4 is excluded by NAME rather than by position, so reordering the list
    cannot quietly promote it into the verdict.
    """
    gating = [g for g in gates if not g["name"].startswith("G4")]
    return GATE_FAIL if any(g["status"] == GATE_FAIL for g in gating) else GATE_PASS


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def hot_file_rounds(rows, hot_files: dict | None) -> list[dict]:
    """The selected hot files, and for each the rounds that read it.

    Driven by the selection document rather than by the rows, so a file that was
    selected and never read shows as a file with no rounds — a finding, rather
    than an absence.
    """
    by_index: dict[int, set] = {}
    for row in rows:
        index = _field(row, "hot_file_index")
        if index is None:
            continue
        by_index.setdefault(int(index), set()).add(_field(row, "round_id"))
    out: list[dict] = []
    for entry in (hot_files or {}).get("files") or []:
        index = entry.get("index")
        rounds = sorted(by_index.get(index, set()))
        out.append(
            {
                "index": index,
                "address": entry.get("address"),
                "bytes": entry.get("bytes"),
                "sha256": entry.get("sha256"),
                "rounds": rounds,
                "round_count": len(rounds),
            }
        )
    return out


def cohort_hosts(entries, extension: str) -> list[dict]:
    """One extension's recorded VMs: hostname, parity, instances and period."""
    out = [
        {
            "hostname": entry.get("hostname"),
            "region": entry.get("region"),
            "instances": entry.get("instances"),
            "round_parity": entry.get("round_parity"),
            "round_period_secs": entry.get("round_period_secs"),
        }
        for entry in entries or []
        if entry.get("extension") == extension
    ]
    out.sort(key=lambda e: e["hostname"] or "")
    return out


def rows_of_arm(rows, arm: str) -> list:
    """An arm's rows restricted to the rounds of its own parity.

    What makes the off-parity rows reportable: the caller keeps both lists and
    the difference between them is the count the report prints.
    """
    return [r for r in rows if arm_of_round(_field(r, "round_id")) == arm]


def build_report(
    *,
    deployment: str,
    deployment_id: int | None,
    swarm_extension: str,
    control_extension: str,
    swarm_native,
    swarm_browser,
    control_native,
    ignored_rows: int,
    config_entries,
    browser_config_entries,
    hot_files: dict | None,
    hot_files_source: str,
    chunk_set: dict,
    gets_by_round: dict[int, dict],
    trace_docs,
    period_secs: int,
    period_source: str,
    window_start: datetime,
    window_end: datetime,
    min_completion: float,
    min_peer_share: float,
    max_get_ratio: float,
) -> dict:
    """The whole report as one JSON-ready document.

    Every row list must already be narrowed to the window by the caller, so the
    "nothing in the window" error can name the window it looked in. ``chunk_set``
    and ``gets_by_round`` likewise come in computed, because one is read from the
    browser traces and the other is a query per round.
    """
    round_ids = window_round_ids(period_secs, window_start, window_end)
    round_summaries = summarise_rounds(
        round_ids,
        swarm_native=swarm_native,
        swarm_browser=swarm_browser,
        control_native=control_native,
        gets_by_round=gets_by_round,
        period_secs=period_secs,
    )
    arms = {
        ARM_SWARM: summarise_arm(
            ARM_SWARM,
            round_summaries,
            native=rows_of_arm(swarm_native, ARM_SWARM),
            browser=rows_of_arm(swarm_browser, ARM_SWARM),
        ),
        ARM_CONTROL: summarise_arm(
            ARM_CONTROL,
            round_summaries,
            native=rows_of_arm(control_native, ARM_CONTROL),
            browser=[],
        ),
    }
    gates = evaluate_gates(
        arms,
        round_summaries,
        min_completion=min_completion,
        min_peer_share=min_peer_share,
        max_get_ratio=max_get_ratio,
    )
    swarm_round_ids = [r["round_id"] for r in round_summaries if r["arm"] == ARM_SWARM]
    all_rows = [*swarm_native, *swarm_browser, *control_native]
    return {
        "deployment": deployment,
        "deployment_id": deployment_id,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "round_period_secs": period_secs,
        "round_period_source": period_source,
        "extensions": {
            ARM_SWARM: {
                "name": swarm_extension,
                "parity": ARM_PARITY[ARM_SWARM],
                "hosts": cohort_hosts(config_entries, swarm_extension),
                "browser_hosts": cohort_hosts(browser_config_entries, swarm_extension),
            },
            ARM_CONTROL: {
                "name": control_extension,
                "parity": ARM_PARITY[ARM_CONTROL],
                "hosts": cohort_hosts(config_entries, control_extension),
                "browser_hosts": cohort_hosts(browser_config_entries, control_extension),
            },
        },
        "hot_files": {
            # Where the selection was read from, for the same reason
            # `round_period_source` is reported: the registry snapshot and the
            # live S3 object can differ, so a reader must be able to see which
            # one this report describes.
            "source": hot_files_source,
            "selected_at": (hot_files or {}).get("selected_at"),
            "size_bytes": (hot_files or {}).get("size_bytes"),
            "files": hot_file_rounds(all_rows, hot_files),
        },
        "chunk_set": chunk_set,
        "totals": {
            "rounds": len(round_summaries),
            "rounds_missing": sum(1 for r in round_summaries if r["missing"]),
            "swarm_native_rows": len(swarm_native),
            "swarm_browser_rows": len(swarm_browser),
            "control_native_rows": len(control_native),
            "ignored_rows": ignored_rows,
            "off_parity_rows": sum(r["off_parity_rows"] for r in round_summaries),
            "trace_records": len(trace_docs),
        },
        "rounds": round_summaries,
        "arms": arms,
        "node_gets": {
            "by_round": [
                {"round_id": r["round_id"], "arm": r["arm"], **(r["gets"] or {})}
                for r in round_summaries
                if r["gets"] is not None
            ],
            "top_hosts": _top_hosts(round_summaries),
        },
        "browser_gets": summarise_browser_gets(trace_docs, swarm_round_ids),
        "gates": gates,
        "verdict": verdict_of(gates),
        "thresholds": {
            "min_completion": min_completion,
            "min_peer_share": min_peer_share,
            "max_get_ratio": max_get_ratio,
            "min_chunk_set": MIN_CHUNK_SET,
            "min_rounds_for_ratio": MIN_ROUNDS_FOR_RATIO,
        },
    }


def _top_hosts(round_summaries: list[dict], limit: int = 10) -> list[dict]:
    return top_hosts([r["gets"] for r in round_summaries if r["gets"] is not None], limit)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _pct(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.0f} ms"


def _num(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def _int(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.0f}"


def _kinds(kinds: dict[str, int]) -> str:
    if not kinds:
        return "—"
    return ", ".join(f"{k}={v}" for k, v in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))


def _ids(values) -> str:
    return ", ".join(str(v) for v in values) or "—"


def _discovery(totals: dict[str, int]) -> str:
    """The discovery counters in DECLARED order, not by size.

    The two arms' lines are meant to be read side by side, and an order that
    depends on the counts puts the same counter in a different column on each —
    which is the one thing a by-eye comparison cannot survive. The
    ``discovery_`` prefix is stripped: every name on the line carries it.
    """
    return ", ".join(
        f"{column.removeprefix('discovery_')}={totals.get(column, 0):,}"
        for column in DISCOVERY_TOTAL_COLUMNS
    )


def _cohort_line(summary: dict) -> str:
    return (
        f"{summary['attempted']} attempted ({summary['success']} ok, "
        f"{summary['failure']} failed, {summary['skipped']} skipped), completion "
        f"{_pct(summary['completion'])}, p50 {_ms(summary['p50_ms'])}, p95 "
        f"{_ms(summary['p95_ms'])}; failures {_kinds(summary['error_kinds'])}; "
        f"verify mismatch {summary['verify_mismatch']} "
        f"(ok {summary['verify_ok']}, unchecked {summary['verify_unchecked']})"
    )


def _peer_line(summary: dict) -> str:
    sums, medians = summary["sums"], summary["medians"]
    return (
        f"{summary['rows_with_peers']} of {summary['attempted']} row(s) took a "
        f"record from a peer ({_pct(summary['peer_share'], 1)}); "
        f"records_from_peers {sums['records_from_peers']:,} "
        f"(median {_num(medians['records_from_peers'])}), bytes_from_peers "
        f"{sums['bytes_from_peers']:,} (median {_int(medians['bytes_from_peers'])}), "
        f"records_served {sums['records_served']:,} "
        f"(median {_num(medians['records_served'])}), bytes_served "
        f"{sums['bytes_served']:,} (median {_int(medians['bytes_served'])}); "
        f"swarm_present true={summary['swarm_present']['true']} "
        f"false={summary['swarm_present']['false']} "
        f"null={summary['swarm_present']['null']}"
    )


def _render_hot_file_section(report: dict, lines: list[str]) -> None:
    lines.append("## 1. The file, the cohorts and the window")
    lines.append("")
    files = report["hot_files"]["files"]
    if not files:
        lines.append(
            "**No hot-file selection carried any file.** The rounds below read "
            "something, but this report cannot say what."
        )
    else:
        source = report["hot_files"].get("source")
        lines.append(
            f"Selected at `{report['hot_files']['selected_at']}`, "
            f"{(report['hot_files']['size_bytes'] or 0):,} bytes each"
            + (f", read from `{source}`." if source else ".")
        )
        lines.append("")
        lines.append("| index | address | bytes | sha256 | rounds | round ids |")
        lines.append("| --: | --- | --: | --- | --: | --- |")
        for entry in files:
            lines.append(
                f"| {entry['index']} | `{entry['address']}` | "
                f"{(entry['bytes'] or 0):,} | `{entry['sha256'] or '—'}` | "
                f"{entry['round_count']} | {_ids(entry['rounds'])} |"
            )
    chunks = report["chunk_set"]
    lines.append("")
    lines.append(
        f"**Chunk set: {chunks['count']} distinct record(s)**, from the browser "
        f"traces of {len(chunks['from_rounds'])} swarm round(s) "
        f"({_ids(chunks['from_rounds'])}). That set is what the fleet's "
        f"`get_rpc` documents are filtered on, on BOTH arms, so the GET counts "
        f"below are like-for-like."
    )
    lines.append("")
    for arm in (ARM_SWARM, ARM_CONTROL):
        entry = report["extensions"][arm]
        lines.append(
            f"* **{arm}** — extension `{entry['name']}`, the "
            f"`{entry['parity']}` rounds: "
            f"{len(entry['hosts'])} native reader VM(s)"
            + (
                f", {len(entry['browser_hosts'])} browser VM(s)"
                if entry["browser_hosts"]
                else ", no browser VM"
            )
            + "."
        )
    lines.append("")
    lines.append("| arm | host | region | instances | parity | period |")
    lines.append("| --- | --- | --- | --: | --- | --: |")
    for arm in (ARM_SWARM, ARM_CONTROL):
        entry = report["extensions"][arm]
        for host in (*entry["hosts"], *entry["browser_hosts"]):
            lines.append(
                f"| {arm} | {host['hostname']} | {host['region'] or '—'} | "
                f"{host['instances']} | {host['round_parity'] or '—'} | "
                f"{host['round_period_secs'] or '—'}s |"
            )
    totals = report["totals"]
    lines.append("")
    lines.append("| arm | rounds | present | missing |")
    lines.append("| --- | --: | --- | --- |")
    for arm in (ARM_SWARM, ARM_CONTROL):
        summary = report["arms"][arm]
        lines.append(
            f"| {arm} | {len(summary['rounds'])} | "
            f"{_ids(summary['rounds_present'])} | {_ids(summary['rounds_missing'])} |"
        )
    lines.append("")
    lines.append(
        f"{totals['ignored_rows']:,} row(s) ignored (another extension, or no "
        f"extension at all); {totals['off_parity_rows']:,} off-parity row(s) "
        f"excluded — a cohort configured `round_parity: all` reads on the other "
        f"arm's rounds too, and those reads are in none of the figures below."
    )


def _render_rounds_section(report: dict, lines: list[str]) -> None:
    lines.append("## 2. Per round")
    lines.append("")
    lines.append(
        "| round | boundary (UTC) | arm | nat att/ok | br att/ok | nat peers | "
        "br peers | from peers | bytes from peers | served GETs | total GETs | "
        "nat p50/p95 | br p50/p95 |"
    )
    lines.append("| --: | --- | --- | --- | --- | --: | --: | --: | --: | --: | --: | --- | --- |")
    for entry in report["rounds"]:
        native, browser, gets = entry["native"], entry["browser"], entry["gets"]
        if entry["missing"]:
            lines.append(
                f"| {entry['round_id']} | {entry['boundary']} | {entry['arm']} | "
                f"**missing** | — | — | — | — | — | "
                f"{'—' if gets is None else format(gets['served'], ',')} | "
                f"{'—' if gets is None else format(gets['total'], ',')} | — | — |"
            )
            continue
        lines.append(
            f"| {entry['round_id']} | {entry['boundary']} | {entry['arm']} | "
            f"{native['attempted']}/{native['success']} | "
            f"{browser['attempted']}/{browser['success']} | "
            f"{native['rows_with_peers']} | {browser['rows_with_peers']} | "
            f"{native['sums']['records_from_peers'] + browser['sums']['records_from_peers']:,} | "
            f"{native['sums']['bytes_from_peers'] + browser['sums']['bytes_from_peers']:,} | "
            f"{'—' if gets is None else format(gets['served'], ',')} | "
            f"{'—' if gets is None else format(gets['total'], ',')} | "
            f"{_ms(native['p50_ms'])} / {_ms(native['p95_ms'])} | "
            f"{_ms(browser['p50_ms'])} / {_ms(browser['p95_ms'])} |"
        )


def _render_arms_section(report: dict, lines: list[str]) -> None:
    lines.append("## 3. Per arm")
    lines.append("")
    for arm in (ARM_SWARM, ARM_CONTROL):
        summary = report["arms"][arm]
        lines.append(
            f"### {arm} — `{report['extensions'][arm]['name']}`, "
            f"{len(summary['rounds_present'])} present round(s)"
        )
        lines.append("")
        lines.append(f"* native: {_cohort_line(summary['native'])}")
        lines.append(f"* native swarm: {_peer_line(summary['native'])}")
        lines.append("* native discovery: " + _discovery(summary["native"]["discovery"]))
        if summary["browser"]["rows"]:
            lines.append(f"* browser: {_cohort_line(summary['browser'])}")
            lines.append(f"* browser swarm: {_peer_line(summary['browser'])}")
            lines.append("* browser discovery: " + _discovery(summary["browser"]["discovery"]))
            sources = summary["browser"]["chunk_sources"]["sums"]
            lines.append(
                f"* browser chunk sources: total {sources['chunks_total']:,}, "
                f"from nodes {sources['chunks_from_nodes']:,}, from cache "
                f"{sources['chunks_from_cache']:,}, from readers "
                f"{sources['chunks_from_readers']:,}"
            )
        lines.append(
            f"* served `get_rpc` median {_num(summary['served_gets_median'])} "
            f"per round (total {_num(summary['total_gets_median'])})"
        )
        if not summary["swarm_expected"]:
            lines.append(
                "* this arm runs a released binary with **no swarm**, so every "
                "counter above is expected to be 0 with `swarm_present` false. "
                + (
                    f"**{summary['unexpected_swarm_rows']} row(s) say otherwise** "
                    "— check that the control extension really is the released "
                    "build."
                    if summary["unexpected_swarm_rows"]
                    else "No row says otherwise."
                )
            )
        lines.append("")


def _render_node_gets_section(report: dict, lines: list[str]) -> None:
    lines.append("## 4. The fleet's GETs for the file")
    lines.append("")
    lines.append(
        "One `get_rpc` document per native-path chunk GET on every node, "
        "filtered to the chunk set above and to the round's own "
        "`[boundary, boundary + period)` window. **The browser listener emits no "
        "per-GET event**, so these are the native readers' GETs plus the "
        "deployment's own native downloaders — which draw random files and "
        "almost never touch this one. A node whose clock is skewed stamps its "
        "documents outside their true round and is lost to the count, equally on "
        "both arms."
    )
    lines.append("")
    rows = report["node_gets"]["by_round"]
    if not rows:
        lines.append("No round's GETs could be counted.")
    else:
        lines.append(
            "| round | arm | total | get | get_or_closer | served | outcomes | "
            "served p50 | served p95 |"
        )
        lines.append("| --: | --- | --: | --: | --: | --: | --- | --: | --: |")
        for entry in rows:
            requests = entry.get("by_request") or {}
            lines.append(
                f"| {entry['round_id']} | {entry['arm']} | {entry['total']:,} | "
                f"{requests.get('get', 0):,} | {requests.get('get_or_closer', 0):,} | "
                f"{entry['served']:,} | {_kinds(entry.get('by_storage_outcome') or {})} | "
                f"{_ms(entry.get('served_p50_ms'))} | {_ms(entry.get('served_p95_ms'))} |"
            )
    top = report["node_gets"]["top_hosts"]
    if top:
        lines.append("")
        lines.append(f"### Busiest {len(top)} host(s) over the window")
        lines.append("")
        lines.append("| host | GETs |")
        lines.append("| --- | --: |")
        for entry in top:
            lines.append(f"| {entry['host']} | {entry['gets']:,} |")


def _render_browser_gets_section(report: dict, lines: list[str]) -> None:
    gets = report["browser_gets"]
    lines.append("## 5. The browsers' own node GETs (swarm rounds)")
    lines.append("")
    lines.append(
        f"From the trace records: an `rpc` `finish` whose detail starts `found` "
        f"is a peer answering that it holds the chunk, one starting `NotFound` is "
        f"it saying it does not. {gets['found']:,} found and "
        f"{gets['not_found']:,} NotFound over {gets['downloads']:,} download(s) "
        f"— {_num(gets['found_per_download'], 2)} and "
        f"{_num(gets['not_found_per_download'], 2)} per download."
    )
    lines.append("")
    if not gets["by_round"]:
        lines.append(
            "No browser trace record fell in a swarm round, so there is nothing "
            "here. A bundle built without `test-utils` forwards no trace at all."
        )
        return
    lines.append("| round | downloads | found | NotFound | found/dl | NotFound/dl |")
    lines.append("| --: | --: | --: | --: | --: | --: |")
    for entry in gets["by_round"]:
        lines.append(
            f"| {entry['round_id']} | {entry['downloads']} | {entry['found']:,} | "
            f"{entry['not_found']:,} | {_num(entry['found_per_download'], 2)} | "
            f"{_num(entry['not_found_per_download'], 2)} |"
        )


def render_markdown(report: dict) -> str:
    """The fixed-format report. Never reads the clock, so it is reproducible."""
    lines: list[str] = []
    totals = report["totals"]
    lines.append(f"# Swarm read report — {report['deployment']}")
    lines.append("")
    lines.append(
        f"Window `{report['window_start']}` → `{report['window_end']}` "
        f"(half-open), deployment id `{report['deployment_id']}`, round period "
        f"`{report['round_period_secs']}s` (from {report['round_period_source']})."
    )
    lines.append("")
    lines.append(
        f"**{report['verdict']}** — {totals['rounds']} round(s) in the window "
        f"({totals['rounds_missing']} missing); "
        f"{totals['swarm_native_rows']:,} swarm native, "
        f"{totals['swarm_browser_rows']:,} swarm browser and "
        f"{totals['control_native_rows']:,} control native row(s); "
        f"{totals['trace_records']:,} trace record(s) read from Elasticsearch."
    )
    lines.append("")
    _render_hot_file_section(report, lines)
    lines.append("")
    _render_rounds_section(report, lines)
    lines.append("")
    _render_arms_section(report, lines)
    _render_node_gets_section(report, lines)
    lines.append("")
    _render_browser_gets_section(report, lines)
    lines.append("")
    lines.append("## 6. Gates")
    lines.append("")
    lines.append("G1–G3 decide the verdict; **G4 is reported and never fails the command**.")
    lines.append("")
    lines.append("| gate | verdict | figure | threshold | detail |")
    lines.append("| --- | --- | --- | --- | --- |")
    for gate in report["gates"]:
        lines.append(
            f"| {gate['name']} | **{gate['status']}** | {gate['figure']} | "
            f"{gate['threshold']} | {gate['detail']} |"
        )
    lines.append("")
    return "\n".join(lines)


def as_epoch_bounds(round_id: int, period_secs: int) -> tuple[str, str]:
    """One round's ``[boundary, boundary + period)`` as ISO-8601 UTC strings.

    The shape Elasticsearch's ``range`` filter takes, and the reason the GET
    query uses ``lt`` on the upper bound: a document stamped exactly on the next
    boundary belongs to the next round.
    """
    start = datetime.fromtimestamp(round_id * period_secs, tz=timezone.utc)
    end = datetime.fromtimestamp((round_id + 1) * period_secs, tz=timezone.utc)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")
