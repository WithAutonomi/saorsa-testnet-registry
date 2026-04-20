import sys

from rich.console import Console
from rich.table import Table
from saorsa_db.models import FileOpType

from saorsa_deploy.influx import aggregate_file_results, fetch_operation_points
from saorsa_deploy.registry import get_repository
from saorsa_deploy.state import load_monitoring_state


def cmd_record_file_results(args):
    """Pull upload/download aggregates from InfluxDB and upsert them to Postgres."""
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
            f"[bold red]Error:[/bold red] No deployment named '{args.name}' in the registry."
        )
        sys.exit(1)

    influx_url = args.influx_url
    influx_token = args.influx_token
    influx_org = args.influx_org
    influx_bucket = args.influx_bucket

    if not (influx_url and influx_token):
        stack_name = args.monitoring_name or args.name
        console.print(
            f"[bold]Loading Influx credentials from monitoring state '{stack_name}'...[/bold]"
        )
        try:
            state = load_monitoring_state(stack_name)
        except RuntimeError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)
        influx_url = influx_url or state.get("influxdb_url")
        influx_token = influx_token or state.get("influxdb_token")
        influx_org = influx_org or state.get("influxdb_org") or "ant"
        influx_bucket = influx_bucket or state.get("influxdb_bucket") or "testnet-metrics"

    if not (influx_url and influx_token):
        console.print(
            "[bold red]Error:[/bold red] InfluxDB URL and token are required. "
            "Provide --influx-url/--influx-token or --monitoring-name pointing "
            "to a provisioned monitoring stack."
        )
        sys.exit(1)

    console.print(
        f"[bold]Querying {influx_url} (bucket={influx_bucket}, window={args.window})...[/bold]"
    )
    try:
        points = fetch_operation_points(
            url=influx_url,
            token=influx_token,
            org=influx_org or "ant",
            bucket=influx_bucket or "testnet-metrics",
            start=args.window,
        )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] InfluxDB query failed: {e}")
        sys.exit(1)

    aggregates = aggregate_file_results(points)
    if not aggregates:
        console.print(
            f"[yellow]No upload/download points found for deployment '{args.name}' "
            f"in window {args.window}. Nothing to record.[/yellow]"
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
