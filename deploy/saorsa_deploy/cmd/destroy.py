import shutil
import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.bootstrap import find_and_destroy_bootstrap_vm
from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.providers import PROVIDERS
from saorsa_deploy.registry import record_event, record_status
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    delete_deployment_state,
    delete_terraform_state,
    load_deployment_state,
)
from saorsa_deploy.terraform import TerraformRunConfig


def cmd_destroy(args):
    """Execute the destroy command: tear down all infrastructure for a deployment."""
    console = Console()

    # Load deployment state from S3
    console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
    try:
        state = load_deployment_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    region_pairs = [(r[0], r[1]) for r in state["regions"]]
    terraform_variables = state["terraform_variables"]

    console.print(f"[dim]Found {len(region_pairs)} region(s) to destroy.[/dim]")
    for provider, region in region_pairs:
        console.print(f"  [dim]{provider}/{region}[/dim]")
    console.print()

    # Confirmation prompt
    if not args.force:
        confirm = input(
            f"This will destroy all infrastructure for '{args.name}'. Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            console.print("[yellow]Aborted.[/yellow]")
            sys.exit(0)
        console.print()

    from saorsa_db.models import DeploymentStatus

    record_event(args.name, "destroy_started")
    record_status(args.name, DeploymentStatus.DESTROYING)

    # Build Terraform configs for each region
    resources_dir = get_resources_dir()
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"

    # Extract per-provider vm_count from state
    providers_config = terraform_variables.get("providers", {})
    # Fallback for old-format state with flat vm_count
    fallback_vm_count = terraform_variables.get("vm_count")

    configs = []
    for provider_name, region in region_pairs:
        provider = PROVIDERS[provider_name]
        tf_source = resources_dir / provider.tf_dir
        name = terraform_variables["name"]
        workspace_dir = workspace_base / f"{provider_name}-{name}-{region}"
        state_key = f"{provider.state_key_prefix}-{name}-{region}.tfstate"

        if provider_name in providers_config:
            vm_count = str(providers_config[provider_name]["vm_count"])
        elif fallback_vm_count:
            vm_count = str(fallback_vm_count)
        else:
            console.print(
                f"[bold red]Error:[/bold red] Cannot determine vm_count for {provider_name}"
            )
            sys.exit(1)

        variables = {
            "name": name,
            "region": region,
            "vm_count": vm_count,
            "attached_volume_size": terraform_variables.get("attached_volume_size", "20"),
        }
        if provider_name in providers_config:
            p_cfg = providers_config[provider_name]
            if provider_name == "digitalocean":
                variables["droplet_size"] = p_cfg.get("droplet_size", "s-2vcpu-4gb")
            elif provider_name == "hetzner":
                variables["server_type"] = p_cfg.get("server_type", "cpx22")
            elif provider_name == "vultr":
                variables["plan"] = p_cfg.get("plan", "vc2-2c-4gb")

        config = TerraformRunConfig(
            provider=provider_name,
            region=region,
            tf_source_dir=tf_source,
            workspace_dir=workspace_dir,
            state_key=state_key,
            variables=variables,
        )
        configs.append(config)

    # Run terraform destroy in parallel
    console.print(f"[bold]Destroying infrastructure across {len(configs)} region(s)...[/bold]")
    console.print()

    results = execute_terraform_runs(configs, action="destroy")

    failures = [r for r in results if not r.success]
    if failures:
        console.print(f"[bold red]{len(failures)} region(s) failed to destroy.[/bold red]")
        console.print("[yellow]Bootstrap VM was NOT destroyed due to Terraform failures.[/yellow]")
        record_event(args.name, "destroy_failed", {"failures": len(failures)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    console.print(f"[bold green]All {len(results)} region(s) destroyed successfully.[/bold green]")
    console.print()

    # Delete Terraform state files from S3
    for config in configs:
        try:
            delete_terraform_state(config.state_key)
            console.print(f"[dim]Deleted Terraform state: {config.state_key}[/dim]")
        except Exception as e:
            console.print(
                f"[yellow]Warning: Failed to delete Terraform state "
                f"{config.state_key}: {e}[/yellow]"
            )

    # Destroy bootstrap VM
    console.print(f"[bold]Destroying bootstrap VM ({args.name}-ant-bootstrap)...[/bold]")
    try:
        bootstrap_result = find_and_destroy_bootstrap_vm(args.name)
        if bootstrap_result["found"]:
            console.print(
                f"[green]Bootstrap VM destroyed: {bootstrap_result['droplet_name']}[/green]"
            )
        else:
            console.print(
                f"[yellow]Bootstrap VM not found: {bootstrap_result['droplet_name']} "
                "(may already be destroyed)[/yellow]"
            )
    except Exception as e:
        console.print(f"[bold red]Failed to destroy bootstrap VM:[/bold red] {e}")
        sys.exit(1)

    # Delete deployment state from S3
    try:
        delete_deployment_state(args.name)
        console.print("[dim]Deployment state removed from S3.[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to delete deployment state: {e}[/yellow]")

    # Clean up local workspace directories
    for config in configs:
        if config.workspace_dir.exists():
            shutil.rmtree(config.workspace_dir)

    record_event(args.name, "destroyed")
    record_status(args.name, DeploymentStatus.DESTROYED)

    console.print()
    console.print(f"[bold green]Deployment '{args.name}' fully destroyed.[/bold green]")
