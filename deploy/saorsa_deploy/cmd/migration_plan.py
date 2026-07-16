"""``saorsa-deploy migration plan`` — record source VMs in the migration DB.

The plan command is purely a record-keeping step: it reads the production
inventory, filters by source provider+region, and inserts a Migration row
plus one MigrationVm per source VM. Target placement is decided later by
``migration infra``, so target_* columns stay null at this point.
"""

import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from saorsa_deploy.inventory import load_node_inventory
from saorsa_deploy.migration_registry import (
    MigrationRegistryError,
    get_migration_repository,
)


def _default_inventory_path(deployment_name: str) -> Path:
    return Path("~/.autonomi/testnets").expanduser() / deployment_name / "inventory.json"


def cmd_migration_plan(args):
    """Create a migration record + per-VM rows from a prod inventory."""
    console = Console()

    try:
        repo = get_migration_repository()
    except MigrationRegistryError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    if repo.get_migration_by_name(args.name) is not None:
        console.print(
            f"[bold red]Error:[/bold red] A migration named '{args.name}' "
            "already exists. Pick a different --name or destroy the existing "
            "one first."
        )
        sys.exit(1)

    # Hetzner/OVH targets attach a data volume; Contabo is root-disk only and
    # ignores --target-volume-size. Require a positive size for the volume
    # providers so a forgotten flag fails here rather than at `migration infra`.
    if args.target_provider != "contabo" and args.target_volume_size <= 0:
        console.print(
            f"[bold red]Error:[/bold red] --target-volume-size must be > 0 for "
            f"target provider '{args.target_provider}'."
        )
        sys.exit(1)

    inventory_path = (
        Path(args.inventory_path).expanduser()
        if args.inventory_path
        else _default_inventory_path(args.deployment_name)
    )

    try:
        all_vms = load_node_inventory(inventory_path)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    matching = [
        vm
        for vm in all_vms
        if vm.provider == args.source_provider and vm.region == args.source_region
    ]
    if not matching:
        console.print(
            f"[bold red]Error:[/bold red] No VMs in {inventory_path} match "
            f"provider={args.source_provider} region={args.source_region}."
        )
        sys.exit(1)

    migration = repo.create_migration(
        name=args.name,
        deployment_name=args.deployment_name,
        source_provider=args.source_provider,
        source_region=args.source_region,
        target_provider=args.target_provider,
        target_server_type=args.target_server_type,
        target_volume_size_gb=args.target_volume_size,
        node_count_per_vm=args.node_count_per_vm,
    )

    vm_records = [
        {
            "source_name": vm.name,
            "source_provider": vm.provider,
            "source_provider_id": vm.provider_id,
            "source_region": vm.region,
            "source_ip": vm.ip,
            "source_ssh_user": vm.ssh_user,
            "source_ssh_key": vm.ssh_key,
            "source_services": list(vm.services),
        }
        for vm in matching
    ]
    repo.add_vms(migration.id, vm_records)

    console.print(
        f"[bold green]Created migration '{args.name}' "
        f"(id={migration.id}) with {len(matching)} source VM(s).[/bold green]"
    )

    table = Table(title=f"Migration plan: {args.name}", show_header=True)
    table.add_column("Source VM")
    table.add_column("Provider ID")
    table.add_column("Region")
    table.add_column("IP")
    table.add_column("Services")
    for vm in matching:
        table.add_row(
            vm.name,
            vm.provider_id,
            vm.region,
            vm.ip,
            str(len(vm.services)),
        )
    console.print(table)

    console.print()
    console.print(
        f"[bold]Target spec:[/bold] {args.target_provider} "
        f"{args.target_server_type}, {args.target_volume_size} GB volume, "
        f"{args.node_count_per_vm} nodes/VM"
    )
    console.print(
        "[dim]Next: run `saorsa-deploy migration infra --name "
        f"{args.name} --locations <loc>=<count>,...` to provision targets.[/dim]"
    )
