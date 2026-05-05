"""``saorsa-deploy migration list`` — list every migration in the DB."""

import sys

from rich.console import Console
from rich.table import Table

from saorsa_deploy.migration_registry import (
    MigrationRegistryError,
    get_migration_repository,
)


def cmd_migration_list(args):
    console = Console()

    try:
        repo = get_migration_repository()
    except MigrationRegistryError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    migrations = repo.list_migrations()
    if not migrations:
        console.print("[dim]No migrations recorded.[/dim]")
        return

    table = Table(show_header=True, title="Migrations")
    table.add_column("Name")
    table.add_column("Deployment")
    table.add_column("Source")
    table.add_column("Target")
    table.add_column("VMs")
    table.add_column("Status")
    table.add_column("Created")
    for m in migrations:
        table.add_row(
            m.name,
            m.deployment_name,
            f"{m.source_provider}/{m.source_region}",
            f"{m.target_provider} {m.target_server_type}",
            str(len(m.vms)),
            m.status.value,
            m.created_at.strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)
