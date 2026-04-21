"""Print per-operation upload/download records from a testnet's InfluxDB.

Two CLI entry points — ``cmd_print_uploads`` and ``cmd_print_downloads`` —
share all their logic except for the measurement name they filter on. Each
renders one Rich table per client host with a row for every recorded
operation in the requested time window.
"""
import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

from saorsa_deploy.influx import fetch_operation_points
from saorsa_deploy.state import load_monitoring_state


def cmd_print_uploads(args):
    _run(args, op_type="upload")


def cmd_print_downloads(args):
    _run(args, op_type="download")


def _run(args, op_type: str) -> None:
    console = Console()

    influx_url = args.influx_url
    influx_token = args.influx_token
    influx_org = args.influx_org
    influx_bucket = args.influx_bucket

    if not (influx_url and influx_token):
        stack_name = args.monitoring_name or args.name
        try:
            state = load_monitoring_state(stack_name)
        except RuntimeError:
            console.print(
                f"[yellow]No monitoring stack found for '{stack_name}'. "
                f"{op_type.capitalize()}s are not being recorded for this "
                f"testnet — run `monitoring-infra` + `provision-monitoring` "
                f"first if you want them.[/yellow]"
            )
            return
        influx_url = influx_url or state.get("influxdb_url")
        influx_token = influx_token or state.get("influxdb_token")
        influx_org = influx_org or state.get("influxdb_org") or "ant"
        influx_bucket = (
            influx_bucket or state.get("influxdb_bucket") or "testnet-metrics"
        )

    if not (influx_url and influx_token):
        console.print(
            f"[yellow]Monitoring state exists for '{args.name}' but InfluxDB "
            f"isn't provisioned yet. Run `provision-monitoring` first.[/yellow]"
        )
        return

    console.print(
        f"[bold]Querying {influx_url} "
        f"(bucket={influx_bucket}, window={args.window})...[/bold]"
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

    filtered = [p for p in points if p.get("_measurement") == op_type]
    if not filtered:
        console.print(
            f"[yellow]No {op_type} operations recorded yet for '{args.name}' "
            f"(window {args.window}).[/yellow]"
        )
        return

    by_host: dict[str, list[dict]] = {}
    for p in filtered:
        by_host.setdefault(p["host"], []).append(p)

    for host in sorted(by_host.keys()):
        ops = sorted(
            by_host[host], key=lambda p: p.get("start_ns") or 0
        )
        table = Table(
            title=f"{op_type.capitalize()}s — {host} ({len(ops)} op(s))",
            title_style="bold",
        )
        table.add_column("#", justify="right")
        table.add_column("Started (UTC)")
        table.add_column("Completed (UTC)")
        table.add_column("Size", justify="right")
        table.add_column("Status")
        table.add_column("Duration", justify="right")
        for i, op in enumerate(ops, start=1):
            table.add_row(
                str(i),
                _fmt_start(op.get("start_ns")),
                _fmt_time(op.get("_time")),
                _fmt_size(op.get("file_size")),
                _fmt_status(op.get("status")),
                _fmt_duration(op.get("total_ms")),
            )
        console.print(table)


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


def _fmt_status(code) -> str:
    if code == 0:
        return "[green]ok[/green]"
    if code == 1:
        return "[red]fail[/red]"
    return "-"


def _fmt_time(dt) -> str:
    if dt is None:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_start(start_ns) -> str:
    if start_ns is None:
        return "-"
    return datetime.fromtimestamp(
        start_ns / 1_000_000_000, tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S")
