"""Helpers shared across track-upgrades subcommands."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import Console

from saorsa_deploy.state import load_elastic_state
from saorsa_deploy.upgrade_tracker.db import default_db_path
from saorsa_deploy.upgrade_tracker.es_client import ESCredentials


@dataclass
class ResolvedES:
    creds: ESCredentials
    index_pattern: str
    testnet_name: str


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_db_path(args, testnet_name: str) -> Path:
    if getattr(args, "db", None):
        return Path(args.db).expanduser()
    return default_db_path(testnet_name)


def resolve_es(
    args, console: Console, testnet_name_hint: Optional[str] = None,
) -> ResolvedES:
    """Resolve ES connection details from --name (elastic state) or flags."""
    name = getattr(args, "name", None)
    if name:
        try:
            state = load_elastic_state(name)
        except RuntimeError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)
        url = state.get("elasticsearch_url")
        username = state.get("elasticsearch_username")
        password = state.get("elasticsearch_password")
        api_key = state.get("elasticsearch_api_key")
        index_pattern = state.get(
            "elasticsearch_index_pattern",
            f"telegraf-ant-nodes-{name.lower()}-*",
        )
        if not url:
            console.print(
                f"[bold red]Error:[/bold red] Elasticsearch state for "
                f"'{name}' is missing elasticsearch_url."
            )
            sys.exit(1)
        # API key is cleaner for server-side queries (fewer privileges);
        # fall back to basic auth if needed.
        creds = ESCredentials(
            url=url,
            username=username,
            password=password,
            api_key=api_key,
        )
        # The index pattern in state uses %Y.%m.%d — for our search we want
        # a glob that matches all date-rotated indices.
        if "%Y" in index_pattern:
            index_pattern = index_pattern.replace("-%Y.%m.%d", "-*")
        return ResolvedES(
            creds=creds,
            index_pattern=index_pattern,
            testnet_name=testnet_name_hint or name,
        )

    url = getattr(args, "es_url", None)
    if not url:
        console.print(
            "[bold red]Error:[/bold red] provide --name to auto-load "
            "Elasticsearch credentials, or pass --es-url with "
            "--es-api-key or --es-username/--es-password."
        )
        sys.exit(1)
    creds = ESCredentials(
        url=url,
        username=getattr(args, "es_username", None),
        password=getattr(args, "es_password", None),
        api_key=getattr(args, "es_api_key", None),
    )
    testnet = testnet_name_hint or getattr(args, "testnet_name", None)
    if not testnet:
        console.print(
            "[bold red]Error:[/bold red] --testnet-name is required when "
            "using --es-url (it filters logs to the correct stream)."
        )
        sys.exit(1)
    index_pattern = getattr(
        args, "es_index_pattern", f"telegraf-ant-nodes-{testnet.lower()}-*"
    )
    return ResolvedES(
        creds=creds, index_pattern=index_pattern, testnet_name=testnet,
    )
