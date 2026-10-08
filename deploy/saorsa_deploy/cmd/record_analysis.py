import sys
import time

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from saorsa_db.models.derived_metric import (
    RATIO_METRICS,
    SOURCE_CAPTURE,
    WINDOW_FULL_RUN,
)

from saorsa_deploy.derived_metrics import (
    compute_from_points,
    format_metric_value,
)
from saorsa_deploy.influx import (
    aggregate_normalised_host_metrics,
    bucket_host_metric_samples,
    detect_clock_offsets,
    iter_host_metric_points,
    node_storage_device,
    normalise_host_metric_points,
    peak_rss_service,
    validate_flux_window,
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


def _fmt_bytes(value) -> str:
    if value is None:
        return "-"
    gb = value / (1024**3)
    if gb >= 1:
        return f"{gb:.2f} GB"
    return f"{value / (1024 * 1024):.0f} MB"


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
        "disk_usage_by_mount": entry.get("disk_usage_by_mount") or {},
        "rss_by_service": entry.get("rss_by_service") or {},
    }


def _fmt_offset(seconds: float) -> str:
    sign = "+" if seconds >= 0 else "-"
    total = int(abs(seconds))
    return f"{sign}{total // 3600}h{(total % 3600) // 60:02d}m"


def _warn_about_clock_offsets(aggregates: list[dict], console: Console) -> None:
    """Surface hosts whose observation window is displaced from its peers.

    Such hosts used to be *silently dropped* — Flux defaults a range's `stop` to
    `now()`, so a forward clock-skewed host's future-dated points fell outside
    every query. They are now captured, but their timestamps remain displaced by
    the skew, which matters the moment anyone slices a sub-window: the same
    wall-clock window lands on a different stretch of that host's run.

    Warn rather than fail. The skew is deliberate on these testnets
    (`provisioning/clock_skew.py`), the data is real, and dropping it is what
    the fix set out to stop.
    """
    offsets = detect_clock_offsets(aggregates)
    if not offsets:
        return

    lines = [f"  {host:38} {_fmt_offset(delta):>8}" for host, delta in offsets]
    console.print(
        Panel(
            "These hosts' metrics are offset from the fleet median:\n\n"
            + "\n".join(lines)
            + "\n\nA positive offset is a clock running ahead — testnet "
            "manifests skew some VMs deliberately\n(nodes by a fixed ±3h, "
            "clients by an explicit duration). Their data IS recorded, but "
            "its\ntimestamps are displaced, so a sub-window re-derived from "
            "the buckets will cover a\ndifferent stretch of these hosts' run "
            "than of everyone else's. A negative offset may\ninstead mean the "
            "host stopped reporting early.",
            title="[yellow]Clock offset detected[/yellow]",
            border_style="yellow",
        )
    )


def _record_derived_metrics(
    *,
    repo,
    deployment,
    points,
    window_start,
    window_end,
    console: Console,
) -> list:
    """Compute and persist the normalised per-GB metrics for the whole run.

    Best-effort in one specific sense: a run whose denominators are missing
    (``record-results`` not yet run, or a deployment with no clients) records
    nothing and says so, rather than failing the host-metric capture that has
    already succeeded. It is NOT best-effort about correctness — a metric that
    cannot be measured is omitted, never estimated.
    """
    upload_records = repo.list_upload_records_for_deployment(deployment.id)
    download_records = repo.list_download_records_for_deployment(deployment.id)
    if not upload_records and not download_records:
        console.print(
            "[yellow]No upload or download records for this deployment, so "
            "there is no user-traffic denominator to normalise against. Run "
            "`deployments record-results --name "
            f"{deployment.name}` first, then re-run this command to derive the "
            "per-GB metrics.[/yellow]"
        )

    rows = compute_from_points(
        points_by_measurement=points,
        upload_records=upload_records,
        download_records=download_records,
        window_start=window_start,
        window_end=window_end,
        window_label=WINDOW_FULL_RUN,
        source=SOURCE_CAPTURE,
    )
    for row in rows:
        repo.upsert_derived_metric(deployment_id=deployment.id, **row)
    return rows


def _print_derived_metrics(rows: list, console: Console) -> None:
    if not rows:
        return
    table = Table(title="Derived per-run metrics (window: full run)")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_column("Numerator", justify="right")
    table.add_column("Denominator", justify="right")
    for row in rows:
        numerator = row.get("numerator")
        denominator = row.get("denominator")
        table.add_row(
            row["metric"],
            format_metric_value(row.get("value"), row["unit"]),
            _fmt_bytes(numerator) if numerator is not None else "-",
            f"{denominator:.2f} GB" if denominator is not None else "-",
        )
    console.print(table)

    missing = [m for m in RATIO_METRICS if m not in {r["metric"] for r in rows}]
    if missing:
        console.print(
            "[yellow]Not derived (no measurable value): "
            + ", ".join(missing)
            + ". A ratio with an empty denominator, or a disk metric with no "
            "host reporting a node-storage series, is omitted rather than "
            "recorded as zero.[/yellow]"
        )


