"""Swap the ant-node binary on a running testnet's node VMs and restart the services.

``nodes swap-binary`` replaces ``/usr/local/bin/ant-node`` on every node VM of a
deployment with a different build — a GitHub release (``--node-version``) or a
custom branch build (``--branch-name``/``--repo-owner``, built with ``build
ant-node`` first) — and restarts the ``ant-node-<i>`` services so they come back
on it. Unit files, ports, data directories and NAT/clock-skew setup are left
exactly as ``nodes provision`` wrote them.

It exists for tests whose subject only appears on a node that already holds
data written by an *older* build (a storage-format migration, for example):
deploy the fleet on the released version, let uploads fill it, then move it to
the branch under test at a chosen moment. The auto-upgrade path cannot do this
without compiling shortened timers into the starting binary and publishing fake
releases, and it stays armed for the rest of the run.

Bootstraps are not touched: they run a different unit (``ant-bootstrap-<i>``)
from a different inventory, and the wire protocol between versions is the
caller's responsibility to have checked.

By default every VM is swapped in one parallel pass, so the whole fleet restarts
within seconds of each other. ``--stagger-secs N`` swaps the VMs one at a time
with ``N`` seconds between them instead, for a fleet where a simultaneous
restart would itself be the event under observation.
"""

from __future__ import annotations

import hashlib
import sys
import time
import tomllib
from pathlib import Path

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operations import run_ops
from rich.console import Console

from saorsa_deploy.binary_descriptor import build_binary_descriptor
from saorsa_deploy.cmd.provision_genesis import _resolve_binary_source
from saorsa_deploy.provisioning.swap_binary import (
    MARKER_BAD_BINARY,
    MARKER_DONE,
    MARKER_NO_EXECSTART,
    MARKER_NOT_ACTIVE,
    NODE_CONFIG_PATH,
    add_swap_binary_op,
)
from saorsa_deploy.registry import record_deploy_args_update, record_event
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_deployment_state, update_deployment_state

EVENT_TYPE = "nodes_binary_swapped"


def _require_explicit_binary(args, console) -> None:
    """A swap with no source would silently install "latest", which is never
    what a test wants and is exactly the accident this command exists to
    avoid. Refuse rather than guess."""
    has_version = bool(getattr(args, "node_version", None))
    has_branch = bool(getattr(args, "branch_name", None))
    if not has_version and not has_branch:
        console.print(
            "[bold red]Error:[/bold red] nodes swap-binary needs an explicit "
            "target: --node-version X.Y.Z, or --branch-name B --repo-owner O."
        )
        sys.exit(1)


def _load_node_config(args, console) -> str | None:
    """Read and validate the local ``--node-config`` file, or ``None`` if unset.

    Only TOML validity is checked here — the field names belong to the ant-node
    build being installed, which this tool cannot know. A file the node rejects
    shows up as every service failing to come back (``ANT_SWAP:NOT_ACTIVE``), with
    the reason in the node's journal.
    """
    raw_path = getattr(args, "node_config", None)
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    try:
        text = path.read_text()
    except OSError as e:
        console.print(f"[bold red]Error:[/bold red] cannot read --node-config {path}: {e}")
        sys.exit(1)
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        console.print(f"[bold red]Error:[/bold red] --node-config {path} is not valid TOML: {e}")
        sys.exit(1)
    if not parsed:
        console.print(f"[bold red]Error:[/bold red] --node-config {path} is empty")
        sys.exit(1)
    return text


def _resolve_host_ips(args, state, console) -> list[str]:
    vm_ips = state.get("vm_ips") or {}
    if not vm_ips:
        console.print(
            "[bold red]Error:[/bold red] No VM IPs found in deployment state. "
            "Run `saorsa-deploy nodes infra` and `nodes provision` first."
        )
        sys.exit(1)
    region = getattr(args, "region", None)
    if region:
        if region not in vm_ips:
            available = ", ".join(sorted(vm_ips.keys()))
            console.print(
                f"[bold red]Error:[/bold red] Region '{region}' not found. "
                f"Available regions: {available}"
            )
            sys.exit(1)
        return list(vm_ips[region])
    host_ips: list[str] = []
    for region_key in sorted(vm_ips.keys()):
        host_ips.extend(vm_ips[region_key])
    return host_ips


def _summarise(results, console) -> tuple[list[str], list[str]]:
    """Split hosts into (swapped, failed) from the op's marker lines."""
    swapped: list[str] = []
    failed: list[str] = []
    for host, meta in results.items():
        lines = list(getattr(meta, "stdout_lines", []) or [])
        done = next((ln for ln in lines if ln.startswith(MARKER_DONE)), None)
        if done:
            swapped.append(host.name)
            console.print(f"  [green]{host.name}[/green]: {done[len(MARKER_DONE) :].strip()}")
            continue
        failed.append(host.name)
        reason = next(
            (
                ln
                for ln in lines
                if ln.startswith((MARKER_BAD_BINARY, MARKER_NOT_ACTIVE, MARKER_NO_EXECSTART))
            ),
            "no completion marker (see host output above)",
        )
        console.print(f"  [red]{host.name}[/red]: {reason}")
    return swapped, failed


