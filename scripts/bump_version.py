"""Bump the version across all workspace packages, commit, tag, and push.

Usage:
    uv run scripts/bump_version.py <version>

Example:
    uv run scripts/bump_version.py 0.2.0
"""

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_FILES = [
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
    if len(sys.argv) != 2:
        print("Usage: uv run scripts/bump_version.py <version>")
        print("Example: uv run scripts/bump_version.py 0.2.0")
        sys.exit(1)

    version = sys.argv[1]
    validate_version(version)
    tag = f"v{version}"

    print(f"Bumping version to {version}...")
    for path in PYPROJECT_FILES:
        update_pyproject(path, version)

    print("\nRegenerating lockfile...")
    run(["uv", "lock"])

    print(f"\nCommitting and tagging {tag}...")
    run(
        [
            "git",
            "add",
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
