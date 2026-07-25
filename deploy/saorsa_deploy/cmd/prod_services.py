"""Manage a single ant-node systemd service on an already-provisioned host.

Motivation: a Hetzner (or any provider's) node VM can run into its storage
quota with no available headroom, while a cloud-side volume-size increase
needs human approval that can take 24-48h. In the meantime the only way to
free disk space is to remove one ``ant-node-N`` service; once the quota
increase lands, a matching service can be added back. ``ls`` shows per-service
disk usage to help pick which one to remove.

Purely imperative — driven by ``--inventory-path``/``--name``/
``--service-name``[/``--ssh-key-path``], with no manifest/orchestrator/DB
wiring, and works against any host in a given inventory, not just prod fleets.
``rm``/``add`` also update the inventory file's per-host ``nodes``/``services``
lists to match (best-effort — a failure here warns but never fails the
command, since the live SSH change has already happened by that point).
"""

import os
import subprocess
import sys
from pathlib import Path

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console
from rich.table import Table

from saorsa_deploy.inventory import (
    NodeVm,
    load_node_inventory,
    update_service_in_inventory,
)
from saorsa_deploy.provisioning.bootstrap import DEFAULT_REWARDS_ADDRESS
from saorsa_deploy.provisioning.node import (
    _build_node_exec_start,
    _build_node_unit_file,
)
from saorsa_deploy.provisioning.node_services import (
    build_list_services_command,
    build_remove_service_command,
    build_reset_data_command,
    build_write_unit_and_start_command,
    parse_node_unit,
    parse_service_list,
    parse_service_name,
)
from saorsa_deploy.provisioning.volume import LOG_DIR, MOUNT_POINT
from saorsa_deploy.ssh import clear_known_hosts

_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
]


def _resolve_ssh_key(vm: NodeVm, override: str | None) -> str:
    raw = override or vm.ssh_key
    return str(Path(raw).expanduser())


def _resolve_host(
    inventory_path: str, name: str, ssh_key_override: str | None, console: Console
) -> tuple[NodeVm, str]:
    """Load the inventory, find the named host, and resolve+validate its SSH
    key. Exits the process with a clear error on any failure — every caller
    handles this the same way, so the three commands' error messages match.
    """
    path = Path(inventory_path).expanduser()
    if not path.is_file():
        console.print(f"[bold red]Error:[/bold red] inventory not found: {path}")
        sys.exit(1)

    try:
        vms = load_node_inventory(path)
    except (ValueError, OSError) as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    matches = [vm for vm in vms if vm.name == name]
    if not matches:
        console.print(
            f"[bold red]Error:[/bold red] no host named {name!r} in {path}"
        )
        sys.exit(1)

    vm = matches[0]
    ssh_key = _resolve_ssh_key(vm, ssh_key_override)
    if not os.path.isfile(ssh_key):
        console.print(
            f"[bold red]Error:[/bold red] SSH key for {vm.name} does not "
            f"exist: {ssh_key}"
        )
        sys.exit(1)
    return vm, ssh_key


def _require_service_name(service_name: str, console: Console) -> None:
    try:
        parse_service_name(service_name)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


def _run_single_host_op(vm: NodeVm, ssh_key: str, op_name: str, command: str) -> list[str]:
    """Run one pyinfra shell op against a single host; return its stdout lines.

    Raises ``RuntimeError`` if pyinfra reports the host as failed.
    """
    hosts_data = [(vm.ip, {"ssh_user": vm.ssh_user, "ssh_key": ssh_key})]
    state = State(inventory=Inventory((hosts_data, {})), config=Config())
    connect_all(state)
    try:
        result = add_op(state, server.shell, name=op_name, commands=[command])
        run_ops(state)
    finally:
        disconnect_all(state)

    if state.failed_hosts:
        raise RuntimeError(f"'{op_name}' failed on {vm.name}")

    host = next(iter(result.keys()))
    return list(result[host].stdout_lines)


