import shutil
import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    delete_elastic_state,
    delete_terraform_state,
    load_elastic_state,
    load_terraform_state,
    terraform_state_attributes,
)
from saorsa_deploy.terraform import TerraformRunConfig


def cmd_destroy_elastic(args):
    """Destroy Elasticsearch infrastructure."""
    console = Console()

    console.print(f"[bold]Loading Elasticsearch state for '{args.name}'...[/bold]")
    try:
        state = load_elastic_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    region = state["region"]
    console.print(f"  Elasticsearch VM in: digitalocean/{region}")
    console.print()

    if not args.force:
        confirm = input(
            f"This will destroy the Elasticsearch stack for '{args.name}'. "
            "Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            console.print("[yellow]Aborted.[/yellow]")
            sys.exit(0)
        console.print()

    resources_dir = get_resources_dir()
    tf_source = resources_dir / "digitalocean-elastic"
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"
    workspace_dir = workspace_base / f"digitalocean-elastic-{args.name}-{region}"
    state_key = f"saorsa-deploy/do-elastic-{args.name}-{region}.tfstate"

    # Pull the actual volume_size and droplet_size from tfstate so Terraform's
    # plan doesn't try to shrink or resize — DO refuses volume shrinks and
    # droplet resize under some conditions, breaking destroy.
    tfstate_vars = _destroy_vars_from_tfstate(state_key)

    config = TerraformRunConfig(
        provider="digitalocean",
        region=region,
        tf_source_dir=tf_source,
        workspace_dir=workspace_dir,
        state_key=state_key,
        variables={
            "name": args.name,
            "region": region,
            "volume_size": tfstate_vars.get("volume_size", "100"),
            "droplet_size": tfstate_vars.get("droplet_size", "s-4vcpu-8gb"),
        },
    )

    console.print("[bold]Destroying Elasticsearch infrastructure...[/bold]")
    console.print()

    results = execute_terraform_runs([config], action="destroy")

    if not results[0].success:
        console.print(
            "[bold red]Elasticsearch infrastructure destroy failed.[/bold red]"
        )
        console.print(results[0].stderr)
        sys.exit(1)

    console.print(
        "[bold green]Elasticsearch infrastructure destroyed successfully.[/bold green]"
    )

    # Delete Terraform state
    try:
        delete_terraform_state(state_key)
        console.print(f"[dim]Deleted Terraform state: {state_key}[/dim]")
    except Exception as e:
        console.print(
            f"[yellow]Warning: Failed to delete Terraform state: {e}[/yellow]"
        )

    # Delete Elasticsearch state
    try:
        delete_elastic_state(args.name)
        console.print("[dim]Elasticsearch state removed from S3.[/dim]")
    except Exception as e:
        console.print(
            f"[yellow]Warning: Failed to delete Elasticsearch state: {e}[/yellow]"
        )

    # Clean up workspace
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir)

    console.print()
    console.print(
        f"[bold green]Elasticsearch stack '{args.name}' fully destroyed.[/bold green]"
    )


def _destroy_vars_from_tfstate(state_key: str) -> dict:
    """Return volume_size / droplet_size (as strings) from the tfstate.

    Missing keys just drop out of the dict so callers can apply defaults.
    """
    try:
        tfstate = load_terraform_state(state_key)
    except Exception:
        return {}

    out: dict = {}
    vol = terraform_state_attributes(
        tfstate, "digitalocean_volume", "elastic_storage"
    )
    if vol and vol.get("size") is not None:
        out["volume_size"] = str(vol["size"])

    droplet = terraform_state_attributes(
        tfstate, "digitalocean_droplet", "elastic"
    )
    if droplet and droplet.get("size"):
        out["droplet_size"] = droplet["size"]

    return out
