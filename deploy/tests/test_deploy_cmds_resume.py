"""Tests for the step-journal resume flow in ``deployments up`` / ``down``.

The journal lets a re-run after a mid-sequence failure skip completed steps.
Because journals can outlive their deployment (legacy destroys, abandoned
runs) and names are reused, a journal hit must never silently skip: these
tests pin the prompt/--resume/refusal contract.
"""

import io
import json
from types import SimpleNamespace

import pytest
from rich.console import Console

import saorsa_deploy.cmd.deploy_cmds as deploy_cmds
import saorsa_deploy.main as main_mod
from saorsa_deploy.orchestrator import journal as step_journal
from saorsa_deploy.orchestrator.steps import Step


def _args(**overrides):
    defaults = dict(config="manifest.yaml", set=None, name=None, dry_run=False, resume=False)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _manifest():
    return SimpleNamespace(name="DEV-01", kind="testnet")


def _plan() -> list[Step]:
    return [
        Step(["build", "ant-node", "--name", "DEV-01"], "Build ant-node from branch"),
        Step(["nodes", "infra", "--name", "DEV-01"], "Nodes infra"),
    ]


def _journal_with(plan: list[Step], count: int) -> dict:
    journal = step_journal.new_journal("DEV-01", "testnet")
    for step in plan[:count]:
        step_journal.record_step(journal, step)
    return journal


@pytest.fixture
def harness(monkeypatch):
    """Patch every boundary of cmd_up/cmd_down and record what happened."""
    plan = _plan()
    record = SimpleNamespace(
        executed=[],  # argv[0] of each step run through the CLI
        saved=[],  # journal snapshots passed to save_step_journal
        deleted=[],  # names passed to delete_step_journal
        journal=None,  # what load_step_journal returns
        plan=plan,
        output=io.StringIO(),
        behaviour={},  # argv[0] -> exit code for the fake CLI
    )

    monkeypatch.setattr(deploy_cmds, "Console", lambda: Console(file=record.output))
    monkeypatch.setattr(deploy_cmds, "load_manifest", lambda *a, **k: _manifest())
    monkeypatch.setattr(deploy_cmds, "build_deploy_steps", lambda m, **k: record.plan)
    monkeypatch.setattr(deploy_cmds, "build_destroy_steps", lambda m, **k: record.plan)
    monkeypatch.setattr(deploy_cmds, "load_step_journal", lambda name: record.journal)
    monkeypatch.setattr(
        deploy_cmds,
        "save_step_journal",
        lambda name, journal: record.saved.append(json.loads(json.dumps(journal))),
    )
    monkeypatch.setattr(
        deploy_cmds, "delete_step_journal", lambda name: record.deleted.append(name)
    )

    def fake_main(argv):
        record.executed.append(argv[0])
        code = record.behaviour.get(argv[0])
        if code:
            raise SystemExit(code)

    monkeypatch.setattr(main_mod, "main", fake_main)
    return record


def _set_tty(monkeypatch, value: bool):
    monkeypatch.setattr(deploy_cmds.sys.stdin, "isatty", lambda: value)


class TestFreshRun:
    def test_executes_all_steps_and_cleans_up(self, harness):
        deploy_cmds.cmd_up(_args())
        assert harness.executed == ["build", "nodes"]
        # one save per completed step, journal deleted on success
        assert [len(s["steps"]) for s in harness.saved] == [1, 2]
        assert harness.deleted == ["DEV-01"]

    def test_failed_step_leaves_journal_behind(self, harness):
        harness.behaviour = {"nodes": 2}
        with pytest.raises(SystemExit) as exc:
            deploy_cmds.cmd_up(_args())
        assert exc.value.code == 2
        # only the completed step was journaled; nothing was deleted
        assert [len(s["steps"]) for s in harness.saved] == [1]
        assert harness.saved[-1]["steps"][0]["label"] == "Build ant-node from branch"
        assert harness.deleted == []

    def test_best_effort_failure_is_journaled_as_completed(self, harness):
        harness.plan = [
            Step(["deploy"], "Deploy"),
            Step(["slack"], "Post to Slack", best_effort=True),
        ]
        harness.behaviour = {"slack": 1}
        deploy_cmds.cmd_up(_args())
        assert [e["status"] for e in harness.saved[-1]["steps"]] == [
            step_journal.STATUS_SUCCEEDED,
            step_journal.STATUS_BEST_EFFORT_FAILED,
        ]
        assert harness.deleted == ["DEV-01"]

    def test_dry_run_never_touches_the_journal(self, harness, monkeypatch):
        monkeypatch.setattr(
            deploy_cmds,
            "load_step_journal",
            lambda name: pytest.fail("dry-run must not read the journal"),
        )
        deploy_cmds.cmd_up(_args(dry_run=True))
        assert harness.executed == []
        assert harness.deleted == []


