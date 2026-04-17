"""`saorsa-deploy track-upgrades details` — full per-node status table."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from saorsa_deploy.upgrade_tracker.db import Repository, open_db

STATUS_COLOURS = {
    "pending": "white",
    "scheduled": "cyan",
    "in_progress": "yellow",
    "completed": "green",
    "rolled_back": "red",
}


def _format_scheduled(iso_ts: str | None, status: str) -> str:
    if iso_ts is None:
        return "-"
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return iso_ts
    if status in ("completed", "rolled_back"):
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")

    now = datetime.now(timezone.utc)
    delta = dt - now
    secs = int(delta.total_seconds())

    if abs(secs) < 60:
        return "just now"

    def _fmt(s: int) -> str:
        s = abs(s)
        h = s // 3600
        m = (s % 3600) // 60
        if h and m:
            return f"{h}h {m}m"
        if h:
            return f"{h}h"
        return f"{m}m"

    if secs >= 0:
        return f"in {_fmt(secs)}"
    return f"{_fmt(secs)} ago"


def cmd_track_upgrades_details(args):
    console = Console()
    db_path = Path(args.db).expanduser() if args.db else None
    if db_path is None or not db_path.exists():
        console.print(
            "[bold red]Error:[/bold red] DB not found. Use --db <path>."
        )
        sys.exit(1)

    with open_db(db_path) as conn:
        repo = Repository(conn)
        nodes = repo.list_nodes()

    if not nodes:
        console.print(
            "[yellow]No nodes tracked.[/yellow] Run `track-upgrades init` "
            "first."
        )
        return

    table = Table(show_lines=False, expand=False)
    table.add_column("Host", no_wrap=True)
    table.add_column("Svc", justify="right", no_wrap=True)
    table.add_column("Running", no_wrap=True)
    table.add_column("Target", no_wrap=True)
    table.add_column("Scheduled", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Err", justify="right", no_wrap=True)

    for n in nodes:
        colour = STATUS_COLOURS.get(n.status, "white")
        status_cell = f"[{colour}]{n.status}[/{colour}]"

        running_style = (
            "green" if n.running_version == n.to_version else "white"
        )
        running_cell = (
            f"[{running_style}]{n.running_version}[/{running_style}]"
        )

        err_cell = str(n.error_count) if n.error_count else ""
        scheduled_cell = _format_scheduled(n.scheduled_at, n.status)

        if n.status == "completed" and n.completed_at:
            scheduled_cell = _format_scheduled(n.completed_at, "completed")

        table.add_row(
            n.host,
            n.service_number,
            running_cell,
            n.to_version,
            scheduled_cell,
            status_cell,
            err_cell,
        )

    console.print(table)