def _ssh_read_file(vm: NodeVm, ssh_key: str, path: str) -> str:
    """Raw one-shot SSH read of a remote file's contents (a single small probe,
    not a pyinfra op — mirrors ``prod_extend_unmanaged_volumes._post_resize_size_bytes``).
    """
    result = subprocess.run(
        ["ssh", "-i", ssh_key, *_SSH_OPTS, f"{vm.ssh_user}@{vm.ip}", f"cat {path}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to read {path} on {vm.name}: {result.stderr.strip()}"
        )
    return result.stdout


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _list_services(vm: NodeVm, ssh_key: str, console: Console) -> list[dict]:
    clear_known_hosts([vm.ip], console)
    try:
        stdout_lines = _run_single_host_op(
            vm, ssh_key, "List ant-node services", build_list_services_command()
        )
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)
    return parse_service_list(stdout_lines)


def cmd_prod_services_ls(args) -> None:
    """List every ant-node-* service on a host with active/enabled state and
    on-disk data size, to help decide which service to remove. Optionally
    restricted to a single --service-name."""
    console = Console()
    service_filter = getattr(args, "service_name", None)
    if service_filter:
        _require_service_name(service_filter, console)
    vm, ssh_key = _resolve_host(args.inventory_path, args.name, args.ssh_key_path, console)
    records = _list_services(vm, ssh_key, console)
    if service_filter:
        records = [r for r in records if r["service_name"] == service_filter]

    if not records:
        target = f" matching {service_filter}" if service_filter else ""
        console.print(f"[yellow]No ant-node-* services found on {vm.name}{target}.[/yellow]")
        return

    table = Table(title=f"ant-node services on {vm.name}")
    table.add_column("Service")
    table.add_column("Active")
    table.add_column("Enabled")
    table.add_column("Disk usage", justify="right")
    for r in records:
        table.add_row(
            r["service_name"], r["active"], r["enabled"], _format_bytes(r["size_bytes"])
        )
    console.print(table)


def _update_inventory(
    inventory_path: str, vm: NodeVm, service_name: str, *, added: bool, console: Console
) -> None:
    """Best-effort: the live SSH change has already succeeded by the time this
    runs, so an inventory-write failure is reported but never fails the
    command — the operator would just need to reconcile the file by hand.
    """
    try:
        update_service_in_inventory(inventory_path, vm.name, service_name, added=added)
    except (ValueError, OSError) as e:
        console.print(
            f"[bold yellow]Warning:[/bold yellow] {service_name} was "
            f"{'added on' if added else 'removed from'} {vm.name}, but the "
            f"inventory file could not be updated: {e}"
        )
        return
    console.print(f"[dim]Inventory updated: {inventory_path}[/dim]")


def cmd_prod_services_rm(args) -> None:
    """Stop, disable, and delete one ant-node service's unit + data/log dirs,
    freeing the disk space it was using. Never touches sibling services."""
    console = Console()
    _require_service_name(args.service_name, console)
    vm, ssh_key = _resolve_host(args.inventory_path, args.name, args.ssh_key_path, console)

    records = _list_services(vm, ssh_key, console)
    if not any(r["service_name"] == args.service_name for r in records):
        console.print(
            f"[bold red]Error:[/bold red] {args.service_name} not found on "
            f"{vm.name}. Run `prod services ls` to see what's there."
        )
        sys.exit(1)

    try:
        _run_single_host_op(
            vm,
            ssh_key,
            f"Remove {args.service_name}",
            build_remove_service_command(args.service_name),
        )
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    console.print(
        f"[bold green]✓[/bold green] Removed {args.service_name} from {vm.name} "
        "(unit, data dir, and log dir deleted)."
    )
    _update_inventory(args.inventory_path, vm, args.service_name, added=False, console=console)


