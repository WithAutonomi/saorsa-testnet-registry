"""The hot-read report's arithmetic, over recorded browser-download rows.

Pure functions over ``DeploymentBrowserDownloadRecord``-shaped rows and the
recorded role config, so every section and every gate is testable against a
committed fixture with no database, no S3 and no InfluxDB. The command around it
(:mod:`saorsa_deploy.analysis.browser_reads`) does the loading, the rendering and
the exit code.

Two rules run through all of it:

* **A ``skipped`` row is excluded from every denominator and listed separately.**
  A round that ran before the operator selected a hot file, or one an instance
  was still busy for, is part of the series — but it launched no browser, so
  counting it would make the cohort's completion rate a function of how early it
  was provisioned.
* **A round's boundary is ``round_id x period``, and that is what the window
  selects on.** The harness stamps ``start_ns`` with the boundary rather than
  with the moment its browser came up (``started_late_ms`` carries that), so the
  boundary is a property of the round and every instance of it agrees.
"""

from __future__ import annotations

from datetime import datetime, timezone

from saorsa_deploy.gate_comparison import percentile

#: Below this many first reads a chunk's busiest-holder share is noise — one
#: holder serving 2 of 3 reads is 67% and says nothing — so such chunks are
#: LISTED but not gated. 20 is the issue's own floor (V2-1618).
MIN_FIRST_READS_FOR_GATE = 20

#: Instances of one round that started more than this far apart are flagged: the
#: whole measurement is "the same file at the same moment", and a cohort spread
#: over more than this is not reading simultaneously.
ROUND_SPREAD_FLAG_MS = 10_000

#: A node admits at most this many WebRTC sessions in total. Every browser
#: download dials every seed, so the number of downloads in a round IS the
#: session count each bootstrap sees at the start of that round — the figure to
#: watch as a cohort grows, and the reason the report prints the per-round
#: maximum rather than an average.
BOOTSTRAP_SESSION_CEILING = 256

#: `error_kind` that means the close group agreed the content is not there. The
#: one failure kind that is never acceptable on a hot file the operator chose
#: from the deployment's own published uploads, which is why it has a gate of
#: its own rather than being one of many in the completion rate.
ERROR_KIND_NOT_FOUND = "not_found"

STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"
STATUS_SKIPPED = "skipped"

GATE_PASS = "PASS"
GATE_FAIL = "FAIL"


class BrowserReadsError(RuntimeError):
    """The report cannot be produced. Carries the operator-facing message."""


def row_status(row) -> str:
    """The row's status as a plain string, whichever shape it arrives in.

    Rows come from SQLAlchemy (an ``OpRecordStatus``) in the command and from
    dicts in the tests, and the arithmetic below should not have to care.
    """
    status = row["status"] if isinstance(row, dict) else row.status
    return getattr(status, "value", status)


def _field(row, name):
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


def as_utc(value: datetime) -> datetime:
    """A stored timestamp as tz-aware UTC.

    Postgres returns `timestamptz` tz-aware; the SQLite fixtures the tests run
    against drop the zone, and a naive `.timestamp()` would then be read in the
    machine's local time -- which on any host west of UTC turns a round boundary
    into a different round.
    """
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def boundary_of(round_id: int, period_secs: int) -> datetime:
    """The wall-clock second a round begins on, as a UTC datetime."""
    return datetime.fromtimestamp(round_id * period_secs, tz=timezone.utc)


