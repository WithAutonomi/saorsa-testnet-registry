"""Render a gate comparison as a fixed-format markdown report (V2-721).

"Same inputs must always produce an identical report." That is a hard
requirement, not a nicety: the report is attached to the release issue as
evidence, and a report whose wording drifted between runs could not be diffed
or trusted.

So everything here is ordered deterministically, every float is formatted to a
fixed precision, and nothing consults the clock, the environment, or dict
insertion order. The one timestamp that appears is passed in by the caller
rather than read from ``datetime.now()`` -- a report that embedded its own
generation time would never be byte-identical twice, which would make the
determinism guarantee untestable.

The renderer is a pure function of the verdict payload, so a stored verdict can
be re-rendered later without recomputing anything.
"""
from __future__ import annotations

from saorsa_deploy.gate_comparison import FAIL, GRAY, NOT_ASSESSED, PASS

# Fixed column order for the gate tables.
_VERDICT_ORDER = {FAIL: 0, GRAY: 1, NOT_ASSESSED: 2, PASS: 3}


def _fmt(value: float | None, unit: str) -> str:
    """Format a metric value at a fixed precision for its unit.

    Fixed rather than adaptive: a value that rendered as "1.2" on one run and
    "1.23" on the next would break byte-identity for no reason.
    """
    if value is None:
        return "-"
    if unit == "bytes":
        return f"{value / 1024**3:.3f} GiB"
    if unit == "bytes/GB":
        return f"{value / 1024**3:.3f} GiB/GB"
    if unit == "ms":
        return f"{value / 1000:.3f} s"
    if unit in ("%", "pp", "%/h"):
        return f"{value:.3f} {unit}"
    if unit == "count":
        # Whole occurrences, thousands-separated: a log-string tally runs to
        # six figures and "71309.000" is harder to read than "71,309".
        return f"{value:,.0f}"
    return f"{value:.3f}"


def _fmt_delta(value: float | None, unit: str) -> str:
    if value is None:
        return "-"
    suffix = "pp" if unit == "pp" else "%"
    return f"{value:+.2f} {suffix}"


def _fmt_threshold(value: float | None, unit: str) -> str:
    if value is None:
        return "-"
    suffix = "pp" if unit == "pp" else "%"
    return f"> {value:g} {suffix}"


def _escape(text: str) -> str:
    """Neutralise pipes so a detail string cannot break the table layout."""
    return (text or "").replace("|", "\\|").replace("\n", " ")


def _sort_key(gate: dict) -> tuple:
    """Worst-first within a table, then alphabetical.

    Severity first so a reader sees what blocks the ship without scanning, and
    the name as a tiebreak so the order never depends on how the gates happened
    to be appended.
    """
    return (_VERDICT_ORDER.get(gate["verdict"], 9), gate["gate"])


