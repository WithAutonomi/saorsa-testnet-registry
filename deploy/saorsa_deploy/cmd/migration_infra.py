"""``saorsa-deploy migration infra`` — Terraform-up the destination VMs.

Creates one Terraform run per ``--locations`` entry. Each location gets its
own state key (``saorsa-deploy/migrations/{migration}-{loc}.tfstate``) so
partial failures can be recovered location-by-location. After a successful
run, the per-location Terraform outputs (``vm_ips``, ``vm_ids``,
``volume_ids``) are mapped onto the migration's ``planned`` VMs in id-order,
transitioning them to ``infra_provisioned``.

Dispatches to a provider-specific profile based on ``migration.target_provider``.
Hetzner and OVH are supported today.
"""

import json
import os
import re
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from rich.console import Console
from saorsa_db.models import MigrationStatus, MigrationVmStatus

from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.hetzner_availability import resolve_hetzner_regions
from saorsa_deploy.migration_registry import (
    MigrationRegistryError,
    get_migration_repository,
)
from saorsa_deploy.paths import state_dir
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import check_terraform_state_exists
from saorsa_deploy.terraform import TerraformRunConfig

# Contabo is provisioned via the REST API, not Terraform, so it isn't a
# ``_ProviderProfile``. Its migrated VMs are named ``<deployment>-cntb-<region>-N``.
_CONTABO = "contabo"
_CONTABO_NAME_TEMPLATE = "cntb"


@dataclass(frozen=True)
class _ProviderProfile:
    """Per-target-provider configuration for migration infra/destroy.

    ``server_type_var`` is the Terraform variable that receives the
    ``migration.target_server_type`` value (Hetzner calls this ``server_type``,
    OVH calls it ``flavor_name``).

    ``required_env_vars`` are checked before any work — the command exits if
    any are unset. ``extra_tf_vars`` returns provider-specific Terraform
    variables to merge on top of the shared set; it's called once per command
    invocation, after the env-var check.

    ``preflight`` is an optional capacity-check callback returning
    ``(resolved_locations, notes)``; ``None`` means no preflight (OVH).
    """

    name_template: str
    tf_dir: str
    server_type_var: str
    required_env_vars: tuple[str, ...]
    extra_tf_vars: Callable[[], dict[str, str]]
    preflight: Callable[[list[str], str], tuple[list[str], list[str]]] | None


def _hetzner_preflight(
    requested_locations: list[str], server_type: str
) -> tuple[list[str], list[str]]:
    token = os.environ["HCLOUD_TOKEN"]
    return resolve_hetzner_regions(
        preferred=requested_locations,
        server_type=server_type,
        count=len(requested_locations),
        token=token,
        fallback_pool=None,
    )


def _ovh_extra_tf_vars() -> dict[str, str]:
    project = os.environ.get("OVH_CLOUD_PROJECT_SERVICE")
    if not project:
        raise RuntimeError(
            "OVH_CLOUD_PROJECT_SERVICE is not set. Export it in your "
            ".env so the OVH Terraform manifest can target the right project."
        )
    return {"service_name": project}


_PROFILES: dict[str, _ProviderProfile] = {
    "hetzner": _ProviderProfile(
        name_template="hz",
        tf_dir="hetzner",
        server_type_var="server_type",
        required_env_vars=("HCLOUD_TOKEN",),
        extra_tf_vars=lambda: {},
        preflight=_hetzner_preflight,
    ),
    "ovh": _ProviderProfile(
        name_template="ovh",
        tf_dir="ovh",
        server_type_var="flavor_name",
        required_env_vars=(
            "OVH_APPLICATION_KEY",
            "OVH_APPLICATION_SECRET",
            "OVH_CONSUMER_KEY",
            "OVH_CLOUD_PROJECT_SERVICE",
        ),
        extra_tf_vars=_ovh_extra_tf_vars,
        # The OVH project's available regions are visible via the API but we
        # don't have a flavor-availability preflight to mirror Hetzner's; OVH
        # just returns a clear error from terraform apply if the flavor isn't
        # offered in a region.
        preflight=None,
    ),
}


def _default_inventory_path(deployment_name: str) -> Path:
    return Path("~/.autonomi/testnets").expanduser() / deployment_name / "inventory.json"


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
            raise ValueError(f"Invalid --locations entry {part!r}; expected '<region>=<count>'")
        loc, count = part.split("=", 1)
        loc = loc.strip()
        try:
            n = int(count.strip())
        except ValueError as e:
            raise ValueError(f"Invalid count in --locations entry {part!r}: {e}") from e
        if n <= 0:
            raise ValueError(f"Count in --locations entry {part!r} must be positive")
        out.append((loc, n))
    if not out:
        raise ValueError("--locations is empty")
    return out


def _state_key(migration_name: str, location: str) -> str:
    return f"saorsa-deploy/migrations/{migration_name}-{location}.tfstate"


def _workspace_dir(migration_name: str, location: str) -> Path:
    return state_dir() / "workspaces" / f"migration-{migration_name}-{location}"


def _contabo_secret_name(migration_name: str, location: str) -> str:
    """Deterministic Contabo ssh-secret name (idempotent create/lookup)."""
    return f"migration-{migration_name.lower()}-{location.lower()}"


