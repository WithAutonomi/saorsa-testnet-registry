"""`saorsa-deploy track-upgrades reconcile` — repair rows from full ES history.

Incremental `sync` advances a forward-only cursor: once an event falls behind
the cursor it is never re-read. If an event is ever missed — e.g. dropped at a
pagination boundary during a high-volume upgrade burst — the affected row stays
frozen forever (it shows in `summary` as stuck `pending`/`scheduled` even though
the node actually finished upgrading).

`reconcile` re-derives each non-terminal row's state from that node's COMPLETE
Elasticsearch history via a per-node point-in-time scan that cannot drop
documents, independent of the sync cursor. It never moves the cursor and never
touches rows that are already terminal.
"""

from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.cmd.track_upgrades.sync_cmd import _load_creds_from_meta
from saorsa_deploy.upgrade_tracker.db import Repository, open_db
from saorsa_deploy.upgrade_tracker.es_client import ESClient, ESError
from saorsa_deploy.upgrade_tracker.state_machine import apply_event


def reconcile_rows(
    repo: Repository,
    es,
    index_pattern: str,
    testnet_name: str,
) -> list[tuple[str, str, str, str, str, str]]:
    """Re-derive every non-terminal row from full ES history.

    Returns a list of (host, service_number, old_status, new_status,
    old_running_version, new_running_version) for each row that changed.
    Pure of any console/IO besides the injected `es` + `repo`, for testing.
    """
    changes: list[tuple[str, str, str, str, str, str]] = []
    for row in repo.list_active():
        events = es.scan_node_events(
            index_pattern=index_pattern,
            testnet_name=testnet_name,
            host=row.host,
            service_number=row.service_number,
        )
        new = row
        for doc in events:
            new = apply_event(new, doc)
        if (
            new.status != row.status
            or new.running_version != row.running_version
        ):
            repo.update_node(new)
            changes.append(
                (
                    row.host,
                    row.service_number,
                    row.status,
                    new.status,
                    row.running_version,
                    new.running_version,
                )
            )
    return changes


def cmd_track_upgrades_reconcile(args):
    console = Console()
    db_path = Path(args.db).expanduser() if args.db else None
    if db_path is None or not db_path.exists():
        console.print(
            "[bold red]Error:[/bold red] DB not found. Use --db <path>."
        )
        sys.exit(1)

    with open_db(db_path) as conn:
        repo = Repository(conn)
        creds = _load_creds_from_meta(repo)
        index_pattern = repo.get_meta("es_index_pattern")
        testnet_name = repo.get_meta("es_testnet_name") or repo.get_meta(
            "testnet_name"
        )
        if not index_pattern or not testnet_name:
            console.print(
                "[bold red]Error:[/bold red] DB metadata is incomplete; "
                "re-run `track-upgrades init`."
            )
            sys.exit(1)
        es = ESClient(creds)

        active_count = len(repo.list_active())
        console.print(
            f"[dim]Reconciling {active_count} non-terminal row(s) against "
            f"full Elasticsearch history…[/dim]"
        )
        try:
            changes = reconcile_rows(repo, es, index_pattern, testnet_name)
        except ESError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)
        conn.commit()

    for host, svc, old, new, old_v, new_v in changes:
        ver = f"  ({old_v}→{new_v})" if old_v != new_v else ""
        console.print(
            f"  {host} svc {svc}: [yellow]{old}[/yellow] → "
            f"[green]{new}[/green]{ver}"
        )
    console.print(
        f"[bold]Reconcile complete:[/bold] {len(changes)} row(s) repaired."
    )
