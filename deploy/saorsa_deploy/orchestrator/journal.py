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

from saorsa_deploy.orchestrator.steps import Step, _quote

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
    journal["steps"].append(
        {"argv": list(step.argv), "label": step.label, "status": status}
    )
    journal["updated_at"] = _now()


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
    """Compare journaled completed steps positionally against ``plan``.

    A manifest ``kind`` change invalidates the journal outright (divergence at
    index 0): the step sequences of different kinds aren't comparable even if
    an argv happened to line up.
    """
    if journal.get("kind") != kind:
        return JournalMatch(resume_index=0, divergence=0)
    entries = journal.get("steps", [])
    for i, entry in enumerate(entries):
        if i >= len(plan) or entry.get("argv") != list(plan[i].argv):
            return JournalMatch(resume_index=i, divergence=i)
    return JournalMatch(resume_index=len(entries))


def _render_argv(argv: list[str]) -> str:
    return " ".join(_quote(str(token)) for token in ["saorsa-deploy", *argv])


def render_journal_summary(journal: dict, plan: list[Step]) -> list[str]:
    """Plain-text lines describing the journal, for the resume prompt."""
    entries = journal.get("steps", [])
    lines = [
        f"Journal found for {journal.get('name')} "
        f"(created {journal.get('created_at')}, "
        f"{len(entries)}/{len(plan)} steps completed):"
    ]
    for i, entry in enumerate(entries, 1):
        suffix = ""
        if entry.get("status") == STATUS_BEST_EFFORT_FAILED:
            suffix = " (best-effort step failed; run continued)"
        lines.append(f"  {i:>2}. ✓ {entry.get('label')}{suffix}")
    return lines


def render_divergence(
    journal: dict, kind: str, plan: list[Step], match: JournalMatch
) -> list[str]:
    """Plain-text lines explaining why the journal doesn't match the plan."""
    journal_kind = journal.get("kind")
    if journal_kind != kind:
        return [
            "The journal does not match the computed plan: it was recorded for "
            f"a '{journal_kind}' deployment, but the manifest is '{kind}'."
        ]
    entries = journal.get("steps", [])
    i = match.divergence
    lines = [
        "The journal does not match the computed plan "
        f"(first divergence at step {i + 1}):"
    ]
    if i >= len(plan):
        lines.append(
            f"  the journal records {len(entries)} completed step(s), but the "
            f"plan only has {len(plan)} step(s)."
        )
    else:
        entry = entries[i]
        lines.append(f"  journal: $ {_render_argv(entry.get('argv', []))}")
        lines.append(f"  plan:    $ {plan[i].render()}")
    lines.append(
        "The manifest (or saorsa-deploy itself) has changed since the journal "
        "was written."
    )
    return lines