def _wait_for_ssh_ready(
    ips,
    console,
    *,
    port: int = 22,
    timeout: int = 900,
    interval: int = 12,
    required_consecutive: int = 5,
    connect_timeout: int = 5,
) -> list[str]:
    """Block until every IP accepts TCP :``port`` on ``required_consecutive``
    consecutive checks; return the IPs that never became ready.

    A freshly-created Contabo VM reports ``running`` (its API status) before
    sshd is up, and its SSH can *flap* down→up→down during first boot — which
    silently broke the migration's hostname step and the Telegraf retrofit for a
    just-born VM. Requiring several consecutive successful connects means we
    only proceed once the VM has settled past that first-boot flap. Never
    raises: any still-unready host is returned + warned so the caller can decide
    (we warn rather than fail, since the VMs already exist and are recorded).
    """
    if not ips:
        return []
    console.print(
        f"[bold]Waiting for SSH (:{port}) to settle on {len(ips)} VM(s) "
        f"(need {required_consecutive} consecutive OK)...[/bold]"
    )
    consecutive = {ip: 0 for ip in ips}
    ready: set[str] = set()
    deadline = time.monotonic() + timeout
    while len(ready) < len(ips) and time.monotonic() < deadline:
        for ip in ips:
            if ip in ready:
                continue
            try:
                with socket.create_connection((ip, port), timeout=connect_timeout):
                    consecutive[ip] += 1
            except OSError:
                consecutive[ip] = 0
            if consecutive[ip] >= required_consecutive:
                ready.add(ip)
                console.print(f"  [green]{ip}: SSH ready[/green]")
        if len(ready) < len(ips):
            time.sleep(interval)
    not_ready = [ip for ip in ips if ip not in ready]
    if not_ready:
        console.print(
            f"[yellow]Warning: {len(not_ready)} VM(s) not SSH-ready after "
            f"{timeout}s: {', '.join(not_ready)}. `migration provision` may need "
            "a re-run for these hosts once they settle.[/yellow]"
        )
    return not_ready


