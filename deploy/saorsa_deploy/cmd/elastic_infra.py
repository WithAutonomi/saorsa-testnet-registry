import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    check_terraform_state_exists,
    delete_terraform_state,
    save_elastic_state,
)
from saorsa_deploy.terraform import TerraformRunConfig


def cmd_elastic_infra(args):
    """Create Elasticsearch VM infrastructure using Terraform."""
    console = Console()

    region = getattr(args, "region", "lon1") or "lon1"
    volume_size = getattr(args, "volume_size", 100) or 100
    droplet_size = getattr(args, "droplet_size", "s-4vcpu-8gb") or "s-4vcpu-8gb"

    resources_dir = get_resources_dir()
    tf_source = resources_dir / "digitalocean-elastic"
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"
    workspace_dir = workspace_base / f"digitalocean-elastic-{args.name}-{region}"
    state_key = f"saorsa-deploy/do-elastic-{args.name}-{region}.tfstate"

    # Check for stale state
    if check_terraform_state_exists(state_key):
        if not getattr(args, "force", False):
            console.print("[bold red]Error:[/bold red] Stale Terraform state found:")
            console.print(f"  {state_key}")
            console.print("Use --force to delete and proceed.")
            sys.exit(1)
        console.print(
            f"[yellow]Warning: Deleting stale Terraform state: {state_key}[/yellow]"
        )
        delete_terraform_state(state_key)

    config = TerraformRunConfig(
        provider="digitalocean",
        region=region,
        tf_source_dir=tf_source,
        workspace_dir=workspace_dir,
        state_key=state_key,
        variables={
            "name": args.name,
            "region": region,
            "volume_size": str(volume_size),
            "droplet_size": droplet_size,
        },
    )

    console.print(
        f"[bold]Creating Elasticsearch VM for '{args.name}' in {region} "
        f"(size: {droplet_size}, volume: {volume_size}GB)...[/bold]"
    )
    console.print()

    results = execute_terraform_runs([config])

    if not results[0].success:
        console.print(
            "[bold red]Elasticsearch infrastructure creation failed.[/bold red]"
        )
        console.print(results[0].stderr)
        sys.exit(1)

    elastic_ip = results[0].outputs.get("elastic_ip", "")
    console.print(
        f"[bold green]Elasticsearch VM created: {elastic_ip}[/bold green]"
    )

    try:
        save_elastic_state(
            name=args.name,
            region=region,
            vm_ip=elastic_ip,
        )
        console.print("[dim]Elasticsearch state saved to S3.[/dim]")
    except Exception as e:
        console.print(
            f"[yellow]Warning: Failed to save Elasticsearch state: {e}[/yellow]"
        )

    console.print()
    console.print(
        f"[dim]Next step: saorsa-deploy provision-elastic "
        f"--name {args.name} --ssh-key-path ~/.ssh/id_rsa[/dim]"
    )
