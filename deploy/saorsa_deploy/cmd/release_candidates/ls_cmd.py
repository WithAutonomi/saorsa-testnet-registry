"""`saorsa-deploy release-candidates ls` — list recorded RCs.

Prints each RC as a parent row, with its constituent crates indented
below it. Sorted newest-first by ``recorded_at``.
"""

from rich.console import Console
from rich.table import Table

from saorsa_deploy.cmd.release_candidates._shared import require_rc_repository


def cmd_release_candidates_ls(args):
    console = Console(width=140)
    repo = require_rc_repository()

    rcs = repo.list(
        binary_target=getattr(args, "binary_target", None),
        limit=getattr(args, "limit", None),
    )

    if not rcs:
        console.print("[dim]No release candidates recorded.[/dim]")
        return

    table = Table(
        title=f"Release candidates ({len(rcs)})",
        show_lines=True,
        show_header=True,
        header_style="bold",
    )
    table.add_column("ID", justify="right")
    table.add_column("Binary / Crate")
    table.add_column("Version")
    table.add_column("RC Branch")
    table.add_column("Repo")
    table.add_column("PRs", justify="right")
    table.add_column("Recorded")

    for rc in rcs:
        table.add_row(
            f"[bold]{rc.id}[/bold]",
            f"[bold]{rc.binary_target}[/bold]",
            f"[bold]{rc.binary_version}[/bold]",
            rc.rc_branch,
            "",
            "",
            rc.recorded_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
        for crate in rc.crates:
            table.add_row(
                "",
                f"  └─ {crate.crate_name}",
                crate.version,
                "",
                crate.repo_slug,
                str(len(crate.prs)),
                "",
            )

    console.print(table)