def derive_period_secs(rows) -> int | None:
    """The round period implied by the rows themselves, or None.

    The fallback for a deployment whose role config was never recorded (no
    ``DATABASE_URL`` at `clients provision` time). ``started_at`` IS the
    boundary, so ``started_at / round_id`` is the period exactly — and it is
    only trusted when every row agrees and divides evenly, because a single
    disagreement means the assumption behind the arithmetic does not hold.
    """
    periods = set()
    for row in rows:
        round_id = _field(row, "round_id")
        started_at = _field(row, "started_at")
        if not round_id or started_at is None:
            continue
        epoch = int(as_utc(started_at).timestamp())
        if epoch % round_id:
            return None
        periods.add(epoch // round_id)
    if len(periods) != 1:
        return None
    period = periods.pop()
    return period if period > 0 else None


def rounds_in_window(rows, period_secs: int, window_start: datetime, window_end: datetime) -> list:
    """The rows whose round boundary lies in ``[start, end)``.

    Half-open, so two adjacent windows partition a run's rounds exactly once
    between them — a round counted in both would be counted twice in any
    phase-by-phase comparison.
    """
    out = []
    for row in rows:
        round_id = _field(row, "round_id")
        if round_id is None:
            continue
        boundary = boundary_of(round_id, period_secs)
        if window_start <= boundary < window_end:
            out.append(row)
    return out


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

    None, never 0.0: a window in which every round was skipped has no completion
    rate, and reporting 0% would fail the gate for a cohort that was never asked
    to do anything.
    """
    attempted = success + failure
    if not attempted:
        return None
    return success / attempted


def summarise_rounds(rows, period_secs: int) -> list[dict]:
    """One entry per round in the given rows, oldest first."""
    by_round: dict[int, list] = {}
    for row in rows:
        by_round.setdefault(_field(row, "round_id"), []).append(row)

    out: list[dict] = []
    for round_id in sorted(by_round):
        group = by_round[round_id]
        success = sum(1 for r in group if row_status(r) == STATUS_SUCCESS)
        failure = sum(1 for r in group if row_status(r) == STATUS_FAILURE)
        skipped = sum(1 for r in group if row_status(r) == STATUS_SKIPPED)
        lates = [
            _field(r, "started_late_ms") for r in group if _field(r, "started_late_ms") is not None
        ]
        p50, p95 = _duration_percentiles(group)
        # The spread, not the max: a cohort that all started 40 s late is still
        # reading simultaneously, which is what is being measured. One instance
        # 40 s behind the rest is not.
        spread_ms = (max(lates) - min(lates)) if len(lates) > 1 else 0
        out.append(
            {
                "round_id": round_id,
                "boundary": boundary_of(round_id, period_secs).isoformat(),
                "hot_file_index": _field(group[0], "hot_file_index"),
                "instances_reporting": len(
                    {(_field(r, "client_hostname"), _field(r, "instance")) for r in group}
                ),
                "hosts": len({_field(r, "client_hostname") for r in group}),
                "success": success,
                "failure": failure,
                "skipped": skipped,
                "completion": _completion(success, failure),
                "p50_ms": p50,
                "p95_ms": p95,
                "error_kinds": _error_kinds(group),
                "started_late_ms_max": max(lates) if lates else None,
                "started_spread_ms": spread_ms,
                "desynchronised": spread_ms > ROUND_SPREAD_FLAG_MS,
                "stalls": sum(
                    _field(r, "stalls") or 0 for r in group if _field(r, "stalls") is not None
                ),
                "refused_dials": sum(
                    _field(r, "refused_dials") or 0
                    for r in group
                    if _field(r, "refused_dials") is not None
                ),
                # Every download dials every seed, so this is the session count
                # each bootstrap sees at the round's start. Skips launch no
                # browser and dial nothing, so they are excluded.
                "downloads": success + failure,
            }
        )
    return out


def summarise_regions(rows) -> list[dict]:
    """The same figures aggregated per region over the whole window."""
    by_region: dict[str, list] = {}
    for row in rows:
        by_region.setdefault(_field(row, "region") or "unknown", []).append(row)

    out: list[dict] = []
    for region in sorted(by_region):
        group = by_region[region]
        success = sum(1 for r in group if row_status(r) == STATUS_SUCCESS)
        failure = sum(1 for r in group if row_status(r) == STATUS_FAILURE)
        skipped = sum(1 for r in group if row_status(r) == STATUS_SKIPPED)
        p50, p95 = _duration_percentiles(group)
        out.append(
            {
                "region": region,
                "hosts": len({_field(r, "client_hostname") for r in group}),
                "rounds": len({_field(r, "round_id") for r in group}),
                "success": success,
                "failure": failure,
                "skipped": skipped,
                "completion": _completion(success, failure),
                "p50_ms": p50,
                "p95_ms": p95,
                "error_kinds": _error_kinds(group),
                "stalls": sum(
                    _field(r, "stalls") or 0 for r in group if _field(r, "stalls") is not None
                ),
                "refused_dials": sum(
                    _field(r, "refused_dials") or 0
                    for r in group
                    if _field(r, "refused_dials") is not None
                ),
            }
        )
    return out


def summarise_chunks(rows) -> list[dict]:
    """Per-chunk first-read spreading: the verdict on ADR-0023.

    For every chunk any download in the window read, how many first reads it
    received, how many distinct holders served them, and the **busiest holder's
    share** of them. Sorted by share descending, so the worst-spread chunk is
    the first line of the table.

    ``known_holders`` is counted as the harness reports it — the discovery count
    for that chunk over the WHOLE download, not the count known at the instant
    of the read (see V2-1617's amended definition). ``null`` is its own bucket
    and means no discovery for that chunk reported a count at all; it is
    deliberately not folded into 0, which would read as "the client knew of no
    holders".
    """
    per_chunk: dict[str, dict] = {}
    for row in rows:
        for read in _field(row, "first_reads") or []:
            if not isinstance(read, dict):
                continue
            chunk = read.get("chunk")
            if not chunk:
                continue
            entry = per_chunk.setdefault(chunk, {"holders": {}, "known_holders": {}, "reads": 0})
            entry["reads"] += 1
            holder = read.get("holder") or "unknown"
            entry["holders"][holder] = entry["holders"].get(holder, 0) + 1
            known = read.get("known_holders")
            key = "null" if known is None else str(known)
            entry["known_holders"][key] = entry["known_holders"].get(key, 0) + 1

    out: list[dict] = []
    for chunk, entry in per_chunk.items():
        reads = entry["reads"]
        busiest_holder, busiest_reads = max(entry["holders"].items(), key=lambda kv: (kv[1], kv[0]))
        out.append(
            {
                "chunk": chunk,
                "reads": reads,
                "distinct_holders": len(entry["holders"]),
                "busiest_holder": busiest_holder,
                "busiest_holder_reads": busiest_reads,
                "busiest_holder_share": busiest_reads / reads,
                "known_holders": entry["known_holders"],
                "gated": reads >= MIN_FIRST_READS_FOR_GATE,
            }
        )
    out.sort(key=lambda c: (-c["busiest_holder_share"], -c["reads"], c["chunk"]))
    return out


def summarise_stalls(rows, round_summaries: list[dict]) -> dict:
    """The per-round sums plus the per-download distribution of ``stalls``."""
    counted = [_field(r, "stalls") for r in rows if _field(r, "stalls") is not None]
    distribution: dict[str, int] = {}
    for value in counted:
        distribution[str(value)] = distribution.get(str(value), 0) + 1
    with_stall = sum(1 for value in counted if value > 0)
    return {
        "by_round": [{"round_id": r["round_id"], "stalls": r["stalls"]} for r in round_summaries],
        "total": sum(counted),
        "downloads_with_counts": len(counted),
        "downloads_with_a_stall": with_stall,
        # Over the downloads that CARRY a count, not over every row: a row
        # written before the field existed, or a skip that launched no browser,
        # cannot be said to have had no stall.
        "share_with_a_stall": (with_stall / len(counted)) if counted else None,
        "distribution": distribution,
    }


def summarise_refusals(rows) -> dict:
    """The window's total ``refused_dials`` and the rows carrying them.

    The rows are named individually because a refusal is a per-IP quota
    symptom: a VM running a fifth Chromium instance would be refused by every
    node it dials, and that reads as a NAT'd fleet unless the count can be
    traced to the VM it came from.
    """
    carriers = [
        {
            "round_id": _field(r, "round_id"),
            "client_hostname": _field(r, "client_hostname"),
            "instance": _field(r, "instance"),
            "refused_dials": _field(r, "refused_dials"),
            "status": row_status(r),
            "error_kind": _field(r, "error_kind"),
        }
        for r in rows
        if (_field(r, "refused_dials") or 0) > 0
    ]
    carriers.sort(key=lambda c: (-c["refused_dials"], c["round_id"], c["client_hostname"]))
    return {
        "total": sum(c["refused_dials"] for c in carriers),
        "rows": carriers,
    }


def summarise_bootstrap_load(round_summaries: list[dict], rows) -> dict:
    """Sessions per bootstrap per round: the downloads in the round.

    Each download dials every seed at its start, so the number of downloads a
    round performed is what one bootstrap sees. The per-round MAXIMUM is the
    figure to watch against the ceiling — an average over a run that started
    small would hide the peak that matters.
    """
    seeds: set[str] = set()
    for row in rows:
        for seed in _field(row, "seeds") or []:
            seeds.add(seed)
    per_round = [
        {"round_id": r["round_id"], "sessions_per_bootstrap": r["downloads"]}
        for r in round_summaries
    ]
    peak = max((r["sessions_per_bootstrap"] for r in per_round), default=0)
    return {
        "seeds": sorted(seeds),
        "seed_count": len(seeds),
        "by_round": per_round,
        "peak_sessions_per_bootstrap": peak,
        "ceiling": BOOTSTRAP_SESSION_CEILING,
        "headroom": BOOTSTRAP_SESSION_CEILING - peak,
    }


def _gate(name: str, passed: bool, figure: str, threshold: str, detail: str) -> dict:
    return {
        "name": name,
        "status": GATE_PASS if passed else GATE_FAIL,
        "figure": figure,
        "threshold": threshold,
        "detail": detail,
    }


def evaluate_gates(
    rows,
    round_summaries: list[dict],
    chunk_summaries: list[dict],
    refusals: dict,
    min_completion: float,
    max_holder_share: float,
    max_refused: int,
) -> list[dict]:
    """The four gates, in order, each with its figure and threshold.

    G1 is per ROUND, not over the window: a cohort that failed every read for
    one round and recovered has still had an outage, and a window-wide average
    over a long run hides it.
    """
    gated_rounds = [r for r in round_summaries if r["completion"] is not None]
    worst = min(gated_rounds, key=lambda r: r["completion"], default=None)
    g1_pass = bool(gated_rounds) and worst["completion"] >= min_completion
    gates = [
        _gate(
            "G1 per-round completion",
            g1_pass,
            (
                f"worst round {worst['round_id']} at {worst['completion']:.2%}"
                if worst is not None
                else "no round attempted a download"
            ),
            f">= {min_completion:.2%} in every round",
            (
                f"{len(gated_rounds)} round(s) assessed; "
                f"{sum(1 for r in gated_rounds if r['completion'] < min_completion)} "
                "below the floor"
                if gated_rounds
                else "every round in the window was skipped, so there is no "
                "completion rate to gate — widen the window or select a hot file"
            ),
        )
    ]

    not_found = [
        r
        for r in rows
        if (_field(r, "error_kind") or "") == ERROR_KIND_NOT_FOUND
        and row_status(r) == STATUS_FAILURE
    ]
    gates.append(
        _gate(
            "G2 no not_found",
            not not_found,
            f"{len(not_found)} row(s)",
            "0 rows",
            (
                "the hot file was chosen from the deployment's own published "
                "uploads, so a close group agreeing it is absent is content "
                "loss, not a read-path property"
                if not_found
                else "no download was told the content does not exist"
            ),
        )
    )

    gatable = [c for c in chunk_summaries if c["gated"]]
    breached = [c for c in gatable if c["busiest_holder_share"] > max_holder_share]
    worst_chunk = gatable[0] if gatable else None
    gates.append(
        _gate(
            "G3 busiest holder share",
            not breached,
            (
                f"worst gated chunk {worst_chunk['chunk'][:16]} at "
                f"{worst_chunk['busiest_holder_share']:.1%}"
                if worst_chunk is not None
                else "no chunk reached the gating floor"
            ),
            f"<= {max_holder_share:.1%} per chunk with >= {MIN_FIRST_READS_FOR_GATE} first reads",
            (
                f"{len(gatable)} chunk(s) gated, {len(breached)} breached; "
                f"{len(chunk_summaries) - len(gatable)} below "
                f"{MIN_FIRST_READS_FOR_GATE} first reads and listed only"
            ),
        )
    )

    gates.append(
        _gate(
            "G4 refused dials",
            refusals["total"] <= max_refused,
            f"{refusals['total']} refused dial(s)",
            f"<= {max_refused}",
            (
                f"carried by {len(refusals['rows'])} download(s) — check the "
                f"per-IP instance count before reading this as the fleet"
                if refusals["total"] > max_refused
                else "no download reconstructed a refusal"
            ),
        )
    )
    return gates


def hot_file_rounds(rows, hot_files: dict | None) -> list[dict]:
    """The selected hot files, and for each the rounds that read it.

    Driven by the selection document rather than by the rows, so a file that was
    selected and never read shows as a file with no rounds — which is a finding
    (the cohort should cycle every file) rather than an absence.
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
                "source": f"{entry.get('source_key')}:{entry.get('line')}",
                "rounds": rounds,
                "round_count": len(rounds),
            }
        )
    return out


