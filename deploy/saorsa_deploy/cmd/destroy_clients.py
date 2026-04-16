import shutil
import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    delete_client_state,
    delete_terraform_state,
    load_client_state,
)
from saorsa_deploy.terraform import TerraformRunConfig

CLIENT_STATE_KEY_PREFIX = "saorsa-deploy/do-client"


def cmd_destroy_clients(args):
    """Execute the destroy-clients command: tear down client VMs."""
    console = Console()

    console.print(f"[bold]Loading client state for '{args.name}'...[/bold]")
    try:
        client_state = load_client_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    clients = client_state.get("clients", [])
    if not clients:
        console.print("[bold red]Error:[/bold red] No clients found in state.")
        sys.exit(1)

    console.print(f"[dim]Found {len(clients)} client VM(s) to destroy.[/dim]")
    for c in clients:
        console.print(
            f"  [dim]#{c['client_index']}: {c.get('ip', 'N/A')} "
            f"({c['region']}, {c['vm_size']})[/dim]"
        )
    console.print()

    if not args.force:
        confirm = input(
            f"This will destroy all client VMs for '{args.name}'. Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            console.print("[yellow]Aborted.[/yellow]")
            sys.exit(0)
        console.print()

    resources_dir = get_resources_dir()
    tf_source = resources_dir / "digitalocean-client"
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"

    configs = []
    for spec in clients:
        workspace_dir = (
            workspace_base
            / f"do-client-{args.name}-{spec['region']}-{spec['client_index']}"
        )
        state_key = (
            f"{CLIENT_STATE_KEY_PREFIX}-{args.name}"
            f"-{spec['region']}-{spec['client_index']}.tfstate"
        )
        variables = {
            "name": args.name,
            "region": spec["region"],
            "client_index": str(spec["client_index"]),
            "droplet_size": spec.get("vm_size", "s-2vcpu-4gb"),
        }
        config = TerraformRunConfig(
            provider="digitalocean",
            region=spec["region"],
            tf_source_dir=tf_source,
            workspace_dir=workspace_dir,
            state_key=state_key,
            variables=variables,
        )
        configs.append(config)

    console.print(f"[bold]Destroying {len(configs)} client VM(s)...[/bold]")

    from saorsa_deploy.terraform import run_terraform_destroy

    failures = []
    for config in configs:
        result = run_terraform_destroy(config)
        if not result.success:
            console.print(
                f"[red]Failed to destroy {config.region} "
                f"(client #{config.variables.get('client_index', '?')}):[/red]"
            )
            console.print(f"  {result.stderr[:500]}")
            failures.append(config)
        else:
            console.print(
                f"[green]  Destroyed client in {config.region} "
                f"(#{config.variables.get('client_index', '?')})[/green]"
            )
            # Clean up Terraform state
            try:
                delete_terraform_state(config.state_key)
            except Exception:
                pass

    # Clean up workspace directories
    for config in configs:
        if config.workspace_dir.exists():
            shutil.rmtree(config.workspace_dir, ignore_errors=True)

    if failures:
        console.print(
            f"[bold red]{len(failures)} client VM(s) failed to destroy.[/bold red]"
        )
        sys.exit(1)

    # Clean up client state
    try:
        delete_client_state(args.name)
        console.print("[dim]Client state deleted from S3.[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to delete client state: {e}[/yellow]")

    console.print()
    console.print(
        f"[bold green]All {len(clients)} client VM(s) destroyed successfully.[/bold green]"
    )
