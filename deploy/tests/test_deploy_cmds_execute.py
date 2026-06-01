"""Tests for the manifest step runner ``_execute`` — specifically the
``best_effort`` contract that keeps the Slack-post tail from failing the run.
"""

import io

import pytest
from rich.console import Console

import saorsa_deploy.main as main_mod
from saorsa_deploy.cmd.deploy_cmds import _execute
from saorsa_deploy.orchestrator.steps import Step


def _console() -> Console:
    return Console(file=io.StringIO())


def _patch_runner(monkeypatch, behaviour):
    """Patch the in-process CLI re-entry with a stub.

    ``behaviour`` maps the first argv token to either an exit code (raises
    ``SystemExit``) or an ``Exception`` instance (raised as-is). Records the
    order of invoked argv[0]s in the returned list.
    """
    called: list[str] = []

    def fake_main(argv):
        called.append(argv[0])
        outcome = behaviour.get(argv[0])
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome:
            raise SystemExit(outcome)

    monkeypatch.setattr(main_mod, "main", fake_main)
    return called


def test_best_effort_step_failure_does_not_abort(monkeypatch):
    called = _patch_runner(monkeypatch, {"slack": 1})
    steps = [
        Step(["deploy"], "Deploy"),
        Step(["slack"], "Post to Slack", best_effort=True),
    ]
    # No SystemExit despite the slack step exiting 1.
    _execute(_console(), steps)
    assert called == ["deploy", "slack"]


def test_best_effort_step_continues_to_later_steps(monkeypatch):
    called = _patch_runner(monkeypatch, {"slack": 1})
    steps = [
        Step(["slack"], "Post to Slack", best_effort=True),
        Step(["after"], "Later step"),
    ]
    _execute(_console(), steps)
    assert called == ["slack", "after"]  # failure did not stop the loop


def test_best_effort_swallows_unexpected_exception(monkeypatch):
    called = _patch_runner(monkeypatch, {"slack": RuntimeError("boom")})
    steps = [Step(["slack"], "Post to Slack", best_effort=True)]
    _execute(_console(), steps)  # must not propagate
    assert called == ["slack"]


def test_non_best_effort_failure_still_aborts(monkeypatch):
    _patch_runner(monkeypatch, {"deploy": 3})
    steps = [Step(["deploy"], "Deploy")]
    with pytest.raises(SystemExit) as exc:
        _execute(_console(), steps)
    assert exc.value.code == 3
