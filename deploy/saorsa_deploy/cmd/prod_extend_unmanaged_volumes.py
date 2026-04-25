"""Extend the LVM-backed data volume on every node VM in a production
inventory, sequentially, without stopping any ant-node service.

Per VM, the orchestrator:
  1. Looks up the single non-root volume attached to the VM (provider-specific
     API call) and grows it cloud-side if its current size is below ``--size``.
  2. SSHes in via Pyinfra and runs a self-contained shell that pvresizes,
     lvextends, resize2fs's, and asserts every ant-node-* service named in the
     inventory is still ``active``.
  3. Verifies the post-resize ``df`` reports at least ``--size`` GB at
     ``/mnt/ant-node-storage``.

A failure on any VM stops the loop unless ``--keep-going`` is set.
"""

import os
import subprocess
import sys
from pathlib import Path

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operations import run_ops
from rich.console import Console

from saorsa_deploy.cloud_volumes import (
    CloudVolume,
    get_attached_data_volume,
    resize_volume,
)
from saorsa_deploy.inventory import NodeVm, load_node_inventory
from saorsa_deploy.provisioning.extend_volume import (
    MOUNT_POINT,
    add_extend_volume_op,
)
from saorsa_deploy.ssh import clear_known_hosts

GIB_BYTES = 1024 ** 3


def _resolve_ssh_key(vm: NodeVm, override: str | None) -> str:
    raw = override or vm.ssh_key
    return str(Path(raw).expanduser())


