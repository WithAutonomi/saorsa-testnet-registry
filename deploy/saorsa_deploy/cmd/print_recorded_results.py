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
    VERIFY_MISMATCH,
    VERIFY_OK,
    VERIFY_UNAVAILABLE,
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

# `src-fail`, not `dl-fail`: status 2 means the uploader could not fetch
# the SOURCE file from its origin (an Internet Archive URL, typically),
# so no upload was ever attempted. In a deployment that also downloads
# its uploads back to verify them, "dl-fail" reads as "the read-back
# failed" — i.e. as a production fault — when it is the opposite: a
# problem fetching our own test material, saying nothing about the
# network.
# `failed` and `over-cap` are browser-path only. `failed` is deliberately NOT
# spelled "partial": it means no payment went out, so nothing is stored — the
# opposite of a partial upload, and the difference decides whether the file's
# pool entry is spent. `over-cap` is not a failure at all: the transport refuses
# the file and never asks the network.
_PROD_STATUS_LABELS = {
    ProdUploadStatus.SUCCESS: "[green]ok[/green]",
    ProdUploadStatus.PARTIAL_UPLOAD: "[red]partial[/red]",
    ProdUploadStatus.DOWNLOAD_FAILED: "[yellow]src-fail[/yellow]",
    ProdUploadStatus.FAILED: "[red]failed[/red]",
    ProdUploadStatus.OVER_CAP: "[yellow]over-cap[/yellow]",
}