class TestJournalHit:
    def test_resume_flag_skips_completed_prefix(self, harness):
        harness.journal = _journal_with(harness.plan, 1)
        deploy_cmds.cmd_up(_args(resume=True))
        assert harness.executed == ["nodes"]  # build skipped
        assert harness.deleted == ["DEV-01"]
        # the resumed run appends to the same journal
        assert [len(s["steps"]) for s in harness.saved] == [2]

    def test_non_tty_without_resume_refuses(self, harness, monkeypatch):
        _set_tty(monkeypatch, False)
        harness.journal = _journal_with(harness.plan, 1)
        with pytest.raises(SystemExit) as exc:
            deploy_cmds.cmd_up(_args())
        assert exc.value.code == 1
        assert harness.executed == []
        assert harness.deleted == []
        assert "--resume" in harness.output.getvalue()

    def test_interactive_resume_choice(self, harness, monkeypatch):
        _set_tty(monkeypatch, True)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "r")
        harness.journal = _journal_with(harness.plan, 1)
        deploy_cmds.cmd_up(_args())
        assert harness.executed == ["nodes"]

    def test_interactive_delete_choice_runs_everything(self, harness, monkeypatch):
        _set_tty(monkeypatch, True)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "d")
        harness.journal = _journal_with(harness.plan, 1)
        deploy_cmds.cmd_up(_args())
        assert harness.executed == ["build", "nodes"]
        # deleted once at the prompt, once on successful completion
        assert harness.deleted == ["DEV-01", "DEV-01"]

    def test_interactive_abort(self, harness, monkeypatch):
        _set_tty(monkeypatch, True)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "a")
        harness.journal = _journal_with(harness.plan, 1)
        with pytest.raises(SystemExit) as exc:
            deploy_cmds.cmd_up(_args())
        assert exc.value.code == 1
        assert harness.executed == []

    def test_journal_summary_is_shown(self, harness, monkeypatch):
        _set_tty(monkeypatch, True)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "r")
        harness.journal = _journal_with(harness.plan, 1)
        deploy_cmds.cmd_up(_args())
        out = harness.output.getvalue()
        assert "Journal found for DEV-01" in out
        assert "Build ant-node from branch" in out


class TestJournalMismatch:
    def test_resume_flag_resumes_from_longest_matching_prefix(self, harness):
        journal = _journal_with(harness.plan, 2)
        journal["steps"][1]["argv"] = ["nodes", "infra", "--name", "OTHER"]
        harness.journal = journal
        deploy_cmds.cmd_up(_args(resume=True))
        assert harness.executed == ["nodes"]  # step 1 matched, step 2 reruns
        # the diverged tail was trimmed before appending the rerun result
        assert [e["argv"][-1] for e in harness.saved[-1]["steps"]] == [
            "DEV-01",
            "DEV-01",
        ]

    def test_divergence_is_explained(self, harness, monkeypatch):
        _set_tty(monkeypatch, False)
        journal = _journal_with(harness.plan, 2)
        journal["steps"][1]["argv"] = ["nodes", "infra", "--name", "OTHER"]
        harness.journal = journal
        with pytest.raises(SystemExit):
            deploy_cmds.cmd_up(_args())
        assert "does not match the computed plan" in harness.output.getvalue()

    def test_kind_change_with_resume_starts_from_scratch(self, harness):
        journal = _journal_with(harness.plan, 2)
        journal["kind"] = "testnet-extension"
        harness.journal = journal
        deploy_cmds.cmd_up(_args(resume=True))
        assert harness.executed == ["build", "nodes"]
