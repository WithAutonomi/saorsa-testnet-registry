"""`saorsa-deploy releases promote` — record Monday's promotion to stable.

Called by the release-from-rc skill after every crate is published and the
post-promotion checklist passes. Takes a JSON payload rather than flags because
it carries the **release manifest** — the per-crate name / version / tag SHA
that the release-from-rc skill has just assembled.

Payload schema::

    {
      "binary_target": "ant-node",          # required
      "version": "0.17.1",                  # required, suffix-free
      "final_tag": "v0.17.1",               # optional
      "final_tag_sha": "cf7de435…",         # optional
      "promoted_at": "2026-08-18T09:00:00Z",# optional, defaults to now
      "rc_version": "0.17.1-rc.1",          # optional, links the RC row
      "notes": "…",                         # optional
      "crates": [                           # optional, replaces the manifest
        {
          "crate_name": "ant-cli",          # required
          "version": "0.3.3",               # required
          "repo_slug": "WithAutonomi/ant-client",  # required
          "tag": "ant-cli-v0.3.3",          # optional (ant-core is never tagged)
          "tag_sha": "cf7de435…"            # optional
        }
      ]
    }

The release must already be recorded as shipped to beta — a promotion with no
ship is a release that skipped the soak, and recording it as though it soaked
would make every later verdict a lie. ``--allow-missing-ship`` is the deliberate
exception, for releases promoted under the **pre-beta** train structure (and for
backfill): it records the promotion with ``shipped_to_beta_at`` left NULL, which
reads as "never on beta" rather than as a soak of unknown length. That path
needs ``rc_branch`` in the payload, since no ship record supplied it.
"""

import json
import sys

from rich.console import Console

from saorsa_deploy.cmd.releases._shared import (
    parse_timestamp,
    require_release_repository,
    resolve_release_candidate_id,
)

_REQUIRED_TOP = ("binary_target", "version")
_REQUIRED_CRATE = ("crate_name", "version", "repo_slug")


def _load_payload(args):
    path = getattr(args, "from_file", None)
    try:
        if path in (None, "-"):
            raw = sys.stdin.read()
        else:
            with open(path) as handle:
                raw = handle.read()
    except OSError as exc:
        sys.stderr.write(f"releases promote: cannot read payload: {exc}\n")
        sys.exit(2)

    if not raw.strip():
        sys.stderr.write("releases promote: payload is empty.\n")
        sys.exit(2)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"releases promote: payload is not valid JSON: {exc}\n")
        sys.exit(2)


def _validate(payload):
    if not isinstance(payload, dict):
        sys.stderr.write("releases promote: payload must be a JSON object.\n")
        sys.exit(2)

    missing = [key for key in _REQUIRED_TOP if not payload.get(key)]
    if missing:
        sys.stderr.write(f"releases promote: payload missing {', '.join(missing)}.\n")
        sys.exit(2)

    crates = payload.get("crates") or []
    if not isinstance(crates, list):
        sys.stderr.write("releases promote: 'crates' must be a list.\n")
        sys.exit(2)
    for index, crate in enumerate(crates):
        if not isinstance(crate, dict):
            sys.stderr.write(f"releases promote: crates[{index}] must be an object.\n")
            sys.exit(2)
        crate_missing = [key for key in _REQUIRED_CRATE if not crate.get(key)]
        if crate_missing:
            sys.stderr.write(
                f"releases promote: crates[{index}] missing {', '.join(crate_missing)}.\n"
            )
            sys.exit(2)


def cmd_releases_promote(args):
    console = Console(width=140)
    payload = _load_payload(args)
    _validate(payload)

    repo = require_release_repository()
    binary_target = payload["binary_target"]
    version = payload["version"]

    release = repo.get_by_version(binary_target, version)
    allow_missing_ship = bool(getattr(args, "allow_missing_ship", False))

    if release is None and not allow_missing_ship:
        sys.stderr.write(
            f"releases promote: {binary_target} {version} has no ship record. "
            f"Record the beta ship first with `releases ship`, or pass "
            f"--allow-missing-ship if this release never went to beta.\n"
        )
        sys.exit(1)

    rc_id = resolve_release_candidate_id(repo, binary_target, payload.get("rc_version"))
    promoted_at = parse_timestamp(payload.get("promoted_at"), field="promoted_at")

    try:
        if release is None:
            if not payload.get("rc_branch"):
                sys.stderr.write(
                    "releases promote: --allow-missing-ship needs 'rc_branch' "
                    "in the payload — no ship record supplied it.\n"
                )
                sys.exit(2)
            release = repo.create_promoted(
                binary_target=binary_target,
                version=version,
                rc_branch=payload["rc_branch"],
                final_tag=payload.get("final_tag"),
                final_tag_sha=payload.get("final_tag_sha"),
                promoted_at=promoted_at,
                release_candidate_id=rc_id,
                notes=payload.get("notes"),
            )
            console.print(
                "[yellow]No ship record — recorded as promoted with no beta soak.[/yellow]"
            )
        else:
            release = repo.promote(
                release.id,
                final_tag=payload.get("final_tag"),
                final_tag_sha=payload.get("final_tag_sha"),
                promoted_at=promoted_at,
                release_candidate_id=rc_id,
                notes=payload.get("notes"),
            )
    except ValueError as exc:
        sys.stderr.write(f"releases promote: {exc}\n")
        sys.exit(2)

    crates = payload.get("crates") or []
    if crates:
        try:
            repo.set_crates(release.id, crates)
        except ValueError as exc:
            sys.stderr.write(f"releases promote: {exc}\n")
            sys.exit(2)

    console.print(
        f"[green]Promoted to stable[/green]  id={release.id}  "
        f"{release.binary_target} {release.version}  "
        f"tag={release.final_tag or '-'}"
    )
    if crates:
        console.print(f"  [cyan]Manifest[/cyan] {len(crates)} crates recorded")
