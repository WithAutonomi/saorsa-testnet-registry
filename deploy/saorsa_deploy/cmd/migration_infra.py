"""``saorsa-deploy migration infra`` — Terraform-up the destination Hetzner VMs.

Creates one Terraform run per ``--locations`` entry (fsn1=N,hel1=N,...). Each
location gets its own state key (``saorsa-deploy/migrations/{migration}-{loc}.tfstate``)
so partial failures can be recovered location-by-location. After a successful
run, the per-location Terraform outputs (``vm_ips``, ``vm_ids``, ``volume_ids``)
are mapped onto the migration's ``planned`` VMs in id-order, transitioning them
to ``infra_provisioned``.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from saorsa_db.models import MigrationStatus, MigrationVmStatus

from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.hetzner_availability import resolve_hetzner_regions
from saorsa_deploy.migration_registry import (
    MigrationRegistryError,
    get_migration_repository,
)
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import check_terraform_state_exists
from saorsa_deploy.terraform import TerraformRunConfig

NAME_TEMPLATE = "hz"


def _default_inventory_path(deployment_name: str) -> Path:
    return (
        Path("~/.autonomi/testnets").expanduser()
        / deployment_name
        / "inventory.json"
    )


def _max_existing_index(
    inventory_path: Path,
    deployment_name: str,
    name_template: str,
    location: str,
) -> int:
    """Return the highest <N> already used in ``{deployment}-{template}-{location}-<N>``.

    Returns 0 when no matching VM exists, so the caller can pass that as a
    ``name_index_offset`` to start counting at <N> + 1. Reads inventory plus
    any other VM names ``ls`` would surface from the inventory ``node_vms``
    array. If the inventory doesn't exist, returns 0.
    """
    if not inventory_path.exists():
        return 0
    raw = json.loads(inventory_path.read_text())
    node_vms = raw.get("node_vms") or []
    pattern = re.compile(
        rf"^{re.escape(deployment_name)}-{re.escape(name_template)}-"
        rf"{re.escape(location)}-(\d+)$"
    )
    max_idx = 0
    for entry in node_vms:
        name = entry.get("name") or ""
        match = pattern.match(name)
        if match:
            idx = int(match.group(1))
            if idx > max_idx:
                max_idx = idx
    return max_idx


def _parse_locations(raw: str) -> list[tuple[str, int]]:
    """Parse ``fsn1=3,hel1=3,nbg1=3`` into ordered ``[(loc, count), ...]``."""
    out: list[tuple[str, int]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(
                f"Invalid --locations entry {part!r}; expected '<region>=<count>'"
            )
        loc, count = part.split("=", 1)
        loc = loc.strip()
        try:
            n = int(count.strip())
        except ValueError as e:
            raise ValueError(
                f"Invalid count in --locations entry {part!r}: {e}"
            ) from e
        if n <= 0:
            raise ValueError(
                f"Count in --locations entry {part!r} must be positive"
            )
        out.append((loc, n))
    if not out:
        raise ValueError("--locations is empty")
    return out


def _state_key(migration_name: str, location: str) -> str:
    return f"saorsa-deploy/migrations/{migration_name}-{location}.tfstate"


def _workspace_dir(migration_name: str, location: str) -> Path:
    return (
        Path.cwd()
        / ".saorsa"
        / "workspaces"
        / f"migration-{migration_name}-{location}"
    )


def cmd_migration_infra(args):
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

    if migration.target_provider != "hetzner":
        console.print(
            f"[bold red]Error:[/bold red] migration target_provider is "
            f"{migration.target_provider!r}; only 'hetzner' is supported by "
            "this command."
        )
        sys.exit(1)

    try:
        parsed = _parse_locations(args.locations)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    requested_total = sum(n for _, n in parsed)
    planned_vms = repo.list_vms(migration.id, status=MigrationVmStatus.PLANNED)
    if requested_total != len(planned_vms):
        console.print(
            f"[bold red]Error:[/bold red] --locations sum is {requested_total} "
            f"but {len(planned_vms)} VM(s) remain in 'planned' status. "
            "Adjust --locations to match the remaining count."
        )
        sys.exit(1)

    # Refuse to overwrite existing per-location Terraform state. Recovery from
    # partial runs uses different --locations on the rerun.
    for loc, _ in parsed:
        key = _state_key(migration.name, loc)
        if check_terraform_state_exists(key):
            console.print(
                f"[bold red]Error:[/bold red] Terraform state already exists "
                f"for {migration.name}/{loc} ({key}). Adjust --locations to "
                "skip this location, or destroy the existing state first."
            )
            sys.exit(1)

    token = os.environ.get("HCLOUD_TOKEN")
    if not token:
        console.print(
            "[bold red]Error:[/bold red] HCLOUD_TOKEN is not set; required "
            "for Hetzner availability preflight."
        )
        sys.exit(1)

    requested_locations = [loc for loc, _ in parsed]
    console.print(
        f"[bold]Preflight: Hetzner capacity for {migration.target_server_type} "
        f"in {', '.join(requested_locations)}...[/bold]"
    )
    try:
        resolved, notes = resolve_hetzner_regions(
            preferred=requested_locations,
            server_type=migration.target_server_type,
            count=len(requested_locations),
            token=token,
            fallback_pool=None,
        )
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    for note in notes:
        console.print(f"[yellow]{note}[/yellow]")
    if set(resolved) != set(requested_locations):
        console.print(
            "[bold red]Error:[/bold red] Hetzner availability resolved to "
            f"{resolved}, which differs from requested {requested_locations}. "
            "Re-run with --locations matching available capacity."
        )
        sys.exit(1)
    console.print(
        f"[green]Hetzner preflight ok: {', '.join(resolved)}[/green]"
    )

    repo.set_target_locations(
        migration.id, {loc: n for loc, n in parsed}
    )

    inventory_path = (
        Path(args.inventory_path).expanduser()
        if getattr(args, "inventory_path", None)
        else _default_inventory_path(migration.deployment_name)
    )
    offsets: dict[str, int] = {}
    for loc, _ in parsed:
        offsets[loc] = _max_existing_index(
            inventory_path,
            migration.deployment_name,
            NAME_TEMPLATE,
            loc,
        )

    resources_dir = get_resources_dir()
    tf_source = resources_dir / "hetzner"

    configs = []
    for loc, count in parsed:
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
                    "name_index_offset": str(offsets[loc]),
                    "region": loc,
                    "vm_count": str(count),
                    "server_type": migration.target_server_type,
                    "attached_volume_size": str(migration.target_volume_size_gb),
                },
            )
        )

    console.print(
        f"[bold]Provisioning {len(configs)} Hetzner location(s) for migration "
        f"{migration.name}...[/bold]"
    )
    for loc, count in parsed:
        offset = offsets[loc]
        first = offset + 1
        last = offset + count
        suffix = (
            f" (existing prod max index {offset} → naming -{loc}-{first}..-{last})"
            if offset > 0
            else f" (-{loc}-{first}..-{last})"
        )
        console.print(
            f"  {loc}: {count} × {migration.target_server_type}{suffix}"
        )
    console.print()

    results = execute_terraform_runs(configs)

    # Map results back onto planned VMs, in --locations order, planned-id order.
    offset = 0
    failures = 0
    for (loc, count), result in zip(parsed, results):
        slice_vms = planned_vms[offset : offset + count]
        offset += count
        if not result.success:
            failures += 1
            console.print(
                f"[bold red]{loc} provisioning failed.[/bold red]"
            )
            continue

        ips = result.outputs.get("vm_ips") or []
        vm_ids = result.outputs.get("vm_ids") or []
        vol_ids = result.outputs.get("volume_ids") or []
        if len(ips) != count or len(vm_ids) != count:
            console.print(
                f"[bold red]Error:[/bold red] {loc} returned "
                f"{len(ips)} IPs and {len(vm_ids)} ids; expected {count}. "
                "Aborting record update for this location; re-run after "
                "investigating Terraform state."
            )
            failures += 1
            continue

        now = datetime.now(timezone.utc)
        loc_offset = offsets[loc]
        for i, vm in enumerate(slice_vms):
            target_name = (
                f"{migration.deployment_name}-{NAME_TEMPLATE}-{loc}-"
                f"{i + 1 + loc_offset}"
            )
            target_volume_id = (
                str(vol_ids[i]) if i < len(vol_ids) else None
            )
            repo.update_vm(
                vm.id,
                target_name=target_name,
                target_provider="hetzner",
                target_provider_id=str(vm_ids[i]),
                target_location=loc,
                target_ip=str(ips[i]),
                target_volume_id=target_volume_id,
                status=MigrationVmStatus.INFRA_PROVISIONED,
                infra_provisioned_at=now,
            )

    if failures:
        console.print(
            f"[bold red]{failures}/{len(configs)} location(s) failed.[/bold red]"
        )
        repo.update_migration_status(migration.id, MigrationStatus.FAILED)
        sys.exit(1)

    repo.update_migration_status(migration.id, MigrationStatus.INFRA_PROVISIONED)
    console.print(
        f"[bold green]All {len(configs)} location(s) provisioned. "
        f"{len(planned_vms)} target VM(s) recorded.[/bold green]"
    )
    console.print(
        "[dim]Next: run `saorsa-deploy migration provision --name "
        f"{args.name} ...` to install ant-node on the new VMs.[/dim]"
    )
