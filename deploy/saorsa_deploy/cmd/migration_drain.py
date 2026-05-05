"""``saorsa-deploy migration drain`` — stop ant-node services on AWS source VMs gradually.

Walks every ``nodes_started`` VM in turn and stops its 15 ant-node services
**one at a time**, with ``--interval-secs`` (default 60) between each stop.
The same interval applies across the VM boundary — there is no extra pause
between VMs because the per-service spacing already keeps the network's
neighbour-sync healing ahead of the loss rate.

Refuses to start until at least ``--soak-mins`` (default 120) have elapsed
since the *most recent* VM was started; ``--force`` overrides the soak check.

Each service stop SSHes to the source VM and runs
``sudo -n systemctl stop ant-node-N`` (passwordless sudo on Ubuntu / no-op as
root). A VM transitions to ``source_drained`` only once all 15 of its
services have been stopped successfully.
"""

import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from rich.console import Console
from saorsa_db.models import MigrationStatus, MigrationVmStatus

from saorsa_deploy.migration_registry import (
    MigrationRegistryError,
    get_migration_repository,
)


def _stop_one_service(
    vm,
    service_index: int,
    ssh_key_path: str,
    console: Console,
) -> bool:
    """Stop ``ant-node-{service_index}`` on the VM via SSH. Returns True on success."""
    service = f"ant-node-{service_index}"
    ssh_key = ssh_key_path or vm.source_ssh_key
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15",
        "-o", "LogLevel=ERROR",
        "-i", ssh_key,
        f"{vm.source_ssh_user}@{vm.source_ip}",
        f"sudo -n systemctl stop {service}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        console.print(
            f"  [red]{vm.source_name} {service}: stop failed "
            f"(rc={result.returncode})[/red]"
        )
        if result.stderr.strip():
            console.print(f"    [dim]{result.stderr.strip()}[/dim]")
        return False
    return True


def cmd_migration_drain(args):
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

    nodes_started = repo.list_vms(
        migration.id, status=MigrationVmStatus.NODES_STARTED
    )
    if not nodes_started:
        console.print(
            "[bold red]Error:[/bold red] No VMs in 'nodes_started' state. "
            "Either nothing to drain or `migration start-nodes` hasn't run."
        )
        sys.exit(1)

    # Soak check — newest nodes_started_at must be >= soak_mins ago.
    if not args.force:
        latest_started = max(
            (vm.nodes_started_at for vm in nodes_started if vm.nodes_started_at),
            default=None,
        )
        if latest_started is None:
            console.print(
                "[bold red]Error:[/bold red] No VM has nodes_started_at; "
                "drain refuses to run without timing data. Use --force to override."
            )
            sys.exit(1)
        soak_window = timedelta(minutes=args.soak_mins)
        elapsed = datetime.now(timezone.utc) - latest_started
        if elapsed < soak_window:
            remaining = soak_window - elapsed
            mins = int(remaining.total_seconds() // 60) + 1
            console.print(
                f"[bold red]Error:[/bold red] Soak window not yet elapsed. "
                f"Newest VM started {int(elapsed.total_seconds() // 60)} min "
                f"ago, need {args.soak_mins}. Wait {mins} more min, "
                "or pass --force."
            )
            sys.exit(1)

    if args.limit is not None and args.limit > 0:
        vms_to_drain = nodes_started[: args.limit]
    else:
        vms_to_drain = nodes_started

    if args.interval_secs <= 0:
        console.print("[bold red]Error:[/bold red] --interval-secs must be > 0")
        sys.exit(1)

    node_count = migration.node_count_per_vm
    total_services = len(vms_to_drain) * node_count
    total_minutes = (total_services * args.interval_secs) / 60.0
    console.print(
        f"[bold]Draining {len(vms_to_drain)} VM(s) "
        f"({total_services} services total) at {args.interval_secs}s "
        f"between each service stop — ~{total_minutes:.0f} min total.[/bold]"
    )
    console.print()

    repo.update_migration_status(migration.id, MigrationStatus.DRAINING)

    failed_vms: list[str] = []
    services_stopped = 0
    for vm in vms_to_drain:
        console.print(
            f"[bold]Draining {vm.source_name}[/bold] "
            f"([dim]{vm.source_ip}[/dim])..."
        )
        vm_failures = 0
        for i in range(1, node_count + 1):
            ok = _stop_one_service(vm, i, args.ssh_key_path, console)
            if ok:
                console.print(
                    f"  [green]{vm.source_name} ant-node-{i}: stopped[/green]"
                )
                services_stopped += 1
            else:
                vm_failures += 1
            # Sleep after every stop, including the last service of the last
            # VM — a small trailing wait does no harm and keeps the cadence
            # uniform if the operator pipes another command behind drain.
            if not (vm is vms_to_drain[-1] and i == node_count):
                time.sleep(args.interval_secs)

        if vm_failures == 0:
            now = datetime.now(timezone.utc)
            repo.update_vm(
                vm.id,
                status=MigrationVmStatus.SOURCE_DRAINED,
                source_drained_at=now,
            )
            console.print(
                f"[green]{vm.source_name}: all {node_count} services "
                "stopped → source_drained[/green]"
            )
        else:
            failed_vms.append(vm.source_name)
            console.print(
                f"[red]{vm.source_name}: {vm_failures}/{node_count} "
                "services failed to stop; VM left in 'nodes_started'[/red]"
            )
        console.print()

    if failed_vms:
        console.print(
            f"[bold red]{len(failed_vms)} VM(s) had partial drain failures: "
            f"{', '.join(failed_vms)}[/bold red]"
        )
        sys.exit(1)

    # If everything in the migration is now drained, transition migration → drained.
    remaining = repo.list_vms(
        migration.id, status=MigrationVmStatus.NODES_STARTED
    )
    if not remaining:
        repo.update_migration_status(migration.id, MigrationStatus.DRAINED)
        console.print(
            f"[bold green]All source VMs drained "
            f"({services_stopped} services stopped).[/bold green]"
        )
        console.print(
            "[dim]Next: run `saorsa-deploy migration destroy-source --name "
            f"{args.name}` to terminate the AWS instances.[/dim]"
        )
    else:
        console.print(
            f"[bold yellow]Drain partial: {len(remaining)} VM(s) still in "
            "'nodes_started'. Re-run drain (without --limit) to finish.[/bold yellow]"
        )
