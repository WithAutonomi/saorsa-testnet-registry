import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.bootstrap import create_bootstrap_vm
from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.providers import PROVIDERS, ProviderSpec, resolve_regions
from saorsa_deploy.registry import record_deployment_created, record_event, record_status
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    check_terraform_state_exists,
    delete_terraform_state,
    save_deployment_state,
)
from saorsa_deploy.terraform import TerraformRunConfig


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

    deploy_args = {
        "name": args.name,
        "attached_volume_size": args.attached_volume_size,
        "providers": {
            spec.name: {"vm_count": spec.vm_count, "regions": spec.region_count}
            for spec in provider_specs
        },
    }
    record_deployment_created(args.name, deploy_args)
    record_event(args.name, "infra_started")

    # Create the bootstrap VM first (idempotent, always on Digital Ocean)
    console.print(f"[bold]Bootstrap VM ({args.name}-ant-bootstrap)...[/bold]")
    try:
        bootstrap = create_bootstrap_vm(args.name, volume_size=args.attached_volume_size)
        if bootstrap["created"]:
            console.print(
                f"[green]Bootstrap VM created: {bootstrap['droplet_name']} "
                f"({bootstrap['ip_address']})[/green]"
            )
        else:
            console.print(
                f"[green]Bootstrap VM already exists: {bootstrap['droplet_name']} "
                f"({bootstrap['ip_address']})[/green]"
            )
    except Exception as e:
        console.print(f"[bold red]Failed to create bootstrap VM:[/bold red] {e}")
        sys.exit(1)

    console.print()

    # Resolve regions for the main deployment
    try:
        region_pairs = resolve_regions(provider_specs)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    # Build a lookup for per-provider vm_count
    vm_count_by_provider = {spec.name: spec.vm_count for spec in provider_specs}

    resources_dir = get_resources_dir()
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"

    hetzner_server_type = getattr(args, "hetzner_server_type", "cpx22")
    do_droplet_size = getattr(args, "do_droplet_size", "s-2vcpu-4gb")
    vultr_plan = getattr(args, "vultr_plan", "vc2-2c-4gb")

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

    # Check for stale Terraform state files
    stale_keys = [c.state_key for c in configs if check_terraform_state_exists(c.state_key)]
    if stale_keys:
        if not getattr(args, "force", False):
            console.print("[bold red]Error:[/bold red] Stale Terraform state found:")
            for key in stale_keys:
                console.print(f"  {key}")
            console.print("Use --force to delete and proceed.")
            sys.exit(1)
        for key in stale_keys:
            console.print(f"[yellow]Warning: Deleting stale Terraform state: {key}[/yellow]")
            delete_terraform_state(key)

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
    for result in results:
        if result.success and result.outputs.get("vm_ips"):
            key = f"{result.provider}/{result.region}"
            vm_ips[key] = result.outputs["vm_ips"]

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
            bootstrap["ip_address"],
            vm_ips=vm_ips,
        )
        console.print("[dim]Deployment state saved to S3.[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to save deployment state: {e}[/yellow]")

    record_event(
        args.name,
        "infra_completed",
        {"regions": len(results), "vm_ips": vm_ips},
    )
