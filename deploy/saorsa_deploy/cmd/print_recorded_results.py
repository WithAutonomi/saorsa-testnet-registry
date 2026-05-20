"""Print recorded results for a deployment from the registry database.

Reads from Postgres (no Influx), so it keeps working after monitoring
infrastructure has been torn down. Renders, in order:

* Header — deployment name, id, kind.
* Analysis — freeform summary text (if any) and per-host CPU / memory /
  bandwidth aggregates from ``deployment_host_results``.
* File results — kind-dispatched:

  - testnet, ``prod-continuous-downloader`` (and unrecognised/empty
    kinds) → per-host aggregates from ``deployment_file_results``.
  - ``prod-uploaders``, ``prod-local-uploads`` → per-file rows from
    ``deployment_prod_upload_results``. Default mode is one Rich table
    per host; ``--details`` prints one full record per upload with no
    truncation.

If a section's table is empty, a friendly "nothing recorded" line is
printed in its place rather than skipped silently — the absence is
itself useful information.
"""
from __future__ import annotations

import sys
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from saorsa_db.models import (
    DeploymentDownloadRecord,
    DeploymentFileResult,
    DeploymentHostResult,
    DeploymentProdUploadResult,
    DeploymentUploadRecord,
    OpRecordStatus,
    ProdUploadStatus,
)

from saorsa_deploy.registry import get_repository

_TESTNET_KIND = "testnet"
_FILE_OPS_KINDS = {"", _TESTNET_KIND, "prod-continuous-downloader"}
_PROD_UPLOAD_KINDS = {"prod-uploaders", "prod-local-uploads"}

_PROD_STATUS_LABELS = {
    ProdUploadStatus.SUCCESS: "[green]ok[/green]",
    ProdUploadStatus.PARTIAL_UPLOAD: "[red]partial[/red]",
    ProdUploadStatus.DOWNLOAD_FAILED: "[red]dl-fail[/red]",
}

_PROD_STATUS_TEXT = {
    ProdUploadStatus.SUCCESS: "ok",
    ProdUploadStatus.PARTIAL_UPLOAD: "partial",
    ProdUploadStatus.DOWNLOAD_FAILED: "dl-fail",
}


def cmd_print_recorded_results(args) -> None:
    console = Console()

    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set or the "
            "database is unreachable."
        )
        sys.exit(1)

    deployment = repo.get(args.deployment_id)
    if deployment is None:
        console.print(
            f"[bold red]Error:[/bold red] No deployment with id="
            f"{args.deployment_id} in the registry."
        )
        sys.exit(1)

    kind = _resolve_kind(deployment.deploy_args)
    console.rule(
        f"[bold]Recorded results — {deployment.name} "
        f"(id={deployment.id}, kind={kind or 'unknown'})[/bold]"
    )

    host_result = repo.get_host_result_for_deployment(deployment.id)
    _print_analysis_section(host_result, deployment.name, console)

    if kind == "prod-canary":
        from saorsa_deploy.cmd.prod_canary_results import (
            print_prod_canary_status,
        )
        print_prod_canary_status(repo, deployment, args, console)
    elif kind in _PROD_UPLOAD_KINDS:
        rows = repo.list_prod_upload_results_for_deployment(deployment.id)
        _print_prod_upload_section(rows, deployment.name, args.details, console)
    else:
        rows = repo.list_file_results_for_deployment(deployment.id)
        _print_file_results_section(rows, deployment.name, console)
        if args.details:
            upload_records = repo.list_upload_records_for_deployment(deployment.id)
            download_records = repo.list_download_records_for_deployment(
                deployment.id
            )
            _print_op_records_section(
                upload_records, download_records, deployment.name, console
            )


def _resolve_kind(deploy_args) -> str:
    if not isinstance(deploy_args, dict):
        return ""
    explicit = deploy_args.get("kind")
    if explicit:
        return explicit
    if "bootstrap_count" in deploy_args:
        return _TESTNET_KIND
    return ""


# ---------------------------------------------------------------------------
# Analysis section (deployment_host_results)
# ---------------------------------------------------------------------------


