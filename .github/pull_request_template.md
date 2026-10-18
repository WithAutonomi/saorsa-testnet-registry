<!-- The Linear issue id and URL go on the first line, so Linear links the PR. -->

Semantic version: <breaking | feature | fix | none>

<!--
The `version-bump` check parses the line above and fails the PR if it does not
match what the PR changes:

  breaking  a change under `deploy/` or `db/` that a manifest or caller must
            adapt to            -> major+1, minor and patch reset
  feature   a new command, flag, manifest key or model field   -> minor+1
  fix       any other change to the tool                       -> patch+1
  none      a PR that changes neither `deploy/` nor `db/` (documentation,
            workflows, the web app) -> the version must not move

Set the version with `uv run scripts/bump_version.py <version> --no-commit` and
commit the four `pyproject.toml` files and `uv.lock` with the rest of the
change. A failing `version-bump` check is fixed by re-bumping, never by editing
the check. See README.md §Releasing.
-->

## Summary

-

## Test plan

-

## Deviations

-
