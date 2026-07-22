import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from saorsa_deploy.influx import (
    aggregate_host_metrics,
    fetch_host_metric_points,
    node_storage_device,
)
from saorsa_deploy.registry import get_repository
from saorsa_deploy.state import load_monitoring_state


def _fmt(value, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.1f}{suffix}"
    return f"{value}{suffix}"


def _fmt_bw(value) -> str:
    if value is None:
        return "-"
    mb = value / (1024 * 1024)
    if mb >= 1:
        return f"{mb:.2f} MB/s"
    kb = value / 1024
    return f"{kb:.1f} KB/s"


def _serialize_host_entry(entry: dict) -> dict:
    return {
        "host": entry["host"],
        "metrics_started_at": entry["metrics_started_at"].isoformat(),
        "metrics_ended_at": entry["metrics_ended_at"].isoformat(),
        "cpu_percent": entry["cpu_percent"],
        "mem_percent": entry["mem_percent"],
        "bytes_in_per_sec": entry["bytes_in_per_sec"],
        "bytes_out_per_sec": entry["bytes_out_per_sec"],
        "disk_io_by_device": entry["disk_io_by_device"],
    }


def cmd_record_analysis(args):
    """Pull per-host CPU/memory/bandwidth aggregates from InfluxDB and upsert them."""
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

    window_label = "full run" if args.window == "0" else args.window
    console.print(
        f"[bold]Querying {influx_url} (bucket={influx_bucket}, "
        f"window={window_label})...[/bold]"
    )
    try:
        points = fetch_host_metric_points(
            url=influx_url,
            token=influx_token,
            org=influx_org or "ant",
            bucket=influx_bucket or "testnet-metrics",
            start=args.window,
        )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] InfluxDB query failed: {e}")
        sys.exit(1)

    aggregates = aggregate_host_metrics(points)
    if not aggregates:
        console.print(
            f"[yellow]No host metric points found for deployment '{args.name}' "
            f"in window {window_label}. Nothing to record.[/yellow]"
        )
        if args.summary:
            console.print(
                "[yellow]--summary was provided but there are no host metrics "
                "to associate it with; summary not persisted.[/yellow]"
            )
        return

    overall_started = min(a["metrics_started_at"] for a in aggregates)
    overall_ended = max(a["metrics_ended_at"] for a in aggregates)
    serialized = [_serialize_host_entry(a) for a in aggregates]

    repo.upsert_host_result(
        deployment_id=deployment.id,
        host_metrics=serialized,
        metrics_started_at=overall_started,
        metrics_ended_at=overall_ended,
        summary=args.summary,
    )

    table = Table(
        title=f"Host metrics for deployment '{args.name}' (id={deployment.id})"
    )
    table.add_column("Host")
    table.add_column("CPU avg/min/max %", justify="right")
    table.add_column("Mem avg/min/max %", justify="right")
    table.add_column("In avg/min/max", justify="right")
    table.add_column("Out avg/min/max", justify="right")
    # Scoped to the NODE STORAGE volume specifically -- that is the disk whose
    # I/O matters for a node, and averaging it together with an idle root disk
    # would describe neither. Kept to two columns because this table is already
    # wide; every device, with full avg/min/max and IOPS, is in the stored
    # `disk_io_by_device` and rendered by `print-recorded-results`.
    table.add_column("Storage vol")
    table.add_column("Vol rd/wr avg", justify="right")
    table.add_column("Started (UTC)")
    table.add_column("Ended (UTC)")
    for agg in aggregates:
        cpu = agg["cpu_percent"]
        mem = agg["mem_percent"]
        bin_ = agg["bytes_in_per_sec"]
        bout = agg["bytes_out_per_sec"]
        storage = node_storage_device(agg)
        if storage is None:
            # No device tagged node-storage: a client VM, or a host whose
            # Telegraf predates the volume_role tag.
            device_label, drd, dwr = "-", {}, {}
        else:
            device_label, stats = storage
            drd = stats.get("read_bytes_per_sec") or {}
            dwr = stats.get("write_bytes_per_sec") or {}
        table.add_row(
            agg["host"],
            f"{_fmt(cpu['avg'])} / {_fmt(cpu['min'])} / {_fmt(cpu['max'])}",
            f"{_fmt(mem['avg'])} / {_fmt(mem['min'])} / {_fmt(mem['max'])}",
            f"{_fmt_bw(bin_['avg'])} / {_fmt_bw(bin_['min'])} / {_fmt_bw(bin_['max'])}",
            f"{_fmt_bw(bout['avg'])} / {_fmt_bw(bout['min'])} / {_fmt_bw(bout['max'])}",
            device_label,
            f"{_fmt_bw(drd.get('avg'))} / {_fmt_bw(dwr.get('avg'))}",
            agg["metrics_started_at"].strftime("%Y-%m-%d %H:%M:%S"),
            agg["metrics_ended_at"].strftime("%Y-%m-%d %H:%M:%S"),
        )
    console.print(table)
    console.print(
        f"[bold green]Recorded host metrics for {len(aggregates)} host(s) "
        f"on deployment '{args.name}'.[/bold green]"
    )

    if args.summary:
        console.print(
            Panel(
                args.summary,
                title=f"Summary for '{args.name}'",
                border_style="cyan",
            )
        )
