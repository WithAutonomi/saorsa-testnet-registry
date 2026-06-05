"""`saorsa-deploy up | down | plan` — manifest-driven orchestration.

``up``   runs the full deploy sequence for a manifest.
``down`` runs the destroy sequence.
``plan`` prints the deploy sequence without executing (always dry-run).

Each step is executed by re-entering the CLI in-process (``main(step.argv)``), so
argparse fills every default and a failing step halts the run (fail-fast, the
``set -euo pipefail`` contract the bash runbooks relied on).
"""

from __future__ import annotations

import os
import sys

from rich.console import Console

from saorsa_deploy.config import ManifestError, load_manifest
from saorsa_deploy.orchestrator import (
    Step,
    build_deploy_steps,
    build_destroy_steps,
    build_destroy_steps_from_state,
)
from saorsa_deploy.orchestrator import journal as step_journal
from saorsa_deploy.state import (
    delete_step_journal,
    load_step_journal,
    save_step_journal,
)

# prod kinds whose wallet key spends real ETH on Arbitrum One — must be present
# before we start, and bridged to the var the underlying commands read.
_SECRET_KEY_KINDS = ("prod-uploaders", "prod-canary", "prod-local-uploads")
_CLI_SECRET_ENV = "ARBITRUM_ONE_SECRET_KEY"


def _load(args, console: Console):
    try:
        return load_manifest(
            args.config, sets=getattr(args, "set", None), name=getattr(args, "name", None)
        )
    except ManifestError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


def _print_plan(
    console: Console, name: str, kind: str, steps: list[Step], verb: str, done: int = 0
) -> None:
    console.print(
        f"[bold]{verb} plan for {name}[/bold] "
        f"([bold]{kind}[/bold]) — {len(steps)} step(s):"
    )
    for i, step in enumerate(steps, 1):
        marker = " [green]✓ done previously[/green]" if i <= done else ""
        console.print(f"  [cyan]{i:>2}.[/cyan] {step.label}{marker}")
        console.print(f"      [dim]$ {step.render()}[/dim]")


def _record_step(journal: dict | None, step: Step, status: str) -> None:
    """Append a completed step to the journal and persist it to S3."""
    if journal is None:
        return
    step_journal.record_step(journal, step, status)
    save_step_journal(journal["name"], journal)


