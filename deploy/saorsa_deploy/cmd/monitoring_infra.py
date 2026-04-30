import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    check_terraform_state_exists,
    delete_terraform_state,
    load_monitoring_state,
    save_monitoring_state,
)
from saorsa_deploy.terraform import TerraformRunConfig


def cmd_monitoring_infra(args):
    """Create monitoring VM infrastructure using Terraform."""
    console = Console()

    region = getattr(args, "region", "lon1") or "lon1"
    volume_size = getattr(args, "volume_size", 50) or 50
    droplet_size = getattr(args, "droplet_size", "s-2vcpu-4gb") or "s-2vcpu-4gb"

    resources_dir = get_resources_dir()
    tf_source = resources_dir / "digitalocean-monitoring"
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"
    workspace_dir = workspace_base / f"digitalocean-monitoring-{args.name}-{region}"
    state_key = f"saorsa-deploy/do-monitoring-{args.name}-{region}.tfstate"

    # Idempotent reuse: skip the apply if we already have a monitoring VM
    # recorded for this deployment. Mirrors `bootstraps infra` so re-running
    # a deploy runbook after a mid-pipeline failure doesn't trip on its own
    # previously-provisioned VM.
    try:
        existing = load_monitoring_state(args.name)
    except RuntimeError:
        existing = None

    if existing and existing.get("vm_ip"):
        existing_region = existing.get("region", region)
        if existing_region != region:
            console.print(
                f"[bold red]Error:[/bold red] Monitoring VM for '{args.name}' "
                f"already exists in region '{existing_region}', but the request "
                f"is for '{region}'. Run `monitoring destroy --name {args.name} "
                f"--force` first if you want to recreate in a different region."
            )
            sys.exit(1)
        console.print(
            f"[bold]Reusing existing monitoring VM for '{args.name}' "
            f"({existing['vm_ip']}, {existing_region}).[/bold]"
        )
        return

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
        f"[bold]Creating monitoring VM for '{args.name}' in {region} "
        f"(size: {droplet_size}, volume: {volume_size}GB)...[/bold]"
    )
    console.print()

    results = execute_terraform_runs([config])

    if not results[0].success:
        console.print("[bold red]Monitoring infrastructure creation failed.[/bold red]")
        console.print(results[0].stderr)
        sys.exit(1)

    monitoring_ip = results[0].outputs.get("monitoring_ip", "")
    console.print(
        f"[bold green]Monitoring VM created: {monitoring_ip}[/bold green]"
    )

    try:
        save_monitoring_state(
            name=args.name,
            region=region,
            vm_ip=monitoring_ip,
        )
        console.print("[dim]Monitoring state saved to S3.[/dim]")
    except Exception as e:
        console.print(
            f"[bold red]Error: Failed to save monitoring state: {e}[/bold red]"
        )
        console.print(
            "[bold red]The monitoring VM was created but state was not saved.[/bold red]"
        )
        console.print(
            "[bold red]The VM must be destroyed manually to avoid orphaned resources.[/bold red]"
        )
        sys.exit(1)

    console.print()
    console.print(
        f"[dim]Next step: saorsa-deploy monitoring provision "
        f"--name {args.name} --ssh-key-path ~/.ssh/id_rsa[/dim]"
    )
