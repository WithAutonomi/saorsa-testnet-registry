"""`saorsa-deploy releases show` — one release with its full manifest."""

import sys

from rich.console import Console
from rich.table import Table

from saorsa_deploy.cmd.releases._shared import require_release_repository


def _or_dash(value):
    return value if value else "[dim]-[/dim]"


def _stamp(value):
    return value.isoformat() if value else "[dim]-[/dim]"


def cmd_releases_show(args):
    console = Console(width=140)
    repo = require_release_repository()

    if getattr(args, "id", None) is not None:
        release = repo.get(args.id)
        identifier = f"id={args.id}"
    elif args.binary_target and args.version:
        release = repo.get_by_version(args.binary_target, args.version)
        identifier = f"{args.binary_target} {args.version}"
    else:
        sys.stderr.write("releases show: provide --id, or both --binary-target and --version.\n")
        sys.exit(2)

    if release is None:
        sys.stderr.write(f"releases show: no release found for {identifier}\n")
        sys.exit(1)

    console.print(f"[bold]Release[/bold]  id={release.id}")
    for label, value in (
        ("Binary", release.binary_target),
        ("Version", release.version),
        ("Outcome", release.outcome.value),
        ("RC branch", release.rc_branch),
        ("RC row", _or_dash(release.release_candidate_id)),
        ("Beta tag", _or_dash(release.beta_tag)),
        ("Beta SHA", _or_dash(release.beta_tag_sha)),
        ("Shipped at", _stamp(release.shipped_to_beta_at)),
        ("Promotion due", _stamp(release.planned_promotion_at)),
        ("Final tag", _or_dash(release.final_tag)),
        ("Final SHA", _or_dash(release.final_tag_sha)),
        ("Promoted at", _stamp(release.promoted_at)),
        ("Recorded at", _stamp(release.recorded_at)),
    ):
        console.print(f"  [cyan]{label:<14}[/cyan] {value}")

    if release.notes:
        console.print(f"  [cyan]{'Notes':<14}[/cyan] {release.notes}")

    if not release.crates:
        console.print("\n[dim]No release manifest recorded.[/dim]")
        return

    table = Table(
        title=f"Manifest ({len(release.crates)} crates)",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Crate")
    table.add_column("Version")
    table.add_column("Repo")
    table.add_column("Tag")
    table.add_column("Tag SHA")

    for crate in release.crates:
        table.add_row(
            crate.crate_name,
            crate.version,
            crate.repo_slug,
            _or_dash(crate.tag),
            _or_dash(crate.tag_sha),
        )

    console.print(table)