def cmd_prod_services_reset_data(args) -> None:
    """Stop one ant-node service, wipe its data dir except node_identity.key,
    then start it again — so it re-bootstraps with the SAME peer ID (unchanged
    XOR range / record responsibility). Reclaims the record-store bytes while
    keeping the node's identity; never touches the unit, port, log dir, or any
    sibling service."""
    console = Console()
    _require_service_name(args.service_name, console)
    vm, ssh_key = _resolve_host(args.inventory_path, args.name, args.ssh_key_path, console)

    records = _list_services(vm, ssh_key, console)
    if not any(r["service_name"] == args.service_name for r in records):
        console.print(
            f"[bold red]Error:[/bold red] {args.service_name} not found on "
            f"{vm.name}. Run `prod services ls` to see what's there."
        )
        sys.exit(1)

    try:
        _run_single_host_op(
            vm,
            ssh_key,
            f"Reset data for {args.service_name}",
            build_reset_data_command(args.service_name),
        )
    except RuntimeError as e:
        console.print(
            f"[bold red]Error:[/bold red] {e} "
            "(the service may be left stopped, or its identity key was missing "
            "— check `prod services ls` and the host)."
        )
        sys.exit(1)

    console.print(
        f"[bold green]✓[/bold green] Reset {args.service_name} on {vm.name}: "
        "data wiped, identity (node_identity.key) retained, service restarted. "
        "It will re-bootstrap with the same peer ID."
    )


def cmd_prod_services_add(args) -> None:
    """Add a new ant-node service at --service-name, cloning its config
    (binary, evm network, NAT wrapping, log level, bootstrap peers, restart
    policy, backtrace env, upgrade channel, rewards address) from an existing
    running sibling service on the same host."""
    console = Console()
    _require_service_name(args.service_name, console)
    vm, ssh_key = _resolve_host(args.inventory_path, args.name, args.ssh_key_path, console)

    records = _list_services(vm, ssh_key, console)
    if any(r["service_name"] == args.service_name for r in records):
        console.print(
            f"[bold red]Error:[/bold red] {args.service_name} already exists "
            f"on {vm.name}."
        )
        sys.exit(1)

    active = [r for r in records if r["active"] == "active"]
    if not active:
        console.print(
            f"[bold red]Error:[/bold red] {vm.name} has no active ant-node "
            "service to clone config from — add needs at least one running "
            "sibling."
        )
        sys.exit(1)
    sibling = min(active, key=lambda r: r["index"])

    try:
        sibling_content = _ssh_read_file(
            vm, ssh_key, f"/etc/systemd/system/{sibling['service_name']}.service"
        )
        parsed = parse_node_unit(sibling_content)
    except (RuntimeError, ValueError) as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    new_index = parse_service_name(args.service_name)
    delta = new_index - sibling["index"]
    new_port = parsed["port"] + delta if parsed["port"] is not None else None

    root_dir = f"{MOUNT_POINT}/data/{args.service_name}"
    log_dir = f"{LOG_DIR}/{args.service_name}"

    exec_start = _build_node_exec_start(
        bootstrap_peers=parsed["bootstrap_peers"],
        root_dir=root_dir,
        log_dir=log_dir,
        port=new_port,
        log_level=parsed["log_level"],
        testnet=parsed["testnet"],
        upgrade_channel=parsed["upgrade_channel"],
        rewards_address=parsed["rewards_address"] or DEFAULT_REWARDS_ADDRESS,
        evm_network=parsed["evm_network"],
    )
    unit_content = _build_node_unit_file(
        args.service_name,
        exec_start,
        is_nat=parsed["is_nat"],
        restart_policy=parsed["restart_policy"],
        restart_sec=parsed["restart_sec"],
        limit_nofile=parsed["limit_nofile"],
        rust_backtrace=parsed["rust_backtrace"],
        coredumps=parsed["coredumps"],
        colorbt_show_hidden=parsed["colorbt_show_hidden"],
    )

    add_command = (
        f"mkdir -p {root_dir} {log_dir}\n"
        + build_write_unit_and_start_command(args.service_name, unit_content)
    )
    try:
        _run_single_host_op(vm, ssh_key, f"Add {args.service_name}", add_command)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    console.print(
        f"[bold green]✓[/bold green] Added and started {args.service_name} on "
        f"{vm.name} (cloned from {sibling['service_name']})."
    )
    _update_inventory(args.inventory_path, vm, args.service_name, added=True, console=console)