def _gate_table(gates: list[dict]) -> list[str]:
    lines = [
        "| Gate | Verdict | Candidate | Baseline | Delta | Fails at | Detail |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for gate in sorted(gates, key=_sort_key):
        unit = gate.get("unit", "")
        detail = _escape(gate.get("detail", ""))
        statistic = gate.get("statistic")
        # Surfaced inline rather than buried: the process spec names node CPU
        # *median* and node RSS *p50*, and a mean silently standing in for
        # either would be exactly the kind of substitution nobody notices.
        if statistic in ("mean", "p50", "p50~"):
            marker = f"[{statistic}]"
            detail = f"{marker} {detail}".strip()
        lines.append(
            "| {gate} | {verdict} | {cand} | {base} | {delta} | {thr} | {detail} |".format(
                gate=_escape(gate["gate"]),
                verdict=gate["verdict"],
                cand=_fmt(gate.get("candidate"), unit),
                base=_fmt(gate.get("baseline"), unit),
                delta=_fmt_delta(gate.get("delta_percent"), unit),
                thr=_fmt_threshold(gate.get("threshold"), unit),
                detail=detail or "-",
            )
        )
    return lines


def render_report(verdict: dict, computed_at: str | None = None) -> str:
    """Render the full fixed-format markdown report for a verdict payload.

    ``computed_at`` is an optional ISO timestamp supplied by the caller. Omit it
    for a byte-identical report; pass it when attaching the report somewhere a
    reader needs to know when it was produced.
    """
    candidate = verdict["candidate"]
    baseline = verdict["baseline"]
    counts = verdict["counts"]
    gates = verdict["gates"]

    hard = [g for g in gates if g["kind"] == "hard"]
    soft = [g for g in gates if g["kind"] == "soft"]

    lines: list[str] = []
    lines.append("# Staging gate comparison")
    lines.append("")
    lines.append(f"**Overall: {verdict['overall']}**")
    lines.append("")
    lines.append(
        f"- Candidate: `{candidate['name']}` (deployment {candidate['deployment_id']})"
    )
    lines.append(
        f"- Baseline: `{baseline['name']}` (deployment {baseline['deployment_id']})"
    )
    lines.append(f"- Window label: `{verdict['window_label']}`")
    if candidate.get("window_start") and candidate.get("window_end"):
        lines.append(
            f"- Candidate window: {candidate['window_start']} to "
            f"{candidate['window_end']}"
        )
    if baseline.get("window_start") and baseline.get("window_end"):
        lines.append(
            f"- Baseline window: {baseline['window_start']} to "
            f"{baseline['window_end']}"
        )
    for label, run in (("Candidate", candidate), ("Baseline", baseline)):
        coverage = run.get("activity_coverage")
        if coverage is not None:
            lines.append(f"- {label} activity coverage: {100 * coverage:.1f}% of window")
    lines.append(f"- Threshold config version: {verdict['threshold_config_version']}")
    lines.append(f"- Outbound ratio metric: `{verdict['outbound_ratio_metric']}`")
    if computed_at:
        lines.append(f"- Computed at: {computed_at}")
    lines.append("")
    lines.append(
        f"{counts[PASS]} pass, {counts[FAIL]} fail, {counts[GRAY]} gray, "
        f"{counts[NOT_ASSESSED]} not assessed."
    )
    lines.append("")

    if verdict.get("warnings"):
        lines.append("## Warnings")
        lines.append("")
        for warning in verdict["warnings"]:
            lines.append(f"> {warning}")
            lines.append("")

    lines.append("## Hard gates")
    lines.append("")
    lines.append("Failure blocks the ship; no override exists.")
    lines.append("")
    lines.extend(_gate_table(hard) if hard else ["_No hard gates evaluated._"])
    lines.append("")

    lines.append("## Soft gates")
    lines.append("")
    lines.append(
        "Compared against the baseline, subject to the noise rule. Overridable "
        "only via the recorded path: RM written justification, one engineer "
        "co-sign, `gate-override` label, and a P1 debt issue blocking the next "
        "train's cut."
    )
    lines.append("")
    lines.extend(_gate_table(soft) if soft else ["_No soft gates evaluated._"])
    lines.append("")

    gray = [g for g in gates if g["verdict"] == GRAY]
    if gray:
        lines.append("## GRAY -- RM judgment required")
        lines.append("")
        lines.append(
            "Each of these tripped one of the two noise-rule criteria but not "
            "the other. Record the judgment on the release issue either way."
        )
        lines.append("")
        for gate in sorted(gray, key=lambda g: g["gate"]):
            noise = gate.get("noise") or {}
            sigma = noise.get("sigma_percent")
            unit = gate.get("unit", "")
            delta = _fmt_delta(gate.get("delta_percent"), unit)
            threshold = _fmt_threshold(gate.get("threshold"), unit)
            lines.append(
                f"- **{gate['gate']}**: {delta} against a {threshold} threshold"
                + (f" and a {sigma:.2f}% 2-sigma band" if sigma is not None else "")
                + f" over {noise.get('history_runs', 0)} accepted runs."
            )
        lines.append("")

    unassessed = [g for g in gates if g["verdict"] == NOT_ASSESSED]
    if unassessed:
        lines.append("## Not assessed")
        lines.append("")
        lines.append(
            "These are **not passes**. Each names why it could not be measured."
        )
        lines.append("")
        for gate in sorted(unassessed, key=lambda g: g["gate"]):
            lines.append(f"- **{gate['gate']}**: {_escape(gate.get('detail', ''))}")
        lines.append("")

    return "\n".join(lines)
