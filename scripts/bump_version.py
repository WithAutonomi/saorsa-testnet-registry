"""Bump the version across all workspace packages.

The four packages share one version number, and CI enforces that a PR touching
`deploy/` or `db/` moves it (see `.github/workflows/version-check.yml`). The
normal use is therefore `--no-commit`, run while preparing a PR: it edits the
four `pyproject.toml` files and `uv.lock` and stops, leaving the author to
commit them with the change.

Usage:
    uv run scripts/bump_version.py <version> --no-commit   # prepare a PR
    uv run scripts/bump_version.py <version>               # emergencies only

Without `--no-commit` the script also commits, tags and pushes. That path is
for a by-hand release when CI cannot make one: the tag it pushes is the same
`v<version>` the release workflow creates on merge, so running it on a branch
that is also going to be merged would leave the release job with nothing to do.

Example:
    uv run scripts/bump_version.py 1.0.0 --no-commit
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_FILES = [
    REPO_ROOT / "db" / "pyproject.toml",
    REPO_ROOT / "web" / "pyproject.toml",
    REPO_ROOT / "mcp" / "pyproject.toml",
    REPO_ROOT / "deploy" / "pyproject.toml",
]
VERSION_RE = re.compile(r'^version\s*=\s*"[^"]+"', re.MULTILINE)
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def validate_version(version: str) -> None:
    if not SEMVER_RE.match(version):
        print(f"Error: '{version}' is not a valid SemVer string (expected X.Y.Z)")
        sys.exit(1)


def update_pyproject(path: Path, version: str) -> None:
    content = path.read_text()
    new_content, count = VERSION_RE.subn(f'version = "{version}"', content, count=1)
    if count == 0:
        print(f"Error: no version field found in {path}")
        sys.exit(1)
    path.write_text(new_content)
    print(f"  Updated {path.relative_to(REPO_ROOT)}")


def run(cmd: list[str], **kwargs) -> None:
    print(f"  $ {' '.join(cmd)}")
    subprocess.run(cmd, check=True, cwd=REPO_ROOT, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="the new version, X.Y.Z")
    parser.add_argument(
        "--no-commit",
        action="store_true",
        help="edit the four pyproject.toml files and uv.lock and stop",
    )
    args = parser.parse_args()

    version = args.version
    validate_version(version)
    tag = f"v{version}"

    print(f"Bumping version to {version}...")
    for path in PYPROJECT_FILES:
        update_pyproject(path, version)

    print("\nRegenerating lockfile...")
    run(["uv", "lock"])

    if args.no_commit:
        print(
            f"\nDone. The four pyproject.toml files and uv.lock are at {version}; "
            "commit them with your change and declare the bump type in the PR body."
        )
        return

    print(f"\nCommitting and tagging {tag}...")
    run(
        [
            "git",
            "add",
            "db/pyproject.toml",
            "web/pyproject.toml",
            "mcp/pyproject.toml",
            "deploy/pyproject.toml",
            "uv.lock",
        ]
    )
    run(["git", "commit", "-m", f"chore: bump version to {version}"])
    run(["git", "tag", "-a", tag, "-m", tag])

    print("\nPushing commit and tag...")
    run(["git", "push", "--follow-tags"])

    print(f"\nDone! Version {version} tagged as {tag} and pushed.")


if __name__ == "__main__":
    main()
