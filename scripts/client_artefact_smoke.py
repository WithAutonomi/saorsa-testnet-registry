"""Prove a shipped `ant` CLI release is usable from both places we publish it.

Run after a release is tagged and the npm package published. It asks the two
questions a release can fail even when the code is fine: are the *artefacts*
right, and are the *two distribution channels* the same build?

1. The GitHub release's musl tarball verifies against the release's own
   `SHA256SUMS.txt` and reports the expected version.
2. `@withautonomi/ant`'s `latest` dist-tag is that version, and `npx
   @withautonomi/ant@<version> --version` agrees.
3. Both installs download the same public address and produce byte-identical
   files. Identical bytes from two independently obtained binaries is the whole
   point: a channel that ships a different build shows up here and nowhere else.
4. `ant update --force` from the npm install refuses, because updating a
   npm-distributed binary in place would leave npm's metadata lying about what
   is installed (ant-client#192). `--force` is required: ant explains the
   refusal only when an update is waiting or a reinstall was asked for, and the
   install under test is the latest version, so a plain `ant update` just says
   "Already up to date".

No wallet and no upload: the promoted binary is the rc head and the rc has
already run the client production tests. This proves the promoted artefacts and
the npm publish, nothing more.

Usage:
    uv run scripts/client_artefact_smoke.py --version <version> [--address <64-hex>]
        [--json-out out.json]

Example:
    uv run scripts/client_artefact_smoke.py --version 0.3.8
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "deploy"))

from saorsa_deploy.prod_bootstrap_upgrade import (  # noqa: E402
    SUMS_ASSET_NAME,
    expected_digest,
    file_digest,
)

GITHUB_REPO = "WithAutonomi/ant-client"
NPM_PACKAGE = "@withautonomi/ant"
#: The musl build is the one a release ships for x86-64 Linux; it is also the
#: one the prod client VMs install, so it is the artefact worth proving.
ASSET_TEMPLATE = "ant-{version}-x86_64-unknown-linux-musl.tar.gz"

DEFAULT_ADDRESSES_PATH = (
    REPO_ROOT / "deploy" / "saorsa_deploy" / "resources" / "prod_addresses.txt"
)

#: ant-client#192's refusal. Matched as a substring, case-insensitively: the
#: sentence around it is free to change, the fact it names npm is not.
NPM_REFUSAL_PHRASE = "installed with npm"

_VERSION_OUTPUT_RE = re.compile(r"^ant\s+(?P<version>\S+)\s*$", re.MULTILINE)

DOWNLOAD_TIMEOUT_SECS = 900
COMMAND_TIMEOUT_SECS = 300
FETCH_TIMEOUT_SECS = 300


class SmokeError(RuntimeError):
    """A misconfiguration or an unusable artefact -- never a failed expectation."""


# -- pure helpers ----------------------------------------------------------
def asset_name(version: str) -> str:
    return ASSET_TEMPLATE.format(version=version)


def release_urls(version: str) -> tuple[str, str]:
    """``(archive_url, sums_url)`` for a released `ant` version.

    The tag is ``ant-cli-v<version>``, not ``v<version>``: ant-client tags each
    binary separately, and the sums file must come from the *same* release as
    the archive or the check proves nothing.
    """
    base = f"https://github.com/{GITHUB_REPO}/releases/download/ant-cli-v{version}"
    return f"{base}/{asset_name(version)}", f"{base}/{SUMS_ASSET_NAME}"


def parse_version_output(text: str) -> str:
    """The version from ``ant --version`` output.

    Raises ``ValueError`` when the output is not ant's, which is what a
    wrong-architecture or truncated download looks like when it runs at all.
    """
    match = _VERSION_OUTPUT_RE.search(text or "")
    if not match:
        first = (text or "").strip().splitlines()[:1]
        raise ValueError(f"not `ant --version` output: {(first[0] if first else '')!r}")
    return match.group("version")


def require_version(text: str, version: str) -> str:
    """The version line, or ``ValueError`` if it is not exactly ``version``."""
    found = parse_version_output(text)
    if found != version:
        raise ValueError(f"expected ant {version}, got ant {found}")
    return f"ant {found}"


def parse_dist_tags(text: str) -> dict[str, str]:
    """``{tag: version}`` from ``npm view <pkg> dist-tags --json``."""
    try:
        parsed = json.loads(text or "")
    except json.JSONDecodeError as exc:
        raise ValueError(f"npm did not return JSON dist-tags: {exc}") from exc
    # npm 12 wraps a single-field `view --json` result in a one-element array
    # (`[{"latest": "0.3.8"}]`); earlier releases return the bare object.
    if isinstance(parsed, list) and len(parsed) == 1:
        parsed = parsed[0]
    if not isinstance(parsed, dict):
        raise ValueError(f"npm dist-tags is not an object: {parsed!r}")
    return {str(k): str(v) for k, v in parsed.items()}


def files_identical(left: Path, right: Path) -> bool:
    """Whether two downloads produced the same bytes.

    Compared by digest rather than ``filecmp.cmp``, which can be told to trust
    size and mtime -- and two downloads of the same address differ in mtime
    while being the file we are trying to prove identical.
    """
    if not left.exists() or not right.exists():
        return False
    if left.stat().st_size == 0 or right.stat().st_size == 0:
        return False
    return file_digest(left) == file_digest(right)


def is_npm_refusal(text: str) -> bool:
    """Whether ``ant update`` refused because the binary came from npm."""
    return NPM_REFUSAL_PHRASE in (text or "").lower()


def first_address(path: Path) -> str:
    """The first address in the curated production list."""
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    raise SmokeError(f"no address found in {path}")


# -- runners ---------------------------------------------------------------
@dataclass
class Source:
    """One distribution channel and what it produced."""

    name: str
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    version_line: str | None = None
    download_bytes: int | None = None
    download_secs: float | None = None
    download_path: Path | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {
            "source": self.name,
            "version_line": self.version_line,
            "download_bytes": self.download_bytes,
            "download_secs": None
            if self.download_secs is None
            else round(self.download_secs, 1),
            "errors": self.errors,
            "pass": self.ok,
        }


def run(
    command: list[str], env: dict[str, str], timeout: int
) -> subprocess.CompletedProcess:
    merged = {**os.environ, **env}
    return subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, env=merged
    )


def fetch(url: str, dest: Path | None = None) -> bytes:
    resp = requests.get(url, timeout=FETCH_TIMEOUT_SECS, allow_redirects=True)
    resp.raise_for_status()
    if dest is not None:
        dest.write_bytes(resp.content)
    return resp.content


def install_from_release(version: str, workdir: Path) -> Source:
    """Download, verify and extract the release tarball; check its version."""
    source = Source(name="github release")
    archive_url, sums_url = release_urls(version)
    archive = workdir / asset_name(version)
    try:
        sums = fetch(sums_url).decode()
        fetch(archive_url, archive)
    except requests.RequestException as exc:
        source.errors.append(f"download failed: {exc}")
        return source
    try:
        wanted = expected_digest(sums, asset_name(version))
    except ValueError as exc:
        source.errors.append(str(exc))
        return source
    found = file_digest(archive)
    if found != wanted:
        source.errors.append(f"sha256 mismatch: expected {wanted}, got {found}")
        return source
    extract_dir = workdir / "extracted"
    extract_dir.mkdir(exist_ok=True)
    with tarfile.open(archive) as tar:
        # `filter="data"` refuses absolute paths and symlinks escaping the
        # destination; without it Python 3.14 removes the permissive default.
        tar.extractall(extract_dir, filter="data")
    binary = extract_dir / "ant"
    if not binary.exists():
        candidates = sorted(p for p in extract_dir.rglob("ant") if p.is_file())
        if not candidates:
            source.errors.append(f"no `ant` binary in {asset_name(version)}")
            return source
        binary = candidates[0]
    binary.chmod(0o755)
    source.command = [str(binary)]
    result = run([*source.command, "--version"], {}, COMMAND_TIMEOUT_SECS)
    try:
        source.version_line = require_version(result.stdout + result.stderr, version)
    except ValueError as exc:
        source.errors.append(str(exc))
    return source


def install_from_npm(version: str, workdir: Path) -> Source:
    """Resolve the npm package in its own cache; check its dist-tag and version."""
    source = Source(name=f"npm {NPM_PACKAGE}")
    cache = workdir / "npm-cache"
    cache.mkdir(exist_ok=True)
    source.env = {"npm_config_cache": str(cache), "npm_config_update_notifier": "false"}
    try:
        view = run(
            ["npm", "view", NPM_PACKAGE, "dist-tags", "--json"],
            source.env,
            COMMAND_TIMEOUT_SECS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        source.errors.append(f"npm view failed: {exc}")
        return source
    if view.returncode != 0:
        detail = (view.stderr or view.stdout).strip().splitlines()[:1]
        source.errors.append(
            f"package not published: npm view exited {view.returncode}"
            + (f" ({detail[0][:160]})" if detail else "")
        )
        return source
    try:
        tags = parse_dist_tags(view.stdout)
    except ValueError as exc:
        source.errors.append(str(exc))
        return source
    latest = tags.get("latest")
    if latest != version:
        source.errors.append(f"dist-tag latest is {latest!r}, expected {version!r}")
    source.command = ["npx", "--yes", f"{NPM_PACKAGE}@{version}"]
    result = run([*source.command, "--version"], source.env, COMMAND_TIMEOUT_SECS)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[:1]
        source.errors.append(
            f"package not published: npx exited {result.returncode}"
            + (f" ({detail[0][:160]})" if detail else "")
        )
        return source
    try:
        source.version_line = require_version(result.stdout + result.stderr, version)
    except ValueError as exc:
        source.errors.append(str(exc))
    return source


def download_address(source: Source, address: str, dest: Path) -> None:
    """`ant -v file download <address> -o <dest>`, timed."""
    if not source.command:
        source.errors.append("no usable install, download skipped")
        return
    started = time.monotonic()
    try:
        result = run(
            [*source.command, "-v", "file", "download", address, "-o", str(dest)],
            source.env,
            DOWNLOAD_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        source.errors.append(f"download timed out after {DOWNLOAD_TIMEOUT_SECS}s")
        return
    source.download_secs = time.monotonic() - started
    source.download_path = dest
    source.download_bytes = dest.stat().st_size if dest.exists() else 0
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        source.errors.append(
            f"download exited {result.returncode}"
            + (f": {detail[0][:200]}" if detail else "")
        )
    elif not source.download_bytes:
        source.errors.append("download produced an empty file")


def check_npm_update_refusal(source: Source) -> tuple[bool, str]:
    """`ant update --force` from the npm install must refuse, naming npm.

    Without ``--force`` an up-to-date install prints "Already up to date" and
    never reaches the refusal, which is the normal case here: the version under
    test is the one npm just installed (measured on 0.3.8).
    """
    if not source.command:
        return False, "no npm install to run `ant update` from"
    result = run(
        [*source.command, "update", "--force"], source.env, COMMAND_TIMEOUT_SECS
    )
    output = (result.stdout or "") + (result.stderr or "")
    if is_npm_refusal(output):
        return True, "refused, naming npm"
    line = output.strip().splitlines()[-1:] if output.strip() else []
    return False, f"no npm refusal: {(line[0][:200] if line else 'no output')}"


# -- report ----------------------------------------------------------------
def render_report(
    version: str, address: str, sources: list[Source], extras: list[dict]
) -> str:
    lines = [
        f"### `ant` client artefact smoke — {version}",
        "",
        f"Address `{address}`.",
        "",
        "| source | version line | download bytes | seconds | pass |",
        "| -- | -- | --: | --: | :-: |",
    ]
    for source in sources:
        lines.append(
            f"| {source.name} | {source.version_line or '-'} | "
            f"{'-' if source.download_bytes is None else f'{source.download_bytes:,}'} | "
            f"{'-' if source.download_secs is None else f'{source.download_secs:.1f}'} | "
            f"{'PASS' if source.ok else 'FAIL'} |"
        )
    lines += ["", "| check | verdict | detail |", "| -- | :-: | -- |"]
    for extra in extras:
        lines.append(f"| {extra['name']} | {extra['verdict']} | {extra['detail']} |")
    failures = [e for s in sources for e in s.errors]
    if failures:
        lines += ["", "Failures:", ""]
        lines += [f"- {f}" for f in failures]
    return "\n".join(lines) + "\n"


def run_smoke(version: str, address: str, parent: Path) -> dict:
    release_dir = parent / "release"
    npm_dir = parent / "npm"
    release_dir.mkdir()
    npm_dir.mkdir()

    release = install_from_release(version, release_dir)
    npm = install_from_npm(version, npm_dir)

    download_address(release, address, release_dir / "download.bin")
    download_address(npm, address, npm_dir / "download.bin")

    extras: list[dict] = []
    identical = files_identical(
        release.download_path or release_dir / "missing",
        npm.download_path or npm_dir / "missing",
    )
    extras.append(
        {
            "name": "downloads byte-identical",
            "verdict": "PASS" if identical else "FAIL",
            "detail": (
                "both installs produced the same bytes"
                if identical
                else "the two downloads differ, are empty, or one did not happen"
            ),
        }
    )
    refused, detail = check_npm_update_refusal(npm)
    extras.append(
        {
            "name": "`ant update --force` refuses on npm install",
            "verdict": "PASS" if refused else "FAIL",
            "detail": detail,
        }
    )
    passed = release.ok and npm.ok and all(e["verdict"] == "PASS" for e in extras)
    return {
        "version": version,
        "address": address,
        "sources": [s.as_dict() for s in (release, npm)],
        "checks": extras,
        "passed": passed,
        "_sources": [release, npm],
        "_extras": extras,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--version", required=True, help="Released `ant` version, e.g. 0.3.8."
    )
    parser.add_argument(
        "--address",
        default=None,
        help=f"Public address to download (default: the first line of {DEFAULT_ADDRESSES_PATH.name}).",
    )
    parser.add_argument(
        "--json-out", default=None, help="Write every figure as JSON here."
    )
    args = parser.parse_args(argv)

    try:
        address = args.address or first_address(DEFAULT_ADDRESSES_PATH)
    except (OSError, SmokeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="ant-artefact-smoke-") as tmp:
        result = run_smoke(args.version, address, Path(tmp))
        markdown = render_report(
            args.version, address, result["_sources"], result["_extras"]
        )

    sys.stdout.write(markdown)
    if args.json_out:
        payload = {k: v for k, v in result.items() if not k.startswith("_")}
        Path(args.json_out).expanduser().write_text(json.dumps(payload, indent=2))
        print(f"wrote {args.json_out}", file=sys.stderr)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
