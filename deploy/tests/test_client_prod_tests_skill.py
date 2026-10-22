"""Regression guards on the `run-client-prod-tests` skill's teardown rule.

Step 9 used to tell the agent to **leave a failed deployment up** unconditionally, on the premise
that the VM, its per-op logs and its InfluxDB were the only forensic surface. They are not: the logs
are pulled over SSH before teardown anyway, and `record-results` has copied every per-operation row
into Postgres by Step 8. On 2026-10-22 that rule kept an `s-4vcpu-8gb` client VM and an
`s-2vcpu-4gb` monitoring VM billing for a further ~2h55m after STG-WUL-01 (id 697) failed one of
three browser-path uploads — for evidence the run had already captured in full.

So the skill now captures the forensics explicitly (Step 8b), checks the capture, and then destroys
the deployment, with exactly two exceptions that keep one standing: the capture failed, or nothing
was recorded. These are text assertions over the skill file, in the style of
`test_state_dir.py`'s source-tree guard: the skill is prose an agent executes, so the invariant can
only be asserted against the prose.
"""

import re
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parents[2] / ".claude/skills"
SKILL_PATH = SKILLS_DIR / "run-client-prod-tests/SKILL.md"

#: A wheel-only install or a sparse clone has no `.claude/`, and the suite must not fail there.
#: The predicate is the skills DIRECTORY, never the skill file: a checkout that has the directory
#: and not this skill has moved or renamed it, and that must fail loudly, or every guard below
#: silently stops asserting anything while CI stays green.
pytestmark = pytest.mark.skipif(
    not SKILLS_DIR.is_dir(), reason=f"{SKILLS_DIR} is not in this checkout"
)

#: The phrases the old rule was written in. Neither may come back in any form.
RETIRED_PHRASES = ("leave it up", "leave that deployment standing")

#: The two exceptions Step 9 allows, named as the report has to name them.
EXCEPTIONS = ("capture failed", "no results recorded")


def _text() -> str:
    assert SKILL_PATH.is_file(), f"{SKILL_PATH} has moved — update SKILL_PATH in this module"
    return SKILL_PATH.read_text()


def _flat(text: str) -> str:
    """Whitespace-normalised, so a phrase straddling the ~100-column wrap still matches."""
    return re.sub(r"\s+", " ", text)


def _section(text: str, heading: str) -> str:
    """The body of one `## ` section, up to the next `## ` heading."""
    start = text.index(heading)
    rest = text[start + len(heading) :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def test_skill_is_present_and_readable():
    """Guards the path above: a later move must break this, not silently skip the module."""
    assert SKILL_PATH.is_file(), f"{SKILL_PATH} has moved — update SKILL_PATH in this module"
    assert _text().strip(), f"{SKILL_PATH} is empty"


def test_failed_deployments_are_torn_down():
    step9 = _section(_text(), "## Step 9")
    failed_bullet = step9[step9.index("- **Failed**") :]
    next_bullet = failed_bullet.find("\n- **")
    if next_bullet != -1:
        failed_bullet = failed_bullet[:next_bullet]
    assert "deployments down --config" in failed_bullet, (
        "Step 9's **Failed** bullet no longer destroys the deployment — a failed client prod test "
        "would be left billing again"
    )


def test_the_retired_phrases_are_gone():
    flat = _flat(_text())
    present = [phrase for phrase in RETIRED_PHRASES if phrase in flat]
    assert present == [], (
        f"the retired unconditional-keep phrasing is back in the skill: {present}; "
        "a failed deployment is captured (Step 8b) and then destroyed (Step 9)"
    )


def test_a_capture_step_precedes_teardown():
    text = _text()
    assert "## Step 8b" in text, "the Step 8b forensic-capture heading is gone"
    assert text.index("## Step 8b") < text.index("## Step 9"), (
        "Step 8b must come before Step 9 — the capture is the precondition for the teardown"
    )
    step8b = _section(text, "## Step 8b")
    for token in ("client-test-forensics", "collect-client-logs"):
        assert token in step8b, (
            f"Step 8b no longer mentions {token!r} — it must name the capture destination "
            "and the command that archives the per-op log directory"
        )


def test_still_running_is_conditional():
    """A `STILL RUNNING` report line must always say which of the two exceptions it hit."""
    text = _text()
    assert "kept for investigation" not in text, (
        "the bare `kept for investigation` teardown form is back; a still-running deployment "
        f"must name one of {EXCEPTIONS}"
    )
    for line_no, line in enumerate(text.splitlines(), start=1):
        if "STILL RUNNING" not in line:
            continue
        assert any(exc in line for exc in EXCEPTIONS), (
            f"{SKILL_PATH.name}:{line_no} reports STILL RUNNING without naming an exception "
            f"from {EXCEPTIONS}: {line.strip()!r}"
        )


def test_both_exceptions_are_named():
    step9 = _flat(_section(_text(), "## Step 9"))
    missing = [exc for exc in EXCEPTIONS if exc not in step9]
    assert missing == [], (
        f"Step 9 no longer names these exceptions: {missing}; they are the only two cases that "
        "may leave a client prod test's infrastructure standing"
    )
