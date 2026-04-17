import os
import sys
from dataclasses import replace
from pathlib import Path

from rich.console import Console

from saorsa_deploy.bootstrap import (
    create_bootstrap_vms,
    list_stale_bootstrap_state_keys,
    purge_stale_bootstrap_state,
)
from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.hetzner_availability import resolve_hetzner_regions
from saorsa_deploy.providers import PROVIDERS, ProviderSpec, resolve_regions
from saorsa_deploy.registry import record_deployment_created, record_event, record_status
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    check_terraform_state_exists,
    delete_terraform_state,
    load_deployment_state,
    save_deployment_state,
)
from saorsa_deploy.terraform import TerraformRunConfig
from saorsa_deploy.vultr_availability import resolve_vultr_regions


def _build_provider_specs(args) -> list[ProviderSpec]:
    """Build provider specs from per-provider CLI flags."""
    specs = []
    if args.do_vm_count > 0:
        specs.append(ProviderSpec("digitalocean", args.do_vm_count, args.do_regions))
    if args.hetzner_vm_count > 0:
        specs.append(
            ProviderSpec("hetzner", args.hetzner_vm_count, args.hetzner_regions)
        )
    if args.vultr_vm_count > 0:
        specs.append(ProviderSpec("vultr", args.vultr_vm_count, args.vultr_regions))
    return specs


def _try_load_deployment_state(name: str) -> dict | None:
    try:
        return load_deployment_state(name)
    except RuntimeError:
        return None


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


