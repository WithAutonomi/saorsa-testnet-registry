"""`saorsa-deploy release-candidates show` — print one RC in full."""

import sys

from rich.console import Console
from rich.table import Table

from saorsa_deploy.cmd.release_candidates._shared import require_rc_repository

_TITLE_MAX = 65


def _truncate(title: str | None) -> str:
    if not title:
        return "[dim]-[/dim]"
    if len(title) <= _TITLE_MAX:
        return title
    return title[: _TITLE_MAX - 1].rstrip() + "…"


def cmd_release_candidates_show(args):
    console = Console(width=140)
    repo = require_rc_repository()

    if getattr(args, "id", None) is not None:
        rc = repo.get(args.id)
        identifier = f"id={args.id}"
    elif args.binary_target and args.version:
        rc = repo.get_by_binary_version(args.binary_target, args.version)
        identifier = f"{args.binary_target} {args.version}"
    else:
        sys.stderr.write(
            "release-candidates show: provide --id, or both --binary-target and --version.\n"
        )
        sys.exit(2)

    if rc is None:
        sys.stderr.write(f"release-candidates show: no release candidate found for {identifier}\n")
        sys.exit(1)

    console.print(f"[bold]Release Candidate[/bold]  id={rc.id}")
    for label, value in (
        ("Binary", rc.binary_target),
        ("Version", rc.binary_version),
        ("RC branch", rc.rc_branch),
        ("Recorded at", rc.recorded_at.isoformat()),
    ):
        console.print(f"  [cyan]{label:<12}[/cyan] {value}")
    if rc.notes:
        console.print(f"  [cyan]Notes[/cyan]        {rc.notes}")

    if not rc.crates:
        console.print("\n[dim]No crates attached.[/dim]")
        return

    for crate in rc.crates:
        console.print()
        console.print(
            f"[bold]{crate.crate_name}[/bold]  "
            f"[green]{crate.version}[/green]  "
            f"[dim]({crate.repo_slug})[/dim]"
        )
        for label, value in (
            ("Tag", crate.tag or "-"),
            ("Tag SHA", crate.tag_sha or "-"),
            ("Since tag", crate.since_tag or "-"),
            ("Since SHA", crate.since_sha or "-"),
            ("Head SHA", crate.head_sha or "-"),
        ):
            console.print(f"  [cyan]{label:<10}[/cyan] {value}")

        if not crate.prs:
            console.print("  [dim]No PRs.[/dim]")
            continue

        table = Table(title=f"PRs ({len(crate.prs)})")
        table.add_column("#", justify="right")
        table.add_column("Title")
        table.add_column("Author")
        table.add_column("Merged")

        for pr in crate.prs:
            table.add_row(
                str(pr.pr_number),
                _truncate(pr.title),
                pr.author or "[dim]-[/dim]",
                pr.merged_at.strftime("%Y-%m-%d %H:%M") if pr.merged_at else "-",
            )

        console.print(table)
