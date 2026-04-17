"""`saorsa-deploy track-upgrades summary` — compact progress dashboard."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console

from saorsa_deploy.upgrade_tracker.db import STATUSES, Repository, open_db

STATUS_COLOURS = {
    "pending": "white",
    "scheduled": "cyan",
    "in_progress": "yellow",
    "completed": "green",
    "rolled_back": "red",
}


def _bar(fraction: float, width: int = 20) -> str:
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled)


def _format_cursor(cursor: str | None) -> str:
    if not cursor:
        return "never"
    try:
        dt = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
    except ValueError:
        return cursor
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"


def cmd_track_upgrades_summary(args):
    console = Console()
    db_path = Path(args.db).expanduser() if args.db else None
    if db_path is None or not db_path.exists():
        console.print(
            "[bold red]Error:[/bold red] DB not found. Use --db <path>."
        )
        sys.exit(1)

    with open_db(db_path) as conn:
        repo = Repository(conn)
        meta = repo.all_meta()
        counts = repo.count_by_status()
        total = repo.count_total()
        if total == 0:
            console.print(
                "[yellow]No nodes tracked.[/yellow] Run `track-upgrades init` "
                "first."
            )
            return

        one_hour_ago = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat().replace("+00:00", "Z")
        errors_last_hour = repo.errors_since(one_hour_ago)

        thirty_min_ago = (
            datetime.now(timezone.utc) - timedelta(minutes=30)
        ).isoformat().replace("+00:00", "Z")
        stuck = repo.stuck_since(thirty_min_ago)

    from_v = meta.get("from_version", "?")
    to_v = meta.get("to_version", "?")
    cursor = meta.get("sync_cursor")

    console.print(
        f"[bold]Release:[/bold] {from_v} → {to_v}     "
        f"last sync: {_format_cursor(cursor)}"
    )
    console.print("─" * 62)
    for status in STATUSES:
        n = counts.get(status, 0)
        frac = (n / total) if total else 0.0
        pct = int(round(frac * 100))
        colour = STATUS_COLOURS.get(status, "white")
        label = f"[{colour}]{status:<13}[/{colour}]"
        bar = f"[{colour}]{_bar(frac)}[/{colour}]"
        console.print(f"  {label} {n:>5}   {bar}  {pct:>3}%")
    console.print("─" * 62)
    if errors_last_hour:
        console.print(
            f"  Errors in last hour: [yellow]{errors_last_hour}[/yellow]  "
            f"(see [dim]details --errors[/dim])"
        )
    if stuck:
        console.print(
            f"  Stuck >30m in_progress: [yellow]{stuck}[/yellow]  "
            f"(see [dim]details --stuck[/dim])"
        )
    if not errors_last_hour and not stuck:
        console.print("  [green]No errors, nothing stuck.[/green]")
