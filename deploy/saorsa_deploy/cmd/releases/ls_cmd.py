"""`saorsa-deploy releases ls` — list recorded releases.

Prints each release as a parent row with its manifest crates indented below.
Sorted newest-first by ``recorded_at``.
"""

from rich.console import Console
from rich.table import Table

from saorsa_deploy.cmd.releases._shared import require_release_repository

_OUTCOME_STYLE = {
    "shipped_to_beta": "cyan",
    "promoted": "green",
    "rolled_past": "yellow",
}


def _outcome_filter(value):
    if not value:
        return None
    from saorsa_db.models.release import ReleaseOutcome

    return ReleaseOutcome(value)


def cmd_releases_ls(args):
    console = Console(width=140)
    repo = require_release_repository()

    releases = repo.list(
        binary_target=getattr(args, "binary_target", None),
        outcome=_outcome_filter(getattr(args, "outcome", None)),
        limit=getattr(args, "limit", None),
    )

    if not releases:
        console.print("[dim]No releases recorded.[/dim]")
        return

    table = Table(
        title=f"Releases ({len(releases)})",
        show_lines=True,
        show_header=True,
        header_style="bold",
    )
    table.add_column("ID", justify="right")
    table.add_column("Binary / Crate")
    table.add_column("Version")
    table.add_column("Outcome")
    table.add_column("RC Branch")
    table.add_column("Beta")
    table.add_column("Promoted")

    for release in releases:
        style = _OUTCOME_STYLE.get(release.outcome.value, "white")
        table.add_row(
            f"[bold]{release.id}[/bold]",
            f"[bold]{release.binary_target}[/bold]",
            f"[bold]{release.version}[/bold]",
            f"[{style}]{release.outcome.value}[/{style}]",
            release.rc_branch,
            release.shipped_to_beta_at.strftime("%Y-%m-%d")
            if release.shipped_to_beta_at
            else "[dim]-[/dim]",
            release.promoted_at.strftime("%Y-%m-%d")
            if release.promoted_at
            else "[dim]-[/dim]",
        )
        for crate in release.crates:
            table.add_row(
                "",
                f"  └─ {crate.crate_name}",
                crate.version,
                "",
                crate.repo_slug,
                "",
                "",
            )

    console.print(table)
