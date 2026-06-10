import sys

from rich.console import Console
from rich.panel import Panel

from saorsa_deploy.state import load_elastic_state


def cmd_elastic_info(args):
    """Print Elasticsearch stack connection details."""
    console = Console()

    try:
        state = load_elastic_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    es_url = state.get("elasticsearch_url", "")
    es_user = state.get("elasticsearch_username", "")
    es_pass = state.get("elasticsearch_password", "")
    es_api_key = state.get("elasticsearch_api_key", "")
    es_index = state.get("elasticsearch_index_pattern", "")
    # Client (uploader/downloader/canary) logs land in a separate index; older
    # state predating client log forwarding won't carry it.
    es_client_index = state.get("elasticsearch_client_index_pattern", "")

    if not es_url:
        console.print(
            "[bold red]Error:[/bold red] Elasticsearch stack not yet provisioned. "
            "Run elastic provision first."
        )
        sys.exit(1)

    lines = [
        f"  [bold]URL:[/bold]                  {es_url}",
        f"  [bold]Username:[/bold]             {es_user}",
        f"  [bold]Password:[/bold]             {es_pass}",
        f"  [bold]API Key:[/bold]              {es_api_key}",
        f"  [bold]Node index pattern:[/bold]   {es_index}",
    ]
    if es_client_index:
        lines.append(
            f"  [bold]Client index pattern:[/bold] {es_client_index}"
        )

    console.print(
        Panel(
            "\n".join(lines),
            title=f"Elasticsearch: {args.name}",
            border_style="green",
        )
    )
