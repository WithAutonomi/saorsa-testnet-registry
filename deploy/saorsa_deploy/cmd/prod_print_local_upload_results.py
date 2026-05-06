"""Print prod local-upload records from the registry database.

Sister of :mod:`saorsa_deploy.cmd.prod_print_upload_results`, but reads
directly from the ``deployment_prod_upload_results`` Postgres table
instead of querying Influx. ``prod local-uploads`` writes its results
straight to Postgres and never provisions a monitoring VM, so there is
no Influx backend to query.

Two output modes:

* default — one Rich table per host with truncated columns
* ``--details`` — one full record per upload, nothing truncated, suitable
  for grabbing addresses / reasons verbatim.
"""

from __future__ import annotations

import sys
from datetime import datetime

from rich.console import Console
from rich.table import Table
from saorsa_db.models import DeploymentProdUploadResult, ProdUploadStatus

from saorsa_deploy.registry import get_repository

_STATUS_LABELS = {
    ProdUploadStatus.SUCCESS: "[green]ok[/green]",
    ProdUploadStatus.PARTIAL_UPLOAD: "[red]partial[/red]",
    ProdUploadStatus.DOWNLOAD_FAILED: "[red]dl-fail[/red]",
}

_STATUS_TEXT = {
    ProdUploadStatus.SUCCESS: "ok",
    ProdUploadStatus.PARTIAL_UPLOAD: "partial",
    ProdUploadStatus.DOWNLOAD_FAILED: "dl-fail",
}


def cmd_prod_print_local_upload_results(args) -> None:
    console = Console()

    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set or the "
            "database is unreachable."
        )
        sys.exit(1)

    deployment = repo.get_active_by_name(args.name) or repo.get_by_name(args.name)
    if deployment is None:
        console.print(
            f"[bold red]Error:[/bold red] No deployment named '{args.name}' "
            "in the registry."
        )
        sys.exit(1)

    rows = repo.list_prod_upload_results_for_deployment(deployment.id)
    if not rows:
        console.print(
            f"[yellow]No upload results recorded for '{args.name}' "
            f"(deployment id={deployment.id}).[/yellow]"
        )
        return

    by_host: dict[str, list[DeploymentProdUploadResult]] = {}
    for row in rows:
        by_host.setdefault(row.client_hostname, []).append(row)

    if getattr(args, "details", False):
        _print_details(by_host, console)
    else:
        _print_table(by_host, console)


def _print_table(
    by_host: dict[str, list[DeploymentProdUploadResult]],
    console: Console,
) -> None:
    for host in sorted(by_host.keys()):
        rows = sorted(by_host[host], key=lambda r: r.started_at)
        table = Table(
            title=f"prod_upload — {host} ({_fmt_host_summary(rows)})",
            title_style="bold",
            show_lines=True,
        )
        table.add_column("#", justify="right")
        table.add_column("File")
        table.add_column("Size", justify="right")
        table.add_column("Status")
        table.add_column("Chunks Uploaded", justify="right")
        table.add_column("Failed", justify="right")
        table.add_column("Duration", justify="right")
        for i, row in enumerate(rows, start=1):
            table.add_row(
                str(i),
                row.file_name or "-",
                _fmt_size(row.file_size_bytes),
                _STATUS_LABELS.get(row.status, "-"),
                f"{row.chunks_stored or 0}/{row.total_chunks or 0}",
                str(row.chunks_failed or 0),
                _fmt_duration(row.duration_ms),
            )
        console.print(table)


def _print_details(
    by_host: dict[str, list[DeploymentProdUploadResult]],
    console: Console,
) -> None:
    for host in sorted(by_host.keys()):
        rows = sorted(by_host[host], key=lambda r: r.started_at)
        console.rule(f"[bold]{host} — {_fmt_host_summary(rows)}[/bold]")
        for i, row in enumerate(rows, start=1):
            console.print(f"[bold]#{i}[/bold]")
            console.print(f"  Started (UTC):     {_fmt_dt(row.started_at)}")
            console.print(f"  Finished (UTC):    {_fmt_dt(row.finished_at)}")
            console.print(f"  File:              {row.file_name or '-'}")
            console.print(f"  Source:            {row.source_url or '-'}")
            console.print(f"  Size (bytes):      {row.file_size_bytes or 0}")
            console.print(
                f"  Status:            {_STATUS_TEXT.get(row.status, '-')}"
            )
            console.print(
                f"  Chunks uploaded:   {row.chunks_stored or 0} / "
                f"{row.total_chunks or 0}"
            )
            console.print(f"  Chunks failed:     {row.chunks_failed or 0}")
            console.print(f"  Address:           {row.address or '-'}")
            console.print(f"  Duration (ms):     {row.duration_ms or 0}")
            console.print(f"  Upload secs:       {row.upload_secs or 0}")
            console.print(f"  Reason:            {row.reason or '-'}")
            console.print()


def _fmt_host_summary(rows: list[DeploymentProdUploadResult]) -> str:
    return f"{len(rows)} record(s)"


def _fmt_size(num_bytes) -> str:
    if num_bytes is None:
        return "-"
    gb = num_bytes / (1024 ** 3)
    mb = num_bytes / (1024 ** 2)
    kb = num_bytes / 1024
    if gb >= 1:
        return f"{gb:.2f} GB"
    if mb >= 1:
        return f"{mb:.1f} MB"
    if kb >= 1:
        return f"{kb:.1f} KB"
    return f"{num_bytes} B"


def _fmt_duration(ms) -> str:
    if ms is None:
        return "-"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes}m {remainder:.1f}s"


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


__all__ = ["cmd_prod_print_local_upload_results"]
