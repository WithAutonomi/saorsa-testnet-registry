"""``build ant-wasm-check`` -- run the WASM client build locally, deploy nothing.

`build ant-wasm` costs a build VM and ~10 minutes before it can tell you that
ant-client's browser API moved under the TypeScript harness. In a release train
that discovery lands at the last step before the client tier comes up, on a
testnet whose node fleet is already running and billing.

This runs the SAME build in a temp directory on the operator's machine: clone,
wasm-pack, the harness, and the load check that catches a renamed browser entry
point. It creates no infrastructure, uploads nothing, and touches no deployment.
The commands themselves live in :mod:`saorsa_deploy.wasm_build` so the two paths
cannot drift -- a local check that builds something slightly different is worse
than none, because it reports a pass the real build will not honour.

Exit 0 means the harness builds against that ref. Anything else prints the
failing step's output.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from rich.console import Console

from saorsa_deploy.binary_source import validate_branch_exists
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.wasm_build import (
    ANT_CLIENT_REPO,
    NODE_MAJOR,
    NPM_BUILD_ARGV,
    NPM_INSTALL_ARGV,
    WASM_PACK_ARGV,
    WASM_PACK_VERSION,
    WASM_PKG_SUBDIR,
    bundle_load_check_source,
    clone_argv,
    shell,
)

WASM_TARGET = "wasm32-unknown-unknown"


class PreflightError(RuntimeError):
    """A required tool is missing. Carries the exact command to install it."""


def _run(argv, cwd=None, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def _node_major(version: str) -> int | None:
    match = re.match(r"v?(\d+)", version.strip())
    return int(match.group(1)) if match else None


def preflight() -> list[str]:
    """Return one "missing tool -> install command" line per absent dependency.

    Nothing is installed here, deliberately: this runs on the operator's own
    machine, and a tool that silently installs a toolchain on a developer
    workstation is a surprise nobody asked for. The exact command is printed so
    the fix is a copy-paste rather than a search.
    """
    problems: list[str] = []

    if shutil.which("git") is None:
        problems.append("git is not on PATH. Install it with your package manager.")

    if shutil.which("cargo") is None:
        problems.append(
            "cargo (Rust) is not on PATH. Install it with:\n"
            "    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"
        )
    else:
        targets = _run(["rustup", "target", "list", "--installed"])
        if targets.returncode != 0 or WASM_TARGET not in targets.stdout:
            problems.append(
                f"the {WASM_TARGET} Rust target is not installed. Install it with:\n"
                f"    rustup target add {WASM_TARGET}"
            )

    installed = shutil.which("wasm-pack")
    if installed is None:
        problems.append(
            f"wasm-pack is not on PATH. Install the pinned version with:\n"
            f"    cargo install wasm-pack --version {WASM_PACK_VERSION} --locked"
        )
    else:
        result = _run(["wasm-pack", "--version"])
        found = result.stdout.strip()
        if WASM_PACK_VERSION not in found:
            # A warning, not a failure: wasm-pack drives wasm-bindgen, whose
            # generated glue must match the crate version the build resolves,
            # and the build VM pins this exact one. A different version may
            # still build and then fail to LOAD -- which the check below would
            # catch anyway, but for a reason that would read as ant-client's
            # fault rather than the local toolchain's.
            problems.append(
                f"wasm-pack is {found or 'an unknown version'}, but the build VM "
                f"pins {WASM_PACK_VERSION}. Match it with:\n"
                f"    cargo install wasm-pack --version {WASM_PACK_VERSION} --locked"
            )

    if shutil.which("node") is None:
        problems.append(
            f"node is not on PATH. The build VM uses Node {NODE_MAJOR}; install it with:\n"
            f"    curl -fsSL https://deb.nodesource.com/setup_{NODE_MAJOR}.x | sudo bash - "
            f"&& sudo apt-get install -y nodejs"
        )
    else:
        major = _node_major(_run(["node", "--version"]).stdout)
        if major is None or major < int(NODE_MAJOR):
            problems.append(
                f"node is v{major}, but the build needs >= {NODE_MAJOR}. Install it with:\n"
                f"    curl -fsSL https://deb.nodesource.com/setup_{NODE_MAJOR}.x | sudo bash - "
                f"&& sudo apt-get install -y nodejs"
            )

    if shutil.which("npm") is None:
        problems.append("npm is not on PATH. It ships with Node; install Node above.")

    return problems


def _stage_client_sources(dest: Path) -> None:
    """Copy the TypeScript harness out of this package, as the build VM does.

    The harness lives here, versioned with the tool that provisions it, rather
    than in the ant-client checkout -- so a check of ant-client `main` is
    exactly "does THIS harness still build against THAT ref".
    """
    src = get_resources_dir() / "wasm-client"
    dest.mkdir(parents=True, exist_ok=True)
    # package-lock.json too: the build runs `npm ci`, which refuses without it.
    for rel in ("package.json", "package-lock.json", "tsconfig.json"):
        shutil.copy2(src / rel, dest / rel)
    (dest / "src").mkdir(exist_ok=True)
    for path in sorted((src / "src").glob("*.ts")):
        shutil.copy2(path, dest / "src" / path.name)


def cmd_build_ant_wasm_check(args) -> None:
    console = Console()

    problems = preflight()
    if problems:
        console.print(
            "[bold red]Error:[/bold red] this check needs a local toolchain "
            "matching the build VM's, and it installs nothing itself:"
        )
        for problem in problems:
            console.print(f"  • {problem}")
        sys.exit(1)

    repo_owner = args.repo_owner
    ref = args.branch

    try:
        validate_branch_exists(repo_owner, ANT_CLIENT_REPO, ref)
    except (ValueError, RuntimeError) as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    work = Path(tempfile.mkdtemp(prefix="ant-wasm-check-"))
    checkout = work / ANT_CLIENT_REPO
    client_dir = work / "wasm-client"
    pkg_dir = checkout / WASM_PKG_SUBDIR

    console.print(
        f"[bold]Checking the WASM client build against "
        f"{repo_owner}/{ANT_CLIENT_REPO} @ {ref}[/bold]"
    )
    console.print(f"[dim]Working in {work}[/dim]")

    # Keep the working tree when the build fails: the whole value of a local
    # check is being able to open the sources that would not compile.
    keep = False
    try:
        steps = [
            (
                f"Clone {repo_owner}/{ANT_CLIENT_REPO} ({ref})",
                clone_argv(repo_owner, ref, str(checkout)),
                None,
            ),
            ("Build ant-core to WASM", list(WASM_PACK_ARGV), checkout),
        ]
        for label, argv, cwd in steps:
            if not _step(console, label, argv, cwd):
                keep = True
                sys.exit(1)

        _stage_client_sources(client_dir)
        for label, argv in (
            ("Install the TypeScript client's dependencies", list(NPM_INSTALL_ARGV)),
            ("Build the TypeScript client", list(NPM_BUILD_ARGV)),
        ):
            if not _step(console, label, argv, client_dir):
                keep = True
                sys.exit(1)

        if not _step(
            console,
            "Verify the bundle loads",
            ["node", "-e", bundle_load_check_source(str(pkg_dir))],
            work,
        ):
            keep = True
            sys.exit(1)
    finally:
        if keep:
            console.print(f"[yellow]Left the working tree at {work}[/yellow]")
        else:
            shutil.rmtree(work, ignore_errors=True)

    console.print()
    console.print(
        f"[bold green]OK[/bold green] — the WASM client builds against "
        f"{repo_owner}/{ANT_CLIENT_REPO} @ {ref}."
    )
    sys.exit(0)


def _step(console: Console, label: str, argv: list[str], cwd) -> bool:
    """Run one build step, reporting its output only when it fails."""
    console.print(f"[bold]›[/bold] {label}")
    console.print(f"  [dim]$ {shell(argv)}[/dim]")
    env = dict(os.environ)
    result = _run(argv, cwd=str(cwd) if cwd else None, env=env)
    if result.returncode == 0:
        return True
    console.print(f"[bold red]FAILED:[/bold red] {label} (exit {result.returncode})")
    for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        if text.strip():
            console.print(f"[dim]--- {stream} ---[/dim]")
            console.print(text.rstrip())
    return False