_PROD_STATUS_TEXT = {
    ProdUploadStatus.SUCCESS: "ok",
    ProdUploadStatus.PARTIAL_UPLOAD: "partial",
    ProdUploadStatus.DOWNLOAD_FAILED: "src-fail",
    ProdUploadStatus.FAILED: "failed",
    ProdUploadStatus.OVER_CAP: "over-cap",
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
            "[bold red]Error:[/bold red] DATABASE_URL is not set or the database is unreachable."
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
    _print_derived_metrics_section(repo.list_derived_metrics_for_deployment(deployment.id), console)

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
        upload_records = repo.list_upload_records_for_deployment(deployment.id)
        download_records = repo.list_download_records_for_deployment(deployment.id)
        if args.details:
            _print_op_records_section(upload_records, download_records, deployment.name, console)
        else:
            # The per-ROW dump stays behind --details, but the counts do not:
            # `deployment_file_results` has no skipped column, so a group that is
            # entirely skips renders as "0 OK / 0 Fail" — which reads as though
            # nothing happened, the opposite of the truth. These are a handful of
            # counts, and they print nothing at all for a run with no WASM rows.
            _print_transport_totals(upload_records, download_records, console)
        # The browser tier is neither an upload nor a download record, so it is
        # its own section — and it prints nothing at all for the runs that have
        # no browser rounds, which is every run before V2-1617.
        _print_browser_downloads_section(
            repo.list_browser_download_records_for_deployment(deployment.id),
            deployment.name,
            console,
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
        console.print("[yellow]Host result row exists but contains no per-host metrics.[/yellow]")
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
            spread = maximum - minimum if minimum is not None and maximum is not None else None
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
    # The transport refused the file (over ant-core's browser cap), the network
    # did not. Its own word and its own colour, because rendering it as a
    # failure reports a transport limitation as a network fault.
    OpRecordStatus.SKIPPED: "[yellow]skip[/yellow]",
}

#: Content verification (V2-757). NULL renders as `-` and covers three
#: different absences -- a failed download, a line carrying no published hash,
#: and a row predating verification -- because none of them is a finding. A
#: MISMATCH is, and is the loudest thing in the table.
_VERIFY_LABELS = {
    VERIFY_OK: "[green]ok[/green]",
    VERIFY_MISMATCH: "[bold red]MISMATCH[/bold red]",
    VERIFY_UNAVAILABLE: "[yellow]n/a[/yellow]",
}


def _client_builds(rows: list) -> list:
    """The distinct client builds in the rows, in first-seen order.

    ``None`` is a build like any other here: it is what a native row carries,
    and what a browser-path row carries when its bundle predates build
    provenance. Collapsing it into one of the real builds would put transfers
    from an unknown binary into a bucket labelled with a commit.
    """
    seen: list = []
    for row in rows:
        build = getattr(row, "client_build", None)
        if build not in seen:
            seen.append(build)
    return seen


def _split_by_build(rows: list) -> list[tuple[str | None, list]]:
    """Group rows by the client build that performed them.

    Returns a single group labelled ``None`` — meaning "do not split" — when a
    deployment carries only one build or none, so every native run and every
    run that has never been upgraded renders exactly as it did before.

    The split exists for `deployments upgrade-wasm-client`, which replaces the
    WASM bundle underneath a permanently-running deployment. The whole reason
    to upgrade in place is to compare what the deployment did before with what
    it does after; one blended table answers that with an average of the two.
    """
    builds = _client_builds(rows)
    if len(builds) <= 1:
        return [(None, list(rows))]
    return [
        (
            _build_label(build),
            [r for r in rows if getattr(r, "client_build", None) == build],
        )
        for build in builds
    ]


def _build_label(build: str | None) -> str:
    """How a build is named in a heading. The commit, truncated, or unknown."""
    return f"build {build[:12]}" if build else "build unknown"


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
    _print_transport_totals(uploads, downloads, console)


def _print_transport_totals(uploads: list, downloads: list, console: Console) -> None:
    """Per-transport success counts, with skips excluded and stated.

    The native and WASM tiers are not the same instrument — the WASM tier
    measured ~4.2x slower on download — so a blended total describes neither.
    Rendered whenever a run carries WASM rows; a native-only run prints exactly
    what it did before. NULL counts as native, as it does everywhere else in
    this pipeline.
    """
    rows = [("upload", uploads), ("download", downloads)]
    if not any((r.client_kind or "native") == "wasm" for _, group in rows for r in group):
        return
    for op_label, group in rows:
        if not group:
            continue
        console.print()
        console.print(f"[bold]{op_label} totals by transport[/bold]")
        for transport in ("native", "wasm"):
            subset = [r for r in group if (r.client_kind or "native") == transport]
            if not subset:
                continue
            colour = "green" if transport == "native" else "cyan"
            # Split again by build when the deployment has been upgraded in
            # place: the transport says which instrument ran, the build says
            # which version of it, and an upgrade is done precisely to see
            # whether the second reads differently from the first.
            for build_label, build_rows in _split_by_build(subset):
                # A skip never enters the denominator: the content is on the
                # network and healthy, and this transport simply will not carry
                # it.
                skipped = sum(1 for r in build_rows if r.status == OpRecordStatus.SKIPPED)
                attempted = [r for r in build_rows if r.status != OpRecordStatus.SKIPPED]
                ok = sum(1 for r in attempted if r.status == OpRecordStatus.SUCCESS)
                total = len(attempted)
                rate = f"{100.0 * ok / total:.2f}%" if total else "n/a"
                extra = f", {skipped} skipped (over-cap)" if skipped else ""
                # Escaped: Rich reads a bare `[...]` as a style tag and would
                # render the label as nothing at all.
                build_note = f" \\[{build_label}]" if build_label else ""
                console.print(
                    f"  [{colour}]{transport:<7}[/{colour}] {ok}/{total} = "
                    f"{rate}{extra}{build_note}"
                )


# ---------------------------------------------------------------------------
# Browser downloads section (deployment_browser_download_records)
# ---------------------------------------------------------------------------


def _print_browser_downloads_section(rows: list, name: str, console: Console) -> None:
    """Rounds, instances and the totals by status and by error kind.

    Prints NOTHING when the deployment has no browser rounds, so every existing
    run's output is unchanged. Deliberately a summary rather than a per-row
    dump: the per-round, per-region and per-chunk reading of this table is
    `analysis browser-reads`, which also carries the gates, and duplicating it
    here would give two answers to maintain.
    """
    if not rows:
        return
    console.print()
    console.rule(f"[bold]Browser downloads — {name}[/bold]")

    rounds = sorted({r.round_id for r in rows})
    hosts = sorted({r.client_hostname for r in rows})
    extensions = sorted({r.extension for r in rows if r.extension})
    archived = sum(1 for r in rows if r.trace_s3_key)

    summary = Table(show_header=False, box=None)
    summary.add_column("k")
    summary.add_column("v", justify="right")
    summary.add_row("rounds", f"{len(rounds):,}")
    summary.add_row(
        "round ids",
        f"{rounds[0]}–{rounds[-1]}" if len(rounds) > 1 else str(rounds[0]),
    )
    summary.add_row("VMs", f"{len(hosts):,}")
    # Distinct (host, instance) pairs: the cohort's size is instances per VM
    # times VMs, and a VM whose harness never came up contributes none.
    summary.add_row("instances", f"{len({(r.client_hostname, r.instance) for r in rows}):,}")
    summary.add_row("downloads", f"{len(rows):,}")
    summary.add_row("traces archived", f"{archived:,}")
    if extensions:
        summary.add_row("extensions", ", ".join(extensions))
    console.print(summary)

    status_table = Table(title="By status")
    status_table.add_column("status")
    status_table.add_column("rows", justify="right")
    status_table.add_column("share", justify="right")
    # The share is over EVERY row, skips included: this is the composition of
    # the series, not a success rate. The success rate — which excludes skips
    # from its denominator — is the report's G1 gate.
    for status in (OpRecordStatus.SUCCESS, OpRecordStatus.FAILURE, OpRecordStatus.SKIPPED):
        count = sum(1 for r in rows if r.status == status)
        if not count:
            continue
        status_table.add_row(status.value, f"{count:,}", f"{100.0 * count / len(rows):.1f}%")
    console.print(status_table)

    kinds: dict[str, int] = {}
    for row in rows:
        if row.error_kind:
            kinds[row.error_kind] = kinds.get(row.error_kind, 0) + 1
    if kinds:
        kind_table = Table(title="By error_kind")
        kind_table.add_column("error_kind")
        kind_table.add_column("rows", justify="right")
        for kind, count in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])):
            kind_table.add_row(kind, f"{count:,}")
        console.print(kind_table)

    console.print(
        "[dim]Per-round, per-region, per-chunk holder share and the gates: "
        "`saorsa-deploy analysis browser-reads --name <name> --window-start … "
        "--window-end …`[/dim]"
    )


