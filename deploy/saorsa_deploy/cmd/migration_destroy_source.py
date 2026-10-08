"""``saorsa-deploy migration destroy-source`` — terminate the source VMs.

Supports three source providers:

* **aws** — the AWS prod fleet is not in any saorsa-deploy Terraform state, so
  destruction goes through boto3 directly. EBS root volumes auto-delete when
  their parent instance terminates if ``DeleteOnTermination`` is true on the
  root device mapping; we verify that before terminating, and fall back to
  explicit ``delete_volume`` for any volume that wouldn't be auto-cleaned.
* **digitalocean** — droplets are deleted via the DO API by droplet ID. DO does
  **not** delete a droplet's attached volumes when the droplet is destroyed, so
  we look up each droplet's volume(s) first and delete them explicitly
  afterwards. If a droplet was already removed by a previous partial run, its
  data volume is found by the ``<name>-data`` naming convention so it is never
  left orphaned.
* **vultr** — instances are deleted via the Vultr API by instance ID. Like DO,
  Vultr keeps a deleted instance's block storage, so we capture each instance's
  attached block(s) first and delete them explicitly afterwards. If the instance
  was already removed by a previous partial run, its data block is found by the
  ``<name>-data`` label convention so it is never left orphaned.

Refuses unless every VM in the migration is in ``source_drained`` state. Use
``--force`` to override (e.g. to clean up after a partial drain).
"""

import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import boto3
import requests
from rich.console import Console
from saorsa_db.models import MigrationStatus, MigrationVmStatus

from saorsa_deploy.migration_registry import (
    MigrationRegistryError,
    get_migration_repository,
)

SUPPORTED_SOURCE_PROVIDERS = ("aws", "digitalocean", "vultr")

DO_API_BASE = "https://api.digitalocean.com/v2"
DO_REQUEST_TIMEOUT = 30
# Deleting a droplet detaches its volumes asynchronously; a volume delete can
# briefly fail ("still attached") in that window, so retry a few times.
DO_VOLUME_DELETE_ATTEMPTS = 6
DO_VOLUME_DELETE_BACKOFF_SECS = 5

VULTR_API_BASE = "https://api.vultr.com/v2"
VULTR_REQUEST_TIMEOUT = 30
# Deleting an instance detaches its block storage ASYNCHRONOUSLY and Vultr
# refuses to delete a block while it still reads as attached ("Block storage
# must be detached before removing"). The detach can take a few minutes, so we
# poll the block until it reports no attachment before deleting — nudging an
# explicit detach if it stalls. 30 × 10s = up to 5 min per block.
VULTR_BLOCK_DELETE_ATTEMPTS = 30
VULTR_BLOCK_DELETE_BACKOFF_SECS = 10


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


def _destroy_aws(by_region: dict, repo, console: Console) -> list[str]:
    """Terminate AWS source instances per region; return failed source names.

    Marks each successfully-terminated VM ``SOURCE_DESTROYED``.
    """
    failures: list[str] = []
    for region, vms in by_region.items():
        instance_ids = [vm.source_provider_id for vm in vms]
        console.print(f"[bold]{region}:[/bold] terminating {len(instance_ids)} instance(s)...")

        ec2 = boto3.client("ec2", region_name=region)

        try:
            _, explicit_volumes = _check_root_delete_on_termination(ec2, instance_ids)
        except Exception as e:
            console.print(f"[red]{region}: failed to inspect block device mappings: {e}[/red]")
            failures.extend(vm.source_name for vm in vms)
            continue

        try:
            ec2.terminate_instances(InstanceIds=instance_ids)
        except Exception as e:
            console.print(f"[red]{region}: terminate_instances failed: {e}[/red]")
            failures.extend(vm.source_name for vm in vms)
            continue

        # Wait for termination to complete (default 600s, 40 attempts × 15s).
        waiter = ec2.get_waiter("instance_terminated")
        try:
            waiter.wait(InstanceIds=instance_ids)
        except Exception as e:
            console.print(f"[red]{region}: waiter failed: {e}[/red]")
            failures.extend(vm.source_name for vm in vms)
            continue

        # Explicit cleanup for any volumes whose mapping had
        # DeleteOnTermination=False.
        for vol_id in explicit_volumes:
            try:
                ec2.delete_volume(VolumeId=vol_id)
                console.print(f"  [dim]Deleted volume {vol_id}[/dim]")
            except Exception as e:
                console.print(f"[yellow]Warning: failed to delete volume {vol_id}: {e}[/yellow]")

        now = datetime.now(timezone.utc)
        for vm in vms:
            repo.update_vm(
                vm.id,
                status=MigrationVmStatus.SOURCE_DESTROYED,
                source_destroyed_at=now,
            )
        console.print(f"[green]{region}: {len(instance_ids)} instance(s) terminated.[/green]")
    return failures


