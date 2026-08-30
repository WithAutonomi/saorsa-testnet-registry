import sys

from rich.console import Console

from saorsa_deploy.registry import record_event, record_status
from saorsa_deploy.state import (
    delete_deployment_state,
    load_client_state,
    load_deployment_state,
)


def cmd_unextend(args):
    """Tear down an extension testnet's state record.

    Inverse of ``extend``. Refuses if the deployment is not an extension
    (no ``shared_bootstrap_from`` recorded) or still has node regions
    populated (run ``nodes destroy`` first). Does not touch any cloud
    infrastructure — the source testnet's bootstraps are left alone.
    """
    console = Console()

    name = args.name
    force = getattr(args, "force", False)

    try:
        state = load_deployment_state(name)
    except RuntimeError:
        console.print(
            f"[bold red]Error:[/bold red] No deployment state found for "
            f"'{name}'."
        )
        sys.exit(1)

    source = state.get("shared_bootstrap_from")
    if source is None:
        console.print(
            f"[bold red]Error:[/bold red] Deployment '{name}' is not an "
            "extension (no shared_bootstrap_from in state). Use "
            "`bootstraps destroy` for regular deployments."
        )
        sys.exit(1)

    regions = state.get("regions") or []
    if regions:
        console.print(
            f"[bold red]Error:[/bold red] Extension '{name}' still has "
            f"{len(regions)} node region(s) in state. Run "
            f"`saorsa-deploy nodes destroy --name {name}` first."
        )
        sys.exit(1)

    # An extension may carry its own client tier (`clients destroy` deletes the
    # client state as its final act, so an orchestrated `down` never trips this).
    try:
        client_state = load_client_state(name)
    except RuntimeError:
        client_state = None
    if client_state and client_state.get("clients"):
        console.print(
            f"[bold red]Error:[/bold red] Extension '{name}' still has "
            f"{len(client_state['clients'])} client VM(s) in state. Run "
            f"`saorsa-deploy clients destroy --name {name}` first."
        )
        sys.exit(1)

    if not force:
        confirm = input(
            f"This will remove extension state for '{name}' (points at "
            f"source '{source}'). The source's bootstraps are untouched. "
            "Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            console.print("[yellow]Aborted.[/yellow]")
            sys.exit(0)
        console.print()

    from saorsa_db.models import DeploymentStatus

    record_event(name, "unextend_started", {"source": source})
    record_status(name, DeploymentStatus.DESTROYING)

    try:
        delete_deployment_state(name)
    except Exception as e:
        console.print(
            f"[bold red]Failed to delete deployment state:[/bold red] {e}"
        )
        record_event(name, "unextend_failed", {"source": source})
        record_status(name, DeploymentStatus.FAILED)
        sys.exit(1)

    record_event(name, "unextend_completed", {"source": source})
    record_status(name, DeploymentStatus.DESTROYED)
    console.print(
        f"[bold green]Extension '{name}' removed (source '{source}' "
        "untouched).[/bold green]"
    )
