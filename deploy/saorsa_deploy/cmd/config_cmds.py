"""`saorsa-deploy` configuration inspection — manifests, profiles, settings.

All of these are read-only: no cloud calls, no state. `deployments config
show|validate` confirm what a manifest resolves to (after ``extends`` +
``--set`` + ``--name``) before running ``up``/``down``; `deployments config
profiles` lists the profiles the installed tool ships for ``extends:
profile:<name>``; `config env-file` reports where the tool read its credentials
from and which of them are set.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from saorsa_deploy.config import (
    ManifestError,
    list_profiles,
    load_manifest,
    profiles_dir,
)
from saorsa_deploy.env_file import ENV_FILE_VAR, ENV_KEY_GROUPS, resolve_env_file


def _load_or_exit(args, console: Console):
    try:
        return load_manifest(
            args.config, sets=getattr(args, "set", None), name=getattr(args, "name", None)
        )
    except ManifestError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        # 2, not 1, matching `clients config show|validate` (which has always
        # used it) and every other manifest-level refusal in the tool. These
        # two commands are read-only and nothing branches on the old value;
        # an unattended runner checking a manifest wants one code for "the
        # manifest is bad" whichever kind it is.
        sys.exit(2)


def cmd_config_show(args):
    """Print the fully-merged, validated manifest as YAML."""
    console = Console()
    manifest = _load_or_exit(args, console)
    rendered = manifest.model_dump(exclude_none=True)
    sys.stdout.write(yaml.safe_dump(rendered, sort_keys=False, default_flow_style=False))


def cmd_config_validate(args):
    """Validate a manifest; exit 2 with a friendly error if invalid."""
    console = Console()
    manifest = _load_or_exit(args, console)
    console.print(
        f"[bold green]OK[/bold green] — '{args.config}' is a valid "
        f"[bold]{manifest.kind}[/bold] manifest for [bold]{manifest.name}[/bold]."
    )


def _profile_description(path: Path) -> str:
    """Return the first line of a profile's leading comment block.

    A profile whose first comment line is just its own file name (staging.yaml
    does this) falls through to the next one, since the point of the listing is
    to say what each profile IS.
    """
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("#"):
            break  # the leading comment block is over
        text = stripped.lstrip("#").strip()
        if not text or text == path.name:
            continue
        return text
    return ""


def cmd_config_profiles(args):
    """List the profiles bundled in this version of the tool."""
    console = Console()
    directory = profiles_dir()
    names = list_profiles()
    if not names:
        console.print(f"[bold red]Error:[/bold red] no profiles found under '{directory}'")
        sys.exit(2)

    table = Table(show_header=True, header_style="bold")
    table.add_column("Profile")
    table.add_column("Description")
    for name in names:
        table.add_row(name, _profile_description(directory / f"{name}.yaml"))

    console.print(table)
    console.print(
        f"\n{len(names)} profiles, bundled with saorsa-deploy at '{directory}'. "
        "Reuse one from a manifest in any directory with [bold]extends: "
        "profile:<name>[/bold]."
    )


def cmd_config_env_file(args) -> None:
    """Report the settings file and which of the tool's variables are set.

    Never prints a value: this is the precondition check an unattended runner
    makes before it deploys anything, and its output goes into a run log.
    """
    console = Console()
    path = resolve_env_file()
    source = (
        f"from ${ENV_FILE_VAR}"
        if os.environ.get(ENV_FILE_VAR, "").strip()
        else f"the default, ${ENV_FILE_VAR} unset"
    )

    console.print(f"[bold]Settings file:[/bold] {path}  ({source})")
    if path.exists():
        console.print(
            "[bold green]exists[/bold green] — loaded, without overriding the environment"
        )
    else:
        console.print(
            "[yellow]absent[/yellow] — not an error; every variable below then "
            "has to come from the environment or ./.env"
        )
    console.print(
        "Precedence: the environment wins, then this file, then "
        "[bold]./.env[/bold] in the working directory."
    )
    console.print()

    missing = 0
    for group in ENV_KEY_GROUPS:
        table = Table(title=group.title, title_justify="left", show_lines=False)
        table.add_column("Variable", style="bold")
        table.add_column("Set")
        table.add_column("Needed for")
        for key in group.keys:
            is_set = bool(os.environ.get(key.name, "").strip())
            if not is_set:
                missing += 1
            table.add_row(
                key.name,
                "[green]yes[/green]" if is_set else "[red]no[/red]",
                key.purpose,
            )
        console.print(table)

    total = sum(len(g.keys) for g in ENV_KEY_GROUPS)
    # Exit 0 whatever it finds. Most of these are provider- or kind-specific —
    # an operator who never deploys to OVH is not misconfigured for having none
    # of the four OVH values — so there is no set of keys whose absence is a
    # failure for every command, and a non-zero exit would have to invent one.
    console.print(f"{total - missing} of {total} set.")
