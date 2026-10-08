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
import time

from rich.console import Console

from saorsa_deploy.config import ManifestError, load_manifest
from saorsa_deploy.orchestrator import (
    Step,
    build_deploy_steps,
    build_destroy_steps,
    build_destroy_steps_from_state,
    retained_observability,
)
from saorsa_deploy.orchestrator import journal as step_journal
from saorsa_deploy.orchestrator.steps import StepGroup, iter_units
from saorsa_deploy.state import (
    delete_step_journal,
    load_step_journal,
    save_step_journal,
)

# prod kinds whose wallet key spends real ETH on Arbitrum One — must be present
# before we start, and bridged to the var the underlying commands read.
_SECRET_KEY_KINDS = (
    "prod-uploaders",
    "prod-canary",
    "prod-local-uploads",
    "prod-continuous-uploader",
)
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
    """Print the numbered command sequence.

    ``done`` counts completed *units* (a parallel group is one unit), used to
    flag already-done commands on a resumed run. Every underlying command is
    still listed and numbered continuously; commands inside a parallel group are
    annotated and grouped under a header.
    """
    units = iter_units(steps)
    console.print(
        f"[bold]{verb} plan for {name}[/bold] ([bold]{kind}[/bold]) — {len(steps)} step(s):"
    )
    n = 0
    for unit_idx, unit in enumerate(units):
        unit_done = unit_idx < done
        marker = " [green]✓ done previously[/green]" if unit_done else ""
        if isinstance(unit, StepGroup):
            console.print(
                f"  [magenta]┌ {unit.label} — "
                f"{len(unit.lanes)} lane(s) run concurrently:[/magenta]{marker}"
            )
            for lane in unit.lanes:
                for step in lane:
                    n += 1
                    console.print(
                        f"  [cyan]{n:>2}.[/cyan] {step.label} [magenta](parallel)[/magenta]{marker}"
                    )
                    console.print(f"      [dim]$ {step.render()}[/dim]")
        else:
            n += 1
            console.print(f"  [cyan]{n:>2}.[/cyan] {unit.label}{marker}")
            console.print(f"      [dim]$ {unit.render()}[/dim]")


def _record_step(journal: dict | None, step: Step, status: str) -> None:
    """Append a completed step to the journal and persist it to S3."""
    if journal is None:
        return
    step_journal.record_step(journal, step, status)
    save_step_journal(journal["name"], journal)


def _record_group(journal: dict | None, group: StepGroup) -> None:
    """Record a fully-completed parallel group as one journal unit and persist."""
    if journal is None:
        return
    step_journal.record_group(journal, group, step_journal.STATUS_SUCCEEDED)
    save_step_journal(journal["name"], journal)


