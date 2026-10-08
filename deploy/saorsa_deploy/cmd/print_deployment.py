"""`deployments print` command.

Prints the same deployment summary that ``deployments slack-post`` would
send, but rendered to the terminal instead of posted to Slack. It operates
on a deployment ID (``--id``) rather than a name, and reuses the Block-Kit
builder plus the plain-text flattener that backs ``slack-post --dry-run``.
"""

from __future__ import annotations

import sys

from rich.console import Console

from saorsa_deploy.cmd.slack_post import (
    _try_load_client_state,
    _try_load_elastic_state,
    _try_load_monitoring_state,
    _try_load_state,
    build_slack_blocks,
    render_payload_text,
)
from saorsa_deploy.registry import get_repository


def cmd_print(args):
    """Print a deployment summary (identified by ``--id``) to the terminal."""
    console = Console()

    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set or the database is unreachable."
        )
        sys.exit(1)

    deployment = repo.get(args.id)
    if deployment is None:
        console.print(
            f"[bold red]Error:[/bold red] No deployment with ID {args.id} found in the registry."
        )
        sys.exit(1)

    state = _try_load_state(deployment.name)
    client_state = _try_load_client_state(deployment.name)
    monitoring_state = _try_load_monitoring_state(deployment.name)
    elastic_state = _try_load_elastic_state(deployment.name)

    payload = build_slack_blocks(
        deployment,
        state,
        client_state,
        monitoring_state=monitoring_state,
        elastic_state=elastic_state,
    )

    # Plain stdout so the mrkdwn markers aren't reflowed or interpreted.
    print(render_payload_text(payload), end="")