def _print_op_records_for_kind(
    op_label: str,
    rows: list,
    name: str,
    console: Console,
) -> None:
    for build_label, group in _split_by_build(rows):
        _print_op_records_tables(op_label, group, name, console, build_label)


def _print_op_records_tables(
    op_label: str,
    rows: list,
    name: str,
    console: Console,
    build_label: str | None = None,
) -> None:
    by_host: dict[str, list] = {}
    for row in rows:
        by_host.setdefault(row.client_hostname, []).append(row)

    for host in sorted(by_host.keys()):
        host_rows = sorted(by_host[host], key=lambda r: r.start_ns)
        build_note = f" — {build_label}" if build_label else ""
        table = Table(
            title=(f"{op_label} — {host}{build_note} ({len(host_rows)} record(s)) — '{name}'"),
            title_style="bold",
            show_lines=False,
        )
        # Only downloads carry an address, and only rows recorded since the
        # column existed. Omitted entirely when nothing has one, so a historical
        # run renders as it did rather than gaining a column of dashes.
        show_address = any(getattr(r, "address", None) for r in host_rows)
        # Uploads have no verification verdict at all, and a download only has
        # one when its uploader published a hash. Omitted when nothing in the
        # table carries one, so a historical run renders as it did.
        show_verify = any(getattr(r, "verify_status", None) for r in host_rows)
        table.add_column("#", justify="right")
        table.add_column("Region")
        if show_address:
            table.add_column("Address")
        table.add_column("Size (MB)", justify="right")
        table.add_column("Status")
        if show_verify:
            table.add_column("Verify")
        table.add_column("Duration", justify="right")
        table.add_column("Started (UTC)")
        table.add_column("Finished (UTC)")
        for i, row in enumerate(host_rows, start=1):
            size_mb = (
                f"{row.file_size_bytes / (1024 * 1024):.1f}"
                if row.file_size_bytes is not None
                else "-"
            )
            cells = [str(i), row.region or "-"]
            if show_address:
                address = getattr(row, "address", None) or "-"
                cells.append(address if len(address) <= 20 else f"{address[:17]}...")
            cells += [
                size_mb,
                _OP_STATUS_LABELS.get(row.status, "-"),
            ]
            if show_verify:
                cells.append(_VERIFY_LABELS.get(getattr(row, "verify_status", None), "-"))
            cells += [
                _fmt_duration(row.duration_ms),
                _fmt_dt(row.started_at),
                _fmt_dt(row.finished_at),
            ]
            table.add_row(*cells)
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

    for build_label, group in _split_by_build(rows):
        if build_label:
            # Only ever printed for a deployment whose bundle was replaced in
            # place; a single-build run renders exactly as it did before.
            console.print()
            console.print(f"[bold]{build_label}[/bold]")
        by_host: dict[str, list[DeploymentProdUploadResult]] = {}
        for row in group:
            by_host.setdefault(row.client_hostname, []).append(row)

        if details:
            _print_prod_upload_details(by_host, console)
        else:
            _print_prod_upload_tables(by_host, console)
    _print_prod_upload_transport_totals(rows, console)