def _format_duration(seconds: float) -> str:
    """Human-friendly elapsed time, e.g. ``42.3s`` / ``3m 07s`` / ``1h 04m 09s``."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s"


def _print_timing_summary(console: Console, timings: list[tuple[str, float]]) -> None:
    """Print per-step durations, slowest first, plus a total — to spotlight the
    slow steps when a deploy feels sluggish."""
    if not timings:
        return
    total = sum(d for _, d in timings)
    console.rule("[bold]Step timing summary[/bold]")
    for label, dur in sorted(timings, key=lambda t: t[1], reverse=True):
        console.print(f"  [cyan]{_format_duration(dur):>11}[/cyan]  {label}")
    console.print(f"  [bold]{_format_duration(total):>11}  total[/bold]")


def _execute(
    console: Console, steps: list[Step], journal: dict | None = None, start: int = 0
) -> None:
    from saorsa_deploy import parallel_infra
    from saorsa_deploy.main import main as run_cli

    units = iter_units(steps)
    total = len(units)
    timings: list[tuple[str, float]] = []
    for i, unit in enumerate(units[start:], start + 1):
        if isinstance(unit, StepGroup):
            console.rule(f"[bold cyan][{i}/{total}][/bold cyan] {unit.label}")
            # run_parallel_group prints failures and raises SystemExit on any
            # lane failure, so reaching the next line means every lane succeeded.
            t0 = time.monotonic()
            parallel_infra.run_parallel_group(console, unit)
            elapsed = time.monotonic() - t0
            timings.append((unit.label, elapsed))
            console.print(
                f"[green]✓[/green] [{i}/{total}] {unit.label} finished in "
                f"[bold]{_format_duration(elapsed)}[/bold]"
            )
            _record_group(journal, unit)
            continue

        step = unit
        console.rule(f"[bold cyan][{i}/{total}][/bold cyan] {step.label}")
        console.print(f"[dim]$ {step.render()}[/dim]")
        t0 = time.monotonic()
        try:
            run_cli(step.argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            if code != 0:
                elapsed = time.monotonic() - t0
                if step.best_effort:
                    console.print(
                        f"[bold yellow]Warning:[/bold yellow] step {i}/{total} "
                        f"({step.label}) exited {code} after "
                        f"{_format_duration(elapsed)}; continuing (best-effort)."
                    )
                    timings.append((step.label, elapsed))
                    _record_step(journal, step, step_journal.STATUS_BEST_EFFORT_FAILED)
                    continue
                console.print(
                    f"[bold red]Aborting:[/bold red] step {i}/{total} "
                    f"({step.label}) exited {code} after {_format_duration(elapsed)}."
                )
                _print_timing_summary(console, timings)
                sys.exit(code)
        except Exception as e:
            # A best-effort step (e.g. the Slack post tail) must never fail the
            # run; the real deployment work is already done by the time it runs.
            if step.best_effort:
                elapsed = time.monotonic() - t0
                console.print(
                    f"[bold yellow]Warning:[/bold yellow] step {i}/{total} "
                    f"({step.label}) raised {type(e).__name__}: {e} after "
                    f"{_format_duration(elapsed)}; continuing (best-effort)."
                )
                timings.append((step.label, elapsed))
                _record_step(journal, step, step_journal.STATUS_BEST_EFFORT_FAILED)
                continue
            raise
        elapsed = time.monotonic() - t0
        timings.append((step.label, elapsed))
        console.print(
            f"[green]✓[/green] [{i}/{total}] {step.label} finished in "
            f"[bold]{_format_duration(elapsed)}[/bold]"
        )
        _record_step(journal, step, step_journal.STATUS_SUCCEEDED)

    _print_timing_summary(console, timings)


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
        for line in step_journal.render_divergence(existing, manifest.kind, steps, match):
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
            f"[green]--resume:[/green] skipping {match.resume_index} previously completed step(s)."
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
    steps = build_deploy_steps(manifest, skip_builds=getattr(args, "skip_builds", False))
    if getattr(args, "dry_run", False):
        _print_plan(console, manifest.name, manifest.kind, steps, "Deploy")
        return
    journal, resume_index = _resolve_step_journal(
        console, manifest, steps, getattr(args, "resume", False)
    )
    _prepare_secret_env(manifest, console)
    _print_plan(console, manifest.name, manifest.kind, steps, "Deploy", done=resume_index)
    console.print()
    _execute(console, steps, journal=journal, start=resume_index)
    # The run completed: the journal has served its purpose. Leaving it around
    # would trip the resume prompt on the next deployment to reuse this name.
    delete_step_journal(manifest.name)
    console.print(f"\n[bold green]{manifest.name} is up.[/bold green]")


def _manifest_keep_overrides(manifest) -> dict:
    """Per-component keep intent as the manifest states it, for the report.

    testnet/extension kinds carry it in their ``elastic:`` / ``monitoring:``
    blocks; prod kinds carry the equivalent as spec-level ``keep_elastic`` /
    ``keep_monitoring`` (which ride as flags on their own destroy command). Either
    way the manifest is what decided the teardown, so it decides the report.
    """
    spec = getattr(manifest, "spec", None)
    if spec is None:
        return {}
    elastic = getattr(spec, "elastic", None)
    monitoring = getattr(spec, "monitoring", None)
    return {
        "elastic_kept": (
            bool(elastic.keep_on_destroy)
            if elastic is not None
            else bool(getattr(spec, "keep_elastic", False))
        ),
        "monitoring_kept": (
            bool(monitoring.keep_on_destroy)
            if monitoring is not None
            else bool(getattr(spec, "keep_monitoring", False))
        ),
    }


def _print_retained(console: Console, retained: list[dict]) -> None:
    """Report the observability stacks a teardown deliberately left running.

    Keeping a stack means no destroy step is generated for it, so without this the
    surviving VMs are invisible in the teardown output — and they keep billing
    until someone remembers them. Print what survived, how to reach it, and the
    exact command that removes it.
    """
    if not retained:
        return
    console.print()
    console.print("[bold]Retained (keep_on_destroy) — still running:[/bold]")
    for item in retained:
        endpoint = item["endpoint"] or "(endpoint not recorded)"
        console.print(f"  {item['component']} ({item['kind']}): {endpoint}")
        console.print(f"    details:  {item['info_command']}")
        console.print(f"    destroy:  {item['destroy_command']}")


def cmd_down(args):
    console = Console()

    # Destroy is name/state-driven: a manifest is optional. With --config we
    # mirror the manifest's components; with --name alone we derive the teardown
    # from what actually exists in S3 state.
    skip_drain_failures = getattr(args, "skip_drain_failures", False)
    keep_overrides: dict = {}
    if getattr(args, "config", None):
        manifest = _load(args, console)
        name, kind = manifest.name, manifest.kind
        steps = build_destroy_steps(manifest, skip_drain_failures=skip_drain_failures)
        # The manifest — not the stored flag — decided the destroy steps above, so
        # it must also decide the retained report, or the two can disagree (a
        # manifest that gained keep_on_destroy after `up` ran without it).
        keep_overrides = _manifest_keep_overrides(manifest)
    elif getattr(args, "name", None):
        name, kind = args.name, "from state"
        steps = build_destroy_steps_from_state(name, skip_drain_failures=skip_drain_failures)
        if not steps:
            # An all-kept observability stack also yields no steps, so check that
            # before claiming there is nothing here — otherwise the operator is
            # told "nothing to destroy" while two VMs keep billing.
            retained = retained_observability(name)
            if retained:
                console.print(
                    f"[yellow]Nothing left to destroy for '{name}' — every "
                    "remaining component is marked keep_on_destroy.[/yellow]"
                )
                _print_retained(console, retained)
                return
            console.print(
                f"[yellow]No resources found in state for '{name}'. Nothing to destroy.[/yellow]"
            )
            console.print(
                "[dim]If this is a prod-* deployment, tear it down with its "
                "`prod <kind> destroy --name ...` command, or pass --config.[/dim]"
            )
            return
    else:
        console.print(
            "[bold red]Error:[/bold red] provide --config <manifest> or --name <deployment>."
        )
        sys.exit(1)

    # Read the retained set BEFORE the teardown runs: the destroy steps delete
    # deployment state as they go, and a component's own state is what records
    # both the keep intent and the endpoint we want to report.
    retained = retained_observability(name, **keep_overrides)

    if getattr(args, "dry_run", False):
        _print_plan(console, name, kind, steps, "Destroy")
        _print_retained(console, retained)
        return
    _print_plan(console, name, kind, steps, "Destroy")
    console.print()
    _execute(console, steps)
    # Any step journal left by a failed `up` is now moot: the deployment is
    # gone, so a future run reusing this name must start from scratch.
    delete_step_journal(name)
    if retained:
        # "has been destroyed" would be a lie with an ES/Influx VM still up.
        console.print(
            f"\n[bold green]{name} has been torn down (observability stacks retained).[/bold green]"
        )
    else:
        console.print(f"\n[bold green]{name} has been destroyed.[/bold green]")
    _print_retained(console, retained)


def cmd_plan(args):
    console = Console()
    manifest = _load(args, console)
    _print_plan(
        console,
        manifest.name,
        manifest.kind,
        build_deploy_steps(manifest, skip_builds=getattr(args, "skip_builds", False)),
        "Deploy",
    )