def _print_analysis_section(
    host_result: DeploymentHostResult | None,
    name: str,
    console: Console,
) -> None:
    console.print()
    console.print("[bold]Analysis[/bold]")

    if host_result is None:
        console.print(
            "[yellow]No analysis recorded for this deployment "
            "(deployment_host_results is empty).[/yellow]"
        )
        return

    if host_result.summary:
        console.print(
            Panel(
                host_result.summary,
                title=f"Summary for '{name}'",
                border_style="cyan",
            )
        )
    else:
        console.print(
            "[dim](no freeform summary attached — set one with "
            "`deployments edit-analysis-summary`)[/dim]"
        )

    table = Table(
        title=(
            f"Host metrics — recorded {_fmt_dt(host_result.recorded_at)} "
            f"(window {_fmt_dt(host_result.metrics_started_at)} → "
            f"{_fmt_dt(host_result.metrics_ended_at)})"
        ),
        title_style="bold",
    )
    table.add_column("Host")
    table.add_column("CPU avg/min/max %", justify="right")
    table.add_column("Mem avg/min/max %", justify="right")
    table.add_column("In avg/min/max", justify="right")
    table.add_column("Out avg/min/max", justify="right")
    table.add_column("Started (UTC)")
    table.add_column("Ended (UTC)")

    metrics = host_result.host_metrics or []
    if not metrics:
        console.print(
            "[yellow]Host result row exists but contains no per-host "
            "metrics.[/yellow]"
        )
        return

    for entry in metrics:
        cpu = entry.get("cpu_percent") or {}
        mem = entry.get("mem_percent") or {}
        bin_ = entry.get("bytes_in_per_sec") or {}
        bout = entry.get("bytes_out_per_sec") or {}
        table.add_row(
            entry.get("host") or "-",
            _fmt_triple(cpu),
            _fmt_triple(mem),
            _fmt_triple_bw(bin_),
            _fmt_triple_bw(bout),
            _fmt_iso(entry.get("metrics_started_at")),
            _fmt_iso(entry.get("metrics_ended_at")),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# File results section: testnet / prod-continuous-downloader
# ---------------------------------------------------------------------------


def _print_file_results_section(
    rows: list[DeploymentFileResult],
    name: str,
    console: Console,
) -> None:
    console.print()
    console.print("[bold]File results (per-host aggregates)[/bold]")

    if not rows:
        console.print(
            "[yellow]No file results recorded for this deployment "
            "(deployment_file_results is empty).[/yellow]"
        )
        return

    table = Table(
        title=f"File results for '{name}'",
        title_style="bold",
    )
    table.add_column("Host")
    table.add_column("Op")
    table.add_column("Size (MB)", justify="right")
    table.add_column("OK", justify="right")
    table.add_column("Fail", justify="right")
    table.add_column("Avg ms", justify="right")
    table.add_column("Min ms", justify="right")
    table.add_column("Max ms", justify="right")
    table.add_column("First start (UTC)")
    table.add_column("Last end (UTC)")
    for row in rows:
        table.add_row(
            row.client_hostname,
            row.op_type.value,
            f"{row.file_size_bytes / (1024 * 1024):.1f}",
            str(row.success_count),
            str(row.failure_count),
            "-" if row.avg_duration_ms is None else str(row.avg_duration_ms),
            "-" if row.min_duration_ms is None else str(row.min_duration_ms),
            "-" if row.max_duration_ms is None else str(row.max_duration_ms),
            _fmt_dt(row.first_started_at),
            _fmt_dt(row.last_finished_at),
        )
    console.print(table)


_OP_STATUS_LABELS = {
    OpRecordStatus.SUCCESS: "[green]ok[/green]",
    OpRecordStatus.FAILURE: "[red]fail[/red]",
}


def _print_op_records_section(
    uploads: list[DeploymentUploadRecord],
    downloads: list[DeploymentDownloadRecord],
    name: str,
    console: Console,
) -> None:
    """Render every per-op row from ``deployment_upload_records`` and
    ``deployment_download_records``, grouped by (op_type, host)."""
    console.print()
    console.print("[bold]File results (per-operation rows)[/bold]")

    if not uploads and not downloads:
        console.print(
            "[yellow]No per-op records for this deployment "
            "(deployment_upload_records and deployment_download_records "
            "are empty).[/yellow]"
        )
        return

    if uploads:
        _print_op_records_for_kind("upload", uploads, name, console)
    if downloads:
        _print_op_records_for_kind("download", downloads, name, console)


def _print_op_records_for_kind(
    op_label: str,
    rows: list,
    name: str,
    console: Console,
) -> None:
    by_host: dict[str, list] = {}
    for row in rows:
        by_host.setdefault(row.client_hostname, []).append(row)

    for host in sorted(by_host.keys()):
        host_rows = sorted(by_host[host], key=lambda r: r.start_ns)
        table = Table(
            title=(
                f"{op_label} — {host} ({len(host_rows)} record(s)) — '{name}'"
            ),
            title_style="bold",
            show_lines=False,
        )
        table.add_column("#", justify="right")
        table.add_column("Region")
        table.add_column("Size (MB)", justify="right")
        table.add_column("Status")
        table.add_column("Duration", justify="right")
        table.add_column("Started (UTC)")
        table.add_column("Finished (UTC)")
        for i, row in enumerate(host_rows, start=1):
            size_mb = (
                f"{row.file_size_bytes / (1024 * 1024):.1f}"
                if row.file_size_bytes is not None
                else "-"
            )
            table.add_row(
                str(i),
                row.region or "-",
                size_mb,
                _OP_STATUS_LABELS.get(row.status, "-"),
                _fmt_duration(row.duration_ms),
                _fmt_dt(row.started_at),
                _fmt_dt(row.finished_at),
            )
        console.print(table)


# ---------------------------------------------------------------------------
# File results section: prod-uploaders / prod-local-uploads
# ---------------------------------------------------------------------------


def _print_prod_upload_section(
    rows: list[DeploymentProdUploadResult],
    name: str,
    details: bool,
    console: Console,
) -> None:
    console.print()
    console.print("[bold]Prod upload results (per-file rows)[/bold]")

    if not rows:
        console.print(
            "[yellow]No prod upload results recorded for this deployment "
            "(deployment_prod_upload_results is empty).[/yellow]"
        )
        return

    by_host: dict[str, list[DeploymentProdUploadResult]] = {}
    for row in rows:
        by_host.setdefault(row.client_hostname, []).append(row)

    if details:
        _print_prod_upload_details(by_host, console)
    else:
        _print_prod_upload_tables(by_host, console)


def _print_prod_upload_tables(
    by_host: dict[str, list[DeploymentProdUploadResult]],
    console: Console,
) -> None:
    for host in sorted(by_host.keys()):
        rows = sorted(by_host[host], key=lambda r: r.started_at)
        table = Table(
            title=f"prod_upload — {host} ({len(rows)} record(s))",
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
                _PROD_STATUS_LABELS.get(row.status, "-"),
                f"{row.chunks_stored or 0}/{row.total_chunks or 0}",
                str(row.chunks_failed or 0),
                _fmt_duration(row.duration_ms),
            )
        console.print(table)


def _print_prod_upload_details(
    by_host: dict[str, list[DeploymentProdUploadResult]],
    console: Console,
) -> None:
    for host in sorted(by_host.keys()):
        rows = sorted(by_host[host], key=lambda r: r.started_at)
        console.rule(f"[bold]{host} — {len(rows)} record(s)[/bold]")
        for i, row in enumerate(rows, start=1):
            console.print(f"[bold]#{i}[/bold]")
            console.print(f"  Started (UTC):     {_fmt_dt(row.started_at)}")
            console.print(f"  Finished (UTC):    {_fmt_dt(row.finished_at)}")
            console.print(f"  File:              {row.file_name or '-'}")
            console.print(f"  Source:            {row.source_url or '-'}")
            console.print(f"  Size (bytes):      {row.file_size_bytes or 0}")
            console.print(
                f"  Status:            {_PROD_STATUS_TEXT.get(row.status, '-')}"
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


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_triple(stat: dict) -> str:
    return (
        f"{_fmt_num(stat.get('avg'))} / "
        f"{_fmt_num(stat.get('min'))} / "
        f"{_fmt_num(stat.get('max'))}"
    )


def _fmt_triple_bw(stat: dict) -> str:
    return (
        f"{_fmt_bw(stat.get('avg'))} / "
        f"{_fmt_bw(stat.get('min'))} / "
        f"{_fmt_bw(stat.get('max'))}"
    )


def _fmt_num(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def _fmt_bw(value) -> str:
    if value is None:
        return "-"
    mb = value / (1024 * 1024)
    if mb >= 1:
        return f"{mb:.2f} MB/s"
    kb = value / 1024
    return f"{kb:.1f} KB/s"


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


def _fmt_iso(value) -> str:
    """Render an ISO-8601 string (as stored in host_metrics JSON)."""
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(value)


__all__ = ["cmd_print_recorded_results"]
