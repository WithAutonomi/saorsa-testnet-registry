"""Block-Kit builders for the results summary posted by
``deployments record-results --update-slack``.

Each builder takes the data a ``record-results`` path has already computed and
renders it as Slack Block-Kit blocks for a threaded reply to the deployment's
original ``slack-post`` message. The per-kind shape mirrors what the command
prints to the terminal so the Slack thread and the CLI tell the same story.

Block helpers (``_section`` / ``_fields_section`` / ``_chunked_sections``) are
reused from :mod:`saorsa_deploy.cmd.slack_post`; the latter keeps each section
under Slack's 3000-char cap and paginates long lists, which bounds the block
count for large per-file / per-host detail.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from saorsa_deploy.cmd.slack_post import (
    _chunked_sections,
    _fields_section,
    _section,
)

# 3 and 4 are browser-path only: `over-cap` means the transport refused the file
# (nothing attempted, nothing paid for) and `failed` means the upload failed
# before any payment went out, so nothing is stored — the opposite of a partial.
_PROD_UPLOAD_STATUS_LABEL = {
    0: "ok",
    1: "partial",
    2: "dl-failed",
    3: "over-cap",
    4: "failed",
}
_PROD_UPLOAD_STATUSES = tuple(_PROD_UPLOAD_STATUS_LABEL)

_ATTO_PER_UNIT = Decimal(10) ** 18


def _cost_decimal(value) -> Decimal | None:
    """Parse a string-encoded uint256 cost field to ``Decimal`` (None if absent)."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _fmt_token(value: Decimal | None, symbol: str) -> str:
    """Render an atto/wei amount (10^-18) as human-readable ANT / ETH."""
    if value is None:
        return "-"
    amount = (value / _ATTO_PER_UNIT).normalize()
    return f"{amount:f} {symbol}"


# Slack caps a message at 50 blocks; _chunked_sections packs ~35 lines/block, so
# a per-op list of unbounded length (e.g. a long-running continuous downloader)
# could blow the cap. Bound the per-op detail and note the truncation rather
# than silently dropping rows or having Slack reject the whole reply.
_MAX_OP_DETAIL_LINES = 300


