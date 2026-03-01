import shutil
import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.bootstrap import find_and_destroy_bootstrap_vm
from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.providers import PROVIDERS
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import delete_deployment_state, load_deployment_state
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

    # Build Terraform configs for each region
    resources_dir = get_resources_dir()
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"

    configs = []
    for provider_name, region in region_pairs:
        provider = PROVIDERS[provider_name]
        tf_source = resources_dir / provider.tf_dir
        workspace_dir = workspace_base / f"{provider_name}-{region}"
        state_key = f"{provider.state_key_prefix}-{region}.tfstate"

        variables = dict(terraform_variables)
        variables["region"] = region

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
        sys.exit(1)

    console.print(f"[bold green]All {len(results)} region(s) destroyed successfully.[/bold green]")
    console.print()

    # Destroy bootstrap VM
    console.print(f"[bold]Destroying bootstrap VM ({args.name}-saorsa-bootstrap)...[/bold]")
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

    console.print()
    console.print(f"[bold green]Deployment '{args.name}' fully destroyed.[/bold green]")
