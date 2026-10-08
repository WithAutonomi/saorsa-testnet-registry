"""Step journal for resumable ``deployments up`` runs.

A journal records the steps of a manifest deploy run that have completed, so a
re-run after a mid-sequence failure can offer to skip them (notably the slow
binary-build steps) instead of re-executing everything.

This module is pure logic — journal construction, per-step recording, and
matching a journal against a freshly computed plan. Persistence (S3) lives in
:mod:`saorsa_deploy.state`; the interactive prompt lives in
:mod:`saorsa_deploy.cmd.deploy_cmds`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from saorsa_deploy.orchestrator.steps import Step, StepGroup, _quote, iter_units

STATUS_SUCCEEDED = "succeeded"
# A best-effort step that failed but let the run continue (e.g. the Slack post)
# is recorded too: the run treated it as done, so a resume must not rerun it.
STATUS_BEST_EFFORT_FAILED = "best_effort_failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_journal(name: str, kind: str) -> dict:
    now = _now()
    return {
        "name": name,
        "kind": kind,
        "created_at": now,
        "updated_at": now,
        "steps": [],
    }


def record_step(journal: dict, step: Step, status: str = STATUS_SUCCEEDED) -> None:
    """Append a completed step to the journal (in-place)."""
    journal["steps"].append({"argv": list(step.argv), "label": step.label, "status": status})
    journal["updated_at"] = _now()


def _group_argvs(group: StepGroup) -> list[list[list[str]]]:
    return [[list(s.argv) for s in lane] for lane in group.lanes]


def record_group(journal: dict, group: StepGroup, status: str = STATUS_SUCCEEDED) -> None:
    """Append a completed parallel group to the journal as a single unit.

    Recorded only once every lane succeeded, so the group is resumable
    atomically: a re-run either skips the whole group or re-executes it.
    """
    journal["steps"].append(
        {
            "group": group.name,
            "label": group.label,
            "argvs": _group_argvs(group),
            "status": status,
        }
    )
    journal["updated_at"] = _now()


def _entry_matches_unit(entry: dict, unit) -> bool:
    """Whether a journal entry corresponds to the given plan unit."""
    if isinstance(unit, StepGroup):
        return entry.get("group") == unit.name and entry.get("argvs") == _group_argvs(unit)
    # A plain Step: a group entry never matches a Step unit.
    return "group" not in entry and entry.get("argv") == list(unit.argv)


@dataclass(frozen=True)
class JournalMatch:
    """Result of comparing a journal against a freshly computed plan.

    ``resume_index`` is the plan index execution should resume from — the
    length of the longest prefix of plan steps whose argv exactly matches the
    journaled completed steps, positionally. ``divergence`` is the index of the
    first journal entry that does not match the plan (``None`` when the whole
    journal is a prefix of the plan).
    """

    resume_index: int
    divergence: int | None = None

    @property
    def matches(self) -> bool:
        return self.divergence is None


def match_journal(journal: dict, kind: str, plan: list[Step]) -> JournalMatch:
    """Compare journaled completed units positionally against ``plan``.

    The plan is collapsed into execution units (:func:`iter_units`) so a parallel
    group counts as one unit; the journal records one entry per unit. For an
    ungrouped plan units are 1:1 with steps, so the index space is unchanged.

    A manifest ``kind`` change invalidates the journal outright (divergence at
    index 0): the step sequences of different kinds aren't comparable even if
    an argv happened to line up.
    """
    if journal.get("kind") != kind:
        return JournalMatch(resume_index=0, divergence=0)
    units = iter_units(plan)
    entries = journal.get("steps", [])
    for i, entry in enumerate(entries):
        if i >= len(units) or not _entry_matches_unit(entry, units[i]):
            return JournalMatch(resume_index=i, divergence=i)
    return JournalMatch(resume_index=len(entries))


def _render_argv(argv: list[str]) -> str:
    return " ".join(_quote(str(token)) for token in ["saorsa-deploy", *argv])


def _render_entry(entry: dict) -> str:
    """Render a journal entry's command(s) — a plain step or a parallel group."""
    if "group" in entry:
        return "  |  ".join(
            " && ".join(_render_argv(argv) for argv in lane) for lane in entry.get("argvs", [])
        )
    return _render_argv(entry.get("argv", []))


def _render_unit(unit) -> str:
    return unit.render()


def render_journal_summary(journal: dict, plan: list[Step]) -> list[str]:
    """Plain-text lines describing the journal, for the resume prompt."""
    entries = journal.get("steps", [])
    total = len(iter_units(plan))
    lines = [
        f"Journal found for {journal.get('name')} "
        f"(created {journal.get('created_at')}, "
        f"{len(entries)}/{total} steps completed):"
    ]
    for i, entry in enumerate(entries, 1):
        suffix = ""
        if entry.get("status") == STATUS_BEST_EFFORT_FAILED:
            suffix = " (best-effort step failed; run continued)"
        lines.append(f"  {i:>2}. ✓ {entry.get('label')}{suffix}")
    return lines


def render_divergence(journal: dict, kind: str, plan: list[Step], match: JournalMatch) -> list[str]:
    """Plain-text lines explaining why the journal doesn't match the plan."""
    journal_kind = journal.get("kind")
    if journal_kind != kind:
        return [
            "The journal does not match the computed plan: it was recorded for "
            f"a '{journal_kind}' deployment, but the manifest is '{kind}'."
        ]
    entries = journal.get("steps", [])
    units = iter_units(plan)
    i = match.divergence
    lines = [f"The journal does not match the computed plan (first divergence at step {i + 1}):"]
    if i >= len(units):
        lines.append(
            f"  the journal records {len(entries)} completed step(s), but the "
            f"plan only has {len(units)} step(s)."
        )
    else:
        entry = entries[i]
        lines.append(f"  journal: $ {_render_entry(entry)}")
        lines.append(f"  plan:    $ {_render_unit(units[i])}")
    lines.append(
        "The manifest (or saorsa-deploy itself) has changed since the journal was written."
    )
    return lines