def cmd_record_analysis(args):
    """Pull per-host CPU/memory/bandwidth aggregates from InfluxDB and upsert them."""
    console = Console()

    try:
        args.window = validate_flux_window(args.window)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set. "
            "This command requires the registry database."
        )
        sys.exit(1)

    deployment_id = getattr(args, "deployment_id", None)
    if deployment_id is not None:
        deployment = repo.get(deployment_id)
        if deployment is None:
            console.print(
                f"[bold red]Error:[/bold red] No deployment with id {deployment_id} "
                "in the registry."
            )
            sys.exit(1)
    elif args.name:
        # Resolve to the NEWEST run bearing the name, never merely the newest
        # non-terminal one. The old `get_active_by_name() or get_by_name()`
        # idiom silently mis-targeted a stale row the moment the run being
        # analysed reached a terminal status — which is now routine, because
        # the compute tiers are torn down before extraction (extraction reads
        # InfluxDB, not the nodes). On the 2026-08-11 staging run that wrote
        # STG-01 #513's metrics onto #442, an aborted July deploy, and derived
        # gb_uploaded/gb_downloaded as 0.0 from #442's empty per-op records.
        deployment = repo.get_by_name(args.name)
        if deployment is None:
            console.print(
                f"[bold red]Error:[/bold red] No deployment named '{args.name}' in the registry."
            )
            sys.exit(1)
    else:
        console.print("[bold red]Error:[/bold red] one of --name or --id is required.")
        sys.exit(1)

    influx_url = args.influx_url
    influx_token = args.influx_token
    influx_org = args.influx_org
    influx_bucket = args.influx_bucket

    if not (influx_url and influx_token):
        stack_name = args.monitoring_name or deployment.name
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
        f"[bold]Querying {influx_url} (bucket={influx_bucket}, window={window_label})...[/bold]"
    )
    try:
        # Lazy: each measurement is fetched and parsed while it is consumed,
        # then released. Holding all six as lists cost 6.2 GB on a 990-node run
        # and was half of why this command needed >23 GB.
        #
        # A measurement re-queries if read again, which `_record_derived_metrics`
        # below does — but it reads only `net` and `disk` (~2.7M rows), not
        # `diskio`/`procstat` (~13.3M), so the second pass is cheap.
        points = iter_host_metric_points(
            url=influx_url,
            token=influx_token,
            org=influx_org or "ant",
            bucket=influx_bucket or "testnet-metrics",
            start=args.window,
        )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] InfluxDB query failed: {e}")
        sys.exit(1)

    # Normalise once, then feed both consumers: the whole-run rollup and the
    # time buckets. The delta stage is the expensive part on a large run.
    normalised = normalise_host_metric_points(points)
    aggregates = aggregate_normalised_host_metrics(normalised)
    if not aggregates:
        console.print(
            f"[yellow]No host metric points found for deployment '{deployment.name}' "
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

    bucket_secs = getattr(args, "bucket_secs", 300)
    bucket_count = 0
    if bucket_secs > 0:
        buckets = bucket_host_metric_samples(normalised, bucket_secs=bucket_secs)
        started = time.monotonic()
        bucket_count = repo.replace_host_metric_buckets(
            deployment_id=deployment.id, buckets=buckets
        )
        elapsed = time.monotonic() - started
        console.print(
            f"[bold]Stored {bucket_count} metric bucket(s) "
            f"at {bucket_secs}s resolution in {elapsed:.1f}s.[/bold]"
        )

    table = Table(title=f"Host metrics for deployment '{deployment.name}' (id={deployment.id})")
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
    # One scalar for the same reason as the disk columns: this table is already
    # wide. Peak RSS names the single heaviest service, which is the fleet-level
    # question ("is anything unusually large?"). The per-service breakdown --
    # the thing that actually identifies a leak -- lives in the stored
    # `rss_by_service` and is rendered by `print-recorded-results`.
    table.add_column("Peak svc RSS", justify="right")
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
        peak = peak_rss_service(agg)
        if peak is None:
            rss_label = "-"
        else:
            service, rss = peak
            rss_label = f"{service} {_fmt_bytes(rss.get('max'))}"
        table.add_row(
            agg["host"],
            f"{_fmt(cpu['avg'])} / {_fmt(cpu['min'])} / {_fmt(cpu['max'])}",
            f"{_fmt(mem['avg'])} / {_fmt(mem['min'])} / {_fmt(mem['max'])}",
            f"{_fmt_bw(bin_['avg'])} / {_fmt_bw(bin_['min'])} / {_fmt_bw(bin_['max'])}",
            f"{_fmt_bw(bout['avg'])} / {_fmt_bw(bout['min'])} / {_fmt_bw(bout['max'])}",
            device_label,
            f"{_fmt_bw(drd.get('avg'))} / {_fmt_bw(dwr.get('avg'))}",
            rss_label,
            agg["metrics_started_at"].strftime("%Y-%m-%d %H:%M:%S"),
            agg["metrics_ended_at"].strftime("%Y-%m-%d %H:%M:%S"),
        )
    console.print(table)
    console.print(
        f"[bold green]Recorded host metrics for {len(aggregates)} host(s) "
        f"on deployment '{deployment.name}' (id={deployment.id}).[/bold green]"
    )

    if getattr(args, "derived", True):
        derived_rows = _record_derived_metrics(
            repo=repo,
            deployment=deployment,
            points=points,
            window_start=overall_started,
            window_end=overall_ended,
            console=console,
        )
        _print_derived_metrics(derived_rows, console)

    _warn_about_clock_offsets(aggregates, console)

    if args.summary:
        console.print(
            Panel(
                args.summary,
                title=f"Summary for '{deployment.name}'",
                border_style="cyan",
            )
        )