def _do_headers() -> dict:
    token = os.environ.get("DO_TOKEN")
    if not token:
        raise RuntimeError("DO_TOKEN environment variable is not set")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _do_check(resp: requests.Response, context: str) -> None:
    """Raise RuntimeError with the response body when a DO call failed."""
    if resp.ok:
        return
    raise RuntimeError(
        f"{context} → HTTP {resp.status_code} {resp.reason}: {resp.text.strip() or '<empty body>'}"
    )


def _do_volume_ids_by_name(name: str, region: str) -> list[str]:
    resp = requests.get(
        f"{DO_API_BASE}/volumes",
        headers=_do_headers(),
        params={"name": name, "region": region},
        timeout=DO_REQUEST_TIMEOUT,
    )
    _do_check(resp, f"DO GET volumes?name={name}")
    return [v["id"] for v in (resp.json().get("volumes") or [])]


def _do_volume_ids_for_vm(vm) -> list[str]:
    """Volume IDs to clean up for a DO source VM.

    Prefers the live droplet's attachment list; if the droplet is already gone
    (a prior run deleted it), falls back to the ``<source_name>-data`` naming
    convention so a half-deleted VM's data volume is still found and removed.
    """
    resp = requests.get(
        f"{DO_API_BASE}/droplets/{vm.source_provider_id}",
        headers=_do_headers(),
        timeout=DO_REQUEST_TIMEOUT,
    )
    if resp.status_code == 404:
        return _do_volume_ids_by_name(f"{vm.source_name}-data", vm.source_region)
    _do_check(resp, f"DO GET droplets/{vm.source_provider_id}")
    return (resp.json().get("droplet") or {}).get("volume_ids") or []


def _do_delete_droplet(droplet_id: str) -> None:
    resp = requests.delete(
        f"{DO_API_BASE}/droplets/{droplet_id}",
        headers=_do_headers(),
        timeout=DO_REQUEST_TIMEOUT,
    )
    if resp.status_code == 404:  # already gone — idempotent
        return
    _do_check(resp, f"DO DELETE droplets/{droplet_id}")


def _do_delete_volume(volume_id: str) -> None:
    last_error = None
    for attempt in range(DO_VOLUME_DELETE_ATTEMPTS):
        resp = requests.delete(
            f"{DO_API_BASE}/volumes/{volume_id}",
            headers=_do_headers(),
            timeout=DO_REQUEST_TIMEOUT,
        )
        if resp.status_code in (204, 404):  # deleted, or already gone
            return
        last_error = f"HTTP {resp.status_code} {resp.reason}: {resp.text.strip() or '<empty body>'}"
        # A 422 here usually means the droplet detach hasn't settled yet; wait
        # and retry rather than orphaning the volume.
        if attempt < DO_VOLUME_DELETE_ATTEMPTS - 1:
            time.sleep(DO_VOLUME_DELETE_BACKOFF_SECS)
    raise RuntimeError(
        f"DO DELETE volumes/{volume_id} failed after "
        f"{DO_VOLUME_DELETE_ATTEMPTS} attempts → {last_error}"
    )


def _destroy_digitalocean(vms: list, repo, console: Console) -> list[str]:
    """Delete DO source droplets and their attached volumes.

    Returns the source names that failed. Marks each fully-destroyed VM
    (droplet **and** volumes gone) ``SOURCE_DESTROYED``.
    """
    failures: list[str] = []
    for vm in vms:
        console.print(
            f"[bold]{vm.source_region}:[/bold] destroying DO droplet "
            f"{vm.source_provider_id} ({vm.source_name})..."
        )

        try:
            volume_ids = _do_volume_ids_for_vm(vm)
        except Exception as e:
            console.print(f"[red]{vm.source_name}: volume lookup failed: {e}[/red]")
            failures.append(vm.source_name)
            continue

        try:
            _do_delete_droplet(vm.source_provider_id)
        except Exception as e:
            console.print(f"[red]{vm.source_name}: droplet delete failed: {e}[/red]")
            failures.append(vm.source_name)
            continue

        # Droplet deletion detaches but does NOT delete volumes — remove them
        # explicitly so no orphaned data volumes are left behind.
        volume_failed = False
        for vid in volume_ids:
            try:
                _do_delete_volume(vid)
                console.print(f"  [dim]Deleted volume {vid}[/dim]")
            except Exception as e:
                console.print(
                    f"[bold red]{vm.source_name}: volume {vid} NOT deleted "
                    f"(orphaned — remove manually):[/bold red] {e}"
                )
                volume_failed = True
        if volume_failed:
            failures.append(vm.source_name)
            continue

        repo.update_vm(
            vm.id,
            status=MigrationVmStatus.SOURCE_DESTROYED,
            source_destroyed_at=datetime.now(timezone.utc),
        )
        console.print(
            f"[green]{vm.source_region}: droplet {vm.source_provider_id} + "
            f"{len(volume_ids)} volume(s) destroyed.[/green]"
        )
    return failures


