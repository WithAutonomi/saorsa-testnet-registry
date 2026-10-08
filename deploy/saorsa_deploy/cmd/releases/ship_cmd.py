"""`saorsa-deploy releases ship` — record a beta ship.

Called by the ship-to-beta skill at Wednesday's beta ship, once the prerelease
has been verified. Idempotent on ``(binary-target, version)``: a re-run after a
partial failure refreshes the existing row rather than failing.

``--version`` is the **final, suffix-free** version (``0.17.1``), not the beta
version — the beta suffix lives in ``--beta-tag``. That is what makes the row
the same lineage that ``promote`` later completes.
"""

import sys

from rich.console import Console

from saorsa_deploy.cmd.releases._shared import (
    parse_timestamp,
    require_release_repository,
    resolve_release_candidate_id,
)


def cmd_releases_ship(args):
    console = Console(width=140)
    repo = require_release_repository()

    shipped_at = parse_timestamp(getattr(args, "shipped_at", None), field="--shipped-at")
    planned = parse_timestamp(getattr(args, "planned_promotion", None), field="--planned-promotion")

    rc_id = resolve_release_candidate_id(
        repo, args.binary_target, getattr(args, "rc_version", None)
    )
    if getattr(args, "rc_version", None) and rc_id is None:
        console.print(
            f"[yellow]No release-candidate row found for "
            f"{args.binary_target} {args.rc_version}; recording the ship "
            f"without the link.[/yellow]"
        )

    try:
        release = repo.ship(
            binary_target=args.binary_target,
            version=args.version,
            rc_branch=args.rc_branch,
            beta_tag=getattr(args, "beta_tag", None),
            beta_tag_sha=getattr(args, "beta_tag_sha", None),
            shipped_to_beta_at=shipped_at,
            planned_promotion_at=planned,
            release_candidate_id=rc_id,
            notes=getattr(args, "notes", None),
        )
    except ValueError as exc:
        sys.stderr.write(f"releases ship: {exc}\n")
        sys.exit(2)

    console.print(
        f"[green]Shipped to beta[/green]  id={release.id}  "
        f"{release.binary_target} {release.version}  "
        f"tag={release.beta_tag or '-'}"
    )
    if release.planned_promotion_at is not None:
        console.print(
            f"  [cyan]Planned promotion[/cyan] {release.planned_promotion_at.date().isoformat()}"
        )
    if rc_id is not None:
        console.print(f"  [cyan]Release candidate[/cyan] id={rc_id}")