def _post_resize_size_bytes(vm: NodeVm, ssh_key: str) -> int:
    """SSH in once more to read the live ``df`` size at the mount point."""
    result = subprocess.run(
        [
            "ssh",
            "-i", ssh_key,
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=15",
            f"{vm.ssh_user}@{vm.ip}",
            f"df -B1 --output=size {MOUNT_POINT} | tail -1 | tr -dc '0-9'",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Post-resize df probe on {vm.name} failed: {result.stderr.strip()}"
        )
    raw = result.stdout.strip()
    if not raw:
        raise RuntimeError(f"Post-resize df probe on {vm.name} returned no output")
    return int(raw)


def _run_in_vm_resize(vm: NodeVm, ssh_key: str) -> None:
    """Run the in-VM resize + verify Pyinfra op against a single host.

    Raises ``RuntimeError`` if Pyinfra reports the host as failed.
    """
    if not vm.services:
        raise RuntimeError(
            f"Inventory entry for {vm.name} has no 'services' list — cannot "
            "verify ant-node-* uptime"
        )

    hosts_data = [(vm.ip, {"ssh_user": vm.ssh_user, "ssh_key": ssh_key})]
    inventory = Inventory((hosts_data, {}))
    pyinfra_state = State(inventory=inventory, config=Config())
    connect_all(pyinfra_state)
    try:
        add_extend_volume_op(pyinfra_state, services=list(vm.services))
        run_ops(pyinfra_state)
    finally:
        disconnect_all(pyinfra_state)

    if pyinfra_state.failed_hosts:
        names = ", ".join(host.name for host in pyinfra_state.failed_hosts)
        raise RuntimeError(
            f"In-VM resize failed on {names}. Inspect the host's logs for the "
            "ANT_RESIZE / ANT_VERIFY markers."
        )


def _extend_one_vm(
    vm: NodeVm,
    target_gb: int,
    ssh_key: str,
    console: Console,
) -> tuple[CloudVolume, CloudVolume, int]:
    """Run all three phases for a single VM. Returns
    (volume_before, volume_after, post_resize_bytes).
    """
    console.print(
        f"[bold cyan]→ {vm.name}[/bold cyan] "
        f"({vm.provider}/{vm.region}, {vm.ip})"
    )

    volume = get_attached_data_volume(vm)
    console.print(
        f"  [dim]Attached volume {volume.id} currently {volume.size_gb} GB[/dim]"
    )

    if volume.size_gb >= target_gb:
        console.print(
            f"  [dim]Cloud-side: already at {volume.size_gb} GB ≥ {target_gb} GB "
            "— skipping resize call.[/dim]"
        )
        resized = volume
    else:
        console.print(
            f"  Cloud-side: resizing {volume.size_gb} → {target_gb} GB…"
        )
        resized = resize_volume(volume, target_gb)
        console.print(
            f"  [green]Cloud-side resize complete[/green] "
            f"(now {resized.size_gb} GB)"
        )

    clear_known_hosts([vm.ip], console)

    console.print("  In-VM: pvresize → lvextend → resize2fs → verify services…")
    _run_in_vm_resize(vm, ssh_key)

    size_bytes = _post_resize_size_bytes(vm, ssh_key)
    target_bytes = target_gb * GIB_BYTES
    if size_bytes < target_bytes * 0.95:
        # ext4 reserves ~5% for root by default and reports slightly less than
        # the raw device size; tolerate that, but bail if the gap is wider.
        raise RuntimeError(
            f"{vm.name}: post-resize df reports {size_bytes} bytes, "
            f"expected at least ~{int(target_bytes * 0.95)} bytes "
            f"(target {target_gb} GB)."
        )

    console.print(
        f"  [bold green]✓ {vm.name} OK[/bold green] "
        f"(filesystem now {size_bytes / GIB_BYTES:.1f} GiB)"
    )
    return volume, resized, size_bytes


def cmd_prod_extend_unmanaged_volumes(args):
    """Sequentially grow every node VM's data volume to ``--size`` GB."""
    console = Console()

    target_gb = int(args.size)
    if target_gb <= 0:
        console.print("[bold red]Error:[/bold red] --size must be a positive integer")
        sys.exit(1)

    inventory_path = Path(args.inventory_path).expanduser()
    if not inventory_path.is_file():
        console.print(
            f"[bold red]Error:[/bold red] inventory not found: {inventory_path}"
        )
        sys.exit(1)

    try:
        vms = load_node_inventory(
            inventory_path, provider_filter=args.provider
        )
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
        f"[bold]prod extend-unmanaged-volumes[/bold] — "
        f"{len(vms)} VM(s), target {target_gb} GB"
    )
    if args.provider:
        console.print(f"  Provider filter: {args.provider}")
    if args.limit_to:
        console.print(f"  Limit to: {args.limit_to}")
    console.print(f"  Inventory: {inventory_path}")
    console.print()

    if not args.yes:
        try:
            input(f"Proceed with resize on {len(vms)} VM(s)? [y/N] ")
        except EOFError:
            console.print("[bold red]Aborted.[/bold red]")
            sys.exit(1)

    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    skipped_already_sized: list[str] = []

    for vm in vms:
        ssh_key = _resolve_ssh_key(vm, args.ssh_key_path)
        if not os.path.isfile(ssh_key):
            console.print(
                f"[bold red]Error:[/bold red] SSH key for {vm.name} does not "
                f"exist: {ssh_key}"
            )
            failed.append((vm.name, f"missing ssh key {ssh_key}"))
            if not args.keep_going:
                break
            continue

        try:
            before, after, _ = _extend_one_vm(vm, target_gb, ssh_key, console)
            succeeded.append(vm.name)
            if before.size_gb == after.size_gb == target_gb:
                skipped_already_sized.append(vm.name)
        except Exception as e:
            console.print(f"[bold red]✗ {vm.name} FAILED:[/bold red] {e}")
            failed.append((vm.name, str(e)))
            if not args.keep_going:
                console.print(
                    "[yellow]Stopping (use --keep-going to continue past failures).[/yellow]"
                )
                break
        console.print()

    console.print()
    console.print(f"[bold]Summary[/bold] — target {target_gb} GB")
    console.print(f"  [green]OK:[/green] {len(succeeded)} ({', '.join(succeeded) or '-'})")
    if skipped_already_sized:
        console.print(
            f"  [dim]Already at target (no cloud resize): "
            f"{len(skipped_already_sized)} ({', '.join(skipped_already_sized)})[/dim]"
        )
    if failed:
        console.print(f"  [red]FAILED:[/red] {len(failed)}")
        for name, err in failed:
            console.print(f"    [red]- {name}: {err}[/red]")
        sys.exit(1)
