"""``clients slack-post`` — a client extension's arrival and removal, in Slack.

A run that adds a browser cohort as a ``kind: clients`` extension was invisible
in Slack between the deployment's own ``slack-post`` and the results comment:
the cohort came up, ran, and went down with nothing said. Those two events are
what say a hot-read run is making progress.

Posted as a **threaded reply** under the deployment's own post whenever its
``(channel, ts)`` is recorded, exactly as ``record-results --update-slack``
does, so the whole run reads top to bottom in one thread. An extension has no
registry row of its own, which is why this is not ``deployments slack-post``:
that command takes a deployment name and reads its row.

**Every failure path exits 0.** This command only ever runs as the best-effort
tail of ``clients up`` / ``clients down``, by which point the cohort is already
up (or already gone) — a missing ``DATABASE_URL``, an unreachable Slack or a
deployment that was never recorded must not turn a finished run into a failed
one. ``deployments slack-post`` exits 1 on the same conditions because it is
also run by hand, where silence would be the wrong answer.
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError, version

import requests
from rich.console import Console

from saorsa_deploy.client_extension import ExtensionError, validate_extension_name
from saorsa_deploy.cmd.slack_post import (
    _clients_block,
    _fields_section,
    _section,
    render_payload_text,
)
from saorsa_deploy.registry import get_repository
from saorsa_deploy.slack_api import SlackApiError, post_message, post_reply
from saorsa_deploy.state import load_client_state


def _tool_version() -> str:
    try:
        return version("saorsa-deploy")
    except PackageNotFoundError:
        return "unknown"


def _regions(clients: list[dict]) -> str:
    """``lon1 ×9, nyc1 ×9`` — counts per region, in first-seen order."""
    counts: dict[str, int] = {}
    for client in clients:
        region = client.get("region", "?")
        counts[region] = counts.get(region, 0) + 1
    return ", ".join(f"{region} ×{count}" for region, count in counts.items()) or "none"


def _hot_files_block(deployment: str) -> dict | None:
    """``*Hot files:* 3 × 50 MiB, selected <ISO>``, when a selection exists.

    Best-effort: the selection is an operator step that may not have run yet,
    and a bucket that cannot be read is not a reason to say nothing about a
    cohort that is up.
    """
    try:
        from saorsa_deploy.hot_files import load_hot_files

        document = load_hot_files(deployment)
    except Exception:  # noqa: BLE001 — a post is worth more than this one line
        return None
    if not document:
        return None
    files = document.get("files") or []
    size_mib = (document.get("size_bytes") or 0) // (1024 * 1024)
    return _section(
        f"*Hot files:* {len(files)} × {size_mib} MiB, selected "
        f"{document.get('selected_at', 'unknown')}"
    )


def _up_payload(console: Console, name: str, extension: str) -> dict | None:
    try:
        client_state = load_client_state(name, extension)
    except RuntimeError as e:
        console.print(f"[yellow]Not posting to Slack: {e}[/yellow]")
        return None

    clients = client_state.get("clients") or []
    blocks = [
        _section(f"*Client extension `{extension}` of `{name}` is up*"),
        _fields_section(
            [
                ("VMs", str(len(clients))),
                ("Regions", _regions(clients)),
                ("saorsa-deploy", _tool_version()),
            ]
        ),
    ]
    if clients:
        blocks.append(_clients_block(clients))
    hot_files = _hot_files_block(name)
    if hot_files is not None:
        blocks.append(hot_files)
    return {
        "text": f"Client extension {extension} of {name} is up ({len(clients)} VMs)",
        "blocks": blocks,
    }


def _down_payload(name: str, extension: str, vms: str) -> dict:
    # No S3 read: `clients down` deletes the extension's state in the destroy
    # step that precedes this one, so what was there has to arrive as an
    # argument. The step builder renders it from the manifest.
    return {
        "text": f"Client extension {extension} of {name} torn down",
        "blocks": [
            _section(f"*Client extension `{extension}` of `{name}` torn down*"),
            _section(f"*Was:* {vms}"),
        ],
    }


def cmd_clients_slack_post(args):
    """Post a client extension's ``up`` or ``down`` into the deployment's thread.

    Exits 0 on every failure (see the module docstring). The two exceptions are
    argument errors, which exit 2: an operator typing the command by hand is
    told what is wrong with it, and the orchestrator never emits either.
    """
    console = Console()
    event = args.event
    vms = getattr(args, "vms", None)
    dry_run = getattr(args, "dry_run", False)

    if event == "up" and vms:
        console.print(
            "[bold red]Error:[/bold red] --vms is only valid with --event down, "
            "where the extension's state has already been deleted. The `up` post "
            "renders the VMs from that state."
        )
        sys.exit(2)
    if event == "down" and not vms:
        console.print(
            "[bold red]Error:[/bold red] --event down requires --vms '<summary>': "
            "the extension's state is gone by the time this runs, so the VM "
            "summary has to be passed in."
        )
        sys.exit(2)

    try:
        validate_extension_name(args.extension)
    except ExtensionError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(2)

    repo = get_repository()
    deployment = None
    if repo is None:
        console.print(
            "[yellow]Not posting to Slack: DATABASE_URL is not set or the "
            "database is unreachable, so the deployment's Slack thread cannot "
            "be looked up.[/yellow]"
        )
    else:
        deployment = repo.get_active_by_name(args.name) or repo.get_by_name(args.name)
        if deployment is None:
            console.print(
                f"[yellow]Not posting to Slack: no deployment named "
                f"'{args.name}' in the registry.[/yellow]"
            )
    # A dry run still renders: it posts nothing and reads nothing it would not
    # read anyway, so there is no reason to make previewing the payload depend
    # on the registry being reachable.
    if deployment is None and not dry_run:
        return

    if event == "up":
        payload = _up_payload(console, args.name, args.extension)
        if payload is None:
            return
    else:
        payload = _down_payload(args.name, args.extension, vms)

    if dry_run:
        # Plain stdout so Rich doesn't reflow or interpret the mrkdwn markers.
        print(render_payload_text(payload), end="")
        return

    bot_token = os.environ.get("ANT_DEPLOY_SLACK_BOT_TOKEN")
    if bot_token:
        thread_channel = getattr(deployment, "slack_channel", None)
        thread_ts = getattr(deployment, "slack_message_ts", None)
        try:
            if thread_channel and thread_ts:
                post_reply(
                    bot_token,
                    thread_channel,
                    thread_ts,
                    text=payload["text"],
                    blocks=payload["blocks"],
                )
                console.print(
                    f"[bold green]Posted '{args.extension}' {event} to the "
                    f"Slack thread of '{args.name}'.[/bold green]"
                )
                return
            channel = getattr(args, "channel", None) or os.environ.get("ANT_DEPLOY_SLACK_CHANNEL")
            if not channel:
                console.print(
                    "[yellow]Not posting to Slack: ANT_DEPLOY_SLACK_BOT_TOKEN is "
                    "set but no channel was provided, and "
                    f"'{args.name}' has no recorded message to reply to. Pass "
                    "--channel or set ANT_DEPLOY_SLACK_CHANNEL.[/yellow]"
                )
                return
            post_message(
                bot_token,
                channel,
                text=payload["text"],
                blocks=payload["blocks"],
            )
        except SlackApiError as e:
            console.print(f"[yellow]Slack post failed: {e}[/yellow]")
            return
        # Nothing is persisted: this post's own ts is not a thread anyone else
        # replies under, and the deployment's reference must not be overwritten.
        console.print(f"[bold green]Posted '{args.extension}' {event} to Slack.[/bold green]")
        return

    webhook_url = os.environ.get("ANT_DEPLOY_SLACK_WEBHOOK_URL")
    if not webhook_url:
        console.print(
            "[yellow]Not posting to Slack: neither "
            "ANT_DEPLOY_SLACK_BOT_TOKEN nor ANT_DEPLOY_SLACK_WEBHOOK_URL is "
            "set.[/yellow]"
        )
        return

    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
    except requests.RequestException as e:
        console.print(f"[yellow]Slack POST failed: {e}[/yellow]")
        return
    if not response.ok:
        console.print(
            f"[yellow]Slack rejected the post: {response.status_code} {response.text}[/yellow]"
        )
        return
    console.print(
        f"[bold green]Posted '{args.extension}' {event} to Slack.[/bold green] "
        "(via incoming webhook — a webhook cannot reply in a thread.)"
    )
