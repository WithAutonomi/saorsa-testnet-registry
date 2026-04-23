"""Print per-operation upload/download records from a testnet's InfluxDB.

Two CLI entry points — ``cmd_print_uploads`` and ``cmd_print_downloads`` —
share all their logic except for the measurement name they filter on. Each
renders one Rich table per client host with a row for every recorded
operation in the requested time window.

With ``--summary``, the per-op tables are replaced by a per-host aggregate:
start time, wall-clock elapsed since that start, total/success/failure
counts, and the average duration of successful ops. For uploads the average
is additionally broken out by ``store_concurrency``.
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

    if args.summary:
        _print_summary(op_type, by_host, console)
        return

    for host in sorted(by_host.keys()):
        ops = sorted(
            by_host[host], key=lambda p: p.get("start_ns") or 0
        )
        total = len(ops)
        if args.recent:
            ops = ops[-10:]
        title_count = (
            f"{len(ops)} of {total} op(s)"
            if args.recent and total > len(ops)
            else f"{len(ops)} op(s)"
        )
        table = Table(
            title=f"{op_type.capitalize()}s — {host} ({title_count})",
            title_style="bold",
        )
        show_concurrency = op_type == "upload"
        table.add_column("#", justify="right")
        table.add_column("Started (UTC)")
        table.add_column("Completed (UTC)")
        table.add_column("Size", justify="right")
        if show_concurrency:
            table.add_column("Concurrency", justify="right")
        table.add_column("Status")
        table.add_column("Duration", justify="right")
        for i, op in enumerate(ops, start=1):
            row = [
                str(i),
                _fmt_start(op.get("start_ns")),
                _fmt_time(op.get("_time")),
                _fmt_size(op.get("file_size")),
            ]
            if show_concurrency:
                row.append(str(op["store_concurrency"]))
            row.extend([
                _fmt_status(op.get("status")),
                _fmt_duration(op.get("total_ms")),
            ])
            table.add_row(*row)
        console.print(table)


def _summarise_host(ops: list[dict], op_type: str, now=None) -> dict:
    """Compute the per-host summary numbers for ``--summary``.

    Pure function — no I/O — so it's unit-testable.

    Returns a dict with keys:
      - ``started_at``: tz-aware UTC :class:`datetime` of earliest ``start_ns``
        (or ``None`` if no op has a usable ``start_ns``).
      - ``elapsed_s``: seconds between ``started_at`` and ``now``
        (``None`` if ``started_at`` is ``None``).
      - ``total``, ``successes``, ``failures``: op counts.
      - ``overall_avg_ms``: mean ``total_ms`` across successful ops
        (``None`` if there were none).
      - ``by_concurrency``: for ``op_type == "upload"``, a list of
        ``(concurrency, count, avg_ms)`` tuples sorted by ``concurrency``
        (``concurrency`` may be ``None`` if the field was absent).
        Empty list for downloads — concurrency is an uploader-side concept.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    starts = [o["start_ns"] for o in ops if o.get("start_ns") is not None]
    if starts:
        started_at = datetime.fromtimestamp(
            min(starts) / 1_000_000_000, tz=timezone.utc
        )
        elapsed_s = (now - started_at).total_seconds()
    else:
        started_at = None
        elapsed_s = None

    total = len(ops)
    successes = sum(1 for o in ops if o.get("status") == 0)
    failures = sum(1 for o in ops if o.get("status") == 1)

    success_durations = [
        o["total_ms"] for o in ops
        if o.get("status") == 0 and o.get("total_ms") is not None
    ]
    overall_avg_ms = (
        sum(success_durations) / len(success_durations)
        if success_durations else None
    )

    by_concurrency: list[tuple] = []
    if op_type == "upload":
        groups: dict = {}
        for o in ops:
            c = o.get("store_concurrency")
            groups.setdefault(c, []).append(o)
        for c in sorted(groups.keys(), key=lambda v: (v is None, v)):
            group_ops = groups[c]
            group_successes = [
                g["total_ms"] for g in group_ops
                if g.get("status") == 0 and g.get("total_ms") is not None
            ]
            avg_ms = (
                sum(group_successes) / len(group_successes)
                if group_successes else None
            )
            by_concurrency.append((c, len(group_ops), avg_ms))

    return {
        "started_at": started_at,
        "elapsed_s": elapsed_s,
        "total": total,
        "successes": successes,
        "failures": failures,
        "overall_avg_ms": overall_avg_ms,
        "by_concurrency": by_concurrency,
    }


def _print_summary(op_type: str, by_host: dict, console: Console) -> None:
    label = op_type.capitalize() + "s"

    all_starts = [
        o["start_ns"]
        for ops in by_host.values()
        for o in ops
        if o.get("start_ns") is not None
    ]
    if all_starts:
        started_at = datetime.fromtimestamp(
            min(all_starts) / 1_000_000_000, tz=timezone.utc
        )
        elapsed_s = (datetime.now(timezone.utc) - started_at).total_seconds()
    else:
        started_at = None
        elapsed_s = None

    console.print(f"[bold]{label} — run window[/bold]")
    header = Table(show_header=False)
    header.add_column("Metric")
    header.add_column("Value")
    header.add_row("Started (UTC)", _fmt_time(started_at))
    header.add_row("Elapsed", _fmt_elapsed(elapsed_s))
    console.print(header)

    for host in sorted(by_host.keys()):
        summary = _summarise_host(by_host[host], op_type)

        console.print(f"[bold]{host}[/bold]")
        table = Table(show_header=False)
        table.add_column("Metric")
        table.add_column("Value")
        table.add_row("Total ops", str(summary["total"]))
        table.add_row("Successes", str(summary["successes"]))
        table.add_row("Failures", str(summary["failures"]))
        if op_type == "upload":
            for concurrency, count, avg_ms in summary["by_concurrency"]:
                c_label = "(unset)" if concurrency is None else str(concurrency)
                op_word = "op" if count == 1 else "ops"
                table.add_row(
                    f"Avg @ concurrency {c_label} ({count} {op_word})",
                    _fmt_duration(avg_ms),
                )
        else:
            table.add_row(
                "Avg duration", _fmt_duration(summary["overall_avg_ms"])
            )
        console.print(table)


def _fmt_elapsed(seconds) -> str:
    if seconds is None:
        return "-"
    hours = int(seconds // 3600)
    remainder = seconds - hours * 3600
    minutes = int(remainder // 60)
    secs = remainder - minutes * 60
    if hours > 0:
        return f"{hours}h {minutes}m {secs:.1f}s"
    if minutes > 0:
        return f"{minutes}m {secs:.1f}s"
    return f"{secs:.1f}s"


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
