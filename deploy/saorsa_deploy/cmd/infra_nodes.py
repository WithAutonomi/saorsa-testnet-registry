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
    return specs


def _build_provider_state(
    provider_specs: list[ProviderSpec],
    do_droplet_size: str,
    hetzner_server_type: str,
    vultr_plan: str,
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
        providers_state[spec.name] = entry
    return providers_state


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
            "Use --do-vm-count, --hetzner-vm-count, or --vultr-vm-count."
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
            "attached_volume_size": str(args.attached_volume_size),
        }
        if provider_name == "digitalocean":
            variables["droplet_size"] = do_droplet_size
        elif provider_name == "hetzner":
            variables["server_type"] = hetzner_server_type
        elif provider_name == "vultr":
            variables["plan"] = vultr_plan

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
        provider_specs, do_droplet_size, hetzner_server_type, vultr_plan
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
