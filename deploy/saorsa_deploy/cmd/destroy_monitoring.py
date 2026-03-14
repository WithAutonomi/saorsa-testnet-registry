import shutil
import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    delete_monitoring_state,
    delete_terraform_state,
    load_monitoring_state,
)
from saorsa_deploy.terraform import TerraformRunConfig


def cmd_destroy_monitoring(args):
    """Destroy monitoring infrastructure."""
    console = Console()

    console.print(f"[bold]Loading monitoring state for '{args.name}'...[/bold]")
    try:
        state = load_monitoring_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    region = state["region"]
    console.print(f"  Monitoring VM in: digitalocean/{region}")
    console.print()

    if not args.force:
        confirm = input(
            f"This will destroy the monitoring stack for '{args.name}'. "
            "Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            console.print("[yellow]Aborted.[/yellow]")
            sys.exit(0)
        console.print()

    resources_dir = get_resources_dir()
    tf_source = resources_dir / "digitalocean-monitoring"
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"
    workspace_dir = workspace_base / f"digitalocean-monitoring-{args.name}-{region}"
    state_key = f"saorsa-deploy/do-monitoring-{args.name}-{region}.tfstate"

    config = TerraformRunConfig(
        provider="digitalocean",
        region=region,
        tf_source_dir=tf_source,
        workspace_dir=workspace_dir,
        state_key=state_key,
        variables={
            "name": args.name,
            "region": region,
        },
    )

    console.print("[bold]Destroying monitoring infrastructure...[/bold]")
    console.print()

    results = execute_terraform_runs([config], action="destroy")

    if not results[0].success:
        console.print(
            "[bold red]Monitoring infrastructure destroy failed.[/bold red]"
        )
        console.print(results[0].stderr)
        sys.exit(1)

    console.print(
        "[bold green]Monitoring infrastructure destroyed successfully.[/bold green]"
    )

    # Delete Terraform state
    try:
        delete_terraform_state(state_key)
        console.print(f"[dim]Deleted Terraform state: {state_key}[/dim]")
    except Exception as e:
        console.print(
            f"[yellow]Warning: Failed to delete Terraform state: {e}[/yellow]"
        )

    # Delete monitoring state
    try:
        delete_monitoring_state(args.name)
        console.print("[dim]Monitoring state removed from S3.[/dim]")
    except Exception as e:
        console.print(
            f"[yellow]Warning: Failed to delete monitoring state: {e}[/yellow]"
        )

    # Clean up workspace
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir)

    console.print()
    console.print(
        f"[bold green]Monitoring stack '{args.name}' fully destroyed.[/bold green]"
    )
