"""Check that a pull request's version bump matches the change it makes.

antops runs a `saorsa-deploy` **wheel pinned by version** in a Linear issue,
fetched from a GitHub release — so a change to the tool that does not move the
version is a change no run can ask for. Nothing enforced that: the version sat
at `0.2.4` from 2026-03-12 through roughly 45 merged PRs.

The bump is decided at PR time by whoever made the change, declared in the PR
body as

    Semantic version: breaking | feature | fix | none

and checked here. The workflow (`.github/workflows/version-check.yml`) only
gathers the four inputs — the head tree, the base `deploy/pyproject.toml`, the
PR body and the changed-file list — and every decision made on them is in this
module, because a decision inside a YAML `run:` block has nowhere to put a test.

Usage:
    python scripts/check_version_bump.py \
        --repo-root . \
        --base-pyproject /tmp/base-deploy-pyproject.toml \
        --body-file /tmp/pr-body.md \
        --changed-files /tmp/changed-files.txt

Exit 0 when the PR is consistent, 1 with an explanation of exactly what to do
when it is not.
"""

import argparse
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The four workspace packages, which share one version number.
PYPROJECT_NAMES = [
    "deploy/pyproject.toml",
    "db/pyproject.toml",
    "web/pyproject.toml",
    "mcp/pyproject.toml",
]

#: A change under one of these prefixes is a change to the tool.
TOOL_PREFIXES = ("deploy/", "db/")

#: Documentation and run manifests living inside the tool's trees are not.
TOOL_EXCLUSIONS = ("deploy/README.md", "deploy/manifests/", "db/README.md")

DECLARED_TYPES = ("breaking", "feature", "fix", "none")

#: Tolerant of surrounding whitespace and markdown emphasis, case-insensitive
#: on the value. Deliberately NOT tolerant of the template's
#: `<breaking | feature | fix | none>` placeholder: an unedited template line
#: has declared nothing, and accepting it would wave through every PR whose
#: author never read the line.
DECLARED_RE = re.compile(
    r"^[ \t]*[*_`]*[ \t]*semantic[ \t]+version[ \t]*[*_`]*[ \t]*:(.*)$",
    re.IGNORECASE | re.MULTILINE,
)

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

BUMP_HELP = (
    "add a `Semantic version: breaking | feature | fix | none` line to the PR body"
)


class CheckError(Exception):
    """A problem with the PR that the author has to fix."""


def read_version(path: Path) -> str:
    """Return `project.version` from a pyproject file."""
    data = tomllib.loads(Path(path).read_text())
    try:
        return data["project"]["version"]
    except KeyError:
        raise CheckError(f"{path}: no [project] version field") from None


def collect_head_versions(repo_root: Path) -> dict[str, str]:
    """Return `{relative path: version}` for the four workspace packages."""
    return {name: read_version(Path(repo_root) / name) for name in PYPROJECT_NAMES}


def single_head_version(versions: dict[str, str]) -> str:
    """The one version the four packages share, or raise naming the dissenters."""
    distinct = sorted(set(versions.values()))
    if len(distinct) > 1:
        detail = ", ".join(
            f"{name} = {version}" for name, version in sorted(versions.items())
        )
        raise CheckError(
            "the workspace packages are at different versions: "
            f"{detail}. They share a single version number — run "
            "`uv run scripts/bump_version.py <version> --no-commit` to set all four."
        )
    return distinct[0]


def parse_declared_type(body: str | None) -> str:
    """Return the declared bump type from a PR body.

    Raises :class:`CheckError` when the line is missing or names something that
    is not one of the four types — a typo must not read as "no bump declared
    and therefore none".
    """
    match = DECLARED_RE.search(body or "")
    if not match:
        raise CheckError(
            f"no `Semantic version:` line found in the PR body — {BUMP_HELP}"
        )
    value = match.group(1).strip().strip("*_`").strip()
    declared = value.lower()
    if declared not in DECLARED_TYPES:
        raise CheckError(
            f"`Semantic version: {value}` is not a known type — {BUMP_HELP}"
        )
    return declared


