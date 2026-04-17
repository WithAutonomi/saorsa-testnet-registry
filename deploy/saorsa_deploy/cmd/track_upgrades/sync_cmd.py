"""`saorsa-deploy track-upgrades sync` — incremental pull + state application."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from rich.console import Console

from saorsa_deploy.cmd.track_upgrades._shared import utcnow_iso
from saorsa_deploy.upgrade_tracker.db import (
    NodeUpgrade,
    Repository,
    open_db,
)
from saorsa_deploy.upgrade_tracker.es_client import (
    ESClient,
    ESCredentials,
    ESError,
)
from saorsa_deploy.upgrade_tracker.state_machine import (
    apply_event,
    identify,
)


def _load_creds_from_meta(repo: Repository) -> ESCredentials:
    url = repo.get_meta("es_url")
    if not url:
        raise RuntimeError(
            "DB has no es_url in metadata; run `track-upgrades init` first."
        )
    return ESCredentials(
        url=url,
        username=repo.get_meta("es_username"),
        password=repo.get_meta("es_password"),
        api_key=repo.get_meta("es_api_key"),
    )


def _parse_interval(s: str) -> int:
    """Accept values like '30s', '5m', '1h' -> seconds."""
    s = s.strip().lower()
    if s.endswith("s"):
        return int(s[:-1])
    if s.endswith("m"):
        return int(s[:-1]) * 60
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    return int(s)


def _sync_once(
    repo: Repository,
    es: ESClient,
    index_pattern: str,
    testnet_name: str,
    console: Console,
) -> dict:
    """Run one incremental sync pass; return a summary dict for printing."""
    cursor = repo.get_meta("sync_cursor") or utcnow_iso()
    to_version = repo.get_meta("to_version")
    if not to_version:
        raise RuntimeError("DB has no to_version; run `track-upgrades init`.")

    # Load active rows keyed for fast lookup.
    active_rows: dict[tuple[str, str], NodeUpgrade] = {}
    for row in repo.list_active():
        active_rows[(row.host, row.service_number)] = row

    events = 0
    skipped_unknown = 0
    max_ts_seen = cursor
    touched: dict[tuple[str, str], NodeUpgrade] = {}

    for doc in es.scan_upgrade_events(
        index_pattern=index_pattern,
        testnet_name=testnet_name,
        since=cursor,
    ):
        events += 1
        ts = doc.get("@timestamp")
        if isinstance(ts, str) and ts > max_ts_seen:
            max_ts_seen = ts

        ident = identify(doc)
        if ident is None:
            skipped_unknown += 1
            continue

        # Use the working copy so multiple events in one pass accumulate.
        row = touched.get(ident) or active_rows.get(ident)
        if row is None:
            # Row already terminal (completed/rolled_back) — the startup log
            # path on a completed row is still useful for reconfirmation, so
            # fetch and apply.
            row = repo.get_node(ident[0], ident[1], to_version)
            if row is None:
                skipped_unknown += 1
                continue

        mutated = apply_event(row, doc)
        touched[ident] = mutated

    # Persist.
    for row in touched.values():
        repo.update_node(row)

    # Advance cursor (exclusive advance to avoid re-reading the last doc).
    repo.set_meta("sync_cursor", max_ts_seen)

    summary = {
        "events": events,
        "rows_touched": len(touched),
        "skipped_unknown": skipped_unknown,
        "cursor": max_ts_seen,
    }
    console.print(
        f"[dim]sync: {events} events, "
        f"{len(touched)} rows updated, "
        f"cursor={max_ts_seen}[/dim]"
    )
    return summary


def cmd_track_upgrades_sync(args):
    console = Console()
    db_path = Path(args.db).expanduser() if args.db else None
    if db_path is None:
        console.print(
            "[bold red]Error:[/bold red] --db is required for sync "
            "(or run from the same directory as init)."
        )
        sys.exit(1)
    if not db_path.exists():
        console.print(
            f"[bold red]Error:[/bold red] DB not found: {db_path}. "
            f"Run `track-upgrades init` first."
        )
        sys.exit(1)

    interval_s = _parse_interval(args.interval) if args.watch else 0

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

        try:
            _sync_once(repo, es, index_pattern, testnet_name, console)
        except ESError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)
        conn.commit()

    if not args.watch:
        return

    console.print(
        f"[dim]Watch mode: polling every {interval_s}s. Ctrl-C to stop.[/dim]"
    )
    try:
        while True:
            time.sleep(interval_s)
            with open_db(db_path) as conn:
                repo = Repository(conn)
                try:
                    _sync_once(
                        repo, es, index_pattern, testnet_name, console,
                    )
                except ESError as e:
                    # Don't die on transient ES errors in watch mode.
                    console.print(f"[yellow]Warning:[/yellow] {e}")
                    continue
                conn.commit()
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
