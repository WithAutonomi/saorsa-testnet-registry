"""Record prod-upload measurements into the registry database.

For each ``prod_upload`` Influx point, upserts a row into
``deployment_prod_upload_results`` keyed by (deployment_id, client_hostname,
source_url). Mirrors :mod:`saorsa_deploy.cmd.record_file_results` but drops
the ``--window`` flag — uploads are bounded runs so we always pull
everything since the Unix epoch.
"""
import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table
from saorsa_db.models import ProdUploadStatus

from saorsa_deploy.influx import fetch_prod_upload_points
from saorsa_deploy.registry import get_repository
from saorsa_deploy.state import load_monitoring_state


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


def cmd_prod_record_upload_results(args):
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
            sys.exit(1)
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
        sys.exit(1)

    console.print(f"[bold]Querying {influx_url} (bucket={influx_bucket})...[/bold]")
    try:
        points = fetch_prod_upload_points(
            url=influx_url,
            token=influx_token,
            org=influx_org or "ant",
            bucket=influx_bucket or "testnet-metrics",
        )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] InfluxDB query failed: {e}")
        sys.exit(1)

    if not points:
        console.print(
            f"[yellow]No prod_upload points found for deployment '{args.name}'. "
            f"Nothing to record.[/yellow]"
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
            store_concurrency=p.get("store_concurrency"),
            duration_ms=p.get("total_ms"),
            upload_secs=p.get("upload_secs"),
            started_at=_started_at(p),
            finished_at=p["_time"],
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
