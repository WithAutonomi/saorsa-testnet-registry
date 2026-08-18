"""`saorsa-deploy releases rolled-past` — record a beta that was never promoted.

Called when a beta health verdict comes back NO-PROMOTE: the train dies
unpromoted, stable stays put, and the beta channel moves on with the next
Wednesday's ship. Until now that left no trace anywhere but a closed Linear
issue, so the widened stable version jump the *next* promotion has to carry was
not answerable from the registry.

``--reason`` is required. A rolled-past row whose reason is unknown tells the
next release manager nothing that the release simply being absent would not.
"""

import sys

from rich.console import Console

from saorsa_deploy.cmd.releases._shared import require_release_repository


def cmd_releases_rolled_past(args):
    console = Console(width=140)
    repo = require_release_repository()

    if getattr(args, "id", None) is not None:
        release = repo.get(args.id)
        identifier = f"id={args.id}"
    elif args.binary_target and args.version:
        release = repo.get_by_version(args.binary_target, args.version)
        identifier = f"{args.binary_target} {args.version}"
    else:
        sys.stderr.write(
            "releases rolled-past: provide --id, or both --binary-target "
            "and --version.\n"
        )
        sys.exit(2)

    if release is None:
        sys.stderr.write(
            f"releases rolled-past: no release found for {identifier}\n"
        )
        sys.exit(1)

    try:
        release = repo.mark_rolled_past(release.id, reason=args.reason)
    except ValueError as exc:
        sys.stderr.write(f"releases rolled-past: {exc}\n")
        sys.exit(2)

    console.print(
        f"[yellow]Rolled past[/yellow]  id={release.id}  "
        f"{release.binary_target} {release.version}"
    )
    console.print(f"  [cyan]Reason[/cyan] {args.reason}")
