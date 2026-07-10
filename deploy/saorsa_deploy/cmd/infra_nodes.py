import os
import sys
from dataclasses import replace
from pathlib import Path

from rich.console import Console

from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.hetzner_availability import resolve_hetzner_regions
from saorsa_deploy.providers import PROVIDERS, ProviderSpec, resolve_regions
from saorsa_deploy.registry import (
    record_deploy_args_update,
    record_event,
    record_status,
)
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    check_terraform_state_exists,
    delete_terraform_state,
    load_deployment_state,
    update_deployment_state,
)
from saorsa_deploy.terraform import TerraformRunConfig
from saorsa_deploy.vultr_availability import resolve_vultr_regions

# OVH 3-AZ regions place instances in a named availability zone; the ovh-3az
# module round-robins VMs across a region's zones (via Nova). Keys are the
# uppercase region names as they appear in a manifest; values are the lowercase
# OpenStack AZ names.
OVH_3AZ_REGION_ZONES = {
    "EU-WEST-PAR": ["eu-west-par-a", "eu-west-par-b", "eu-west-par-c"],
    "EU-SOUTH-MIL": ["eu-south-mil-a", "eu-south-mil-b", "eu-south-mil-c"],
}


def _ovh_az_tfvar(zones: list[str]) -> str:
    """Render an HCL list literal for a Terraform list(string) ``-var`` value."""
    return "[" + ",".join(f'"{z}"' for z in zones) + "]"


# Internal provider name -> the CLI arg prefix for its per-provider volume flag.
_VOLUME_ARG_PREFIX = {
    "digitalocean": "do",
    "hetzner": "hetzner",
    "vultr": "vultr",
    "ovh": "ovh",
}


def _provider_volume_size(args, provider_name: str) -> int:
    """Attached-volume size for a provider: its per-provider override if set,
    else the global ``--attached-volume-size``. Not called for ``ovh_3az``
    (no block storage)."""
    prefix = _VOLUME_ARG_PREFIX[provider_name]
    override = getattr(args, f"{prefix}_attached_volume_size", None)
    return override if override is not None else args.attached_volume_size


