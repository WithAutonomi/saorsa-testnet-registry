"""``saorsa-deploy migration destroy-infra`` — tear down the migration's Hetzner targets.

For partial-failure recovery (and any other case where you need to throw away
a migration's destination VMs and try again). Walks every location that has
a Terraform state file for this migration, runs ``terraform destroy`` against
it, deletes the S3 state object, and resets the affected ``MigrationVm`` rows
back to ``planned`` (clearing target_*). The migration row itself goes back
to ``planning``.

Refuses to run if any VM is past ``nodes_started`` (i.e. anything that has
joined the network) — call ``migration drain`` first. ``--force`` overrides.
"""

import sys
from pathlib import Path

from rich.console import Console
from saorsa_db.models import MigrationStatus, MigrationVmStatus

from saorsa_deploy.cmd.migration_infra import (
    NAME_TEMPLATE,
    _default_inventory_path,
    _max_existing_index,
    _state_key,
    _workspace_dir,
)
from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.migration_registry import (
    MigrationRegistryError,
    get_migration_repository,
)
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    check_terraform_state_exists,
    delete_terraform_state,
)
from saorsa_deploy.terraform import TerraformRunConfig


def _resolve_locations_to_destroy(
    migration, parsed_filter: list[str] | None
) -> list[str]:
    """Pick which locations have Terraform state to destroy.

    When ``parsed_filter`` is provided, restrict to those; otherwise destroy
    every location that has S3 state for this migration. Each location may
    or may not have state — only existing ones are returned.
    """
    candidates: list[str]
    if parsed_filter is not None:
        candidates = parsed_filter
    elif migration.target_locations:
        candidates = list(migration.target_locations.keys())
    else:
        candidates = []

    return [
        loc
        for loc in candidates
        if check_terraform_state_exists(_state_key(migration.name, loc))
    ]


def cmd_migration_destroy_infra(args):
    console = Console()

    try:
        repo = get_migration_repository()
    except MigrationRegistryError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    migration = repo.get_migration_by_name(args.name)
    if migration is None:
        console.print(
            f"[bold red]Error:[/bold red] No migration named '{args.name}'."
        )
        sys.exit(1)

    if not args.force:
        forbidden_states = [
            MigrationVmStatus.NODES_STARTED,
            MigrationVmStatus.SOURCE_DRAINED,
            MigrationVmStatus.SOURCE_DESTROYED,
        ]
        for state in forbidden_states:
            stuck = repo.list_vms(migration.id, status=state)
            if stuck:
                console.print(
                    f"[bold red]Error:[/bold red] {len(stuck)} VM(s) are in "
                    f"'{state.value}'. Refusing to destroy infra past the "
                    "nodes-started boundary. Pass --force to override."
                )
                sys.exit(1)

    parsed_filter = None
    if args.locations:
        parsed_filter = [
            loc.strip() for loc in args.locations.split(",") if loc.strip()
        ]

    targets = _resolve_locations_to_destroy(migration, parsed_filter)
    if not targets:
        console.print(
            "[yellow]No Terraform state to destroy for this migration.[/yellow]"
        )
        return

    inventory_path = (
        Path(args.inventory_path).expanduser()
        if args.inventory_path
        else _default_inventory_path(migration.deployment_name)
    )

    resources_dir = get_resources_dir()
    tf_source = resources_dir / "hetzner"

    configs: list[TerraformRunConfig] = []
    for loc in targets:
        # Same vars as infra would have used. Terraform destroy needs them
        # to be able to refresh state before destroying.
        offset = _max_existing_index(
            inventory_path,
            migration.deployment_name,
            NAME_TEMPLATE,
            loc,
        )
        # Use planned-vm-count for this location from target_locations,
        # falling back to the count of VMs whose target_location matches.
        if migration.target_locations and loc in migration.target_locations:
            count = int(migration.target_locations[loc])
        else:
            count = sum(
                1
                for vm in repo.list_vms(migration.id)
                if vm.target_location == loc
            )
        if count <= 0:
            count = 1  # Defensive: Terraform refuses count=0 var

        configs.append(
            TerraformRunConfig(
                provider="hetzner",
                region=loc,
                tf_source_dir=tf_source,
                workspace_dir=_workspace_dir(migration.name, loc),
                state_key=_state_key(migration.name, loc),
                variables={
                    "name": migration.deployment_name,
                    "name_template": NAME_TEMPLATE,
                    "name_index_offset": str(offset),
                    "region": loc,
                    "vm_count": str(count),
                    "server_type": migration.target_server_type,
                    "attached_volume_size": str(migration.target_volume_size_gb),
                },
            )
        )

    console.print(
        f"[bold]Destroying Hetzner infrastructure for migration "
        f"'{migration.name}' in {len(configs)} location(s)...[/bold]"
    )
    for loc in targets:
        console.print(f"  {loc}")
    console.print()

    results = execute_terraform_runs(configs, action="destroy")

    failures: list[str] = []
    for cfg, result in zip(configs, results):
        if not result.success:
            failures.append(cfg.region)
            console.print(
                f"[bold red]{cfg.region}: destroy failed.[/bold red]"
            )
            continue
        try:
            delete_terraform_state(cfg.state_key)
        except Exception as e:
            console.print(
                f"[yellow]Warning: {cfg.region} destroyed but failed to "
                f"delete S3 state {cfg.state_key}: {e}[/yellow]"
            )

    if failures:
        console.print()
        console.print(
            f"[bold red]{len(failures)} location(s) failed to destroy:[/bold red] "
            f"{', '.join(failures)}"
        )
        sys.exit(1)

    # Reset MigrationVm rows for the destroyed locations.
    destroyed_locs = set(targets)
    reset_count = 0
    for vm in repo.list_vms(migration.id):
        if vm.target_location in destroyed_locs or vm.status == MigrationVmStatus.PLANNED:
            repo.update_vm(
                vm.id,
                status=MigrationVmStatus.PLANNED,
                target_name=None,
                target_provider=None,
                target_provider_id=None,
                target_location=None,
                target_ip=None,
                target_volume_id=None,
                infra_provisioned_at=None,
                nodes_provisioned_at=None,
                nodes_started_at=None,
            )
            reset_count += 1

    repo.update_migration_status(migration.id, MigrationStatus.PLANNING)

    console.print()
    console.print(
        f"[bold green]Destroyed {len(targets)} location(s); "
        f"reset {reset_count} VM(s) to 'planned'.[/bold green]"
    )
    console.print(
        f"[dim]Migration '{migration.name}' is back at 'planning'. "
        "Run `migration infra` again with adjusted --locations.[/dim]"
    )
