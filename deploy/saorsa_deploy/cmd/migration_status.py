"""``saorsa-deploy migration status`` — show progress for one migration."""

import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

from saorsa_deploy.migration_registry import (
    MigrationRegistryError,
    get_migration_repository,
)


def _humanize_age(then: datetime | None) -> str:
    if then is None:
        return "—"
    delta = datetime.now(timezone.utc) - then
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    minutes = minutes % 60
    if hours < 24:
        return f"{hours}h{minutes}m ago" if minutes else f"{hours}h ago"
    days = hours // 24
    hours = hours % 24
    return f"{days}d{hours}h ago" if hours else f"{days}d ago"


def _last_status_at(vm) -> datetime | None:
    """Return the most recent ``*_at`` timestamp for the VM's current status."""
    candidates = [
        vm.source_destroyed_at,
        vm.source_drained_at,
        vm.nodes_started_at,
        vm.nodes_provisioned_at,
        vm.infra_provisioned_at,
        vm.created_at,
    ]
    for ts in candidates:
        if ts is not None:
            return ts
    return None


def cmd_migration_status(args):
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

    vms = repo.list_vms(migration.id)

    console.print(f"[bold]Migration:[/bold] {migration.name} ([dim]id={migration.id}[/dim])")
    console.print(
        f"  deployment: {migration.deployment_name}  status: [bold]{migration.status.value}[/bold]"
    )
    console.print(
        f"  source: {migration.source_provider}/{migration.source_region}"
        f"  →  target: {migration.target_provider} "
        f"{migration.target_server_type} ({migration.target_volume_size_gb} GB)"
    )
    if migration.target_locations:
        locs = ", ".join(f"{k}={v}" for k, v in migration.target_locations.items())
        console.print(f"  locations: {locs}")
    if migration.soak_completed_at is not None:
        console.print(f"  soak completed: {migration.soak_completed_at.isoformat()}")
    console.print()

    state_counts: dict[str, int] = {}
    for vm in vms:
        state_counts[vm.status.value] = state_counts.get(vm.status.value, 0) + 1
    summary = "  ".join(f"{k}={v}" for k, v in sorted(state_counts.items()))
    console.print(f"[dim]VMs by status:[/dim] {summary}")
    console.print()

    table = Table(show_header=True, title=None)
    table.add_column("Source")
    table.add_column("Source IP")
    table.add_column("→")
    table.add_column("Target")
    table.add_column("Target IP")
    table.add_column("Status")
    table.add_column("Age")
    for vm in vms:
        table.add_row(
            vm.source_name,
            vm.source_ip,
            "→",
            vm.target_name or "—",
            vm.target_ip or "—",
            vm.status.value,
            _humanize_age(_last_status_at(vm)),
        )
    console.print(table)
