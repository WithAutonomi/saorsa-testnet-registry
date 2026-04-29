"""Shared helper for recording which binary was deployed.

Three deploy commands resolve a binary from the same trio of flags
(``--branch-name`` / ``--repo-owner`` / ``--<x>-version``): prod-uploaders,
prod-continuous-downloader, and `nodes provision`. They all want to
persist the choice into ``deploy_args`` so consumers like
``deployments slack-post`` can show what was actually installed.
"""

from __future__ import annotations


def build_binary_descriptor(
    branch_name: str | None,
    repo_owner: str | None,
    version: str | None,
) -> dict:
    """Produce a JSON-friendly record of the resolved binary source.

    Branch takes precedence over version, mirroring the resolver logic in
    ``binary_source.py`` and the per-command ``_resolve_*`` helpers. When
    ``branch_name`` is set without ``repo_owner``, the owner defaults to
    ``maidsafe`` to match those resolvers.
    """
    if branch_name:
        return {
            "kind": "branch",
            "branch": branch_name,
            "repo_owner": repo_owner or "maidsafe",
        }
    if version:
        return {"kind": "release", "version": version}
    return {"kind": "latest"}