def _format_duration_ms(ms) -> str:
    """Render a millisecond duration human-readably for Slack posts: sub-minute
    durations as ``12.3s`` and longer ones as ``3m 48s``. ``None`` → ``no
    timing``."""
    if ms is None:
        return "no timing"
    total_seconds = ms / 1000.0
    if total_seconds < 60:
        return f"{total_seconds:.1f}s"
    minutes = int(total_seconds // 60)
    seconds = int(round(total_seconds - minutes * 60))
    if seconds == 60:  # rounding nudged us up to a whole minute
        minutes += 1
        seconds = 0
    return f"{minutes}m {seconds}s"


def _totals_section(total_ok: int, total_fail: int, op_records: list[dict] | None) -> dict:
    """Totals, split by transport when the run had a WASM client tier.

    A native and a WASM client are not the same instrument -- measured on
    DEV-01-wasm the WASM tier ran ~2.2x slower on upload and ~4.2x on download,
    and it is judged against its own gates -- so one blended "Total failures"
    describes neither and moves with whatever VM mix the run happened to use.
    The reader of this reply is deciding whether a run went badly, and the
    answer differs per tier.

    ``client_kind`` is NULL on every row predating the WASM tier and on a run
    whose client state could not be read, which counts as native: NULL reads as
    native everywhere else in this pipeline, and it is what the gates do too.
    With no WASM record at all the two original fields are emitted unchanged.
    """
    wasm_ok = wasm_fail = 0
    skipped = crashes = 0
    for record in op_records or []:
        # Counted across both transports: a skip or a crash is worth seeing
        # whichever tier produced it, and only the browser path produces either
        # today.
        if record.get("status") == "skipped":
            skipped += 1
            continue
        if record.get("error_kind") == "crash":
            crashes += 1
        if record.get("client_kind") != "wasm":
            continue
        if record.get("status") == "success":
            wasm_ok += 1
        else:
            wasm_fail += 1

    # A skip is neither: the transport refused a file the network holds happily,
    # so it stays out of both totals and is reported on its own.
    extra: list[tuple[str, str]] = []
    if skipped:
        extra.append(("Skipped (over-cap)", str(skipped)))
    if crashes:
        extra.append(("Crashes (killed by signal)", str(crashes)))

    if not (wasm_ok or wasm_fail):
        return _fields_section(
            [
                ("Total successes", str(total_ok)),
                ("Total failures", str(total_fail)),
                *extra,
            ]
        )

    # The aggregates are the authoritative totals (they cover the whole run,
    # while the op list may be capped), so native is the remainder rather than
    # a second count of its own.
    return _fields_section(
        [
            ("Native successes", str(total_ok - wasm_ok)),
            ("Native failures", str(total_fail - wasm_fail)),
            ("WASM successes", str(wasm_ok)),
            ("WASM failures", str(wasm_fail)),
            *extra,
        ]
    )


def file_ops_result_blocks(
    aggregates: list[dict], op_records: list[dict] | None = None
) -> list[dict]:
    """Per-(host, op_type) breakdown plus per-operation detail for testnet /
    downloader file_ops.

    ``op_records`` are the individual upload/download operations (one dict per
    op, with ``client_hostname`` / ``op_type`` / ``status`` (``success`` or
    ``failure``) / ``file_size_bytes`` / ``duration_ms`` / ``started_at`` /
    ``client_kind``). When provided, the most recent operations are listed in
    full; the list is capped at ``_MAX_OP_DETAIL_LINES`` with a note when it
    overflows. ``client_kind`` also splits the headline totals per transport.
    """
    if not aggregates:
        return [_section("*Results:* _no upload/download points found._")]

    total_ok = sum(a["success_count"] for a in aggregates)
    total_fail = sum(a["failure_count"] for a in aggregates)
    blocks: list[dict] = [_totals_section(total_ok, total_fail, op_records)]

    host_lines: list[str] = []
    for a in sorted(aggregates, key=lambda x: (x["client_hostname"], x["op_type"])):
        size_mb = a["file_size_bytes"] / (1024 * 1024)
        avg = a["avg_duration_ms"]
        if avg is not None:
            dur = (
                f"avg {_format_duration_ms(avg)} "
                f"(min {_format_duration_ms(a['min_duration_ms'])} / "
                f"max {_format_duration_ms(a['max_duration_ms'])})"
            )
        else:
            dur = "no timing"
        host_lines.append(
            f"• *{a['client_hostname']}* {a['op_type']}: "
            f"{a['success_count']} ok / {a['failure_count']} fail — "
            f"{size_mb:.1f}MB files — {dur}"
        )
    blocks.extend(_chunked_sections("*Per-host results:*", host_lines))

    if op_records:
        # Most-recent-first, so the cap keeps the freshest operations.
        ordered = sorted(op_records, key=lambda r: r.get("started_at") or 0, reverse=True)
        total = len(ordered)
        shown = ordered[:_MAX_OP_DETAIL_LINES]
        op_lines = [_op_detail_line(r) for r in shown]
        header = (
            f"*Per-operation detail (most recent {len(shown)} of {total}):*"
            if total > len(shown)
            else "*Per-operation detail:*"
        )
        blocks.extend(_chunked_sections(header, op_lines))

    return blocks


def _op_detail_line(record: dict) -> str:
    size_mb = (record.get("file_size_bytes") or 0) / (1024 * 1024)
    label = "ok" if record.get("status") == "success" else "fail"
    dur = _format_duration_ms(record.get("duration_ms"))
    started = record.get("started_at")
    when = started.strftime("%Y-%m-%d %H:%M:%S") if started is not None else "?"
    return (
        f"• *{record.get('client_hostname', '?')}* {record.get('op_type', '?')} "
        f"[{label}] — {size_mb:.1f}MB — {dur} @ {when}"
    )


def pointer_result_blocks(records: list[dict]) -> list[dict]:
    """Pointer results for the `record-results --update-slack` threaded reply.

    Reports the stale rate BESIDE the success rate rather than folding it in.
    ADR-0016 expects a non-zero stale rate, so a single "N ok / M fail" line
    would read as a network in trouble on a run that was healthy -- and would
    hide the one figure whose trend is the actual signal.

    Empty when a run had no pointer clients, so the reply of every existing
    testnet is unchanged.
    """
    from saorsa_deploy.pointer_results import OPS, summarise, summarise_by_host

    if not records:
        return []
    pooled = summarise(records)
    blocks: list[dict] = [_section("*Pointer results:*")]
    blocks.append(
        _fields_section(
            [
                (
                    "Writes",
                    f"{pooled.write_ok}/{pooled.write_attempts} ok"
                    + (
                        f" ({pooled.write_success_rate:.2f}%)"
                        if pooled.write_success_rate is not None
                        else ""
                    ),
                ),
                (
                    "Reads",
                    f"{pooled.read_ok}/{pooled.read_attempts} ok"
                    + (
                        f" ({pooled.read_success_rate:.2f}%)"
                        if pooled.read_success_rate is not None
                        else ""
                    ),
                ),
                (
                    "Stale reads",
                    f"{pooled.stale_reads}"
                    + (
                        f" ({pooled.stale_read_rate:.2f}%) — expected non-zero"
                        if pooled.stale_read_rate is not None
                        else ""
                    ),
                ),
                (
                    "Lost writes",
                    f"{pooled.lost_writes}"
                    + (
                        f" ({pooled.lost_write_rate:.2f}%)"
                        if pooled.lost_write_rate is not None
                        else ""
                    ),
                ),
                (
                    "Read-your-writes",
                    f"{pooled.read_your_writes_ok}/{pooled.read_your_writes_total}",
                ),
                ("RPC-error writes", str(pooled.rpc_error_writes)),
                ("Double payments", str(pooled.double_payments)),
                ("Paid but not stored", str(pooled.paid_but_failed_writes)),
                (
                    "Mean cost per write",
                    f"{_fmt_token(pooled.mean_ant_atto_per_write, 'ANT')}"
                    f" + {_fmt_token(pooled.mean_gas_wei_per_write, 'ETH')}",
                ),
            ]
        )
    )

    host_lines = []
    for host, summary in summarise_by_host(records).items():
        per_op = " ".join(
            f"{op}:{summary.by_op[op].ok}/{summary.by_op[op].attempts}"
            for op in OPS
            if summary.by_op[op].attempts
        )
        host_lines.append(f"• *{host}* — {per_op or 'no operations'}")
    blocks.extend(_chunked_sections("*Per-host pointer ops (ok/attempts):*", host_lines))
    return blocks


def prod_upload_result_blocks(points: list[dict]) -> list[dict]:
    """Per-host summary, grand totals, and per-file detail for prod-uploaders.

    Each point may carry a ``client_kind`` (``native``/``wasm``/absent). When a
    run has a browser-path uploader the headline counts are split per transport,
    because the two are not the same instrument and one blended figure describes
    neither. Absent reads as native, as it does everywhere else in this pipeline.
    """
    usable = [p for p in points if p.get("status") in _PROD_UPLOAD_STATUSES]
    if not usable:
        return [_section("*Results:* _no prod_upload points found._")]

    def _bucket(status: int) -> str:
        # An over-cap file is neither a success nor a failure: the transport
        # refused it, so it stays out of both totals and is reported on its own.
        return {0: "ok", 1: "partial", 2: "failed", 3: "over_cap", 4: "failed"}[status]

    per_host: dict[str, dict] = defaultdict(
        lambda: {
            "ok": 0,
            "partial": 0,
            "failed": 0,
            "over_cap": 0,
            "stored": 0,
            "cfailed": 0,
            "ant": Decimal(0),
            "gas": Decimal(0),
        }
    )
    totals = {"ok": 0, "partial": 0, "failed": 0, "over_cap": 0}
    per_transport: dict[str, dict] = defaultdict(
        lambda: {"ok": 0, "partial": 0, "failed": 0, "over_cap": 0}
    )
    grand_ant = Decimal(0)
    grand_gas = Decimal(0)
    for p in usable:
        status = p["status"]
        host = p.get("host") or "?"
        bucket = _bucket(status)
        per_host[host][bucket] += 1
        totals[bucket] += 1
        per_transport[p.get("client_kind") or "native"][bucket] += 1
        per_host[host]["stored"] += p.get("chunks_stored") or 0
        per_host[host]["cfailed"] += p.get("chunks_failed") or 0
        ant = _cost_decimal(p.get("storage_cost_atto"))
        gas = _cost_decimal(p.get("gas_cost_wei"))
        if ant is not None:
            per_host[host]["ant"] += ant
            grand_ant += ant
        if gas is not None:
            per_host[host]["gas"] += gas
            grand_gas += gas

    headline: list[tuple[str, str]]
    if per_transport.get("wasm"):
        # Split rather than blended: the browser path measured several times
        # slower and is judged against its own expectations, so a single
        # "Uploads OK" moves with whatever VM mix the run happened to use.
        headline = []
        for transport in ("native", "wasm"):
            counts = per_transport.get(transport)
            if not counts:
                continue
            label = transport.capitalize() if transport == "native" else "WASM"
            headline += [
                (f"{label} OK", str(counts["ok"])),
                (f"{label} partial", str(counts["partial"])),
                (f"{label} failed", str(counts["failed"])),
            ]
            if counts["over_cap"]:
                headline.append((f"{label} over-cap", str(counts["over_cap"])))
    else:
        headline = [
            ("Uploads OK", str(totals["ok"])),
            ("Partial", str(totals["partial"])),
            ("Failed", str(totals["failed"])),
        ]
    headline += [
        ("Storage cost", _fmt_token(grand_ant, "ANT")),
        ("Gas cost", _fmt_token(grand_gas, "ETH")),
    ]
    blocks: list[dict] = [_fields_section(headline)]

    host_lines = [
        f"• *{host}*: {d['ok']} ok / {d['partial']} partial / {d['failed']} failed"
        + (f" / {d['over_cap']} over-cap" if d["over_cap"] else "")
        + f" — chunks {d['stored']} stored / {d['cfailed']} failed "
        f"— {_fmt_token(d['ant'], 'ANT')} / {_fmt_token(d['gas'], 'ETH')}"
        for host, d in sorted(per_host.items())
    ]
    blocks.extend(_chunked_sections("*Per-host upload results:*", host_lines))

    file_lines = []
    for p in usable:
        name = p.get("file_name") or p.get("source_url") or "?"
        label = _PROD_UPLOAD_STATUS_LABEL[p["status"]]
        stored = p.get("chunks_stored") or 0
        total_chunks = p.get("total_chunks") or 0
        ant = _cost_decimal(p.get("storage_cost_atto"))
        gas = _cost_decimal(p.get("gas_cost_wei"))
        cost = ""
        if ant is not None or gas is not None:
            cost = f" — {_fmt_token(ant, 'ANT')} / {_fmt_token(gas, 'ETH')}"
        file_lines.append(
            f"• `{name}` [{label}] — {stored}/{total_chunks} chunks on {p.get('host') or '?'}{cost}"
        )
    blocks.extend(_chunked_sections("*Per-file detail:*", file_lines))
    return blocks


def canary_result_blocks(summary: dict) -> list[dict]:
    """Counts + alive/pending/confirmed-lost + hours-to-loss histogram for
    prod-canary. ``summary`` is built by the canary record path."""
    blocks: list[dict] = [
        _fields_section(
            [
                ("Uploads recorded", str(summary.get("upload_count", 0))),
                ("Chunks", str(summary.get("chunk_count", 0))),
                ("Probes", str(summary.get("probe_count", 0))),
            ]
        ),
        _fields_section(
            [
                ("Total canaries", str(summary.get("total", 0))),
                ("Alive", str(summary.get("alive", 0))),
                ("Pending failure", str(summary.get("pending", 0))),
                ("Confirmed lost", str(summary.get("confirmed_lost", 0))),
            ]
        ),
    ]
    histogram = summary.get("histogram") or []
    hist_lines = [f"• {label}: {count}" for label, count in histogram if count]
    if hist_lines:
        blocks.append(_section("*Hours-to-confirmed-loss:*\n" + "\n".join(hist_lines)))
    return blocks
