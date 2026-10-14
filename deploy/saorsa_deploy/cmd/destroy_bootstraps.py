import sys

from rich.console import Console

from saorsa_deploy.bootstrap import (
    destroy_bootstrap_vms,
    list_stale_bootstrap_state_keys,
    purge_stale_bootstrap_state,
)
from saorsa_deploy.local_inventory import retire_local_inventory
from saorsa_deploy.registry import record_event, record_status
from saorsa_deploy.state import (
    delete_deployment_state,
    list_extensions_of,
    load_deployment_state,
    update_deployment_state,
)


def cmd_destroy_bootstraps(args):
    """Tear down bootstrap VMs for a deployment.

    Refuses when node-VM state is still populated — run ``nodes destroy``
    first. Falls back to scanning S3 for orphaned bootstrap tfstate files
    when no deployment state is found.
    """
    console = Console()

    console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
    try:
        state = load_deployment_state(args.name)
    except RuntimeError:
        _destroy_bootstraps_without_state(args.name, args.force, console)
        return

    regions = state.get("regions") or []
    if regions:
        console.print(
            f"[bold red]Error:[/bold red] Deployment '{args.name}' still has "
            f"{len(regions)} node region(s) in state. Run "
            f"`saorsa-deploy nodes destroy --name {args.name}` first."
        )
        sys.exit(1)

    try:
        dependents = list_extensions_of(args.name)
    except Exception as e:
        console.print(
            f"[bold red]Error:[/bold red] Failed to scan for extensions of '{args.name}': {e}"
        )
        sys.exit(1)
    if dependents:
        console.print(
            f"[bold red]Error:[/bold red] Cannot destroy bootstraps of "
            f"'{args.name}' — the following extension testnet(s) share "
            "these bootstraps:"
        )
        for dep in dependents:
            console.print(f"  [red]- {dep}[/red]")
        console.print(
            "\n[bold]Run `saorsa-deploy unextend --name <extension>` for "
            "each before retrying.[/bold] --force does not override this "
            "check."
        )
        sys.exit(1)

    bootstrap_nodes = state.get("bootstrap_nodes") or []
    if not bootstrap_nodes:
        console.print(f"[yellow]No bootstrap nodes recorded for '{args.name}'.[/yellow]")
        # Nothing to destroy from state; still try the S3 fallback in case
        # tfstate was left behind somehow.
        stale_keys = list_stale_bootstrap_state_keys(args.name)
        if stale_keys:
            _destroy_bootstraps_without_state(args.name, args.force, console)
            return
        console.print(f"[dim]Removing empty deployment state for '{args.name}'.[/dim]")
        try:
            delete_deployment_state(args.name)
        except Exception as e:
            console.print(f"[yellow]Warning: Failed to delete deployment state: {e}[/yellow]")
        else:
            retire_local_inventory(args.name, console)
        return

    if not args.force:
        confirm = input(
            f"This will destroy {len(bootstrap_nodes)} bootstrap VM(s) for "
            f"'{args.name}'. Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            console.print("[yellow]Aborted.[/yellow]")
            sys.exit(0)
        console.print()

    from saorsa_db.models import DeploymentStatus

    record_event(args.name, "destroy_bootstraps_started")
    record_status(args.name, DeploymentStatus.DESTROYING)

    bootstrap_regions = [n["region"] for n in bootstrap_nodes]
    terraform_variables = state.get("terraform_variables") or {}
    volume_size = int(terraform_variables.get("attached_volume_size", "20"))

    console.print(f"[bold]Destroying {len(bootstrap_regions)} bootstrap VM(s)...[/bold]")
    try:
        destroy_bootstrap_vms(
            args.name,
            regions=bootstrap_regions,
            volume_size=volume_size,
            console=console,
        )
        console.print("[green]Bootstrap VM(s) destroyed.[/green]")
    except Exception as e:
        console.print(f"[bold red]Failed to destroy bootstrap VMs:[/bold red] {e}")
        record_event(args.name, "destroy_bootstraps_failed")
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    # Clear the bootstrap slice. If no node slice either, tear down the whole
    # state file so it doesn't linger.
    #
    # The deployment is only *fully* destroyed when the whole state file goes,
    # so that branch is also where the status flips to DESTROYED. When the node
    # tier survives the row stays DESTROYING, which is honest: the deployment is
    # partially torn down.
    fully_destroyed = False
    if state.get("regions"):
        try:
            update_deployment_state(
                args.name,
                {"bootstrap_nodes": [], "bootstrap_ip": ""},
            )
        except Exception as e:
            console.print(f"[yellow]Warning: Failed to update deployment state: {e}[/yellow]")
    else:
        try:
            delete_deployment_state(args.name)
            console.print("[dim]Deployment state removed from S3.[/dim]")
            fully_destroyed = True
        except Exception as e:
            console.print(f"[yellow]Warning: Failed to delete deployment state: {e}[/yellow]")
        else:
            # Last tier gone: the local snapshot is now a list of released IPs.
            retire_local_inventory(args.name, console)

    record_event(args.name, "destroy_bootstraps_completed")
    if fully_destroyed:
        record_status(args.name, DeploymentStatus.DESTROYED)
    console.print(f"[bold green]Bootstrap VM(s) for '{args.name}' destroyed.[/bold green]")


def _destroy_bootstraps_without_state(name: str, force: bool, console: Console) -> None:
    """Fallback: destroy bootstraps by scanning S3 for orphan tfstate files."""
    stale_keys = list_stale_bootstrap_state_keys(name)
    if not stale_keys:
        console.print(
            f"[yellow]No bootstrap infrastructure found for '{name}'. Nothing to destroy.[/yellow]"
        )
        return

    console.print(
        "[yellow]No deployment state in S3 — scanning for orphaned bootstrap "
        "tfstate files...[/yellow]"
    )
    console.print(f"[dim]Found {len(stale_keys)} bootstrap tfstate(s):[/dim]")
    for key in stale_keys:
        console.print(f"  [dim]{key}[/dim]")
    console.print()

    if not force:
        confirm = input(
            f"Destroy orphaned bootstrap resources for '{name}'? Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            console.print("[yellow]Aborted.[/yellow]")
            sys.exit(0)
        console.print()

    try:
        purge_stale_bootstrap_state(name, console=console)
    except Exception as e:
        console.print(f"[bold red]Failed to destroy orphan bootstraps:[/bold red] {e}")
        sys.exit(1)

    console.print(
        f"[bold green]Orphaned bootstrap infrastructure for '{name}' destroyed.[/bold green]"
    )
