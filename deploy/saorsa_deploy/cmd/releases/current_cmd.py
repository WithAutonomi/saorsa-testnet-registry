"""`saorsa-deploy releases current` — the one-shot channel answer.

What is on stable, what is on beta, and how long the beta has soaked. This is
the question the registry could not answer before: it was resolvable only by
reading GitHub tags and hand-transcribed prose in a Linear issue.

Also reports the betas **rolled past** since the current stable was promoted.
That count is the widened stable version-jump window the next promotion has to
carry — a stable fleet that skipped two betas jumps N+3, not N+1.
"""

from rich.console import Console
from rich.table import Table

from saorsa_deploy.cmd.releases._shared import (
    format_duration,
    require_release_repository,
)


def _version_cell(release, timestamp_attr, tag_attr):
    if release is None:
        return "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]"
    stamp = getattr(release, timestamp_attr)
    return (
        f"[bold]{release.version}[/bold]",
        getattr(release, tag_attr) or "[dim]-[/dim]",
        stamp.strftime("%Y-%m-%d") if stamp else "[dim]-[/dim]",
    )


def cmd_releases_current(args):
    console = Console(width=140)
    repo = require_release_repository()

    states = repo.current(binary_target=getattr(args, "binary_target", None))
    if not states:
        console.print("[dim]No releases recorded.[/dim]")
        return

    table = Table(
        title="Channel state",
        show_lines=True,
        show_header=True,
        header_style="bold",
    )
    table.add_column("Binary")
    table.add_column("Channel")
    table.add_column("Version")
    table.add_column("Tag")
    table.add_column("Since")
    table.add_column("Soak")

    for state in states:
        stable_version, stable_tag, stable_since = _version_cell(
            state.stable, "promoted_at", "final_tag"
        )
        table.add_row(
            f"[bold]{state.binary_target}[/bold]",
            "stable",
            stable_version,
            stable_tag,
            stable_since,
            "",
        )

        beta_version, beta_tag, beta_since = _version_cell(
            state.beta, "shipped_to_beta_at", "beta_tag"
        )
        soak = format_duration(state.soak_seconds)
        elapsed = state.soak_elapsed
        if elapsed is True:
            soak = f"[green]{soak}[/green] (soak elapsed)"
        elif elapsed is False:
            planned = state.beta.planned_promotion_at.date().isoformat()
            soak = f"{soak} (until {planned})"
        table.add_row("", "beta", beta_version, beta_tag, beta_since, soak)

        if state.rolled_past:
            versions = ", ".join(r.version for r in state.rolled_past)
            table.add_row(
                "",
                "[yellow]rolled past[/yellow]",
                versions,
                "[dim]-[/dim]",
                "[dim]-[/dim]",
                f"[yellow]stable jumps N+{len(state.rolled_past) + 1}[/yellow]",
            )

    console.print(table)
