"""``saorsa-deploy migration drain`` — stop ant-node services on AWS source VMs in waves.

Walks ``nodes_started`` VMs in waves of ``--wave-size`` (default 1), with
``--interval-mins`` (default 20) between waves to give the network's lazy
neighbour-sync healing one full cycle. Refuses to start until at least
``--soak-mins`` (default 120) have elapsed since the *most recent* VM was
started; ``--force`` overrides the soak check.

Each wave SSHes to the source VM(s) and runs ``systemctl stop ant-node-{1..N}``
in parallel via Pyinfra. Drained VMs transition to ``source_drained``. The
migration row goes to ``draining`` on the first wave and ``drained`` once
every VM has been drained.
"""

import sys
import time
from datetime import datetime, timedelta, timezone

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


def _stop_wave(
    wave_vms: list,
    node_count: int,
    ssh_key_path: str,
    console: Console,
) -> set[str]:
    """Stop ant-node services on the wave's source VMs. Returns failed IPs."""
    ips = [vm.source_ip for vm in wave_vms]
    clear_known_hosts(ips, console)

    # AWS prod VMs SSH in as 'ubuntu' (passwordless sudo); Hetzner is root.
    # `sudo -n` is a harmless no-op for root and uses passwordless sudo for
    # ubuntu, so it's safe to use unconditionally.
    service_list = " ".join(f"ant-node-{i + 1}" for i in range(node_count))
    stop_command = (
        f"sudo -n systemctl stop {service_list} && echo 'ANT_NODES:STOPPED'"
    )

    hosts_data = [
        (
            vm.source_ip,
            {
                "ssh_user": vm.source_ssh_user,
                "ssh_key": ssh_key_path or vm.source_ssh_key,
            },
        )
        for vm in wave_vms
    ]
    inventory = Inventory((hosts_data, {}))
    pyinfra_state = State(inventory=inventory, config=Config())
    connect_all(pyinfra_state)
    try:
        add_op(
            pyinfra_state,
            server.shell,
            name=f"Stop ant-node-1..{node_count}",
            commands=[stop_command],
        )
        run_ops(pyinfra_state)
    finally:
        disconnect_all(pyinfra_state)

    return {host.name for host in pyinfra_state.failed_hosts}


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
        wave_pool = nodes_started[: args.limit]
    else:
        wave_pool = nodes_started

    if args.wave_size <= 0:
        console.print("[bold red]Error:[/bold red] --wave-size must be >= 1")
        sys.exit(1)

    waves = [
        wave_pool[i : i + args.wave_size]
        for i in range(0, len(wave_pool), args.wave_size)
    ]

    console.print(
        f"[bold]Draining {len(wave_pool)} source VM(s) in {len(waves)} "
        f"wave(s) of {args.wave_size}, "
        f"{args.interval_mins} min between waves...[/bold]"
    )
    console.print(
        f"  {migration.node_count_per_vm} services per VM"
    )
    console.print()

    repo.update_migration_status(migration.id, MigrationStatus.DRAINING)

    total_failures: list[str] = []
    for i, wave in enumerate(waves):
        console.print(
            f"[bold]Wave {i + 1}/{len(waves)}:[/bold] "
            f"{', '.join(vm.source_name for vm in wave)}"
        )

        failed_ips = _stop_wave(
            wave,
            node_count=migration.node_count_per_vm,
            ssh_key_path=args.ssh_key_path,
            console=console,
        )

        now = datetime.now(timezone.utc)
        for vm in wave:
            if vm.source_ip in failed_ips:
                total_failures.append(vm.source_name)
                continue
            repo.update_vm(
                vm.id,
                status=MigrationVmStatus.SOURCE_DRAINED,
                source_drained_at=now,
            )

        if failed_ips:
            console.print(
                f"[red]Wave {i + 1} had {len(failed_ips)} failure(s).[/red]"
            )

        # Sleep between waves, except after the last one.
        if i < len(waves) - 1:
            console.print(
                f"[dim]Waiting {args.interval_mins} min before next wave...[/dim]"
            )
            time.sleep(args.interval_mins * 60)

    if total_failures:
        console.print()
        console.print(
            f"[bold red]{len(total_failures)} VM(s) failed to drain:[/bold red] "
            f"{', '.join(total_failures)}"
        )
        sys.exit(1)

    # If everything in the migration is now drained, transition migration → drained.
    remaining = repo.list_vms(
        migration.id, status=MigrationVmStatus.NODES_STARTED
    )
    if not remaining:
        repo.update_migration_status(migration.id, MigrationStatus.DRAINED)
        console.print()
        console.print("[bold green]All source VMs drained.[/bold green]")
        console.print(
            "[dim]Next: run `saorsa-deploy migration destroy-source --name "
            f"{args.name}` to terminate the AWS instances.[/dim]"
        )
    else:
        console.print()
        console.print(
            f"[bold yellow]Drain partial: {len(remaining)} VM(s) still in "
            "'nodes_started'. Re-run drain (without --limit) to finish.[/bold yellow]"
        )
