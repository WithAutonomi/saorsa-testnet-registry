import sys

from rich.console import Console
from rich.panel import Panel

from saorsa_deploy.registry import get_repository


def cmd_edit_analysis_summary(args):
    """Update the freeform summary on a deployment's recorded host results."""
    console = Console()

    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set. "
            "This command requires the registry database."
        )
        sys.exit(1)

    deployment = repo.get_active_by_name(args.name) or repo.get_by_name(args.name)
    if deployment is None:
        console.print(
            f"[bold red]Error:[/bold red] No deployment named '{args.name}' in the registry."
        )
        sys.exit(1)

    try:
        row = repo.set_host_result_summary(deployment.id, args.summary)
    except ValueError:
        console.print(
            f"[bold red]Error:[/bold red] No host results recorded for "
            f"deployment '{args.name}' yet. Run `saorsa-deploy deployments "
            f"record-analysis --name {args.name}` first."
        )
        sys.exit(1)

    console.print(
        f"[bold green]Summary updated for deployment '{args.name}' "
        f"(id={deployment.id}).[/bold green]"
    )
    console.print(
        Panel(row.summary, title=f"Summary for '{args.name}'", border_style="cyan")
    )
