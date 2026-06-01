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
    console: Console, name: str, kind: str, steps: list[Step], verb: str
) -> None:
    console.print(
        f"[bold]{verb} plan for {name}[/bold] "
        f"([bold]{kind}[/bold]) — {len(steps)} step(s):"
    )
    for i, step in enumerate(steps, 1):
        console.print(f"  [cyan]{i:>2}.[/cyan] {step.label}")
        console.print(f"      [dim]$ {step.render()}[/dim]")


def _execute(console: Console, steps: list[Step]) -> None:
    from saorsa_deploy.main import main as run_cli

    total = len(steps)
    for i, step in enumerate(steps, 1):
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
                continue
            raise


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
    _prepare_secret_env(manifest, console)
    _print_plan(console, manifest.name, manifest.kind, steps, "Deploy")
    console.print()
    _execute(console, steps)
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
    console.print(f"\n[bold green]{name} has been destroyed.[/bold green]")


def cmd_plan(args):
    console = Console()
    manifest = _load(args, console)
    _print_plan(
        console, manifest.name, manifest.kind, build_deploy_steps(manifest), "Deploy"
    )