def build_report(
    *,
    deployment: str,
    deployment_id: int | None,
    rows,
    hot_files: dict | None,
    period_secs: int,
    period_source: str,
    expected_instances: int | None,
    window_start: datetime,
    window_end: datetime,
    min_completion: float,
    max_holder_share: float,
    max_refused: int,
) -> dict:
    """The whole report as one JSON-ready document.

    ``rows`` must already be narrowed to the window: the caller does that so the
    "no rows in the window" error can name the window it looked in.
    """
    round_summaries = summarise_rounds(rows, period_secs)
    for entry in round_summaries:
        entry["instances_expected"] = expected_instances
        entry["instances_missing"] = (
            expected_instances - entry["instances_reporting"]
            if expected_instances is not None
            else None
        )
    chunk_summaries = summarise_chunks(rows)
    refusals = summarise_refusals(rows)
    gates = evaluate_gates(
        rows,
        round_summaries,
        chunk_summaries,
        refusals,
        min_completion,
        max_holder_share,
        max_refused,
    )
    success = sum(1 for r in rows if row_status(r) == STATUS_SUCCESS)
    failure = sum(1 for r in rows if row_status(r) == STATUS_FAILURE)
    skipped = [r for r in rows if row_status(r) == STATUS_SKIPPED]
    p50, p95 = _duration_percentiles(rows)
    return {
        "deployment": deployment,
        "deployment_id": deployment_id,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "round_period_secs": period_secs,
        "round_period_source": period_source,
        "instances_expected": expected_instances,
        "totals": {
            "rounds": len(round_summaries),
            "downloads": len(rows),
            "success": success,
            "failure": failure,
            "skipped": len(skipped),
            "completion": _completion(success, failure),
            "p50_ms": p50,
            "p95_ms": p95,
            "error_kinds": _error_kinds(rows),
            "traces_archived": sum(1 for r in rows if _field(r, "trace_s3_key")),
        },
        "hot_files": {
            "selected_at": (hot_files or {}).get("selected_at"),
            "size_bytes": (hot_files or {}).get("size_bytes"),
            "files": hot_file_rounds(rows, hot_files),
        },
        "rounds": round_summaries,
        "regions": summarise_regions(rows),
        "chunks": chunk_summaries,
        "stalls": summarise_stalls(rows, round_summaries),
        "refusals": refusals,
        "bootstrap_load": summarise_bootstrap_load(round_summaries, rows),
        # Listed separately, never in a denominator.
        "skipped_rows": [
            {
                "round_id": _field(r, "round_id"),
                "client_hostname": _field(r, "client_hostname"),
                "instance": _field(r, "instance"),
                "error_kind": _field(r, "error_kind"),
            }
            for r in skipped
        ],
        "gates": gates,
        "verdict": GATE_FAIL if any(g["status"] == GATE_FAIL for g in gates) else GATE_PASS,
        "thresholds": {
            "min_completion": min_completion,
            "max_holder_share": max_holder_share,
            "max_refused": max_refused,
            "min_first_reads_for_gate": MIN_FIRST_READS_FOR_GATE,
            "round_spread_flag_ms": ROUND_SPREAD_FLAG_MS,
            "bootstrap_session_ceiling": BOOTSTRAP_SESSION_CEILING,
        },
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _pct(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.0f} ms"


def _kinds(kinds: dict[str, int]) -> str:
    if not kinds:
        return "—"
    return ", ".join(f"{k}={v}" for k, v in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))


