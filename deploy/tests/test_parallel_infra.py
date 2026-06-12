"""Tests for the parallel infrastructure group runner.

Covers the concurrency contract: every lane runs, a lane stops at its first
failing step (so nodes infra never runs if bootstraps infra failed in the same
lane), independent lanes still run, and any failure aborts the deploy after
displaying the captured output.
"""

import io
from pathlib import Path

import pytest
from rich.console import Console

from saorsa_deploy import parallel_infra
from saorsa_deploy.orchestrator.steps import Step, StepGroup


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=200)


def _group() -> StepGroup:
    return StepGroup(
        name="infra",
        label="Create infrastructure (parallel)",
        lanes=[
            [
                Step(["bootstraps", "infra", "--name", "X"], "Bootstraps"),
                Step(["nodes", "infra", "--name", "X"], "Nodes"),
            ],
            [Step(["elastic", "infra", "--name", "X"], "Elastic")],
            [Step(["monitoring", "infra", "--name", "X"], "Monitoring")],
        ],
    )


def _stub(monkeypatch, fail_first_token=None, code=1):
    """Stub the subprocess runner; fail steps whose argv[0] == fail_first_token."""
    ran: list[tuple] = []

    def fake(step: Step, log_path: Path) -> int:
        ran.append(tuple(step.argv[:2]))
        if fail_first_token is not None and step.argv[0] == fail_first_token:
            Path(log_path).write_text(f"FAILED OUTPUT for {step.argv[0]} SENTINEL42")
            return code
        Path(log_path).write_text("ok")
        return 0

    monkeypatch.setattr(parallel_infra, "_run_step_subprocess", fake)
    return ran


def test_all_lanes_succeed_runs_every_step(monkeypatch, tmp_path):
    ran = _stub(monkeypatch)
    parallel_infra.run_parallel_group(_console(), _group(), log_dir=tmp_path)
    assert ("bootstraps", "infra") in ran
    assert ("nodes", "infra") in ran  # sequential second step in lane 0 ran
    assert ("elastic", "infra") in ran
    assert ("monitoring", "infra") in ran
    assert len(ran) == 4


def test_lane_stops_at_first_failure(monkeypatch, tmp_path):
    ran = _stub(monkeypatch, fail_first_token="bootstraps", code=2)
    with pytest.raises(SystemExit) as exc:
        parallel_infra.run_parallel_group(_console(), _group(), log_dir=tmp_path)
    assert exc.value.code == 2
    # bootstraps failed, so nodes (same lane, sequential) must NOT run...
    assert ("nodes", "infra") not in ran
    # ...but the independent lanes still ran to completion.
    assert ("elastic", "infra") in ran
    assert ("monitoring", "infra") in ran


def test_failure_displays_captured_output(monkeypatch, tmp_path):
    _stub(monkeypatch, fail_first_token="elastic", code=1)
    console = _console()
    with pytest.raises(SystemExit):
        parallel_infra.run_parallel_group(console, _group(), log_dir=tmp_path)
    out = console.file.getvalue()
    assert "FAILED" in out
    assert "SENTINEL42" in out  # the failed lane's log content is surfaced


def test_first_failure_code_propagates(monkeypatch, tmp_path):
    _stub(monkeypatch, fail_first_token="monitoring", code=7)
    with pytest.raises(SystemExit) as exc:
        parallel_infra.run_parallel_group(_console(), _group(), log_dir=tmp_path)
    assert exc.value.code == 7


def test_empty_group_is_noop(monkeypatch, tmp_path):
    ran = _stub(monkeypatch)
    parallel_infra.run_parallel_group(
        _console(), StepGroup("infra", [], "Create infrastructure"), log_dir=tmp_path
    )
    assert ran == []
