"""``clients select-hot-files`` / ``clients print-hot-files``.

Choosing the file a browser-downloader cohort hammers is an explicit operator
step, not a side effect of provisioning — see :mod:`saorsa_deploy.hot_files` for
why. These two commands are the whole interface: one writes the selection, the
other reads it back.
"""

from __future__ import annotations

import sys

from rich.console import Console
from rich.table import Table

from saorsa_deploy.hot_files import (
    HotFileError,
    hot_files_key,
    load_hot_files,
    save_hot_files,
    select_hot_files,
)
from saorsa_deploy.state import S3_ADDRESSES_BUCKET


def _print_document(console: Console, document: dict) -> None:
    table = Table(title=f"Hot files for {document.get('deployment', '?')}")
    table.add_column("index", justify="right")
    table.add_column("address")
    table.add_column("bytes", justify="right")
    table.add_column("source")
    for entry in document.get("files", []):
        table.add_row(
            str(entry.get("index")),
            str(entry.get("address")),
            f"{entry.get('bytes', 0):,}",
            f"{entry.get('source_key')}:{entry.get('line')}",
        )
    console.print(table)
    console.print(f"[dim]selected_at {document.get('selected_at')}[/dim]")


def cmd_select_hot_files(args):
    """Pick the cohort's hot files and write the manifest every instance reads."""
    console = Console()

    existing = load_hot_files(args.name)
    if existing is not None and not getattr(args, "replace", False):
        # Printed and left alone rather than refused: a re-run during a live
        # cohort is almost always someone checking, and silently re-selecting
        # would change what the next round reads mid-measurement.
        console.print(
            f"[yellow]'{args.name}' already has a hot-file selection — leaving "
            f"it alone. Pass --replace to overwrite it.[/yellow]"
        )
        _print_document(console, existing)
        return

    try:
        document = select_hot_files(args.name, count=args.count, size_mb=args.size_mb)
    except HotFileError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(2)

    save_hot_files(args.name, document)
    verb = "Replaced" if existing is not None else "Wrote"
    console.print(
        f"[bold green]{verb} the hot-file selection for {args.name}[/bold green] "
        f"(s3://{S3_ADDRESSES_BUCKET}/{hot_files_key(args.name)})"
    )
    _print_document(console, document)


def cmd_print_hot_files(args):
    """Print a deployment's hot-file selection; exit 2 when there is none."""
    console = Console()
    document = load_hot_files(args.name)
    if document is None:
        console.print(
            f"[bold red]Error:[/bold red] no hot-file selection for "
            f"'{args.name}' — s3://{S3_ADDRESSES_BUCKET}/"
            f"{hot_files_key(args.name)} does not exist. Run `clients "
            f"select-hot-files --name {args.name} --count N --size-mb N` once "
            f"the uploaders have published files of that size."
        )
        sys.exit(2)
    _print_document(console, document)