def _swap_hosts(
    host_ips: list[str],
    ssh_key_path: str,
    download_url: str,
    is_archive: bool,
    node_count: int,
    restart: bool,
    console,
    node_config: str | None = None,
) -> tuple[list[str], list[str]]:
    hosts_data = [(ip, {"ssh_user": "root", "ssh_key": ssh_key_path}) for ip in host_ips]
    inventory = Inventory((hosts_data, {}))
    pyinfra_state = State(inventory=inventory, config=Config())
    connect_all(pyinfra_state)
    try:
        results = add_swap_binary_op(
            pyinfra_state,
            download_url,
            is_archive,
            node_count,
            restart=restart,
            node_config=node_config,
        )
        run_ops(pyinfra_state)
    finally:
        disconnect_all(pyinfra_state)

    swapped, failed = _summarise(results, console)
    # A host pyinfra itself marks failed (SSH dropped, non-zero exit with no
    # marker) may not have produced a line at all.
    for host in pyinfra_state.failed_hosts:
        if host.name not in failed and host.name not in swapped:
            failed.append(host.name)
            console.print(f"  [red]{host.name}[/red]: pyinfra reported failure")
    return swapped, failed


def cmd_swap_nodes_binary(args):
    """Execute the `nodes swap-binary` command."""
    console = Console()
    _require_explicit_binary(args, console)

    console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
    try:
        state = load_deployment_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    node_count = state.get("node_count")
    if not node_count:
        console.print(
            "[bold red]Error:[/bold red] No node_count found in deployment state. "
            "Run `saorsa-deploy nodes provision` first."
        )
        sys.exit(1)

    host_ips = _resolve_host_ips(args, state, console)
    node_config = _load_node_config(args, console)
    download_url, is_archive = _resolve_binary_source(args, console)
    restart = not getattr(args, "no_restart", False)
    stagger = getattr(args, "stagger_secs", 0) or 0

    console.print(
        f"[bold]Swapping ant-node on {len(host_ips)} VM(s) "
        f"({node_count} service(s) each)"
        + (f", one VM every {stagger}s" if stagger else ", all in parallel")
        + ("" if restart else ", WITHOUT restarting services")
        + (f", installing node config at {NODE_CONFIG_PATH}" if node_config else "")
        + "...[/bold]"
    )
    clear_known_hosts(host_ips, console)

    started_at = time.time()
    swapped: list[str] = []
    failed: list[str] = []
    if stagger:
        for idx, ip in enumerate(host_ips):
            if idx:
                time.sleep(stagger)
            ok, bad = _swap_hosts(
                [ip],
                args.ssh_key_path,
                download_url,
                is_archive,
                node_count,
                restart,
                console,
                node_config=node_config,
            )
            swapped.extend(ok)
            failed.extend(bad)
    else:
        swapped, failed = _swap_hosts(
            host_ips,
            args.ssh_key_path,
            download_url,
            is_archive,
            node_count,
            restart,
            console,
            node_config=node_config,
        )

    descriptor = build_binary_descriptor(
        getattr(args, "branch_name", None),
        getattr(args, "repo_owner", None),
        getattr(args, "node_version", None),
    )
    details = {
        "node_binary": descriptor,
        "binary_url": download_url,
        "vm_count": len(host_ips),
        "swapped": len(swapped),
        "failed": failed,
        "node_count": node_count,
        "restarted": restart,
        "stagger_secs": stagger,
        "region": getattr(args, "region", None),
        "duration_secs": round(time.time() - started_at, 1),
    }
    if node_config is not None:
        # The file itself, not just its name: a run's record has to say what
        # the nodes were actually told, and the local file may change later.
        details["node_config"] = {
            "path": NODE_CONFIG_PATH,
            "sha256": hashlib.sha256(node_config.encode()).hexdigest(),
            "content": node_config,
        }
    record_event(args.name, EVENT_TYPE, details)
    if not failed and not getattr(args, "region", None):
        # Only a whole-fleet swap changes what the deployment is running.
        record_deploy_args_update(args.name, {"node_binary": descriptor})
        try:
            update_deployment_state(
                args.name,
                {"binary": {"url": download_url, "is_archive": is_archive, **_binary_info(args)}},
            )
        except Exception as e:  # state is advisory; never fail the swap on it
            console.print(f"[yellow]Warning: Failed to save binary info to state: {e}[/yellow]")

    console.print()
    console.print(
        f"[bold green]Binary swapped on {len(swapped)}/{len(host_ips)} host(s) "
        f"in {details['duration_secs']}s.[/bold green]"
    )
    if failed:
        console.print(
            f"[bold red]{len(failed)} host(s) failed; the old binary is still in "
            "place on any host that reported BAD_BINARY, and services were not "
            "restarted there.[/bold red]"
        )
        sys.exit(1)


def _binary_info(args) -> dict:
    info: dict = {}
    if getattr(args, "node_version", None):
        info["version"] = args.node_version
    if getattr(args, "branch_name", None):
        info["branch"] = args.branch_name
        info["repo_owner"] = args.repo_owner
    return info
