"""``saorsa-deploy migration start-nodes`` — systemctl start the new VMs.

Walks every ``nodes_provisioned`` VM in the migration and starts all of its
``ant-node-{1..N}`` services in parallel via Pyinfra. Transitions VMs to
``nodes_started`` and the migration to ``soaking``.
"""

import sys
from datetime import datetime, timezone

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console
from saorsa_db.models import MigrationStatus, MigrationVmStatus

from saorsa_deploy.migration_registry import (
    MigrationRegistryError,
    get_migration_repository,
)
from saorsa_deploy.ssh import clear_known_hosts


def cmd_migration_start_nodes(args):
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

    vms = repo.list_vms(migration.id, status=MigrationVmStatus.NODES_PROVISIONED)
    if not vms:
        console.print(
            "[bold red]Error:[/bold red] No VMs in 'nodes_provisioned' state. "
            "Run `migration provision` first."
        )
        sys.exit(1)

    target_ips = [vm.target_ip for vm in vms]
    node_count = migration.node_count_per_vm

    console.print(
        f"[bold]Starting {node_count} ant-node service(s) on {len(target_ips)} VM(s)...[/bold]"
    )

    clear_known_hosts(target_ips, console)

    service_list = " ".join(f"ant-node-{i + 1}" for i in range(node_count))
    start_command = (
        f"systemctl start {service_list} && "
        f"systemctl is-active --quiet {service_list} && "
        f"echo 'ANT_NODES:STARTED'"
    )

    hosts_data = [(ip, {"ssh_user": "root", "ssh_key": args.ssh_key_path}) for ip in target_ips]
    inventory = Inventory((hosts_data, {}))
    pyinfra_state = State(inventory=inventory, config=Config())
    connect_all(pyinfra_state)

    try:
        add_op(
            pyinfra_state,
            server.shell,
            name=f"Start ant-node-1..{node_count}",
            commands=[start_command],
        )
        run_ops(pyinfra_state)
    finally:
        disconnect_all(pyinfra_state)

    failed = pyinfra_state.failed_hosts
    failed_ips = {host.name for host in failed}

    now = datetime.now(timezone.utc)
    started_count = 0
    for vm in vms:
        if vm.target_ip in failed_ips:
            continue
        repo.update_vm(
            vm.id,
            status=MigrationVmStatus.NODES_STARTED,
            nodes_started_at=now,
        )
        started_count += 1

    if failed:
        console.print()
        console.print(f"[bold red]Failed to start nodes on {len(failed)} host(s):[/bold red]")
        for host in failed:
            console.print(f"  [red]{host.name}[/red]")
        # Don't transition migration status to SOAKING if there are failures —
        # leave it where it was so the user can retry.
        sys.exit(1)

    repo.update_migration_status(migration.id, MigrationStatus.SOAKING)

    console.print()
    console.print(
        f"[bold green]Started {node_count} services on {started_count} host(s).[/bold green]"
    )
    console.print(
        "[dim]Migration is now soaking. Wait at least 2 hours before "
        f"`saorsa-deploy migration drain --name {args.name}`.[/dim]"
    )