def paths_touch_tool(paths) -> bool:
    """True when any changed path is a change to `deploy/` or `db/` itself.

    Documentation and the run manifests live in those trees but are not the
    tool, so a docs-only PR is not asked for a bump.
    """
    for raw in paths:
        path = raw.strip()
        if not path or path.startswith(TOOL_EXCLUSIONS):
            continue
        if path.startswith(TOOL_PREFIXES):
            return True
    return False


def expected_version(base: str, declared: str) -> str:
    """The version a `breaking` / `feature` / `fix` PR must carry."""
    match = SEMVER_RE.match(base)
    if not match:
        raise CheckError(
            f"the base version '{base}' is not a SemVer string (expected X.Y.Z)"
        )
    major, minor, patch = (int(part) for part in match.groups())
    if declared == "breaking":
        return f"{major + 1}.0.0"
    if declared == "feature":
        return f"{major}.{minor + 1}.0"
    if declared == "fix":
        return f"{major}.{minor}.{patch + 1}"
    raise CheckError(f"'{declared}' does not imply a version bump")


def check(
    base_version: str,
    head_versions: dict[str, str],
    body: str | None,
    tool_touched: bool,
):
    """Raise :class:`CheckError` unless the PR's bump matches what it changes.

    Returns a one-line summary of what was accepted.
    """
    head_version = single_head_version(head_versions)
    declared = parse_declared_type(body)

    if tool_touched:
        if declared == "none":
            raise CheckError(
                "this PR changes `deploy/` or `db/`, so it needs a version bump: "
                "declare `Semantic version: breaking | feature | fix` in the PR body "
                "and run `uv run scripts/bump_version.py <version> --no-commit`."
            )
        wanted = expected_version(base_version, declared)
        if head_version != wanted:
            raise CheckError(
                f"this PR declares `{declared}` against base version {base_version}, "
                f"so every package must be at {wanted}, but they are at {head_version}. "
                f"Run `uv run scripts/bump_version.py {wanted} --no-commit` and commit "
                "the four pyproject.toml files and uv.lock."
            )
        return f"{declared}: {base_version} -> {head_version}"

    if declared != "none":
        raise CheckError(
            "this PR does not change `deploy/` or `db/`; revert the version bump and "
            f"declare `none` (it declares `{declared}`)."
        )
    if head_version != base_version:
        raise CheckError(
            "this PR does not change `deploy/` or `db/`; revert the version bump and "
            f"declare `none` (the version moved {base_version} -> {head_version})."
        )
    return f"none: version unchanged at {head_version}"


def _read_lines(path: str | None) -> list[str]:
    if not path:
        return []
    return Path(path).read_text().splitlines()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="tree holding the four pyproject.toml files (the PR head)",
    )
    parser.add_argument(
        "--base-pyproject",
        help="deploy/pyproject.toml as it is on the base branch",
    )
    parser.add_argument(
        "--base-version", help="base version, instead of --base-pyproject"
    )
    parser.add_argument("--body-file", help="file holding the PR body")
    parser.add_argument(
        "--changed-files",
        help="file holding the output of `git diff --name-only <base>...<head>`",
    )
    args = parser.parse_args(argv)

    try:
        if args.base_version:
            base_version = args.base_version
        elif args.base_pyproject:
            base_version = read_version(Path(args.base_pyproject))
        else:
            raise CheckError("one of --base-version or --base-pyproject is required")
        head_versions = collect_head_versions(Path(args.repo_root))
        body = Path(args.body_file).read_text() if args.body_file else ""
        tool_touched = paths_touch_tool(_read_lines(args.changed_files))
        summary = check(base_version, head_versions, body, tool_touched)
    except CheckError as error:
        print(f"version-bump: FAIL\n\n{error}", file=sys.stderr)
        return 1

    scope = "changes the tool" if tool_touched else "does not change the tool"
    print(f"version-bump: OK — this PR {scope}; {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
