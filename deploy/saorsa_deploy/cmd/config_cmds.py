"""`saorsa-deploy config show|validate` — inspect a merged manifest.

These are read-only: no cloud calls, no state. They let you confirm what a
manifest resolves to (after ``extends`` + ``--set`` + ``--name``) before running
``up``/``down``.
"""

from __future__ import annotations

import sys

import yaml
from rich.console import Console

from saorsa_deploy.config import ManifestError, load_manifest


def _load_or_exit(args, console: Console):
    try:
        return load_manifest(
            args.config, sets=getattr(args, "set", None), name=getattr(args, "name", None)
        )
    except ManifestError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


def cmd_config_show(args):
    """Print the fully-merged, validated manifest as YAML."""
    console = Console()
    manifest = _load_or_exit(args, console)
    rendered = manifest.model_dump(exclude_none=True)
    sys.stdout.write(yaml.safe_dump(rendered, sort_keys=False, default_flow_style=False))


def cmd_config_validate(args):
    """Validate a manifest; exit non-zero with a friendly error if invalid."""
    console = Console()
    manifest = _load_or_exit(args, console)
    console.print(
        f"[bold green]OK[/bold green] — '{args.config}' is a valid "
        f"[bold]{manifest.kind}[/bold] manifest for [bold]{manifest.name}[/bold]."
    )
