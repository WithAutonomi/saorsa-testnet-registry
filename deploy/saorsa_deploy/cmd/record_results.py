"""Record per-file/per-host upload+download results into the registry DB.

The CLI entry point ``cmd_record_results`` resolves the deployment's
``kind`` and dispatches to the correct Influx → Postgres pipeline:

* ``testnet`` and ``prod-continuous-downloader`` →
  per-(host, op_type) aggregates of the ``file_ops`` measurement, written
  to ``deployment_file_results``. Honours ``--window``.
* ``prod-uploaders`` → per-file rows of the ``prod_upload`` measurement,
  written to ``deployment_prod_upload_results``. ``--window`` is ignored
  (uploads are bounded runs; we always pull from epoch).
* ``prod-local-uploads`` → rejected. Local-uploads writes directly to
  Postgres at run time; there is nothing to record.
"""
import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table
from saorsa_db.models import FileOpType, ProdUploadStatus

from saorsa_deploy.influx import (
    aggregate_file_results,
    fetch_operation_points,
    fetch_prod_upload_points,
)
from saorsa_deploy.registry import get_repository
from saorsa_deploy.state import load_monitoring_state

_TESTNET_KIND = "testnet"


def cmd_record_results(args):
    console = Console()

    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set. "
            "This command requires the registry database."
        )
        sys.exit(1)

    deployment = repo.get_active_by_name(args.name) or repo.get_by_name(args.name)
    if deployment is None:
        console.print(
            f"[bold red]Error:[/bold red] No deployment named '{args.name}' "
            "in the registry."
        )
        sys.exit(1)

    kind = _resolve_kind(deployment.deploy_args)

    if kind == "prod-local-uploads":
        console.print(
            f"[yellow]'{args.name}' is a prod-local-uploads deployment — "
            f"its per-file results are written directly to Postgres at run "
            f"time. There is nothing to record.[/yellow]"
        )
        return

    creds = _resolve_influx(args, console)
    if creds is None:
        sys.exit(1)
    influx_url, influx_token, influx_org, influx_bucket = creds

    if kind == "prod-uploaders":
        _record_prod_upload(
            repo, deployment, args, console,
            influx_url, influx_token, influx_org, influx_bucket,
        )
        return

    if kind == "prod-canary":
        from saorsa_deploy.cmd.prod_canary_results import (
            record_prod_canary_results,
        )
        record_prod_canary_results(
            repo, deployment, args, console,
            influx_url, influx_token, influx_org, influx_bucket,
        )
        return

    # testnet, prod-continuous-downloader, and any unrecognised/empty kind
    # fall through to the historical aggregate path.
    _record_file_ops(
        repo, deployment, args, console,
        influx_url, influx_token, influx_org, influx_bucket,
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _resolve_kind(deploy_args) -> str:
    if not isinstance(deploy_args, dict):
        return ""
    explicit = deploy_args.get("kind")
    if explicit:
        return explicit
    if "bootstrap_count" in deploy_args:
        return _TESTNET_KIND
    return ""


def _resolve_influx(args, console: Console):
    influx_url = args.influx_url
    influx_token = args.influx_token
    influx_org = args.influx_org
    influx_bucket = args.influx_bucket

    if not (influx_url and influx_token):
        stack_name = args.monitoring_name or args.name
        console.print(
            f"[bold]Loading Influx credentials from monitoring state "
            f"'{stack_name}'...[/bold]"
        )
        try:
            state = load_monitoring_state(stack_name)
        except RuntimeError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            return None
        influx_url = influx_url or state.get("influxdb_url")
        influx_token = influx_token or state.get("influxdb_token")
        influx_org = influx_org or state.get("influxdb_org") or "ant"
        influx_bucket = (
            influx_bucket or state.get("influxdb_bucket") or "testnet-metrics"
        )

    if not (influx_url and influx_token):
        console.print(
            "[bold red]Error:[/bold red] InfluxDB URL and token are required. "
            "Provide --influx-url/--influx-token or --monitoring-name pointing "
            "to a provisioned monitoring stack."
        )
        return None

    return (
        influx_url,
        influx_token,
        influx_org or "ant",
        influx_bucket or "testnet-metrics",
    )


# ---------------------------------------------------------------------------
# testnet / prod-continuous-downloader: file_ops aggregate path
# ---------------------------------------------------------------------------


def _record_file_ops(
    repo, deployment, args, console,
    influx_url, influx_token, influx_org, influx_bucket,
) -> None:
    console.print(
        f"[bold]Querying {influx_url} "
        f"(bucket={influx_bucket}, window={args.window})...[/bold]"
    )
    try:
        points = fetch_operation_points(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
            start=args.window,
        )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] InfluxDB query failed: {e}")
        sys.exit(1)

    aggregates = aggregate_file_results(points)
    if not aggregates:
        console.print(
            f"[yellow]No upload/download points found for deployment "
            f"'{args.name}' in window {args.window}. "
            f"Nothing to record.[/yellow]"
        )
        return

    for agg in aggregates:
        repo.upsert_file_result(
            deployment_id=deployment.id,
            client_hostname=agg["client_hostname"],
            op_type=FileOpType(agg["op_type"]),
            file_size_bytes=agg["file_size_bytes"],
            first_started_at=agg["first_started_at"],
            last_finished_at=agg["last_finished_at"],
            success_count=agg["success_count"],
            failure_count=agg["failure_count"],
            avg_duration_ms=agg["avg_duration_ms"],
            min_duration_ms=agg["min_duration_ms"],
            max_duration_ms=agg["max_duration_ms"],
        )

    table = Table(
        title=f"File results for deployment '{args.name}' (id={deployment.id})"
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
    for agg in aggregates:
        table.add_row(
            agg["client_hostname"],
            agg["op_type"],
            f"{agg['file_size_bytes'] / (1024 * 1024):.1f}",
            str(agg["success_count"]),
            str(agg["failure_count"]),
            "-" if agg["avg_duration_ms"] is None else str(agg["avg_duration_ms"]),
            "-" if agg["min_duration_ms"] is None else str(agg["min_duration_ms"]),
            "-" if agg["max_duration_ms"] is None else str(agg["max_duration_ms"]),
            agg["first_started_at"].strftime("%Y-%m-%d %H:%M:%S"),
            agg["last_finished_at"].strftime("%Y-%m-%d %H:%M:%S"),
        )
    console.print(table)
    console.print(
        f"[bold green]Recorded {len(aggregates)} row(s) "
        f"for deployment '{args.name}'.[/bold green]"
    )


# ---------------------------------------------------------------------------
# prod-uploaders: prod_upload per-file path
# ---------------------------------------------------------------------------


def _status_from_int(code) -> ProdUploadStatus | None:
    if code == 0:
        return ProdUploadStatus.SUCCESS
    if code == 1:
        return ProdUploadStatus.PARTIAL_UPLOAD
    if code == 2:
        return ProdUploadStatus.DOWNLOAD_FAILED
    return None


def _started_at(point: dict) -> datetime:
    start_ns = point.get("start_ns")
    if start_ns is not None:
        return datetime.fromtimestamp(start_ns / 1_000_000_000, tz=timezone.utc)
    return point["_time"]


def _record_prod_upload(
    repo, deployment, args, console,
    influx_url, influx_token, influx_org, influx_bucket,
) -> None:
    console.print(f"[bold]Querying {influx_url} (bucket={influx_bucket})...[/bold]")
    try:
        points = fetch_prod_upload_points(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
        )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] InfluxDB query failed: {e}")
        sys.exit(1)

    if not points:
        console.print(
            f"[yellow]No prod_upload points found for deployment "
            f"'{args.name}'. Nothing to record.[/yellow]"
        )
        return

    recorded = 0
    skipped = 0
    for p in points:
        status = _status_from_int(p.get("status"))
        if status is None:
            skipped += 1
            continue
        source_url = p.get("source_url") or ""
        if not source_url:
            skipped += 1
            continue
        repo.upsert_prod_upload_result(
            deployment_id=deployment.id,
            client_hostname=p["host"],
            region=p.get("region") or "",
            source_url=source_url,
            file_name=p.get("file_name") or "",
            file_size_bytes=p.get("file_size") or 0,
            status=status,
            address=p.get("address") or None,
            chunks_stored=p.get("chunks_stored") or 0,
            chunks_failed=p.get("chunks_failed") or 0,
            total_chunks=p.get("total_chunks") or 0,
            duration_ms=p.get("total_ms"),
            upload_secs=p.get("upload_secs"),
            started_at=_started_at(p),
            finished_at=p["_time"],
            reason=p.get("reason") or None,
        )
        recorded += 1

    table = Table(
        title=f"Prod uploads recorded for '{args.name}' (id={deployment.id})"
    )
    table.add_column("Recorded", justify="right")
    table.add_column("Skipped", justify="right")
    table.add_row(str(recorded), str(skipped))
    console.print(table)
    console.print(
        f"[bold green]Persisted {recorded} prod_upload row(s) "
        f"for deployment '{args.name}'.[/bold green]"
    )