def _vultr_headers() -> dict:
    token = os.environ.get("VULTR_API_KEY")
    if not token:
        raise RuntimeError("VULTR_API_KEY environment variable is not set")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _vultr_check(resp: requests.Response, context: str) -> None:
    """Raise RuntimeError with the response body when a Vultr call failed."""
    if resp.ok:
        return
    raise RuntimeError(
        f"{context} → HTTP {resp.status_code} {resp.reason}: {resp.text.strip() or '<empty body>'}"
    )


def _vultr_list_blocks() -> list[dict]:
    """Return every block-storage volume on the account (cursor-paginated)."""
    blocks: list[dict] = []
    cursor: str | None = None
    while True:
        params = {"per_page": 500}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(
            f"{VULTR_API_BASE}/blocks",
            headers=_vultr_headers(),
            params=params,
            timeout=VULTR_REQUEST_TIMEOUT,
        )
        _vultr_check(resp, "Vultr GET blocks")
        body = resp.json()
        blocks.extend(body.get("blocks") or [])
        cursor = ((body.get("meta") or {}).get("links") or {}).get("next")
        if not cursor:
            return blocks


def _vultr_block_ids_for_vm(vm) -> list[str]:
    """Block IDs to clean up for a Vultr source VM.

    Prefers the live instance's attachment list; if the instance is already gone
    (a prior run deleted it, so the block is now detached), falls back to the
    ``<source_name>-data`` label convention so a half-deleted VM's data block is
    still found and removed.
    """
    blocks = _vultr_list_blocks()
    attached = [b["id"] for b in blocks if b.get("attached_to_instance") == vm.source_provider_id]
    if attached:
        return attached
    label = f"{vm.source_name}-data"
    return [b["id"] for b in blocks if b.get("label") == label]


def _vultr_delete_instance(instance_id: str) -> None:
    resp = requests.delete(
        f"{VULTR_API_BASE}/instances/{instance_id}",
        headers=_vultr_headers(),
        timeout=VULTR_REQUEST_TIMEOUT,
    )
    if resp.status_code == 404:  # already gone — idempotent
        return
    _vultr_check(resp, f"Vultr DELETE instances/{instance_id}")