def _infra_contabo(args, console, repo, migration, parsed, planned_vms) -> None:
    """Create/adopt Contabo target VMs via the REST API and record them.

    Mirrors the Terraform path's record-keeping (set target_locations, map
    results onto the ``planned`` VMs in --locations order, transition to
    ``infra_provisioned``) but provisions through ``ContaboClient`` instead of
    Terraform, and records no volume (Contabo is root-disk only). Idempotent:
    instances are adopted by their fleet-convention display name, so a re-run
    after a partial failure resumes rather than duplicating.
    """
    from saorsa_deploy.cmd.contabo_infra import _create_or_adopt
    from saorsa_deploy.contabo import require_contabo_env
    from saorsa_deploy.contabo_api import ContaboClient, ContaboError

    require_contabo_env(console)

    product = migration.target_server_type
    image_id = args.contabo_image_id
    ssh_key_path = args.contabo_ssh_public_key_path

    inventory_path = (
        Path(args.inventory_path).expanduser()
        if getattr(args, "inventory_path", None)
        else _default_inventory_path(migration.deployment_name)
    )
    offsets: dict[str, int] = {
        loc: _max_existing_index(
            inventory_path,
            migration.deployment_name,
            _CONTABO_NAME_TEMPLATE,
            loc,
        )
        for loc, _ in parsed
    }

    repo.set_target_locations(migration.id, {loc: n for loc, n in parsed})

    console.print(
        f"[bold]Provisioning {sum(n for _, n in parsed)} Contabo VM(s) "
        f"({product}) for migration {migration.name}...[/bold]"
    )
    for loc, count in parsed:
        first = offsets[loc] + 1
        console.print(f"  {loc}: {count} × {product} (-{loc}-{first}..-{offsets[loc] + count})")
    console.print()

    client = ContaboClient(console)
    now = datetime.now(timezone.utc)
    created_ips: list[str] = []
    idx = 0
    failures = 0
    for loc, count in parsed:
        slice_vms = planned_vms[idx : idx + count]
        idx += count
        try:
            secret_id = client.ensure_ssh_secret(
                _contabo_secret_name(migration.name, loc), ssh_key_path
            )
        except (ContaboError, OSError) as e:
            console.print(f"[bold red]{loc}: could not register ssh secret: {e}[/bold red]")
            failures += 1
            continue
        for i, vm in enumerate(slice_vms):
            target_name = (
                f"{migration.deployment_name}-{_CONTABO_NAME_TEMPLATE}-{loc}-{i + 1 + offsets[loc]}"
            )
            try:
                instance_id, ip = _create_or_adopt(
                    client,
                    console,
                    display_name=target_name,
                    product_id=product,
                    image_id=image_id,
                    region=loc,
                    ssh_secret_id=secret_id,
                )
            except ContaboError as e:
                console.print(f"[bold red]{target_name}: provisioning failed: {e}[/bold red]")
                failures += 1
                continue
            created_ips.append(str(ip))
            repo.update_vm(
                vm.id,
                target_name=target_name,
                target_provider=_CONTABO,
                target_provider_id=str(instance_id),
                target_location=loc,
                target_ip=str(ip),
                target_volume_id=None,
                status=MigrationVmStatus.INFRA_PROVISIONED,
                infra_provisioned_at=now,
            )

    if failures:
        console.print(
            f"[bold red]{failures} VM(s)/location(s) failed. Re-run "
            "`migration infra` to resume (existing instances are adopted).[/bold red]"
        )
        repo.update_migration_status(migration.id, MigrationStatus.FAILED)
        sys.exit(1)

    # A just-created Contabo VM reports "running" before sshd is up and can flap
    # during first boot; wait until SSH has settled so the subsequent
    # `migration provision` (hostname + ant-node) and the Telegraf retrofit
    # don't silently skip a not-yet-reachable host.
    _wait_for_ssh_ready(created_ips, console)

    repo.update_migration_status(migration.id, MigrationStatus.INFRA_PROVISIONED)
    console.print(
        f"[bold green]All Contabo VM(s) provisioned. "
        f"{len(planned_vms)} target VM(s) recorded.[/bold green]"
    )
    console.print(
        "[dim]Next: run `saorsa-deploy migration provision --name "
        f"{args.name} ...` to install ant-node on the new VMs.[/dim]"
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
        console.print(f"[bold red]Error:[/bold red] No migration named '{args.name}'.")
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

    # Contabo bypasses Terraform entirely (root-disk only, slow async REST
    # "orders" that blow the TF provider's 20-min timeout) — see contabo_api.py.
    # Provision it directly via the REST API and record onto the MigrationVm rows.
    if migration.target_provider == _CONTABO:
        _infra_contabo(args, console, repo, migration, parsed, planned_vms)
        return

    profile = _PROFILES.get(migration.target_provider)
    if profile is None:
        supported = ", ".join(sorted([*_PROFILES, _CONTABO]))
        console.print(
            f"[bold red]Error:[/bold red] migration target_provider is "
            f"{migration.target_provider!r}; supported: {supported}."
        )
        sys.exit(1)

    missing = [v for v in profile.required_env_vars if not os.environ.get(v)]
    if missing:
        console.print(
            f"[bold red]Error:[/bold red] required env var(s) not set for "
            f"{migration.target_provider!r}: {', '.join(missing)}"
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

    requested_locations = [loc for loc, _ in parsed]

    if profile.preflight is not None:
        console.print(
            f"[bold]Preflight: {migration.target_provider} capacity for "
            f"{migration.target_server_type} in "
            f"{', '.join(requested_locations)}...[/bold]"
        )
        try:
            resolved, notes = profile.preflight(requested_locations, migration.target_server_type)
        except RuntimeError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)
        for note in notes:
            console.print(f"[yellow]{note}[/yellow]")
        if set(resolved) != set(requested_locations):
            console.print(
                f"[bold red]Error:[/bold red] {migration.target_provider} "
                f"availability resolved to {resolved}, which differs from "
                f"requested {requested_locations}. Re-run with --locations "
                "matching available capacity."
            )
            sys.exit(1)
        console.print(
            f"[green]{migration.target_provider} preflight ok: {', '.join(resolved)}[/green]"
        )

    repo.set_target_locations(migration.id, {loc: n for loc, n in parsed})

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
            profile.name_template,
            loc,
        )

    try:
        extra_vars = profile.extra_tf_vars()
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    resources_dir = get_resources_dir()
    tf_source = resources_dir / profile.tf_dir

    configs = []
    for loc, count in parsed:
        variables = {
            "name": migration.deployment_name,
            "name_template": profile.name_template,
            "name_index_offset": str(offsets[loc]),
            "region": loc,
            "vm_count": str(count),
            profile.server_type_var: migration.target_server_type,
            "attached_volume_size": str(migration.target_volume_size_gb),
            **extra_vars,
        }
        configs.append(
            TerraformRunConfig(
                provider=migration.target_provider,
                region=loc,
                tf_source_dir=tf_source,
                workspace_dir=_workspace_dir(migration.name, loc),
                state_key=_state_key(migration.name, loc),
                variables=variables,
            )
        )

    console.print(
        f"[bold]Provisioning {len(configs)} {migration.target_provider} "
        f"location(s) for migration {migration.name}...[/bold]"
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
        console.print(f"  {loc}: {count} × {migration.target_server_type}{suffix}")
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
            console.print(f"[bold red]{loc} provisioning failed.[/bold red]")
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
                f"{migration.deployment_name}-{profile.name_template}-{loc}-{i + 1 + loc_offset}"
            )
            target_volume_id = str(vol_ids[i]) if i < len(vol_ids) else None
            repo.update_vm(
                vm.id,
                target_name=target_name,
                target_provider=migration.target_provider,
                target_provider_id=str(vm_ids[i]),
                target_location=loc,
                target_ip=str(ips[i]),
                target_volume_id=target_volume_id,
                status=MigrationVmStatus.INFRA_PROVISIONED,
                infra_provisioned_at=now,
            )

    if failures:
        console.print(f"[bold red]{failures}/{len(configs)} location(s) failed.[/bold red]")
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
