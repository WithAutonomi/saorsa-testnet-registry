"""``saorsa-deploy migration complete`` — rewrite the prod inventory file.

Removes the now-destroyed AWS VMs (matched by ``source_provider_id``) from
``inventory.json`` and appends the new Hetzner VMs in the same schema_version
3 shape. Refuses unless every migration VM is in ``source_destroyed`` state
(``--force`` overrides). Atomic: writes ``inventory.json.tmp`` then renames.
"""

import json
import os
import sys
from pathlib import Path

from rich.console import Console
from saorsa_db.models import MigrationStatus, MigrationVmStatus

from saorsa_deploy.migration_registry import (
    MigrationRegistryError,
    get_migration_repository,
)


def _default_inventory_path(deployment_name: str) -> Path:
    return Path("~/.autonomi/testnets").expanduser() / deployment_name / "inventory.json"


def _build_target_entry(vm, node_count: int) -> dict:
    """Build a schema_version 3 node_vms entry for a migrated target VM."""
    services = [f"ant-node-{i + 1}" for i in range(node_count)]
    return {
        "name": vm.target_name,
        "provider": vm.target_provider,
        "provider_id": vm.target_provider_id,
        "region": vm.target_location,
        "ip": vm.target_ip,
        "ssh_user": "root",
        "ssh_key": "~/.ssh/ant_deploy",
        "nodes": [{"service_name": s} for s in services],
        "services": services,
    }


def cmd_migration_complete(args):
    console = Console()

    try:
        repo = get_migration_repository()
    except MigrationRegistryError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    migration = repo.get_migration_by_name(args.name)
    if migration is None:
        console.print(f"[bold red]Error:[/bold red] No migration named '{args.name}'.")
        sys.exit(1)

    if not args.force:
        leftover_states = [
            MigrationVmStatus.PLANNED,
            MigrationVmStatus.INFRA_PROVISIONED,
            MigrationVmStatus.NODES_PROVISIONED,
            MigrationVmStatus.NODES_STARTED,
            MigrationVmStatus.SOURCE_DRAINED,
        ]
        for state in leftover_states:
            stuck = repo.list_vms(migration.id, status=state)
            if stuck:
                console.print(
                    f"[bold red]Error:[/bold red] {len(stuck)} VM(s) still in "
                    f"'{state.value}'. Finish destroy-source first or use --force."
                )
                sys.exit(1)

    destroyed = repo.list_vms(migration.id, status=MigrationVmStatus.SOURCE_DESTROYED)
    if not destroyed:
        console.print(
            "[bold red]Error:[/bold red] No VMs in 'source_destroyed' state. "
            "Nothing to splice into the inventory."
        )
        sys.exit(1)

    inventory_path = (
        Path(args.inventory_path).expanduser()
        if args.inventory_path
        else _default_inventory_path(migration.deployment_name)
    )
    if not inventory_path.exists():
        console.print(f"[bold red]Error:[/bold red] Inventory file not found: {inventory_path}")
        sys.exit(1)

    raw = json.loads(inventory_path.read_text())
    existing_vms = list(raw.get("node_vms") or [])

    drop_ids = {vm.source_provider_id for vm in destroyed}
    kept = [entry for entry in existing_vms if str(entry.get("provider_id")) not in drop_ids]
    removed_count = len(existing_vms) - len(kept)
    if removed_count != len(destroyed):
        console.print(
            f"[bold yellow]Warning:[/bold yellow] expected to remove "
            f"{len(destroyed)} entries from inventory but matched {removed_count}. "
            "Continuing — check inventory afterwards."
        )

    new_entries = [_build_target_entry(vm, migration.node_count_per_vm) for vm in destroyed]
    raw["node_vms"] = kept + new_entries

    tmp_path = inventory_path.with_suffix(inventory_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(raw, indent=2) + "\n")
    os.replace(tmp_path, inventory_path)

    repo.update_migration_status(migration.id, MigrationStatus.COMPLETE)

    console.print(f"[bold green]Inventory updated: {inventory_path}[/bold green]")
    console.print(
        f"  removed: {removed_count} source VM(s)  | added: {len(new_entries)} target VM(s)"
    )
    console.print(f"[bold green]Migration '{migration.name}' marked complete.[/bold green]")
