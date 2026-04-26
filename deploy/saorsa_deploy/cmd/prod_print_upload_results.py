"""Print prod-upload records from a deployment's InfluxDB.

Sister of :mod:`saorsa_deploy.cmd.print_file_ops` but targets the
``prod_upload`` measurement and prints per-file detail (file name, size,
status, chunk counts, address, duration). Uploads are bounded runs so the
default Influx range starts at the Unix epoch.

Two output modes:

* default — one Rich table per uploader VM with truncated columns
* ``--details`` — one full record per upload, nothing truncated, suitable
  for grabbing addresses and complete file names verbatim.
"""
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

from saorsa_deploy.influx import fetch_prod_upload_points
from saorsa_deploy.state import load_monitoring_state

_STATUS_LABELS = {
    0: "[green]ok[/green]",
    1: "[red]partial[/red]",
    2: "[red]dl-fail[/red]",
}

_STATUS_TEXT = {
    0: "ok",
    1: "partial",
    2: "dl-fail",
}


def cmd_prod_print_upload_results(args):
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
                f"Run `monitoring-infra` + `provision-monitoring` first if "
                f"you expected to query Influx.[/yellow]"
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
        f"[bold]Querying {influx_url} (bucket={influx_bucket})...[/bold]"
    )
    points = fetch_prod_upload_points(
        url=influx_url,
        token=influx_token,
        org=influx_org or "ant",
        bucket=influx_bucket or "testnet-metrics",
    )
    if not points:
        console.print(
            f"[yellow]No prod_upload records yet for '{args.name}'.[/yellow]"
        )
        return

    by_host: dict[str, list[dict]] = {}
    for p in points:
        by_host.setdefault(p["host"], []).append(p)

    if getattr(args, "details", False):
        _print_details(by_host, console)
    else:
        _print_table(by_host, console)


def _print_table(by_host: dict[str, list[dict]], console: Console) -> None:
    for host in sorted(by_host.keys()):
        ops = sorted(by_host[host], key=lambda p: p.get("start_ns") or 0)
        table = Table(
            title=f"prod_upload — {host} ({len(ops)} record(s))",
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
        for i, op in enumerate(ops, start=1):
            table.add_row(
                str(i),
                op.get("file_name") or "-",
                _fmt_size(op.get("file_size")),
                _fmt_status(op.get("status")),
                f"{op.get('chunks_stored') or 0}/{op.get('total_chunks') or 0}",
                str(op.get("chunks_failed") or 0),
                _fmt_duration(op.get("total_ms")),
            )
        console.print(table)


def _print_details(by_host: dict[str, list[dict]], console: Console) -> None:
    for host in sorted(by_host.keys()):
        ops = sorted(by_host[host], key=lambda p: p.get("start_ns") or 0)
        console.rule(f"[bold]{host} — {len(ops)} record(s)[/bold]")
        for i, op in enumerate(ops, start=1):
            console.print(f"[bold]#{i}[/bold]")
            console.print(f"  Started (UTC):     {_fmt_start(op.get('start_ns'))}")
            console.print(f"  Finished (UTC):    {_fmt_finish(op.get('_time'))}")
            console.print(f"  File:              {op.get('file_name') or '-'}")
            console.print(f"  Source URL:        {op.get('source_url') or '-'}")
            console.print(f"  Size (bytes):      {op.get('file_size') or 0}")
            console.print(f"  Status:            {_STATUS_TEXT.get(op.get('status'), '-')}")
            console.print(
                f"  Chunks uploaded:   {op.get('chunks_stored') or 0} / "
                f"{op.get('total_chunks') or 0}"
            )
            console.print(f"  Chunks failed:     {op.get('chunks_failed') or 0}")
            console.print(f"  Store concurrency: {op.get('store_concurrency') or '-'}")
            console.print(f"  Address:           {op.get('address') or '-'}")
            console.print(f"  Duration (ms):     {op.get('total_ms') or 0}")
            console.print(f"  Upload secs:       {op.get('upload_secs') or 0}")
            console.print(f"  Reason:            {op.get('reason') or '-'}")
            console.print()


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
    return _STATUS_LABELS.get(code, "-")


def _fmt_start(start_ns) -> str:
    if start_ns is None:
        return "-"
    return datetime.fromtimestamp(
        start_ns / 1_000_000_000, tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_finish(dt) -> str:
    if dt is None:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M:%S")