def _prod_upload_transport(row) -> str:
    """The transport that performed an upload; NULL reads as native.

    NULL is every row predating the browser path, and it is what NULL means
    everywhere else in this pipeline.
    """
    return getattr(row, "client_kind", None) or "native"


def _prod_upload_transport_label(rows: list, row) -> str:
    """`` — [wasm]`` for a browser-path host, nothing when the run had none.

    The bracket is escaped for Rich, which would otherwise read ``[wasm]`` as a
    style tag and render the label as nothing at all.
    """
    if not any(_prod_upload_transport(r) == "wasm" for r in rows):
        return ""
    return f" — \\[{_prod_upload_transport(row)}]"


def _print_prod_upload_transport_totals(rows: list, console: Console) -> None:
    """Per-transport success counts, with over-cap files excluded and stated.

    The native and browser paths are not the same instrument, so one blended
    success rate describes neither. Printed only when the run carries WASM rows;
    a native-only run prints exactly what it did before. An over-cap file never
    enters the denominator — the transport refused it and the network was never
    asked.
    """
    if not any(_prod_upload_transport(r) == "wasm" for r in rows):
        return
    console.print()
    console.print("[bold]Upload totals by transport[/bold]")
    for transport in ("native", "wasm"):
        subset = [r for r in rows if _prod_upload_transport(r) == transport]
        if not subset:
            continue
        colour = "green" if transport == "native" else "cyan"
        # And by build within the transport: an in-place upgrade is done to see
        # whether the new bundle reads differently, which one pooled rate hides.
        for build_label, build_rows in _split_by_build(subset):
            over_cap = sum(1 for r in build_rows if r.status is ProdUploadStatus.OVER_CAP)
            attempted = [r for r in build_rows if r.status is not ProdUploadStatus.OVER_CAP]
            ok = sum(1 for r in attempted if r.status is ProdUploadStatus.SUCCESS)
            total = len(attempted)
            rate = f"{100.0 * ok / total:.2f}%" if total else "n/a"
            extra = f", {over_cap} over-cap (not uploaded)" if over_cap else ""
            # Escaped for Rich, which would otherwise read it as a style tag.
            build_note = f" \\[{build_label}]" if build_label else ""
            console.print(
                f"  [{colour}]{transport:<7}[/{colour}] {ok}/{total} = {rate}{extra}{build_note}"
            )