def _parse_regions_list(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    parts = [r.strip() for r in raw.split(",") if r.strip()]
    return parts or None


def _build_provider_specs(args) -> list[ProviderSpec]:
    """Build provider specs from per-provider CLI flags.

    When a ``--<provider>-regions-list`` flag is supplied it overrides both
    the default region ordering and the corresponding count flag — the list
    length becomes the region count.
    """
    specs = []
    if args.do_vm_count > 0:
        override = _parse_regions_list(getattr(args, "do_regions_list", None))
        count = len(override) if override else args.do_regions
        specs.append(
            ProviderSpec("digitalocean", args.do_vm_count, count, override)
        )
    if args.hetzner_vm_count > 0:
        override = _parse_regions_list(getattr(args, "hetzner_regions_list", None))
        count = len(override) if override else args.hetzner_regions
        specs.append(
            ProviderSpec("hetzner", args.hetzner_vm_count, count, override)
        )
    if args.vultr_vm_count > 0:
        override = _parse_regions_list(getattr(args, "vultr_regions_list", None))
        count = len(override) if override else args.vultr_regions
        specs.append(
            ProviderSpec("vultr", args.vultr_vm_count, count, override)
        )
    if getattr(args, "ovh_vm_count", 0) > 0:
        override = _parse_regions_list(getattr(args, "ovh_regions_list", None))
        count = len(override) if override else args.ovh_regions
        specs.append(
            ProviderSpec("ovh", args.ovh_vm_count, count, override)
        )
    if getattr(args, "ovh_3az_vm_count", 0) > 0:
        override = _parse_regions_list(getattr(args, "ovh_3az_regions_list", None))
        count = len(override) if override else getattr(args, "ovh_3az_regions", 1)
        specs.append(
            ProviderSpec("ovh_3az", args.ovh_3az_vm_count, count, override)
        )
    return specs


def _build_provider_state(
    provider_specs: list[ProviderSpec],
    do_droplet_size: str,
    hetzner_server_type: str,
    vultr_plan: str,
    ovh_flavor: str,
    ovh_3az_flavor: str = "b3-16",
) -> dict:
    providers_state = {}
    for spec in provider_specs:
        entry = {"vm_count": spec.vm_count, "regions": spec.region_count}
        if spec.name == "digitalocean":
            entry["droplet_size"] = do_droplet_size
        elif spec.name == "hetzner":
            entry["server_type"] = hetzner_server_type
        elif spec.name == "vultr":
            entry["plan"] = vultr_plan
        elif spec.name == "ovh":
            entry["flavor_name"] = ovh_flavor
        elif spec.name == "ovh_3az":
            entry["flavor_name"] = ovh_3az_flavor
        providers_state[spec.name] = entry
    return providers_state


# Env vars the OVH + OpenStack Terraform providers need. The ovh module mints
# its own project-scoped OpenStack user for volume attach, so only the OVH API
# credentials and the Public Cloud project id are required here. Mirrors the
# migration flow's OVH profile (cmd/migration_infra.py).
OVH_REQUIRED_ENV_VARS = (
    "OVH_APPLICATION_KEY",
    "OVH_APPLICATION_SECRET",
    "OVH_CONSUMER_KEY",
    "OVH_CLOUD_PROJECT_SERVICE",
)


def _check_ovh_env(provider_specs: list[ProviderSpec]) -> str | None:
    """Fail fast when OVH is requested without the env vars Terraform needs.

    Returns the OVH Public Cloud project id (``service_name``) when an OVH
    spec is present, ``None`` otherwise.
    """
    if not any(s.name in ("ovh", "ovh_3az") for s in provider_specs):
        return None
    missing = [v for v in OVH_REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"OVH nodes requested but required environment variable(s) not "
            f"set: {', '.join(missing)}"
        )
    return os.environ["OVH_CLOUD_PROJECT_SERVICE"]


def cmd_infra_nodes(args):
    """Create the main node VMs for a deployment.

    Requires bootstrap state to already exist — run ``bootstraps infra``
    first. Owns the ``regions``, ``terraform_variables``, ``vm_ips``, and
    ``vm_ids`` slices of the deployment state.
    """
    console = Console()

    provider_specs = _build_provider_specs(args)
    if not provider_specs:
        console.print(
            "[bold red]Error:[/bold red] At least one provider must have vm_count > 0. "
            "Use --do-vm-count, --hetzner-vm-count, --vultr-vm-count, "
            "--ovh-vm-count, or --ovh-3az-vm-count."
        )
        sys.exit(1)

    try:
        state = load_deployment_state(args.name)
    except RuntimeError:
        console.print(
            f"[bold red]Error:[/bold red] No deployment state found for "
            f"'{args.name}'. Run `saorsa-deploy bootstraps infra --name "
            f"{args.name} ...` first."
        )
        sys.exit(1)

    bootstrap_nodes = state.get("bootstrap_nodes") or []
    if not bootstrap_nodes:
        console.print(
            f"[bold red]Error:[/bold red] Deployment '{args.name}' has no "
            "bootstrap nodes in state. Run `saorsa-deploy bootstraps infra "
            f"--name {args.name} ...` first."
        )
        sys.exit(1)

    already_has_nodes = bool(state.get("regions"))

    do_droplet_size = getattr(args, "do_droplet_size", "s-2vcpu-4gb")
    hetzner_server_type = getattr(args, "hetzner_server_type", "cpx22")
    vultr_plan = getattr(args, "vultr_plan", "vc2-2c-4gb")
    ovh_flavor = getattr(args, "ovh_flavor", "b3-16")
    ovh_3az_flavor = getattr(args, "ovh_3az_flavor", "b3-16")
    force = getattr(args, "force", False)

    record_event(args.name, "infra_nodes_started")

    # --- Preflight phase ---
    try:
        providers_for_resolve = _apply_hetzner_preflight(
            provider_specs, hetzner_server_type, console
        )
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    try:
        providers_for_resolve = _apply_vultr_preflight(
            provider_specs, vultr_plan, providers_for_resolve, console
        )
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    # OVH has no availability preflight (parity with the migration flow);
    # just validate credentials before any Terraform runs.
    try:
        ovh_service_name = _check_ovh_env(provider_specs)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    try:
        region_pairs = resolve_regions(
            provider_specs, providers=providers_for_resolve
        )
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    # Record planned VM count + per-provider breakdown back into the
    # deployment row so `saorsa-deploy ls` (and other DB consumers) can see it.
    # Note: vm_count is total VMs (sum of vm_count * region_count); the count
    # of node *services* per VM is recorded later by `nodes provision` as
    # `nodes_per_vm`. Total node services = vm_count * nodes_per_vm.
    vm_count = sum(spec.vm_count * spec.region_count for spec in provider_specs)
    record_deploy_args_update(
        args.name,
        {
            "vm_count": vm_count,
            "node_specs": [
                {
                    "provider": spec.name,
                    "vm_count": spec.vm_count,
                    "region_count": spec.region_count,
                }
                for spec in provider_specs
            ],
        },
    )

    console.print()

    # Build main-VM Terraform configs.
    vm_count_by_provider = {spec.name: spec.vm_count for spec in provider_specs}
    resources_dir = get_resources_dir()
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"

    configs = []
    for provider_name, region in region_pairs:
        provider = PROVIDERS[provider_name]
        tf_source = resources_dir / provider.tf_dir
        workspace_dir = workspace_base / f"{provider_name}-{args.name}-{region}"
        state_key = f"{provider.state_key_prefix}-{args.name}-{region}.tfstate"

        variables = {
            "name": args.name,
            "region": region,
            "vm_count": str(vm_count_by_provider[provider_name]),
        }
        # attached_volume_size is per-provider (a --<provider>-attached-volume-size
        # override, else the global --attached-volume-size). ovh_3az is excluded:
        # 3-AZ regions have no block storage, so those nodes use the root disk and
        # the ovh-3az module has no volume.
        if provider_name != "ovh_3az":
            variables["attached_volume_size"] = str(
                _provider_volume_size(args, provider_name)
            )
        if provider_name == "digitalocean":
            variables["droplet_size"] = do_droplet_size
        elif provider_name == "hetzner":
            variables["server_type"] = hetzner_server_type
        elif provider_name == "vultr":
            variables["plan"] = vultr_plan
        elif provider_name == "ovh":
            variables["flavor_name"] = ovh_flavor
            variables["service_name"] = ovh_service_name
            # The module's name_template default ("ovh") serves the migration
            # flow; testnet node VMs follow the NAME-ant-node-REGION-N
            # convention used by the other providers.
            variables["name_template"] = "ant-node"
        elif provider_name == "ovh_3az":
            variables["flavor_name"] = ovh_3az_flavor
            variables["service_name"] = ovh_service_name
            variables["name_template"] = "ant-node"
            # Round-robin VMs across the 3-AZ region's zones (Nova places the
            # instance in the given AZ). No data volume — root disk only.
            zones = OVH_3AZ_REGION_ZONES.get(region.upper())
            if zones:
                variables["availability_zones"] = _ovh_az_tfvar(zones)

        configs.append(
            TerraformRunConfig(
                provider=provider_name,
                region=region,
                tf_source_dir=tf_source,
                workspace_dir=workspace_dir,
                state_key=state_key,
                variables=variables,
            )
        )

    # Stale main-VM tfstate is only expected on first run (regions not yet in
    # state). Once regions is populated, Terraform's own state handling makes
    # re-applies idempotent — no pre-check needed.
    if not already_has_nodes:
        stale_configs = [c for c in configs if check_terraform_state_exists(c.state_key)]
        if stale_configs:
            if not force:
                console.print("[bold red]Error:[/bold red] Main-VM Terraform state exists:")
                for c in stale_configs:
                    console.print(f"  {c.state_key}")
                console.print(
                    "These are orphaned (no parent deployment state). Use --force "
                    "to destroy the tracked cloud resources and proceed."
                )
                sys.exit(1)
            console.print(
                f"[yellow]Destroying cloud resources tracked by "
                f"{len(stale_configs)} stale main-VM tfstate(s)...[/yellow]"
            )
            purge_results = execute_terraform_runs(stale_configs, action="destroy")
            if any(not r.success for r in purge_results):
                console.print(
                    "[bold red]Failed to destroy stale main-VM resources. "
                    "Resolve manually and retry.[/bold red]"
                )
                sys.exit(1)
            for c in stale_configs:
                try:
                    delete_terraform_state(c.state_key)
                except Exception:
                    pass

    # Persist the planned regions BEFORE terraform apply so nodes destroy
    # can tear down anything terraform may have started, even on partial or
    # total failure. Previously we saved regions only after a successful
    # apply, which left orphans invisible to destroy.
    providers_state = _build_provider_state(
        provider_specs, do_droplet_size, hetzner_server_type, vultr_plan,
        ovh_flavor, ovh_3az_flavor,
    )
    terraform_variables = {
        "name": args.name,
        "attached_volume_size": str(args.attached_volume_size),
        "providers": providers_state,
    }
    try:
        update_deployment_state(
            args.name,
            {
                "regions": [[provider, region] for provider, region in region_pairs],
                "terraform_variables": terraform_variables,
            },
        )
    except Exception as e:
        console.print(
            f"[bold red]Failed to record planned regions before apply:[/bold red] {e}"
        )
        console.print(
            "[bold red]Aborting to avoid creating resources destroy can't find.[/bold red]"
        )
        sys.exit(1)

    console.print(f"[bold]Provisioning infrastructure across {len(configs)} region(s)...[/bold]")
    for spec in provider_specs:
        console.print(
            f"  {spec.name}: {spec.vm_count} VMs × {spec.region_count} region(s)"
        )
    console.print()

    results = execute_terraform_runs(configs)

    # Collect outputs for whichever regions did succeed so destroy/inventory
    # know about them; partial success is recorded.
    vm_ips = dict(state.get("vm_ips") or {})
    vm_ids = dict(state.get("vm_ids") or {})
    for result in results:
        if result.success and result.outputs.get("vm_ips"):
            key = f"{result.provider}/{result.region}"
            vm_ips[key] = result.outputs["vm_ips"]
            ids = result.outputs.get("vm_ids")
            if ids:
                vm_ids[key] = [str(i) for i in ids]

    try:
        update_deployment_state(
            args.name,
            {"vm_ips": vm_ips, "vm_ids": vm_ids},
        )
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to save vm_ips to state: {e}[/yellow]")

    failures = [r for r in results if not r.success]
    if failures:
        console.print(f"[bold red]{len(failures)} region(s) failed.[/bold red]")
        from saorsa_db.models import DeploymentStatus

        record_event(args.name, "infra_nodes_failed", {"failures": len(failures)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    console.print(
        f"[bold green]All {len(results)} region(s) provisioned successfully.[/bold green]"
    )
    console.print("[dim]Deployment state updated.[/dim]")

    record_event(
        args.name,
        "infra_nodes_completed",
        {"regions": len(results), "vm_ips": vm_ips},
    )


def _apply_hetzner_preflight(
    provider_specs: list[ProviderSpec],
    server_type: str,
    console: Console,
) -> dict:
    """Check Hetzner capacity for the chosen server type; substitute fallbacks."""
    hetzner_spec = next((s for s in provider_specs if s.name == "hetzner"), None)
    if hetzner_spec is None:
        return PROVIDERS

    token = os.environ.get("HCLOUD_TOKEN")
    if not token:
        raise RuntimeError(
            "HCLOUD_TOKEN is not set; Hetzner availability preflight cannot run."
        )

    hetzner_cfg = PROVIDERS["hetzner"]
    if hetzner_spec.regions_override is not None:
        preferred = list(hetzner_spec.regions_override)
        fallback_pool = list(hetzner_spec.regions_override)
    else:
        preferred = hetzner_cfg.regions[: hetzner_spec.region_count]
        fallback_pool = hetzner_cfg.regions
    console.print(
        f"[bold]Checking Hetzner capacity for {server_type} "
        f"in {', '.join(preferred)}...[/bold]"
    )
    resolved, notes = resolve_hetzner_regions(
        preferred=preferred,
        server_type=server_type,
        count=hetzner_spec.region_count,
        token=token,
        fallback_pool=fallback_pool,
    )
    for note in notes:
        console.print(f"[yellow]{note}[/yellow]")
    console.print(
        f"[green]Hetzner preflight ok: using {', '.join(resolved)}[/green]"
    )

    new_providers = dict(PROVIDERS)
    new_providers["hetzner"] = replace(hetzner_cfg, regions=resolved)
    return new_providers


def _apply_vultr_preflight(
    provider_specs: list[ProviderSpec],
    plan: str,
    providers_in: dict,
    console: Console,
) -> dict:
    """Check that ``plan`` is offered in the preferred Vultr regions."""
    vultr_spec = next((s for s in provider_specs if s.name == "vultr"), None)
    if vultr_spec is None:
        return providers_in

    token = os.environ.get("VULTR_API_KEY")
    if not token:
        raise RuntimeError(
            "VULTR_API_KEY is not set; Vultr availability preflight cannot run."
        )

    vultr_cfg = providers_in["vultr"]
    if vultr_spec.regions_override is not None:
        preferred = list(vultr_spec.regions_override)
        fallback_pool = list(vultr_spec.regions_override)
    else:
        preferred = vultr_cfg.regions[: vultr_spec.region_count]
        fallback_pool = vultr_cfg.regions
    console.print(
        f"[bold]Checking Vultr availability for {plan} "
        f"in {', '.join(preferred)}...[/bold]"
    )
    resolved, notes = resolve_vultr_regions(
        preferred=preferred,
        plan=plan,
        count=vultr_spec.region_count,
        token=token,
        fallback_pool=fallback_pool,
    )
    for note in notes:
        console.print(f"[yellow]{note}[/yellow]")
    console.print(
        f"[green]Vultr preflight ok: using {', '.join(resolved)}[/green]"
    )

    new_providers = dict(providers_in)
    new_providers["vultr"] = replace(vultr_cfg, regions=resolved)
    return new_providers
