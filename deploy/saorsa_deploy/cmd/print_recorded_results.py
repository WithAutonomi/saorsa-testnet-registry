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
  - ``prod-local-downloads`` → per-address rows from
    ``deployment_prod_download_results``. Default mode is one Rich table
    per host; ``--details`` prints one full record per download.

If a section's table is empty, a friendly "nothing recorded" line is
printed in its place rather than skipped silently — the absence is
itself useful information.
"""
from __future__ import annotations

import sys
from datetime import datetime
from decimal import Decimal

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from saorsa_db.models import (
    DeploymentDownloadRecord,
    DeploymentFileResult,
    DeploymentHostResult,
    DeploymentProdDownloadResult,
    DeploymentProdUploadResult,
    DeploymentUploadRecord,
    OpRecordStatus,
    ProdDownloadStatus,
    ProdUploadStatus,
)

from saorsa_deploy.derived_metrics import format_metric_value
from saorsa_deploy.registry import get_repository

_TESTNET_KIND = "testnet"
_FILE_OPS_KINDS = {
    "",
    _TESTNET_KIND,
    "prod-continuous-downloader",
    "prod-downloaders",
}
_PROD_UPLOAD_KINDS = {"prod-uploaders", "prod-local-uploads"}
_PROD_DOWNLOAD_KINDS = {"prod-local-downloads"}

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

_PROD_DOWNLOAD_STATUS_LABELS = {
    ProdDownloadStatus.SUCCESS: "[green]ok[/green]",
    ProdDownloadStatus.FAILED: "[red]fail[/red]",
}

_PROD_DOWNLOAD_STATUS_TEXT = {
    ProdDownloadStatus.SUCCESS: "ok",
    ProdDownloadStatus.FAILED: "fail",
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
    _print_derived_metrics_section(
        repo.list_derived_metrics_for_deployment(deployment.id), console
    )

    if kind == "prod-canary":
        from saorsa_deploy.cmd.prod_canary_results import (
            print_prod_canary_status,
        )
        print_prod_canary_status(repo, deployment, args, console)
    elif kind == "prod-continuous-uploader":
        rows = repo.list_prod_upload_results_for_deployment(deployment.id)
        _print_prod_upload_section(rows, deployment.name, args.details, console)
        _print_verification_section(
            repo.list_prod_upload_verifications_for_deployment(deployment.id),
            deployment.name,
            args.details,
            console,
        )
    elif kind in _PROD_UPLOAD_KINDS:
        rows = repo.list_prod_upload_results_for_deployment(deployment.id)
        _print_prod_upload_section(rows, deployment.name, args.details, console)
    elif kind in _PROD_DOWNLOAD_KINDS:
        rows = repo.list_prod_download_results_for_deployment(deployment.id)
        _print_prod_download_section(rows, deployment.name, args.details, console)
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

    _print_disk_io_table(metrics, console)
    _print_disk_usage_table(metrics, console)
    _print_rss_table(metrics, console)


def _print_disk_usage_table(metrics: list[dict], console: Console) -> None:
    """Render filesystem occupancy, one row per (host, mountpoint).

    Distinct from the disk I/O table above, and not a duplicate of it: I/O
    counts bytes written, which includes rewrites and compaction, while this is
    how much data is actually resident. Only the latter can answer "disk
    consumed per GB stored".

    `Growth` is `last - first` across the run, and is the figure that feeds the
    derived metric. `Peak spread` (max - min) is shown alongside because a run
    whose store was pruned mid-window has a growth well below its spread, and
    that difference is the only way to tell the two apart afterwards.

    Skipped entirely for runs recorded before occupancy capture existed — which
    is every run before this landed, since `[[inputs.disk]]` was collected by
    Telegraf but never queried. No backfill is possible.
    """
    if not any(entry.get("disk_usage_by_mount") for entry in metrics):
        return

    table = Table(
        title="Disk usage — per mountpoint",
        title_style="bold",
    )
    table.add_column("Host")
    table.add_column("Mount")
    table.add_column("Role")
    table.add_column("First", justify="right")
    table.add_column("Last", justify="right")
    table.add_column("Growth", justify="right")
    table.add_column("Peak spread", justify="right")

    for entry in metrics:
        mounts = entry.get("disk_usage_by_mount") or {}
        host = entry.get("host") or "-"
        for mount, stats in sorted(mounts.items()):
            used = (stats or {}).get("used_bytes") or {}
            role = (stats or {}).get("volume_role")
            minimum, maximum = used.get("min"), used.get("max")
            spread = (
                maximum - minimum
                if minimum is not None and maximum is not None
                else None
            )
            table.add_row(
                host,
                mount,
                f"[bold]{role}[/bold]" if role else "-",
                _fmt_bytes(used.get("first")),
                _fmt_bytes(used.get("last")),
                _fmt_bytes(used.get("growth_bytes")),
                _fmt_bytes(spread),
            )
    console.print(table)


def _print_derived_metrics_section(rows: list, console: Console) -> None:
    """Render the normalised per-run metrics, grouped by window.

    Several windows can coexist for one run — the whole run captured exactly at
    extraction time, plus any phase re-derived from buckets afterwards — so the
    window and its source are columns rather than a heading: comparing a
    `capture` value against a `buckets` value is precisely the mistake this
    display should make hard.

    Absent for every run recorded before this landed, and for any run whose
    denominators were never recorded. Nothing is printed in that case.
    """
    if not rows:
        return

    console.print()
    console.print("[bold]Derived per-run metrics[/bold]")

    table = Table(title="Normalised metrics", title_style="bold")
    table.add_column("Window")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_column("Numerator", justify="right")
    table.add_column("Denominator", justify="right")
    table.add_column("Source")
    table.add_column("Window start (UTC)")
    table.add_column("Window end (UTC)")

    for row in rows:
        table.add_row(
            row.window_label,
            row.metric,
            format_metric_value(row.value, row.unit),
            _fmt_bytes(row.numerator) if row.numerator is not None else "-",
            f"{row.denominator:.2f} GB" if row.denominator is not None else "-",
            row.source,
            _fmt_dt(row.window_start),
            _fmt_dt(row.window_end),
        )
    console.print(table)


def _print_disk_io_table(metrics: list[dict], console: Console) -> None:
    """Render disk I/O as its own table, one row per (host, device).

    Split out from the main host-metrics table rather than adding columns to
    it, which would push that table past a readable width — and it needs its
    own rows anyway, since a host has several devices.

    Deliberately per-device: a node's root disk and its storage volume are not
    interchangeable, so there is no host-level average here. The storage volume
    is flagged in the Role column; `record-analysis`'s own summary table shows
    that device alone.

    Rows recorded before disk I/O collection existed have no
    `disk_io_by_device` key, so the section is skipped for them — no migration,
    no placeholder rows.
    """
    if not any(entry.get("disk_io_by_device") for entry in metrics):
        return

    table = Table(
        title="Disk I/O — per device",
        title_style="bold",
    )
    table.add_column("Host")
    table.add_column("Device")
    table.add_column("Role")
    table.add_column("Read avg/min/max", justify="right")
    table.add_column("Write avg/min/max", justify="right")
    table.add_column("Read IOPS avg/min/max", justify="right")
    table.add_column("Write IOPS avg/min/max", justify="right")

    for entry in metrics:
        devices = entry.get("disk_io_by_device") or {}
        host = entry.get("host") or "-"
        for device, stats in sorted(devices.items()):
            role = stats.get("volume_role")
            table.add_row(
                host,
                device,
                # Highlighted because this is the device that matters for a
                # node; the others are context.
                f"[bold]{role}[/bold]" if role else "-",
                _fmt_triple_bw(stats.get("read_bytes_per_sec") or {}),
                _fmt_triple_bw(stats.get("write_bytes_per_sec") or {}),
                _fmt_triple(stats.get("reads_per_sec") or {}),
                _fmt_triple(stats.get("writes_per_sec") or {}),
            )
    console.print(table)


def _print_rss_table(metrics: list[dict], console: Console) -> None:
    """Render per-service RSS as its own table, one row per (host, service).

    Its own rows rather than columns on the main table for the same reason as
    disk I/O: a node VM runs 15 services, and each needs a row.

    Deliberately not summed or averaged into a host total. The signal worth
    catching is a *single* service growing without bound, which a 15-service
    mean would dilute fifteenfold and a host total would hide behind normal
    fleet variation. `mem_percent` on the main table is not a substitute
    either — that is whole-host memory including page cache, so it cannot
    separate a genuine ant-node leak from cache growth, nor attribute anything
    to one service.

    Rows recorded before RSS capture existed have no `rss_by_service` key, so
    the section is skipped for them — the same degrade-to-nothing pattern
    `disk_io_by_device` already relies on. No backfill is possible: the source
    InfluxDB for those runs is destroyed.
    """
    if not any(entry.get("rss_by_service") for entry in metrics):
        return

    table = Table(
        title="Node service memory (RSS) — per service",
        title_style="bold",
    )
    table.add_column("Host")
    table.add_column("Service")
    table.add_column("RSS avg/min/max", justify="right")
    table.add_column("Samples", justify="right")

    for entry in metrics:
        services = entry.get("rss_by_service") or {}
        host = entry.get("host") or "-"
        for service, stats in sorted(services.items(), key=_service_sort_key):
            table.add_row(
                host,
                service,
                _fmt_triple_bytes(stats or {}),
                str((stats or {}).get("samples") or 0),
            )
    console.print(table)


def _service_sort_key(item: tuple[str, dict]) -> tuple[str, int, str]:
    """Sort `ant-node-2` before `ant-node-10`, which a plain sort would not."""
    name = item[0]
    prefix, _, suffix = name.rpartition("-")
    if prefix and suffix.isdigit():
        return (prefix, int(suffix), "")
    return (name, 0, "")


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
    grand_ant = Decimal(0)
    grand_gas = Decimal(0)
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
        table.add_column("ANT cost", justify="right")
        table.add_column("ETH gas", justify="right")
        host_ant = Decimal(0)
        host_gas = Decimal(0)
        for i, row in enumerate(rows, start=1):
            if row.storage_cost_atto is not None:
                host_ant += row.storage_cost_atto
            if row.gas_cost_wei is not None:
                host_gas += row.gas_cost_wei
            table.add_row(
                str(i),
                row.file_name or "-",
                _fmt_size(row.file_size_bytes),
                _PROD_STATUS_LABELS.get(row.status, "-"),
                f"{row.chunks_stored or 0}/{row.total_chunks or 0}",
                str(row.chunks_failed or 0),
                _fmt_duration(row.duration_ms),
                _fmt_token(row.storage_cost_atto, "ANT"),
                _fmt_token(row.gas_cost_wei, "ETH"),
            )
        table.add_section()
        table.add_row(
            "", "[bold]Total[/bold]", "", "", "", "", "",
            f"[bold]{_fmt_token(host_ant, 'ANT')}[/bold]",
            f"[bold]{_fmt_token(host_gas, 'ETH')}[/bold]",
        )
        console.print(table)
        grand_ant += host_ant
        grand_gas += host_gas

    if len(by_host) > 1:
        console.print(
            f"[bold]Run total:[/bold] {_fmt_token(grand_ant, 'ANT')} storage / "
            f"{_fmt_token(grand_gas, 'ETH')} gas"
        )


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
            console.print(
                f"  Storage cost:      {_fmt_token(row.storage_cost_atto, 'ANT')}"
                f"  ({row.storage_cost_atto if row.storage_cost_atto is not None else '-'} atto)"
            )
            console.print(
                f"  Gas cost:          {_fmt_token(row.gas_cost_wei, 'ETH')}"
                f"  ({row.gas_cost_wei if row.gas_cost_wei is not None else '-'} wei)"
            )
            console.print(f"  Reason:            {row.reason or '-'}")
            console.print()


# ---------------------------------------------------------------------------
# File results section: prod-local-downloads
# ---------------------------------------------------------------------------


def _print_prod_download_section(
    rows: list[DeploymentProdDownloadResult],
    name: str,
    details: bool,
    console: Console,
) -> None:
    console.print()
    console.print("[bold]Prod download results (per-address rows)[/bold]")

    if not rows:
        console.print(
            "[yellow]No prod download results recorded for this deployment "
            "(deployment_prod_download_results is empty).[/yellow]"
        )
        return

    by_host: dict[str, list[DeploymentProdDownloadResult]] = {}
    for row in rows:
        by_host.setdefault(row.client_hostname, []).append(row)

    if details:
        _print_prod_download_details(by_host, console)
    else:
        _print_prod_download_tables(by_host, console)


def _print_prod_download_tables(
    by_host: dict[str, list[DeploymentProdDownloadResult]],
    console: Console,
) -> None:
    for host in sorted(by_host.keys()):
        rows = sorted(by_host[host], key=lambda r: r.started_at)
        table = Table(
            title=f"prod_download — {host} ({len(rows)} record(s))",
            title_style="bold",
            show_lines=True,
        )
        table.add_column("#", justify="right")
        table.add_column("Address")
        table.add_column("Status")
        table.add_column("Size", justify="right")
        table.add_column("Duration", justify="right")
        table.add_column("Started (UTC)")
        for i, row in enumerate(rows, start=1):
            table.add_row(
                str(i),
                row.address,
                _PROD_DOWNLOAD_STATUS_LABELS.get(row.status, "-"),
                _fmt_size(row.file_size_bytes),
                _fmt_duration(row.duration_ms),
                _fmt_dt(row.started_at),
            )
        console.print(table)


def _print_prod_download_details(
    by_host: dict[str, list[DeploymentProdDownloadResult]],
    console: Console,
) -> None:
    for host in sorted(by_host.keys()):
        rows = sorted(by_host[host], key=lambda r: r.started_at)
        console.rule(f"[bold]{host} — {len(rows)} record(s)[/bold]")
        for i, row in enumerate(rows, start=1):
            console.print(f"[bold]#{i}[/bold]")
            console.print(f"  Started (UTC):     {_fmt_dt(row.started_at)}")
            console.print(f"  Finished (UTC):    {_fmt_dt(row.finished_at)}")
            console.print(f"  Address:           {row.address}")
            console.print(
                f"  Status:            "
                f"{_PROD_DOWNLOAD_STATUS_TEXT.get(row.status, '-')}"
            )
            console.print(f"  Size (bytes):      {row.file_size_bytes or '-'}")
            console.print(f"  Duration (ms):     {row.duration_ms or '-'}")
            console.print(f"  Download secs:     {row.download_secs or '-'}")
            console.print(f"  Output path:       {row.output_path or '-'}")
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


def _fmt_triple_bytes(stat: dict) -> str:
    return (
        f"{_fmt_bytes(stat.get('avg'))} / "
        f"{_fmt_bytes(stat.get('min'))} / "
        f"{_fmt_bytes(stat.get('max'))}"
    )


def _fmt_bytes(value) -> str:
    """Format a byte count. Distinct from `_fmt_bw`, which is a per-second rate."""
    if value is None:
        return "-"
    gb = value / (1024**3)
    if gb >= 1:
        return f"{gb:.2f} GB"
    return f"{value / (1024 * 1024):.0f} MB"


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


_ATTO_PER_UNIT = Decimal(10) ** 18


def _fmt_token(value, symbol: str) -> str:
    """Render an atto/wei amount (10^-18) as a human-readable token value.

    Storage cost is in atto-ANT and gas in wei; both divide by 10^18 to give
    ANT / ETH. Uses ``Decimal`` so we never lose precision, and fixed-point
    formatting (never scientific) with trailing zeros stripped.
    """
    if value is None:
        return "-"
    amount = (Decimal(value) / _ATTO_PER_UNIT).normalize()
    return f"{amount:f} {symbol}"


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


# ---------------------------------------------------------------------------
# Read-back verification section: prod-continuous-uploader
# ---------------------------------------------------------------------------


def _print_verification_section(rows, name: str, details: bool, console) -> None:
    """Per-region read-back results for the daily write test.

    Broken out by region rather than pooled: the whole design has each region
    check every address independently, so a file that verifies in one region
    and not another is the single most interesting thing this test can find,
    and a pooled total would hide it.
    """
    from collections import defaultdict

    from saorsa_db.models import ProdUploadVerifyStatus

    console.print()
    console.print("[bold]Read-back verification (per region)[/bold]")
    if not rows:
        console.print(
            "[yellow]No verifications recorded yet. The verifiers check on "
            "their own cycle, so a freshly-deployed run has nothing here "
            "until the first one completes.[/yellow]"
        )
        return

    by_host = defaultdict(list)
    for row in rows:
        by_host[(row.client_hostname, row.region)].append(row)

    table = Table(show_header=True, header_style="bold")
    table.add_column("Host")
    table.add_column("Region")
    table.add_column("Checked", justify="right")
    table.add_column("OK", justify="right")
    table.add_column("Mismatch", justify="right")
    table.add_column("Failed", justify="right")
    table.add_column("Inconclusive", justify="right")
    table.add_column("Median age", justify="right")

    total_mismatch = 0
    for (host, region), host_rows in sorted(by_host.items()):
        counts = defaultdict(int)
        for row in host_rows:
            counts[row.status] += 1
        mismatches = counts[ProdUploadVerifyStatus.DIGEST_MISMATCH]
        total_mismatch += mismatches
        ages = sorted(r.age_at_check_secs or 0 for r in host_rows)
        median_age = ages[len(ages) // 2] if ages else 0
        table.add_row(
            host,
            region,
            str(len(host_rows)),
            f"[green]{counts[ProdUploadVerifyStatus.OK]}[/green]",
            f"[bold red]{mismatches}[/bold red]" if mismatches else "0",
            str(counts[ProdUploadVerifyStatus.DOWNLOAD_FAILED]),
            str(counts[ProdUploadVerifyStatus.INCONCLUSIVE]),
            _format_age(median_age),
        )
    console.print(table)

    if total_mismatch:
        # Louder than a table cell: this is the only signal here that means the
        # network returned data and the data was wrong.
        console.print(
            f"[bold red]{total_mismatch} content mismatch(es) — the network "
            "returned bytes that differ from what was uploaded.[/bold red]"
        )

    if details:
        detail = Table(show_header=True, header_style="bold")
        detail.add_column("Region")
        detail.add_column("File")
        detail.add_column("Address")
        detail.add_column("Status")
        detail.add_column("Age", justify="right")
        detail.add_column("Reason")
        for row in rows:
            detail.add_row(
                row.region,
                row.file_name[:40],
                (row.address or "")[:16] + "…",
                _verify_status_label(row.status),
                _format_age(row.age_at_check_secs or 0),
                (row.reason or "")[:60],
            )
        console.print(detail)


def _verify_status_label(status) -> str:
    from saorsa_db.models import ProdUploadVerifyStatus

    return {
        ProdUploadVerifyStatus.OK: "[green]ok[/green]",
        ProdUploadVerifyStatus.DIGEST_MISMATCH: "[bold red]MISMATCH[/bold red]",
        ProdUploadVerifyStatus.DOWNLOAD_FAILED: "[red]failed[/red]",
        ProdUploadVerifyStatus.INCONCLUSIVE: "[yellow]inconclusive[/yellow]",
    }.get(status, str(status))


def _format_age(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"
