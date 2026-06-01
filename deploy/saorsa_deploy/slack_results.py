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

from saorsa_deploy.cmd.slack_post import (
    _chunked_sections,
    _fields_section,
    _section,
)

_PROD_UPLOAD_STATUS_LABEL = {0: "ok", 1: "partial", 2: "dl-failed"}


def file_ops_result_blocks(aggregates: list[dict]) -> list[dict]:
    """Full per-(host, op_type) breakdown for testnet / downloader file_ops."""
    if not aggregates:
        return [_section("*Results:* _no upload/download points found._")]

    total_ok = sum(a["success_count"] for a in aggregates)
    total_fail = sum(a["failure_count"] for a in aggregates)
    blocks: list[dict] = [
        _fields_section(
            [
                ("Total successes", str(total_ok)),
                ("Total failures", str(total_fail)),
            ]
        )
    ]

    lines: list[str] = []
    for a in sorted(aggregates, key=lambda x: (x["client_hostname"], x["op_type"])):
        size_mb = a["file_size_bytes"] / (1024 * 1024)
        avg = a["avg_duration_ms"]
        if avg is not None:
            dur = f"avg {avg}ms (min {a['min_duration_ms']} / max {a['max_duration_ms']})"
        else:
            dur = "no timing"
        lines.append(
            f"• *{a['client_hostname']}* {a['op_type']}: "
            f"{a['success_count']} ok / {a['failure_count']} fail — "
            f"{size_mb:.1f}MB files — {dur}"
        )
    blocks.extend(_chunked_sections("*Per-host results:*", lines))
    return blocks


def prod_upload_result_blocks(points: list[dict]) -> list[dict]:
    """Per-host summary, grand totals, and per-file detail for prod-uploaders."""
    usable = [p for p in points if p.get("status") in (0, 1, 2)]
    if not usable:
        return [_section("*Results:* _no prod_upload points found._")]

    per_host: dict[str, dict] = defaultdict(
        lambda: {"ok": 0, "partial": 0, "failed": 0, "stored": 0, "cfailed": 0}
    )
    totals = {"ok": 0, "partial": 0, "failed": 0}
    for p in usable:
        status = p["status"]
        host = p.get("host") or "?"
        bucket = {0: "ok", 1: "partial", 2: "failed"}[status]
        per_host[host][bucket] += 1
        totals[bucket] += 1
        per_host[host]["stored"] += p.get("chunks_stored") or 0
        per_host[host]["cfailed"] += p.get("chunks_failed") or 0

    blocks: list[dict] = [
        _fields_section(
            [
                ("Uploads OK", str(totals["ok"])),
                ("Partial", str(totals["partial"])),
                ("Failed", str(totals["failed"])),
            ]
        )
    ]

    host_lines = [
        f"• *{host}*: {d['ok']} ok / {d['partial']} partial / {d['failed']} failed "
        f"— chunks {d['stored']} stored / {d['cfailed']} failed"
        for host, d in sorted(per_host.items())
    ]
    blocks.extend(_chunked_sections("*Per-host upload results:*", host_lines))

    file_lines = []
    for p in usable:
        name = p.get("file_name") or p.get("source_url") or "?"
        label = _PROD_UPLOAD_STATUS_LABEL[p["status"]]
        stored = p.get("chunks_stored") or 0
        total_chunks = p.get("total_chunks") or 0
        file_lines.append(
            f"• `{name}` [{label}] — {stored}/{total_chunks} chunks on "
            f"{p.get('host') or '?'}"
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
        blocks.append(
            _section("*Hours-to-confirmed-loss:*\n" + "\n".join(hist_lines))
        )
    return blocks
