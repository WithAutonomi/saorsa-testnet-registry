"""``saorsa-deploy migration destroy-source`` — terminate the AWS source VMs.

The AWS prod fleet is not in any saorsa-deploy Terraform state, so destruction
goes through boto3 directly. EBS root volumes auto-delete when their parent
instance terminates if ``DeleteOnTermination`` is true on the root device
mapping; we verify that before terminating, and fall back to explicit
``delete_volume`` for any volume that wouldn't be auto-cleaned.

Refuses unless every VM in the migration is in ``source_drained`` state. Use
``--force`` to override (e.g. to clean up after a partial drain).
"""

import sys
from collections import defaultdict
from datetime import datetime, timezone

import boto3
from rich.console import Console
from saorsa_db.models import MigrationStatus, MigrationVmStatus

from saorsa_deploy.migration_registry import (
    MigrationRegistryError,
    get_migration_repository,
)


def _check_root_delete_on_termination(
    ec2_client, instance_ids: list[str]
) -> tuple[set[str], list[str]]:
    """Return (instances_with_auto_delete_root, volumes_needing_explicit_delete).

    A volume needs explicit delete if it's attached to one of the target
    instances and its block-device mapping has DeleteOnTermination=False
    (or the mapping is missing — defensive).
    """
    auto_delete: set[str] = set()
    explicit_volumes: list[str] = []
    if not instance_ids:
        return auto_delete, explicit_volumes

    response = ec2_client.describe_instances(InstanceIds=instance_ids)
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            iid = instance["InstanceId"]
            mappings = instance.get("BlockDeviceMappings", [])
            instance_auto = True
            for mapping in mappings:
                ebs = mapping.get("Ebs") or {}
                if not ebs.get("DeleteOnTermination", False):
                    vol_id = ebs.get("VolumeId")
                    if vol_id:
                        explicit_volumes.append(vol_id)
                    instance_auto = False
            if instance_auto:
                auto_delete.add(iid)
    return auto_delete, explicit_volumes


def cmd_migration_destroy_source(args):
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

    if args.force:
        # Force: terminate any source instance that hasn't been destroyed yet,
        # regardless of where it is in the state machine. A VM still in
        # NODES_STARTED gets killed alongside SOURCE_DRAINED siblings.
        targets = [
            vm
            for vm in repo.list_vms(migration.id)
            if vm.status != MigrationVmStatus.SOURCE_DESTROYED
            and vm.source_provider_id
        ]
    else:
        leftover_states = [
            MigrationVmStatus.PLANNED,
            MigrationVmStatus.INFRA_PROVISIONED,
            MigrationVmStatus.NODES_PROVISIONED,
            MigrationVmStatus.NODES_STARTED,
        ]
        for state in leftover_states:
            stuck = repo.list_vms(migration.id, status=state)
            if stuck:
                console.print(
                    f"[bold red]Error:[/bold red] {len(stuck)} VM(s) still in "
                    f"'{state.value}'. Drain them first or pass --force."
                )
                sys.exit(1)
        targets = repo.list_vms(
            migration.id, status=MigrationVmStatus.SOURCE_DRAINED
        )

    if not targets:
        console.print(
            "[bold red]Error:[/bold red] No VMs left to destroy."
        )
        sys.exit(1)

    # Group by AWS region — boto3.ec2 is per-region.
    by_region: dict[str, list] = defaultdict(list)
    for vm in targets:
        if vm.source_provider != "aws":
            console.print(
                f"[bold red]Error:[/bold red] VM {vm.source_name} has "
                f"source_provider={vm.source_provider!r}; only 'aws' is "
                "supported by destroy-source."
            )
            sys.exit(1)
        by_region[vm.source_region].append(vm)

    repo.update_migration_status(migration.id, MigrationStatus.DESTROYING)

    failures: list[str] = []
    for region, vms in by_region.items():
        instance_ids = [vm.source_provider_id for vm in vms]
        console.print(
            f"[bold]{region}:[/bold] terminating "
            f"{len(instance_ids)} instance(s)..."
        )

        ec2 = boto3.client("ec2", region_name=region)

        try:
            _, explicit_volumes = _check_root_delete_on_termination(
                ec2, instance_ids
            )
        except Exception as e:
            console.print(
                f"[red]{region}: failed to inspect block device mappings: {e}"
                "[/red]"
            )
            failures.extend(vm.source_name for vm in vms)
            continue

        try:
            ec2.terminate_instances(InstanceIds=instance_ids)
        except Exception as e:
            console.print(
                f"[red]{region}: terminate_instances failed: {e}[/red]"
            )
            failures.extend(vm.source_name for vm in vms)
            continue

        # Wait for termination to complete (default 600s, 40 attempts × 15s).
        waiter = ec2.get_waiter("instance_terminated")
        try:
            waiter.wait(InstanceIds=instance_ids)
        except Exception as e:
            console.print(
                f"[red]{region}: waiter failed: {e}[/red]"
            )
            failures.extend(vm.source_name for vm in vms)
            continue

        # Explicit cleanup for any volumes whose mapping had
        # DeleteOnTermination=False.
        for vol_id in explicit_volumes:
            try:
                ec2.delete_volume(VolumeId=vol_id)
                console.print(f"  [dim]Deleted volume {vol_id}[/dim]")
            except Exception as e:
                console.print(
                    f"[yellow]Warning: failed to delete volume {vol_id}: {e}"
                    "[/yellow]"
                )

        now = datetime.now(timezone.utc)
        for vm in vms:
            repo.update_vm(
                vm.id,
                status=MigrationVmStatus.SOURCE_DESTROYED,
                source_destroyed_at=now,
            )
        console.print(
            f"[green]{region}: {len(instance_ids)} instance(s) terminated.[/green]"
        )

    if failures:
        console.print()
        console.print(
            f"[bold red]{len(failures)} VM(s) failed:[/bold red] "
            f"{', '.join(failures)}"
        )
        sys.exit(1)

    # Migration is fully destroyed only when every VM is SOURCE_DESTROYED.
    leftover = [
        vm
        for vm in repo.list_vms(migration.id)
        if vm.status != MigrationVmStatus.SOURCE_DESTROYED
    ]
    if not leftover:
        repo.update_migration_status(migration.id, MigrationStatus.COMPLETE)
        console.print()
        console.print(
            "[bold green]All source VMs destroyed. "
            "Migration status: complete.[/bold green]"
        )
        console.print(
            "[dim]Next: run `saorsa-deploy migration complete --name "
            f"{args.name}` to update the prod inventory file.[/dim]"
        )
