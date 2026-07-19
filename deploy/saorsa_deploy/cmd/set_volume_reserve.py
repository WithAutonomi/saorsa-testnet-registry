"""Lower the ext4 reserved-block percentage on every node VM's data volume in a
production inventory, sequentially, without stopping any ant-node service.

ext4's default 5% reserved-block allocation wastes many GB on a large node data
volume — the volume reports 100% full while ``df`` still shows headroom. This
command reads a ``generate-inventory`` inventory and, per node VM, SSHes in and
runs ``tune2fs -m <percent>`` on whichever device backs
``/mnt/ant-node-storage`` (autodetected via ``findmnt`` — raw ext4 or LVM
stripe). Root-disk hosts (Contabo, OVH 3-AZ) have no separate data volume and
are skipped so the OS root filesystem is never touched.

Future data volumes get the 1% reserve at format time (see
``provisioning/volume.py``); this command is the retrofit for already-running
fleets.

A failure on any VM stops the loop unless ``--keep-going`` is set.
"""

import os
import sys
from pathlib import Path

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operations import run_ops
from rich.console import Console

from saorsa_deploy.inventory import NodeVm, load_node_inventory
from saorsa_deploy.provisioning.set_reserved_blocks import add_set_reserved_blocks_op
from saorsa_deploy.ssh import clear_known_hosts


def _resolve_ssh_key(vm: NodeVm, override: str | None) -> str:
    raw = override or vm.ssh_key
    return str(Path(raw).expanduser())


def _run_in_vm(vm: NodeVm, ssh_key: str, reserve_percent: int) -> None:
    """Run the in-VM tune2fs op against a single host.

    Raises ``RuntimeError`` if Pyinfra reports the host as failed.
    """
    hosts_data = [(vm.ip, {"ssh_user": vm.ssh_user, "ssh_key": ssh_key})]
    inventory = Inventory((hosts_data, {}))
    pyinfra_state = State(inventory=inventory, config=Config())
    connect_all(pyinfra_state)
    try:
        add_set_reserved_blocks_op(pyinfra_state, reserve_percent=reserve_percent)
        run_ops(pyinfra_state)
    finally:
        disconnect_all(pyinfra_state)

    if pyinfra_state.failed_hosts:
        names = ", ".join(host.name for host in pyinfra_state.failed_hosts)
        raise RuntimeError(
            f"tune2fs failed on {names}. Inspect the host for the "
            "ANT_RESERVE marker."
        )


def cmd_set_volume_reserve(args):
    """Set the ext4 reserved-block percentage on every node VM's data volume."""
    console = Console()

    reserve_percent = int(args.reserve_percent)
    if not (0 <= reserve_percent <= 50):
        console.print(
            "[bold red]Error:[/bold red] --reserve-percent must be between 0 and 50"
        )
        sys.exit(1)

    inventory_path = Path(args.inventory_path).expanduser()
    if not inventory_path.is_file():
        console.print(
            f"[bold red]Error:[/bold red] inventory not found: {inventory_path}"
        )
        sys.exit(1)

    try:
        vms = load_node_inventory(inventory_path, provider_filter=args.provider)
    except (ValueError, OSError) as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    if args.limit_to:
        wanted = {n.strip() for n in args.limit_to.split(",") if n.strip()}
        vms = [vm for vm in vms if vm.name in wanted]
        if not vms:
            console.print(
                f"[bold red]Error:[/bold red] --limit-to matched no VMs "
                f"({sorted(wanted)})"
            )
            sys.exit(1)

    console.print(
        f"[bold]prod set-volume-reserve[/bold] — "
        f"{len(vms)} VM(s), reserve {reserve_percent}%"
    )
    if args.provider:
        console.print(f"  Provider filter: {args.provider}")
    if args.limit_to:
        console.print(f"  Limit to: {args.limit_to}")
    console.print(f"  Inventory: {inventory_path}")
    console.print()

    if not args.yes:
        try:
            input(f"Proceed with tune2fs on {len(vms)} VM(s)? [y/N] ")
        except EOFError:
            console.print("[bold red]Aborted.[/bold red]")
            sys.exit(1)

    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []

    for vm in vms:
        console.print(
            f"[bold cyan]→ {vm.name}[/bold cyan] "
            f"({vm.provider}/{vm.region}, {vm.ip})"
        )
        ssh_key = _resolve_ssh_key(vm, args.ssh_key_path)
        if not os.path.isfile(ssh_key):
            console.print(
                f"[bold red]Error:[/bold red] SSH key for {vm.name} does not "
                f"exist: {ssh_key}"
            )
            failed.append((vm.name, f"missing ssh key {ssh_key}"))
            if not args.keep_going:
                break
            console.print()
            continue

        try:
            clear_known_hosts([vm.ip], console)
            _run_in_vm(vm, ssh_key, reserve_percent)
            console.print(f"  [bold green]✓ {vm.name} OK[/bold green]")
            succeeded.append(vm.name)
        except Exception as e:
            console.print(f"[bold red]✗ {vm.name} FAILED:[/bold red] {e}")
            failed.append((vm.name, str(e)))
            if not args.keep_going:
                console.print(
                    "[yellow]Stopping (use --keep-going to continue past "
                    "failures).[/yellow]"
                )
                break
        console.print()

    console.print()
    console.print(f"[bold]Summary[/bold] — reserve {reserve_percent}%")
    console.print(
        f"  [green]OK:[/green] {len(succeeded)} ({', '.join(succeeded) or '-'})"
    )
    if failed:
        console.print(f"  [red]FAILED:[/red] {len(failed)}")
        for name, err in failed:
            console.print(f"    [red]- {name}: {err}[/red]")
        sys.exit(1)