def _known_holders(distribution: dict[str, int]) -> str:
    if not distribution:
        return "—"

    def key(item):
        label = item[0]
        # Descending by reported count, with `null` last: it is not a count at
        # all, so it does not belong anywhere in the ordering of counts. A label
        # that is neither sorts beside it rather than raising -- a renderer that
        # crashes on one odd value in a JSON column loses the whole report.
        try:
            return (0, -int(label))
        except ValueError:
            return (1, label)

    return ", ".join(f"{k}={v}" for k, v in sorted(distribution.items(), key=key))


def render_markdown(report: dict) -> str:
    """The fixed-format report. Never reads the clock, so it is reproducible."""
    lines: list[str] = []
    totals = report["totals"]
    lines.append(f"# Browser hot-read report — {report['deployment']}")
    lines.append("")
    lines.append(
        f"Window `{report['window_start']}` → `{report['window_end']}` "
        f"(half-open), deployment id `{report['deployment_id']}`, round period "
        f"`{report['round_period_secs']}s` (from {report['round_period_source']})."
    )
    lines.append("")
    lines.append(
        f"**{report['verdict']}** — {totals['rounds']} round(s), "
        f"{totals['downloads']} download(s): {totals['success']} ok, "
        f"{totals['failure']} failed, {totals['skipped']} skipped; completion "
        f"{_pct(totals['completion'])}, p50 {_ms(totals['p50_ms'])}, p95 "
        f"{_ms(totals['p95_ms'])}; {totals['traces_archived']} trace(s) archived."
    )

    lines.append("")
    lines.append("## 1. Hot files")
    lines.append("")
    files = report["hot_files"]["files"]
    if not files:
        lines.append("No hot-file selection carried any file.")
    else:
        lines.append(
            f"Selected at `{report['hot_files']['selected_at']}`, "
            f"{report['hot_files']['size_bytes']:,} bytes each."
        )
        lines.append("")
        lines.append("| index | address | bytes | source | rounds | round ids |")
        lines.append("| --: | --- | --: | --- | --: | --- |")
        for entry in files:
            ids = ", ".join(str(r) for r in entry["rounds"]) or "—"
            lines.append(
                f"| {entry['index']} | `{entry['address']}` | "
                f"{(entry['bytes'] or 0):,} | {entry['source']} | "
                f"{entry['round_count']} | {ids} |"
            )

    lines.append("")
    lines.append("## 2. Per round")
    lines.append("")
    lines.append(
        "| round | boundary (UTC) | file | inst (exp) | ok | fail | skip | "
        "completion | p50 | p95 | late max | spread | failures |"
    )
    lines.append("| --: | --- | --: | --- | --: | --: | --: | --: | --: | --: | --: | --- | --- |")
    for entry in report["rounds"]:
        expected = (
            "n/a" if entry["instances_expected"] is None else str(entry["instances_expected"])
        )
        spread = f"{entry['started_spread_ms']:,} ms"
        if entry["desynchronised"]:
            spread += " **⚠ desync**"
        lines.append(
            f"| {entry['round_id']} | {entry['boundary']} | "
            f"{'—' if entry['hot_file_index'] is None else entry['hot_file_index']} | "
            f"{entry['instances_reporting']} ({expected}) | {entry['success']} | "
            f"{entry['failure']} | {entry['skipped']} | {_pct(entry['completion'])} | "
            f"{_ms(entry['p50_ms'])} | {_ms(entry['p95_ms'])} | "
            f"{_ms(entry['started_late_ms_max'])} | {spread} | "
            f"{_kinds(entry['error_kinds'])} |"
        )

    lines.append("")
    lines.append("## 3. Per region")
    lines.append("")
    lines.append(
        "| region | hosts | rounds | ok | fail | skip | completion | p50 | p95 "
        "| stalls | refused | failures |"
    )
    lines.append("| --- | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: | --- |")
    for entry in report["regions"]:
        lines.append(
            f"| {entry['region']} | {entry['hosts']} | {entry['rounds']} | "
            f"{entry['success']} | {entry['failure']} | {entry['skipped']} | "
            f"{_pct(entry['completion'])} | {_ms(entry['p50_ms'])} | "
            f"{_ms(entry['p95_ms'])} | {entry['stalls']} | "
            f"{entry['refused_dials']} | {_kinds(entry['error_kinds'])} |"
        )

    lines.append("")
    lines.append("## 4. Per chunk")
    lines.append("")
    lines.append(
        "The busiest holder's share is the verdict on hot-record read spreading "
        "(ADR-0023), and it reads `first_reads` alone — the holder each chunk's "
        "FIRST read went to. `known_holders` is the discovery-reported count for "
        "the whole download, not the count known at the read; `null` means no "
        "count was reported."
    )
    lines.append("")
    if not report["chunks"]:
        lines.append(
            "No `first_reads` were recorded in this window. A bundle built "
            "without `test-utils` reports none, so check that before reading "
            "this as a quiet network."
        )
    else:
        lines.append("| chunk | reads | holders | busiest share | busiest holder | known_holders |")
        lines.append("| --- | --: | --: | --: | --- | --- |")
        for entry in report["chunks"]:
            share = f"{entry['busiest_holder_share'] * 100:.1f}%"
            if not entry["gated"]:
                share += f" (< {MIN_FIRST_READS_FOR_GATE} reads, not gated)"
            lines.append(
                f"| `{entry['chunk']}` | {entry['reads']} | "
                f"{entry['distinct_holders']} | {share} | "
                f"`{entry['busiest_holder']}` | "
                f"{_known_holders(entry['known_holders'])} |"
            )

    stalls = report["stalls"]
    lines.append("")
    lines.append("## 5. Stalls")
    lines.append("")
    lines.append(
        f"{stalls['total']} stall(s) over {stalls['downloads_with_counts']} "
        f"download(s) carrying a count; {stalls['downloads_with_a_stall']} had at "
        f"least one ({_pct(stalls['share_with_a_stall'])}). Distribution: "
        f"{_kinds(stalls['distribution'])}."
    )
    lines.append("")
    lines.append("| round | stalls |")
    lines.append("| --: | --: |")
    for entry in stalls["by_round"]:
        lines.append(f"| {entry['round_id']} | {entry['stalls']} |")

    refusals = report["refusals"]
    lines.append("")
    lines.append("## 6. Refusals")
    lines.append("")
    lines.append(
        f"{refusals['total']} refused dial(s), reconstructed from dial timing "
        f"(the trace never carries a refusal), carried by "
        f"{len(refusals['rows'])} download(s)."
    )
    if refusals["rows"]:
        lines.append("")
        lines.append("| round | host | instance | refused | status | kind |")
        lines.append("| --: | --- | --: | --: | --- | --- |")
        for entry in refusals["rows"]:
            lines.append(
                f"| {entry['round_id']} | {entry['client_hostname']} | "
                f"{entry['instance']} | {entry['refused_dials']} | "
                f"{entry['status']} | {entry['error_kind'] or '—'} |"
            )

    load = report["bootstrap_load"]
    lines.append("")
    lines.append("## 7. Bootstrap load")
    lines.append("")
    lines.append(
        f"Every download dials every seed, so a round's download count is the "
        f"session count ONE bootstrap sees at its start. Peak "
        f"**{load['peak_sessions_per_bootstrap']}** against a ceiling of "
        f"{load['ceiling']} ({load['headroom']} of headroom), across "
        f"{load['seed_count']} seed(s)."
    )
    lines.append("")
    lines.append("| round | sessions per bootstrap |")
    lines.append("| --: | --: |")
    for entry in load["by_round"]:
        lines.append(f"| {entry['round_id']} | {entry['sessions_per_bootstrap']} |")

    lines.append("")
    lines.append("## 8. Gates")
    lines.append("")
    lines.append("| gate | verdict | figure | threshold | detail |")
    lines.append("| --- | --- | --- | --- | --- |")
    for gate in report["gates"]:
        lines.append(
            f"| {gate['name']} | **{gate['status']}** | {gate['figure']} | "
            f"{gate['threshold']} | {gate['detail']} |"
        )

    if report["skipped_rows"]:
        lines.append("")
        lines.append(
            f"### Skipped ({len(report['skipped_rows'])}), excluded from every denominator"
        )
        lines.append("")
        lines.append("| round | host | instance | kind |")
        lines.append("| --: | --- | --: | --- |")
        for entry in report["skipped_rows"]:
            lines.append(
                f"| {entry['round_id']} | {entry['client_hostname']} | "
                f"{entry['instance']} | {entry['error_kind'] or '—'} |"
            )

    lines.append("")
    return "\n".join(lines)
