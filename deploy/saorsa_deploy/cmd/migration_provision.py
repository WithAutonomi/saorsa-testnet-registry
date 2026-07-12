"""``saorsa-deploy migration provision`` — install ant-node on the new VMs.

Reuses ``AntNodeProvisioner`` (mount volume → install binary → write systemd
units → enable, but don't start). Bootstrap peers come from the local prod
config (``~/.config/ant/bootstrap_peers.toml`` by default) rather than from
S3 deployment state, since the migration's target VMs join an existing prod
network whose bootstraps are static.
"""

import sys
from datetime import datetime, timezone

from rich.console import Console
from saorsa_db.models import MigrationStatus, MigrationVmStatus

from saorsa_deploy.cmd.provision_genesis import _resolve_binary_source
from saorsa_deploy.cmd.provision_telegraf import set_hostnames
from saorsa_deploy.migration_registry import (
    MigrationRegistryError,
    get_migration_repository,
)
from saorsa_deploy.prod_bootstraps import (
    ProdBootstrapsError,
    load_prod_bootstraps_from_file,
)
from saorsa_deploy.provisioning.node import AntNodeProvisioner
from saorsa_deploy.ssh import clear_known_hosts


def cmd_migration_provision(args):
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

    vms = repo.list_vms(migration.id, status=MigrationVmStatus.INFRA_PROVISIONED)
    if not vms:
        console.print(
            "[bold red]Error:[/bold red] No VMs in 'infra_provisioned' state. "
            "Run `migration infra` first, or check `migration status`."
        )
        sys.exit(1)

    try:
        bootstrap_peers = load_prod_bootstraps_from_file(
            args.bootstrap_peers_file
        )
    except ProdBootstrapsError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    binary_url, binary_is_archive = _resolve_binary_source(args, console)

    target_ips = [vm.target_ip for vm in vms]

    console.print(
        f"[bold]Provisioning {len(target_ips)} target VM(s) for migration "
        f"{migration.name}...[/bold]"
    )
    console.print(f"  Bootstrap peers: {', '.join(bootstrap_peers)}")
    console.print(
        f"  Nodes per VM: {migration.node_count_per_vm}"
        f"  | initial port: {args.port}"
    )
    console.print(f"  Rewards address: {args.rewards_address}")
    console.print(f"  SSH key: {args.ssh_key_path}")
    console.print()

    clear_known_hosts(target_ips, console)

    # Migrated VMs come up with the cloud provider's default hostname, which is
    # often lowercased (e.g. OVH turns the SBG5-1 instance name into the OS
    # hostname `ant-prod-01-ovh-sbg5-1`). Set each VM's hostname to its
    # fleet-convention target name so host-keyed telemetry — notably the InfluxDB
    # host tag, which uses the raw OS hostname — matches the rest of the fleet
    # (the ES forwarder force-uppercases, so it was unaffected either way).
    set_hostnames(
        [
            {
                "ip": vm.target_ip,
                "name": vm.target_name,
                "ssh_user": "root",
                "ssh_key": args.ssh_key_path,
            }
            for vm in vms
        ],
        console,
    )

    kwargs = {
        "host_ips": target_ips,
        "bootstrap_peers": bootstrap_peers,
        "ssh_key_path": args.ssh_key_path,
        "node_count": migration.node_count_per_vm,
        "initial_port": args.port,
        "log_level": args.log_level,
        "testnet": False,
        "console": console,
        "rewards_address": args.rewards_address,
        "nat_ips": None,
        # Match the existing prod fleet's units exactly (see prod fsn1
        # ant-node-1.service): no --evm-network (ant-node defaults to
        # arbitrum-one for prod), Restart=always with 10s back-off, and
        # LimitNOFILE=65536 to avoid fd exhaustion on a busy DHT node.
        "evm_network": None,
        "restart_policy": "always",
        "restart_sec": 10,
        "limit_nofile": 65536,
    }
    if binary_url:
        kwargs["binary_url"] = binary_url
        kwargs["binary_is_archive"] = binary_is_archive

    provisioner = AntNodeProvisioner(**kwargs)

    try:
        provisioner.execute()
    except Exception as e:
        console.print(f"[bold red]Provisioning failed:[/bold red] {e}")
        repo.update_migration_status(migration.id, MigrationStatus.FAILED)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    for vm in vms:
        repo.update_vm(
            vm.id,
            status=MigrationVmStatus.NODES_PROVISIONED,
            nodes_provisioned_at=now,
        )
    repo.update_migration_status(migration.id, MigrationStatus.NODES_PROVISIONED)

    console.print()
    console.print(
        f"[bold green]All {len(target_ips)} VM(s) provisioned. "
        "Services enabled but not started.[/bold green]"
    )
    console.print(
        "[dim]Next: run `saorsa-deploy migration start-nodes --name "
        f"{args.name}` to bring them online.[/dim]"
    )