def _vultr_detach_block(block_id: str) -> None:
    """Best-effort detach; ignore errors (already detached / instance gone)."""
    try:
        requests.post(
            f"{VULTR_API_BASE}/blocks/{block_id}/detach",
            headers=_vultr_headers(),
            timeout=VULTR_REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        pass


def _vultr_delete_block(block_id: str) -> None:
    """Delete a block, waiting for the instance-deletion detach to settle first.

    Vultr detaches a deleted instance's block asynchronously and rejects a
    delete while the block still reads as attached. So poll the block until it
    reports no attachment (``attached_to_instance`` empty) — nudging one explicit
    detach if it stalls — then delete. A 404 at any point means it's already gone.
    """
    last_error = "block never reported detached"
    nudged = False
    for attempt in range(VULTR_BLOCK_DELETE_ATTEMPTS):
        get = requests.get(
            f"{VULTR_API_BASE}/blocks/{block_id}",
            headers=_vultr_headers(),
            timeout=VULTR_REQUEST_TIMEOUT,
        )
        if get.status_code == 404:  # already gone
            return
        _vultr_check(get, f"Vultr GET blocks/{block_id}")
        attached = (get.json().get("block") or {}).get("attached_to_instance")
        if attached:
            # Still attached: nudge one explicit detach, then keep polling for
            # the asynchronous detach to complete.
            if not nudged:
                _vultr_detach_block(block_id)
                nudged = True
            last_error = f"still attached to {attached}"
        else:
            delete = requests.delete(
                f"{VULTR_API_BASE}/blocks/{block_id}",
                headers=_vultr_headers(),
                timeout=VULTR_REQUEST_TIMEOUT,
            )
            if delete.status_code in (204, 404):  # deleted, or already gone
                return
            last_error = (
                f"HTTP {delete.status_code} {delete.reason}: "
                f"{delete.text.strip() or '<empty body>'}"
            )
        if attempt < VULTR_BLOCK_DELETE_ATTEMPTS - 1:
            time.sleep(VULTR_BLOCK_DELETE_BACKOFF_SECS)
    raise RuntimeError(
        f"Vultr DELETE blocks/{block_id} failed after "
        f"{VULTR_BLOCK_DELETE_ATTEMPTS} attempts → {last_error}"
    )


def _destroy_vultr(vms: list, repo, console: Console) -> list[str]:
    """Delete Vultr source instances and their attached block storage.

    Returns the source names that failed. Marks each fully-destroyed VM
    (instance **and** block gone) ``SOURCE_DESTROYED``.
    """
    failures: list[str] = []
    for vm in vms:
        console.print(
            f"[bold]{vm.source_region}:[/bold] destroying Vultr instance "
            f"{vm.source_provider_id} ({vm.source_name})..."
        )

        try:
            block_ids = _vultr_block_ids_for_vm(vm)
        except Exception as e:
            console.print(f"[red]{vm.source_name}: block lookup failed: {e}[/red]")
            failures.append(vm.source_name)
            continue

        try:
            _vultr_delete_instance(vm.source_provider_id)
        except Exception as e:
            console.print(f"[red]{vm.source_name}: instance delete failed: {e}[/red]")
            failures.append(vm.source_name)
            continue

        # Instance deletion detaches but does NOT delete block storage — remove
        # it explicitly so no orphaned data blocks are left behind.
        block_failed = False
        for bid in block_ids:
            try:
                _vultr_delete_block(bid)
                console.print(f"  [dim]Deleted block {bid}[/dim]")
            except Exception as e:
                console.print(
                    f"[bold red]{vm.source_name}: block {bid} NOT deleted "
                    f"(orphaned — remove manually):[/bold red] {e}"
                )
                block_failed = True
        if block_failed:
            failures.append(vm.source_name)
            continue

        repo.update_vm(
            vm.id,
            status=MigrationVmStatus.SOURCE_DESTROYED,
            source_destroyed_at=datetime.now(timezone.utc),
        )
        console.print(
            f"[green]{vm.source_region}: instance {vm.source_provider_id} + "
            f"{len(block_ids)} block(s) destroyed.[/green]"
        )
    return failures


def cmd_migration_destroy_source(args):
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

    if args.force:
        # Force: terminate any source instance that hasn't been destroyed yet,
        # regardless of where it is in the state machine. A VM still in
        # NODES_STARTED gets killed alongside SOURCE_DRAINED siblings.
        targets = [
            vm
            for vm in repo.list_vms(migration.id)
            if vm.status != MigrationVmStatus.SOURCE_DESTROYED and vm.source_provider_id
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
        targets = repo.list_vms(migration.id, status=MigrationVmStatus.SOURCE_DRAINED)

    if not targets:
        console.print("[bold red]Error:[/bold red] No VMs left to destroy.")
        sys.exit(1)

    unsupported = sorted({vm.source_provider for vm in targets} - set(SUPPORTED_SOURCE_PROVIDERS))
    if unsupported:
        console.print(
            f"[bold red]Error:[/bold red] unsupported source_provider(s): "
            f"{', '.join(unsupported)}. Supported: "
            f"{', '.join(SUPPORTED_SOURCE_PROVIDERS)}."
        )
        sys.exit(1)

    repo.update_migration_status(migration.id, MigrationStatus.DESTROYING)

    aws_by_region: dict[str, list] = defaultdict(list)
    do_vms: list = []
    vultr_vms: list = []
    for vm in targets:
        if vm.source_provider == "aws":
            aws_by_region[vm.source_region].append(vm)
        elif vm.source_provider == "digitalocean":
            do_vms.append(vm)
        elif vm.source_provider == "vultr":
            vultr_vms.append(vm)

    failures: list[str] = []
    if aws_by_region:
        failures += _destroy_aws(aws_by_region, repo, console)
    if do_vms:
        failures += _destroy_digitalocean(do_vms, repo, console)
    if vultr_vms:
        failures += _destroy_vultr(vultr_vms, repo, console)

    if failures:
        console.print()
        console.print(f"[bold red]{len(failures)} VM(s) failed:[/bold red] {', '.join(failures)}")
        sys.exit(1)

    # Migration is fully destroyed only when every VM is SOURCE_DESTROYED.
    leftover = [
        vm for vm in repo.list_vms(migration.id) if vm.status != MigrationVmStatus.SOURCE_DESTROYED
    ]
    if not leftover:
        repo.update_migration_status(migration.id, MigrationStatus.COMPLETE)
        console.print()
        console.print(
            "[bold green]All source VMs destroyed. Migration status: complete.[/bold green]"
        )
        console.print(
            "[dim]Next: run `saorsa-deploy migration complete --name "
            f"{args.name}` to update the prod inventory file.[/dim]"
        )