def cmd_infra(args):
    """Execute the infra command: provision VMs using Terraform."""
    console = Console()

    provider_specs = _build_provider_specs(args)
    if not provider_specs:
        console.print(
            "[bold red]Error:[/bold red] At least one provider must have vm_count > 0. "
            "Use --do-vm-count, --hetzner-vm-count, or --vultr-vm-count."
        )
        sys.exit(1)

    bootstrap_count = getattr(args, "bootstrap_count", 1)
    if bootstrap_count < 1 or bootstrap_count > 7:
        console.print(
            "[bold red]Error:[/bold red] --bootstrap-count must be between 1 and 7."
        )
        sys.exit(1)

    deploy_args = {
        "name": args.name,
        "attached_volume_size": args.attached_volume_size,
        "bootstrap_count": bootstrap_count,
        "providers": {
            spec.name: {"vm_count": spec.vm_count, "regions": spec.region_count}
            for spec in provider_specs
        },
    }
    record_deployment_created(args.name, deploy_args)
    record_event(args.name, "infra_started")

    do_droplet_size = getattr(args, "do_droplet_size", "s-2vcpu-4gb")
    hetzner_server_type = getattr(args, "hetzner_server_type", "cpx22")
    vultr_plan = getattr(args, "vultr_plan", "vc2-2c-4gb")
    force = getattr(args, "force", False)

    # --- Bootstrap phase ---
    existing_state = _try_load_deployment_state(args.name)

    if existing_state and existing_state.get("bootstrap_nodes"):
        bootstrap_nodes = existing_state["bootstrap_nodes"]
        if len(bootstrap_nodes) != bootstrap_count:
            console.print(
                f"[bold red]Error:[/bold red] Deployment '{args.name}' already has "
                f"{len(bootstrap_nodes)} bootstrap node(s), but --bootstrap-count is "
                f"{bootstrap_count}. Destroy the existing deployment first or re-run "
                "with the matching count."
            )
            sys.exit(1)
        console.print(
            f"[bold]Reusing {len(bootstrap_nodes)} existing bootstrap VM(s) "
            f"for {args.name}.[/bold]"
        )
        for node in bootstrap_nodes:
            console.print(
                f"[green]  Bootstrap #{node['bootstrap_index']}: "
                f"{node['ip']} ({node['region']})[/green]"
            )
    else:
        stale_bootstrap_keys = list_stale_bootstrap_state_keys(args.name)
        if stale_bootstrap_keys:
            if not force:
                console.print(
                    "[bold red]Error:[/bold red] Bootstrap Terraform state exists for "
                    f"'{args.name}' but no parent deployment state was found:"
                )
                for key in stale_bootstrap_keys:
                    console.print(f"  {key}")
                console.print(
                    "These are orphaned. Use --force to destroy the tracked cloud "
                    "resources and proceed."
                )
                sys.exit(1)
            try:
                purge_stale_bootstrap_state(
                    args.name,
                    volume_size=args.attached_volume_size,
                    console=console,
                )
            except Exception as e:
                console.print(
                    f"[bold red]Failed to purge stale bootstrap state:[/bold red] {e}"
                )
                sys.exit(1)

        console.print(
            f"[bold]Creating {bootstrap_count} bootstrap VM(s) for {args.name}...[/bold]"
        )
        try:
            bootstrap_nodes = create_bootstrap_vms(
                args.name,
                count=bootstrap_count,
                volume_size=args.attached_volume_size,
                console=console,
            )
            for node in bootstrap_nodes:
                console.print(
                    f"[green]  Bootstrap #{node['bootstrap_index']}: "
                    f"{node['ip']} ({node['region']})[/green]"
                )
        except Exception as e:
            console.print(f"[bold red]Failed to create bootstrap VMs:[/bold red] {e}")
            sys.exit(1)

    # --- Persist state immediately so a later crash leaves destroy-able state ---
    initial_regions = (
        [tuple(r) for r in existing_state.get("regions", [])] if existing_state else []
    )
    initial_vm_ips = existing_state.get("vm_ips", {}) if existing_state else {}
    initial_vm_ids = existing_state.get("vm_ids", {}) if existing_state else {}

    initial_providers_state = _build_provider_state(
        provider_specs, do_droplet_size, hetzner_server_type, vultr_plan
    )
    initial_terraform_variables = {
        "name": args.name,
        "attached_volume_size": str(args.attached_volume_size),
        "providers": initial_providers_state,
    }

    try:
        save_deployment_state(
            args.name,
            initial_regions,
            initial_terraform_variables,
            bootstrap_nodes,
            vm_ips=initial_vm_ips,
            vm_ids=initial_vm_ids,
        )
    except Exception as e:
        console.print(
            f"[bold red]Failed to save deployment state after bootstraps:[/bold red] {e}"
        )
        console.print(
            "[bold red]Aborting — without saved state, destroy cannot clean up "
            "these bootstraps.[/bold red]"
        )
        sys.exit(1)

    console.print()

    # Preflight: check Hetzner capacity and substitute fallback regions when
    # preferred ones lack capacity for the chosen server type.
    try:
        providers_for_resolve = _apply_hetzner_preflight(
            provider_specs, hetzner_server_type, console
        )
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    # Preflight: check that the chosen Vultr plan is actually offered in the
    # preferred regions, substituting fallbacks otherwise.
    try:
        providers_for_resolve = _apply_vultr_preflight(
            provider_specs, vultr_plan, providers_for_resolve, console
        )
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    # Resolve regions for the main deployment
    try:
        region_pairs = resolve_regions(
            provider_specs, providers=providers_for_resolve
        )
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    # Build a lookup for per-provider vm_count
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

        config = TerraformRunConfig(
            provider=provider_name,
            region=region,
            tf_source_dir=tf_source,
            workspace_dir=workspace_dir,
            state_key=state_key,
            variables=variables,
        )
        configs.append(config)

    # Stale main-VM tfstate only applies when there is no parent deployment
    # state. Re-runs on an existing deployment rely on Terraform's own
    # idempotency (apply on matching state is a no-op).
    if existing_state is None:
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

    console.print(f"[bold]Provisioning infrastructure across {len(configs)} region(s)...[/bold]")
    for spec in provider_specs:
        console.print(
            f"  {spec.name}: {spec.vm_count} VMs × {spec.region_count} region(s)"
        )
    console.print()

    results = execute_terraform_runs(configs)

    failures = [r for r in results if not r.success]
    if failures:
        console.print(f"[bold red]{len(failures)} region(s) failed.[/bold red]")
        from saorsa_db.models import DeploymentStatus

        record_event(args.name, "infra_failed", {"failures": len(failures)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)
    else:
        console.print(
            f"[bold green]All {len(results)} region(s) provisioned successfully.[/bold green]"
        )

    vm_ips = {}
    vm_ids = {}
    for result in results:
        if result.success and result.outputs.get("vm_ips"):
            key = f"{result.provider}/{result.region}"
            vm_ips[key] = result.outputs["vm_ips"]
            # Persist provider instance IDs in the same order as IPs so downstream
            # commands (e.g. generate-inventory) can match each VM to its provider_id.
            ids = result.outputs.get("vm_ids")
            if ids:
                vm_ids[key] = [str(i) for i in ids]

    providers_state = _build_provider_state(
        provider_specs, do_droplet_size, hetzner_server_type, vultr_plan
    )
    terraform_variables = {
        "name": args.name,
        "attached_volume_size": str(args.attached_volume_size),
        "providers": providers_state,
    }
    try:
        save_deployment_state(
            args.name,
            region_pairs,
            terraform_variables,
            bootstrap_nodes,
            vm_ips=vm_ips,
            vm_ids=vm_ids,
        )
        console.print("[dim]Deployment state saved to S3.[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to save deployment state: {e}[/yellow]")

    record_event(
        args.name,
        "infra_completed",
        {"regions": len(results), "vm_ips": vm_ips},
    )


def _apply_hetzner_preflight(
    provider_specs: list[ProviderSpec],
    server_type: str,
    console: Console,
) -> dict:
    """Check Hetzner capacity for the chosen server type; substitute fallbacks.

    Returns a providers dict (copy of PROVIDERS) with the Hetzner region list
    replaced by regions that currently have capacity for ``server_type``, in
    the order they should be consumed. No-op when Hetzner isn't used.
    """
    hetzner_spec = next((s for s in provider_specs if s.name == "hetzner"), None)
    if hetzner_spec is None:
        return PROVIDERS

    token = os.environ.get("HCLOUD_TOKEN")
    if not token:
        raise RuntimeError(
            "HCLOUD_TOKEN is not set; Hetzner availability preflight cannot run."
        )

    hetzner_cfg = PROVIDERS["hetzner"]
    preferred = hetzner_cfg.regions[: hetzner_spec.region_count]
    console.print(
        f"[bold]Checking Hetzner capacity for {server_type} "
        f"in {', '.join(preferred)}...[/bold]"
    )
    resolved, notes = resolve_hetzner_regions(
        preferred=preferred,
        server_type=server_type,
        count=hetzner_spec.region_count,
        token=token,
        fallback_pool=hetzner_cfg.regions,
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
    """Check that ``plan`` is offered in the preferred Vultr regions.

    Returns a providers dict (copied from ``providers_in``) with the Vultr
    region list replaced by regions that actually offer ``plan``. No-op when
    Vultr isn't used.
    """
    vultr_spec = next((s for s in provider_specs if s.name == "vultr"), None)
    if vultr_spec is None:
        return providers_in

    token = os.environ.get("VULTR_API_KEY")
    if not token:
        raise RuntimeError(
            "VULTR_API_KEY is not set; Vultr availability preflight cannot run."
        )

    vultr_cfg = providers_in["vultr"]
    preferred = vultr_cfg.regions[: vultr_spec.region_count]
    console.print(
        f"[bold]Checking Vultr availability for {plan} "
        f"in {', '.join(preferred)}...[/bold]"
    )
    resolved, notes = resolve_vultr_regions(
        preferred=preferred,
        plan=plan,
        count=vultr_spec.region_count,
        token=token,
        fallback_pool=vultr_cfg.regions,
    )
    for note in notes:
        console.print(f"[yellow]{note}[/yellow]")
    console.print(
        f"[green]Vultr preflight ok: using {', '.join(resolved)}[/green]"
    )

    new_providers = dict(providers_in)
    new_providers["vultr"] = replace(vultr_cfg, regions=resolved)
    return new_providers
