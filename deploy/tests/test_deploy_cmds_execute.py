"""Tests for the manifest step runner ``_execute`` — specifically the
``best_effort`` contract that keeps the Slack-post tail from failing the run.
"""

import io

import pytest
from rich.console import Console

import saorsa_deploy.main as main_mod
import saorsa_deploy.parallel_infra as parallel_infra
from saorsa_deploy.cmd.deploy_cmds import _execute, _format_duration
from saorsa_deploy.orchestrator.steps import Step, StepGroup


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
    _execute(_console(), steps, deployment_name="DEV-01")
    assert called == ["deploy", "slack"]


def test_best_effort_step_continues_to_later_steps(monkeypatch):
    called = _patch_runner(monkeypatch, {"slack": 1})
    steps = [
        Step(["slack"], "Post to Slack", best_effort=True),
        Step(["after"], "Later step"),
    ]
    _execute(_console(), steps, deployment_name="DEV-01")
    assert called == ["slack", "after"]  # failure did not stop the loop


def test_best_effort_swallows_unexpected_exception(monkeypatch):
    called = _patch_runner(monkeypatch, {"slack": RuntimeError("boom")})
    steps = [Step(["slack"], "Post to Slack", best_effort=True)]
    _execute(_console(), steps, deployment_name="DEV-01")  # must not propagate
    assert called == ["slack"]


def test_non_best_effort_failure_still_aborts(monkeypatch):
    _patch_runner(monkeypatch, {"deploy": 3})
    steps = [Step(["deploy"], "Deploy")]
    with pytest.raises(SystemExit) as exc:
        _execute(_console(), steps, deployment_name="DEV-01")
    assert exc.value.code == 3


# --------------------------------------------------------------------------- #
# parallel-group units are dispatched to the parallel runner
# --------------------------------------------------------------------------- #
def _infra_group() -> StepGroup:
    return StepGroup(
        name="infra",
        label="Create infrastructure (parallel)",
        lanes=[
            [Step(["bootstraps", "infra"], "Bootstraps", group="infra", lane="bn")],
            [Step(["elastic", "infra"], "Elastic", group="infra", lane="el")],
        ],
    )


def test_group_dispatched_to_parallel_runner_then_continues(monkeypatch):
    called = _patch_runner(monkeypatch, {})
    groups: list[str] = []
    monkeypatch.setattr(
        parallel_infra,
        "run_parallel_group",
        lambda console, group, deployment_name: groups.append((group.name, deployment_name)),
    )
    # A group followed by a normal sequential step.
    steps = [
        Step(["bootstraps", "infra"], "Bootstraps", group="infra", lane="bn"),
        Step(["elastic", "infra"], "Elastic", group="infra", lane="el"),
        Step(["after"], "Later step"),
    ]
    _execute(_console(), steps, deployment_name="DEV-01")
    # The group ran once via the parallel runner, named so its lane logs are
    # this deployment's and no other's.
    assert groups == [("infra", "DEV-01")]
    assert called == ["after"]  # the trailing sequential step still ran in-process


def test_group_failure_aborts_the_run(monkeypatch):
    _patch_runner(monkeypatch, {})

    def boom(console, group, deployment_name):
        raise SystemExit(4)

    monkeypatch.setattr(parallel_infra, "run_parallel_group", boom)
    steps = [
        Step(["bootstraps", "infra"], "Bootstraps", group="infra", lane="bn"),
        Step(["after"], "Later step"),
    ]
    with pytest.raises(SystemExit) as exc:
        _execute(_console(), steps, deployment_name="DEV-01")
    assert exc.value.code == 4


# --------------------------------------------------------------------------- #
# Per-step timing
# --------------------------------------------------------------------------- #
def test_format_duration_units():
    assert _format_duration(5) == "5.0s"
    assert _format_duration(42.34) == "42.3s"
    assert _format_duration(127) == "2m 07s"
    assert _format_duration(3845) == "1h 04m 05s"


def _capture_console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=120, force_terminal=False), buf


def test_prints_per_step_time_and_summary(monkeypatch):
    _patch_runner(monkeypatch, {})
    console, buf = _capture_console()
    steps = [Step(["a"], "Step A"), Step(["b"], "Step B")]
    _execute(console, steps, deployment_name="DEV-01")
    out = buf.getvalue()
    # Each step reports its own elapsed time...
    assert out.count("finished in") == 2
    # ...and a final summary lists every step plus a total.
    assert "Step timing summary" in out
    assert "Step A" in out and "Step B" in out
    assert "total" in out


def test_group_elapsed_is_reported(monkeypatch):
    _patch_runner(monkeypatch, {})
    monkeypatch.setattr(
        parallel_infra, "run_parallel_group", lambda console, group, deployment_name: None
    )
    console, buf = _capture_console()
    steps = [
        Step(["bootstraps", "infra"], "Bootstraps", group="infra", lane="bn"),
        Step(["after"], "Later step"),
    ]
    _execute(console, steps, deployment_name="DEV-01")
    out = buf.getvalue()
    assert out.count("finished in") == 2  # the group and the trailing step


def test_summary_printed_on_abort(monkeypatch):
    _patch_runner(monkeypatch, {"b": 3})
    console, buf = _capture_console()
    steps = [Step(["a"], "Step A"), Step(["b"], "Step B")]
    with pytest.raises(SystemExit):
        _execute(console, steps, deployment_name="DEV-01")
    out = buf.getvalue()
    # The partial summary is still printed so a failed run shows what it spent.
    assert "Step timing summary" in out
    assert "Step A" in out
