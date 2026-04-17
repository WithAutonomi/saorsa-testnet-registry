"""`saorsa-deploy track-upgrades init` — seed the SQLite DB from an inventory."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Iterable

from rich.console import Console

from saorsa_deploy.cmd.track_upgrades._shared import (
    resolve_db_path,
    resolve_es,
    utcnow_iso,
)
from saorsa_deploy.upgrade_tracker.db import (
    NodeUpgrade,
    Repository,
    open_db,
)

SERVICE_NAME_RE = re.compile(r"^ant-node-(.+)$")


def _enumerate_nodes(inventory_path: Path) -> Iterable[tuple[str, str]]:
    """Yield (host_uppercased, service_number) pairs for every node in the inventory."""
    with open(inventory_path) as f:
        data = json.load(f)
    for vm in data.get("node_vms", []):
        name = vm.get("name")
        if not name:
            continue
        host = name.upper()
        nodes = vm.get("nodes") or []
        services = vm.get("services") or []
        service_names = [
            n.get("service_name") for n in nodes if isinstance(n, dict)
        ]
        service_names.extend(s for s in services if isinstance(s, str))
        seen: set[str] = set()
        for service_name in service_names:
            if not service_name or service_name in seen:
                continue
            seen.add(service_name)
            m = SERVICE_NAME_RE.match(service_name)
            if m:
                yield host, m.group(1)
            elif service_name.startswith("ant-genesis"):
                yield host, "genesis"


def cmd_track_upgrades_init(args):
    console = Console()
    inventory_path = Path(args.inventory_path).expanduser()
    if not inventory_path.exists():
        console.print(
            f"[bold red]Error:[/bold red] Inventory file not found: "
            f"{inventory_path}"
        )
        sys.exit(1)

    testnet_name = args.testnet_name or (args.name or "unnamed")
    resolved = resolve_es(args, console, testnet_name_hint=testnet_name)
    db_path = resolve_db_path(args, testnet_name)

    console.print(f"[bold]Initialising upgrade tracker for '{testnet_name}'[/bold]")
    console.print(f"  Inventory:   {inventory_path}")
    console.print(f"  DB path:     {db_path}")
    console.print(f"  From:        {args.from_version}")
    console.print(f"  To:          {args.to_version}")
    console.print(f"  ES URL:      {resolved.creds.url}")
    console.print(f"  Index:       {resolved.index_pattern}")
    console.print()

    pairs = list(_enumerate_nodes(inventory_path))
    if not pairs:
        console.print(
            "[bold red]Error:[/bold red] No nodes found in inventory "
            "(node_vms[].nodes or node_vms[].services must be populated)."
        )
        sys.exit(1)

    now = utcnow_iso()
    with open_db(db_path) as conn:
        repo = Repository(conn)

        if args.reset:
            conn.execute("DELETE FROM node_upgrades")
            conn.execute("DELETE FROM metadata")

        repo.set_meta("testnet_name", testnet_name)
        repo.set_meta("from_version", args.from_version)
        repo.set_meta("to_version", args.to_version)
        repo.set_meta("es_url", resolved.creds.url)
        repo.set_meta("es_index_pattern", resolved.index_pattern)
        repo.set_meta("es_testnet_name", resolved.testnet_name)
        if resolved.creds.api_key:
            repo.set_meta("es_api_key", resolved.creds.api_key)
        if resolved.creds.username:
            repo.set_meta("es_username", resolved.creds.username)
        if resolved.creds.password:
            repo.set_meta("es_password", resolved.creds.password)
        # Seed sync cursor to the initialisation time so we ignore historical
        # logs from before the release window.
        if repo.get_meta("sync_cursor") is None or args.reset:
            repo.set_meta("sync_cursor", args.since or now)

        inserted = 0
        skipped = 0
        for host, svc in pairs:
            existing = repo.get_node(host, svc, args.to_version)
            if existing is not None:
                skipped += 1
                continue
            node = NodeUpgrade(
                host=host,
                service_number=svc,
                from_version=args.from_version,
                to_version=args.to_version,
                running_version=args.from_version,
                status="pending",
                last_synced_at=now,
            )
            repo.insert_node(node)
            inserted += 1

    console.print(
        f"  [green]Inserted {inserted} row(s); skipped {skipped} existing."
        f"[/green]"
    )
    console.print()
    console.print("Next steps:")
    console.print(
        f"  saorsa-deploy track-upgrades sync --db {db_path} "
        f"--watch --interval 5m"
    )
    console.print(
        f"  saorsa-deploy track-upgrades summary --db {db_path}"
    )