def _execute(
    console: Console, steps: list[Step], journal: dict | None = None, start: int = 0
) -> None:
    from saorsa_deploy.main import main as run_cli

    total = len(steps)
    for i, step in enumerate(steps[start:], start + 1):
        console.rule(f"[bold cyan][{i}/{total}][/bold cyan] {step.label}")
        console.print(f"[dim]$ {step.render()}[/dim]")
        try:
            run_cli(step.argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            if code != 0:
                if step.best_effort:
                    console.print(
                        f"[bold yellow]Warning:[/bold yellow] step {i}/{total} "
                        f"({step.label}) exited {code}; continuing (best-effort)."
                    )
                    _record_step(journal, step, step_journal.STATUS_BEST_EFFORT_FAILED)
                    continue
                console.print(
                    f"[bold red]Aborting:[/bold red] step {i}/{total} ({step.label}) exited {code}."
                )
                sys.exit(code)
        except Exception as e:
            # A best-effort step (e.g. the Slack post tail) must never fail the
            # run; the real deployment work is already done by the time it runs.
            if step.best_effort:
                console.print(
                    f"[bold yellow]Warning:[/bold yellow] step {i}/{total} "
                    f"({step.label}) raised {type(e).__name__}: {e}; continuing (best-effort)."
                )
                _record_step(journal, step, step_journal.STATUS_BEST_EFFORT_FAILED)
                continue
            raise
        _record_step(journal, step, step_journal.STATUS_SUCCEEDED)


def _resolve_step_journal(
    console: Console, manifest, steps: list[Step], resume: bool
) -> tuple[dict, int]:
    """Decide how an ``up`` run interacts with any existing step journal.

    Returns ``(journal, resume_index)``: the journal to record this run's
    steps into, and the plan index to start executing from.

    A journal can outlive its deployment (a failed run torn down via the
    legacy destroy commands, or simply abandoned) and deployment names are
    reused, so a journal hit must NEVER silently skip steps. Interactively we
    stop and ask; non-interactively ``--resume`` is required, and without it
    we refuse rather than hang or guess.
    """
    existing = load_step_journal(manifest.name)
    if existing is None:
        return step_journal.new_journal(manifest.name, manifest.kind), 0

    match = step_journal.match_journal(existing, manifest.kind, steps)
    console.print()
    for line in step_journal.render_journal_summary(existing, steps):
        console.print(line)
    if not match.matches:
        console.print()
        for line in step_journal.render_divergence(
            existing, manifest.kind, steps, match
        ):
            console.print(f"[yellow]{line}[/yellow]")

    def _resume() -> tuple[dict, int]:
        if match.resume_index == 0:
            console.print(
                "[yellow]No journaled steps match the plan — running all "
                "steps from scratch.[/yellow]"
            )
            return step_journal.new_journal(manifest.name, manifest.kind), 0
        if not match.matches:
            console.print(
                f"[yellow]Resuming from the longest matching prefix: skipping "
                f"{match.resume_index} step(s).[/yellow]"
            )
        # Trim any diverged tail so future appends line up with the plan.
        existing["steps"] = existing["steps"][: match.resume_index]
        return existing, match.resume_index

    if resume:
        console.print(
            f"[green]--resume:[/green] skipping {match.resume_index} previously "
            "completed step(s)."
        )
        return _resume()

    if not sys.stdin.isatty():
        console.print(
            "[bold red]Error:[/bold red] a step journal exists for "
            f"'{manifest.name}' but this session is non-interactive. Pass "
            "--resume to skip the completed steps, or run `deployments up` "
            "interactively to delete the journal and start fresh."
        )
        sys.exit(1)

    console.print()
    while True:
        choice = (
            console.input(
                "\\[r]esume (skip completed steps) / "
                "\\[d]elete journal and run all steps / "
                "\\[a]bort? "
            )
            .strip()
            .lower()
        )
        if choice == "r":
            return _resume()
        if choice == "d":
            delete_step_journal(manifest.name)
            return step_journal.new_journal(manifest.name, manifest.kind), 0
        if choice == "a":
            console.print("Aborted.")
            sys.exit(1)


def _prepare_secret_env(manifest, console: Console) -> None:
    """Fail fast if a real-ETH wallet key is missing; bridge a custom env var name.

    The underlying prod commands read ``ARBITRUM_ONE_SECRET_KEY`` from the
    environment. If the manifest names a different ``secret_key_env``, copy it
    across so those commands find it — without ever putting the key in argv.
    """
    if manifest.kind not in _SECRET_KEY_KINDS:
        return
    env_name = manifest.spec.secret_key_env
    value = os.environ.get(env_name)
    if not value:
        console.print(
            f"[bold red]Error:[/bold red] {env_name} must be set — "
            f"{manifest.kind} spends real ETH on Arbitrum One."
        )
        sys.exit(1)
    if env_name != _CLI_SECRET_ENV:
        os.environ[_CLI_SECRET_ENV] = value


def cmd_up(args):
    console = Console()
    manifest = _load(args, console)
    steps = build_deploy_steps(manifest)
    if getattr(args, "dry_run", False):
        _print_plan(console, manifest.name, manifest.kind, steps, "Deploy")
        return
    journal, resume_index = _resolve_step_journal(
        console, manifest, steps, getattr(args, "resume", False)
    )
    _prepare_secret_env(manifest, console)
    _print_plan(
        console, manifest.name, manifest.kind, steps, "Deploy", done=resume_index
    )
    console.print()
    _execute(console, steps, journal=journal, start=resume_index)
    # The run completed: the journal has served its purpose. Leaving it around
    # would trip the resume prompt on the next deployment to reuse this name.
    delete_step_journal(manifest.name)
    console.print(f"\n[bold green]{manifest.name} is up.[/bold green]")


def cmd_down(args):
    console = Console()

    # Destroy is name/state-driven: a manifest is optional. With --config we
    # mirror the manifest's components; with --name alone we derive the teardown
    # from what actually exists in S3 state.
    skip_drain_failures = getattr(args, "skip_drain_failures", False)
    if getattr(args, "config", None):
        manifest = _load(args, console)
        name, kind = manifest.name, manifest.kind
        steps = build_destroy_steps(
            manifest, skip_drain_failures=skip_drain_failures
        )
    elif getattr(args, "name", None):
        name, kind = args.name, "from state"
        steps = build_destroy_steps_from_state(
            name, skip_drain_failures=skip_drain_failures
        )
        if not steps:
            console.print(
                f"[yellow]No resources found in state for '{name}'. "
                "Nothing to destroy.[/yellow]"
            )
            console.print(
                "[dim]If this is a prod-* deployment, tear it down with its "
                "`prod <kind> destroy --name ...` command, or pass --config.[/dim]"
            )
            return
    else:
        console.print(
            "[bold red]Error:[/bold red] provide --config <manifest> or "
            "--name <deployment>."
        )
        sys.exit(1)

    if getattr(args, "dry_run", False):
        _print_plan(console, name, kind, steps, "Destroy")
        return
    _print_plan(console, name, kind, steps, "Destroy")
    console.print()
    _execute(console, steps)
    # Any step journal left by a failed `up` is now moot: the deployment is
    # gone, so a future run reusing this name must start from scratch.
    delete_step_journal(name)
    console.print(f"\n[bold green]{name} has been destroyed.[/bold green]")


def cmd_plan(args):
    console = Console()
    manifest = _load(args, console)
    _print_plan(
        console, manifest.name, manifest.kind, build_deploy_steps(manifest), "Deploy"
    )
