import shutil
import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.cmd.obs_provider import (
    require_ovh_service_name,
    state_key_abbrev,
)
from saorsa_deploy.contabo import require_contabo_env
from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    delete_monitoring_state,
    delete_terraform_state,
    load_monitoring_state,
    load_terraform_state,
    terraform_state_attributes,
)
from saorsa_deploy.terraform import TerraformRunConfig


def cmd_destroy_monitoring(args):
    """Destroy monitoring infrastructure."""
    console = Console()

    console.print(f"[bold]Loading monitoring state for '{args.name}'...[/bold]")
    try:
        state = load_monitoring_state(args.name)
    except RuntimeError:
        console.print(
            f"[yellow]No monitoring infrastructure found for '{args.name}'. "
            "Nothing to destroy.[/yellow]"
        )
        return

    region = state["region"]
    # provider defaults to digitalocean for stacks created before the field
    # existed.
    provider = state.get("provider", "digitalocean")
    console.print(f"  Monitoring VMs in: {provider}/{region}")
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
    tf_source = resources_dir / f"{provider}-monitoring"
    abbrev = state_key_abbrev(provider)
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"
    workspace_dir = workspace_base / f"{provider}-monitoring-{args.name}-{region}"
    state_key = f"saorsa-deploy/{abbrev}-monitoring-{args.name}-{region}.tfstate"

    # Must match what was created so Terraform's count expressions line up with
    # state. Legacy stacks (no grafana_enabled key) default True → the existing
    # Grafana VM is included in the destroy plan.
    enable_grafana = "true" if state.get("grafana_enabled", True) else "false"

    if provider == "ovh":
        service_name = require_ovh_service_name(console)
        variables = {
            "name": args.name,
            "region": region,
            "service_name": service_name,
            "enable_grafana": enable_grafana,
        }
    elif provider == "contabo":
        require_contabo_env(console)
        # Root-disk only (no volume); product_id/image_id/ssh_public_key_path
        # fall back to the module defaults on destroy.
        variables = {
            "name": args.name,
            "region": region,
            "enable_grafana": enable_grafana,
        }
    else:
        # Pull the actual volume_size from tfstate so Terraform's plan doesn't
        # try to shrink it — DO refuses volume shrinks, breaking destroy when
        # the infra was created with a non-default size.
        volume_size = _volume_size_from_tfstate(state_key) or "50"
        variables = {
            "name": args.name,
            "region": region,
            "volume_size": volume_size,
            "enable_grafana": enable_grafana,
        }

    config = TerraformRunConfig(
        provider=provider,
        region=region,
        tf_source_dir=tf_source,
        workspace_dir=workspace_dir,
        state_key=state_key,
        variables=variables,
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


def _volume_size_from_tfstate(state_key: str) -> str | None:
    """Return the monitoring volume size recorded in the tfstate, as a string.

    Returns None when the tfstate is missing or doesn't contain the volume.
    """
    try:
        tfstate = load_terraform_state(state_key)
    except Exception:
        return None
    attrs = terraform_state_attributes(
        tfstate, "digitalocean_volume", "monitoring_storage"
    )
    if not attrs:
        return None
    size = attrs.get("size")
    return None if size is None else str(size)