def _print_prod_upload_tables(
    by_host: dict[str, list[DeploymentProdUploadResult]],
    console: Console,
) -> None:
    all_rows = [row for group in by_host.values() for row in group]
    grand_ant = Decimal(0)
    grand_gas = Decimal(0)
    for host in sorted(by_host.keys()):
        rows = sorted(by_host[host], key=lambda r: r.started_at)
        table = Table(
            title=(
                f"prod_upload — {host}"
                f"{_prod_upload_transport_label(all_rows, rows[0])} "
                f"({len(rows)} record(s))"
            ),
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
            "",
            "[bold]Total[/bold]",
            "",
            "",
            "",
            "",
            "",
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
    all_rows = [row for group in by_host.values() for row in group]
    for host in sorted(by_host.keys()):
        rows = sorted(by_host[host], key=lambda r: r.started_at)
        console.rule(
            f"[bold]{host}{_prod_upload_transport_label(all_rows, rows[0])} — "
            f"{len(rows)} record(s)[/bold]"
        )
        for i, row in enumerate(rows, start=1):
            console.print(f"[bold]#{i}[/bold]")
            console.print(f"  Started (UTC):     {_fmt_dt(row.started_at)}")
            console.print(f"  Finished (UTC):    {_fmt_dt(row.finished_at)}")
            console.print(f"  File:              {row.file_name or '-'}")
            console.print(f"  Source:            {row.source_url or '-'}")
            console.print(f"  Size (bytes):      {row.file_size_bytes or 0}")
            console.print(f"  Status:            {_PROD_STATUS_TEXT.get(row.status, '-')}")
            console.print(
                f"  Chunks uploaded:   {row.chunks_stored or 0} / {row.total_chunks or 0}"
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
            console.print(f"  Status:            {_PROD_DOWNLOAD_STATUS_TEXT.get(row.status, '-')}")
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
        f"{_fmt_num(stat.get('avg'))} / {_fmt_num(stat.get('min'))} / {_fmt_num(stat.get('max'))}"
    )


def _fmt_triple_bw(stat: dict) -> str:
    return f"{_fmt_bw(stat.get('avg'))} / {_fmt_bw(stat.get('min'))} / {_fmt_bw(stat.get('max'))}"


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
    gb = num_bytes / (1024**3)
    mb = num_bytes / (1024**2)
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
    console.print()
    console.print("[bold]Read-back verification (per region)[/bold]")
    if not rows:
        console.print(
            "[yellow]No verifications recorded yet. The verifiers check on "
            "their own cycle, so a freshly-deployed run has nothing here "
            "until the first one completes.[/yellow]"
        )
        return

    for build_label, group in _split_by_build(rows):
        if build_label:
            # The independent verifier VMs are native and carry no build, so a
            # deployment whose uploader was upgraded in place shows its own
            # read-backs split by bundle and the verifiers under "unknown".
            console.print()
            console.print(f"[bold]{build_label}[/bold]")
        _print_verification_table(group, details, console)


def _print_verification_table(rows, details: bool, console) -> None:
    """One per-region table, for one client build (or for all of them)."""
    from collections import defaultdict

    from saorsa_db.models import ProdUploadVerifyStatus

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
    table.add_column("Longest failing", justify="right")

    total_mismatch = 0
    for (host, region), host_rows in sorted(by_host.items()):
        counts = defaultdict(int)
        for row in host_rows:
            counts[row.status] += 1
        mismatches = counts[ProdUploadVerifyStatus.DIGEST_MISMATCH]
        total_mismatch += mismatches
        ages = sorted(r.age_at_check_secs or 0 for r in host_rows)
        median_age = ages[len(ages) // 2] if ages else 0
        # A failure that has been retried for days is a different animal from
        # one seen once, and the retry overwrites the row — so the longest
        # standing failure is surfaced rather than just a count.
        failing = [
            r
            for r in host_rows
            if r.status is ProdUploadVerifyStatus.DOWNLOAD_FAILED and r.first_failed_at is not None
        ]
        if failing:
            oldest = min(failing, key=lambda r: r.first_failed_at)
            failing_for = int((oldest.finished_at - oldest.first_failed_at).total_seconds())
            worst = f"{_format_age(failing_for)} / {oldest.attempts}x"
        else:
            worst = "-"
        table.add_row(
            host,
            region,
            str(len(host_rows)),
            f"[green]{counts[ProdUploadVerifyStatus.OK]}[/green]",
            f"[bold red]{mismatches}[/bold red]" if mismatches else "0",
            str(counts[ProdUploadVerifyStatus.DOWNLOAD_FAILED]),
            str(counts[ProdUploadVerifyStatus.INCONCLUSIVE]),
            _format_age(median_age),
            worst,
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
        detail.add_column("Tries", justify="right")
        detail.add_column("Reason")
        for row in rows:
            detail.add_row(
                row.region,
                row.file_name[:40],
                (row.address or "")[:16] + "…",
                _verify_status_label(row.status),
                _format_age(row.age_at_check_secs or 0),
                str(row.attempts),
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
