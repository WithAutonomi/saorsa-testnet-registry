"""Concurrent execution of a :class:`StepGroup` for ``deployments up``.

The infrastructure-creation steps of a deploy (bootstraps/nodes/elastic/
monitoring/clients ``infra``) have no cross-dependencies except bootstraps→nodes
(which share the deployment-state object). The orchestrator groups them into one
:class:`StepGroup` with one lane per independent chain; this module runs those
lanes in parallel.

Each lane's steps are executed as **subprocesses** (``python -m
saorsa_deploy.main <argv>``) rather than in-process, so that the several infra
commands — each of which drives its own ``rich.Live`` progress table and prints
freely to stdout — stay fully isolated. The parent process owns a single
aggregate table (one row per lane) and tees each lane's combined output to its
own log file so the operator can ``tail -f`` the detail.

Failure semantics: a lane stops at its first failing step (so nodes infra never
runs if bootstraps infra failed). In-flight lanes are allowed to finish — we
never kill a running ``terraform apply`` mid-flight — and once every lane has
settled, if any failed we print the failed lanes' captured output and abort the
whole deploy (``SystemExit``). The group is therefore recorded in the journal
only when every lane succeeds (the caller records it), making the group a single
atomic, idempotent, resumable unit.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.table import Table

from saorsa_deploy.orchestrator.steps import Step, StepGroup

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _format_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _log_dir() -> Path:
    """Per-run-independent directory for lane logs (cwd-relative, like workspaces)."""
    return Path.cwd() / ".saorsa" / "infra-logs"


def _log_path(log_dir: Path, lane_idx: int, step: Step) -> Path:
    tag = "-".join(step.argv[:2]) if len(step.argv) >= 2 else step.argv[0]
    return log_dir / f"lane{lane_idx:02d}-{tag}.log"


def _run_step_subprocess(step: Step, log_path: Path) -> int:
    """Run one step as a fresh ``saorsa-deploy`` process, teeing output to a log.

    Returns the process exit code. Isolated as a module function so tests can
    stub it without spawning real subprocesses. cwd is inherited (the infra
    commands resolve their terraform workspaces relative to it).
    """
    with open(log_path, "w") as fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "saorsa_deploy.main", *step.argv],
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
        return proc.wait()


@dataclass
class LaneResult:
    lane_idx: int
    label: str
    success: bool
    failed_step: Step | None = None
    exit_code: int = 0
    log_path: Path | None = None


@dataclass
class _LaneState:
    label: str
    current: str = ""
    state: str = "pending"  # pending | running | done | failed
    start: float | None = None


def _lane_label(lane: list[Step]) -> str:
    """Human label for a lane row — the lane's command verbs."""
    return " → ".join(" ".join(s.argv[:2]) for s in lane)


def _build_table(states: list[_LaneState], spinner_tick: int) -> Table:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Lane")
    table.add_column("Step")
    table.add_column("Status")
    table.add_column("Elapsed")
    now = time.monotonic()
    for st in states:
        elapsed = ""
        if st.start is not None:
            elapsed = _format_elapsed(now - st.start)
        if st.state == "running":
            frame = SPINNER_FRAMES[spinner_tick % len(SPINNER_FRAMES)]
            symbol = f"[yellow]{frame} running[/yellow]"
        elif st.state == "done":
            symbol = "[green]done[/green]"
        elif st.state == "pending":
            symbol = "[dim]pending[/dim]"
            elapsed = ""
        else:
            symbol = "[red]FAILED[/red]"
        table.add_row(st.label, st.current, symbol, elapsed)
    return table


def _run_lane(
    lane_idx: int,
    lane: list[Step],
    state: _LaneState,
    lock: threading.Lock,
    log_dir: Path,
) -> LaneResult:
    label = state.label
    with lock:
        state.state = "running"
        state.start = time.monotonic()
    for step in lane:
        with lock:
            state.current = " ".join(step.argv[:2])
        log_path = _log_path(log_dir, lane_idx, step)
        code = _run_step_subprocess(step, log_path)
        if code != 0:
            with lock:
                state.state = "failed"
            return LaneResult(
                lane_idx=lane_idx,
                label=label,
                success=False,
                failed_step=step,
                exit_code=code,
                log_path=log_path,
            )
    with lock:
        state.state = "done"
        state.current = "all steps complete"
    return LaneResult(lane_idx=lane_idx, label=label, success=True)


def _print_failure(console: Console, result: LaneResult) -> None:
    step = result.failed_step
    console.print(
        f"[bold red]FAILED:[/bold red] lane '{result.label}' — "
        f"`{step.render() if step else '?'}` exited {result.exit_code}."
    )
    if result.log_path and result.log_path.exists():
        text = result.log_path.read_text(errors="replace").strip()
        if text:
            console.print(f"[dim]── output ({result.log_path}) ──[/dim]")
            console.print(text)
    console.print()


def run_parallel_group(console: Console, group: StepGroup, *, log_dir: Path | None = None) -> None:
    """Run every lane of ``group`` concurrently; abort the deploy on any failure.

    Returns normally only when all lanes succeed (the caller then records the
    group in the journal). On any lane failure this prints the failed lanes'
    captured output and raises ``SystemExit`` to halt the deploy.
    """
    lanes = group.lanes
    if not lanes:
        return

    log_dir = log_dir or _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    console.print(
        f"[magenta]Running {len(lanes)} infrastructure lane(s) in parallel.[/magenta] "
        "Per-lane output is tee'd to:"
    )
    for i, lane in enumerate(lanes):
        for step in lane:
            console.print(f"  [dim]{_log_path(log_dir, i, step)}[/dim]")

    states = [_LaneState(label=_lane_label(lane)) for lane in lanes]
    lock = threading.Lock()
    results: list[LaneResult | None] = [None] * len(lanes)

    with Live(_build_table(states, 0), console=console, refresh_per_second=4) as live:
        with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
            future_to_idx = {
                pool.submit(_run_lane, i, lane, states[i], lock, log_dir): i
                for i, lane in enumerate(lanes)
            }
            spinner_tick = 0
            remaining = set(future_to_idx)
            while remaining:
                spinner_tick += 1
                with lock:
                    live.update(_build_table(states, spinner_tick))
                done = {f for f in remaining if f.done()}
                for fut in done:
                    idx = future_to_idx[fut]
                    results[idx] = fut.result()
                remaining -= done
                if remaining:
                    time.sleep(0.25)
            with lock:
                live.update(_build_table(states, spinner_tick))

    failures = [r for r in results if r is not None and not r.success]
    if failures:
        console.print()
        for result in failures:
            _print_failure(console, result)
        succeeded = sum(1 for r in results if r is not None and r.success)
        console.print(
            f"[bold red]Aborting:[/bold red] {len(failures)} of {len(lanes)} "
            f"infrastructure lane(s) failed ({succeeded} succeeded). "
            "Fix the cause and re-run `deployments up --resume` to retry the "
            "infrastructure group (each lane is idempotent)."
        )
        sys.exit(failures[0].exit_code or 1)

    console.print(f"[green]All {len(lanes)} infrastructure lane(s) completed.[/green]")
