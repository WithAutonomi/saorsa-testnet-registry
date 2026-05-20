"""`saorsa-deploy release-candidates new` — record a release candidate.

A release candidate is the *whole* RC across every crate that was
bumped (e.g. "ant-node 0.11.1-rc.1" — saorsa-core + ant-protocol +
ant-node, etc). The payload describes the RC at the binary level and
embeds the per-crate detail.

Reads JSON from --from-file or stdin (`-`). Schema:

    {
      "binary_target":  "ant-node",        # required: "ant" or "ant-node"
      "binary_version": "0.11.1-rc.1",     # required: full RC version
      "rc_branch":      "rc-2026.04.1",    # required
      "notes":          null,              # optional, free text

      "crates": [
        {
          "crate_name": "saorsa-core",     # required
          "version":    "0.25.0-rc.1",     # required
          "repo_slug":  "saorsa-labs/saorsa-core",  # required
          "tag":        null,              # optional (only set for the
                                           # crate that got tagged in Step 6)
          "tag_sha":    null,
          "since_tag":  "v0.24.3",
          "since_sha":  "<sha>",
          "head_sha":   "<sha>",
          "prs": [
            {"number": 118, "title": "...", "author": "...",
             "merged_at": "2026-05-19T07:56:54Z",
             "url": "https://github.com/.../pull/118"}
          ]
        },
        ...
      ]
    }

Idempotent on `(binary_target, binary_version)`: re-running the same
payload reuses the existing RC, adds any missing crates, and within
each crate appends only newly-listed PRs.
"""

import json
import sys

from rich.console import Console

from saorsa_deploy.cmd.release_candidates._shared import require_rc_repository

_REQUIRED_TOP = ("binary_target", "binary_version", "rc_branch")
_REQUIRED_CRATE = ("crate_name", "version", "repo_slug")


def _load_payload(args) -> dict:
    if args.from_file == "-" or args.from_file is None:
        raw = sys.stdin.read()
        source = "<stdin>"
    else:
        with open(args.from_file, "r", encoding="utf-8") as fh:
            raw = fh.read()
        source = args.from_file
    if not raw.strip():
        sys.stderr.write(f"release-candidates new: empty payload from {source}\n")
        sys.exit(2)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(
            f"release-candidates new: invalid JSON from {source}: {exc}\n"
        )
        sys.exit(2)


def _validate(payload: dict) -> None:
    missing_top = [k for k in _REQUIRED_TOP if not payload.get(k)]
    if missing_top:
        sys.stderr.write(
            "release-candidates new: payload missing top-level field(s): "
            + ", ".join(missing_top)
            + "\n"
        )
        sys.exit(2)
    crates = payload.get("crates")
    if not isinstance(crates, list) or not crates:
        sys.stderr.write(
            "release-candidates new: payload missing non-empty 'crates' list\n"
        )
        sys.exit(2)
    for i, c in enumerate(crates):
        missing = [k for k in _REQUIRED_CRATE if not c.get(k)]
        if missing:
            sys.stderr.write(
                f"release-candidates new: crates[{i}] missing field(s): "
                + ", ".join(missing)
                + "\n"
            )
            sys.exit(2)


def cmd_release_candidates_new(args):
    console = Console()
    payload = _load_payload(args)
    _validate(payload)

    repo = require_rc_repository()

    existing = repo.get_by_binary_version(
        payload["binary_target"], payload["binary_version"]
    )
    if existing is not None:
        rc = existing
        console.print(
            f"[yellow]release-candidates new:[/yellow] "
            f"{rc.binary_target} {rc.binary_version} already recorded "
            f"(id={rc.id}); appending any new crates / PRs."
        )
    else:
        rc = repo.create(
            binary_target=payload["binary_target"],
            binary_version=payload["binary_version"],
            rc_branch=payload["rc_branch"],
            notes=payload.get("notes"),
        )
        console.print(
            f"[green]Recorded[/green] {rc.binary_target} {rc.binary_version} "
            f"(id={rc.id}) on branch {rc.rc_branch}."
        )

    for crate_payload in payload["crates"]:
        crate_name = crate_payload["crate_name"]
        existing_crate = repo.get_crate(rc.id, crate_name)
        if existing_crate is not None:
            crate = existing_crate
            console.print(
                f"  [yellow]crate[/yellow] {crate_name} already attached "
                f"(id={crate.id}); appending any new PRs."
            )
        else:
            crate = repo.add_crate(
                rc.id,
                crate_name=crate_name,
                version=crate_payload["version"],
                repo_slug=crate_payload["repo_slug"],
                tag=crate_payload.get("tag"),
                tag_sha=crate_payload.get("tag_sha"),
                since_tag=crate_payload.get("since_tag"),
                since_sha=crate_payload.get("since_sha"),
                head_sha=crate_payload.get("head_sha"),
            )
            console.print(
                f"  [green]+ crate[/green] {crate_name} {crate.version}"
            )

        prs = crate_payload.get("prs") or []
        if prs:
            created = repo.add_prs(crate.id, prs)
            console.print(
                f"      attached {len(created)} new PR(s) "
                f"(payload contained {len(prs)})"
            )
