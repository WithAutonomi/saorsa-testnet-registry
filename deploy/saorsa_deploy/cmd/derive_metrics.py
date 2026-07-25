"""Re-derive the normalised per-GB metrics for an arbitrary window.

`record-analysis` computes these for the whole run at extraction time, from raw
Influx points, exactly. This command computes them for a *slice* of a run —
typically the steady-state phase — from data already in Postgres, so it still
works after the monitoring stack (and its InfluxDB) has been destroyed. That is
what the time-bucketed host metrics exist for.

The two paths are not equivalent and the stored row says which it is:

* ``source=capture`` — outbound bytes summed from raw counter deltas. Exact.
* ``source=buckets`` — outbound bytes integrated from bucketed per-second
  averages (``avg × bucket width``), and storage growth taken between the first
  and last bucket average in the window. Approximate, bounded by the bucket
  width.

The denominator is exact on both paths: per-op upload/download records carry
their own timestamps and are simply filtered to the window.
"""
import sys
from datetime import datetime, timedelta, timezone

from rich.console import Console
from rich.table import Table
from saorsa_db.models.derived_metric import (
    RATIO_METRICS,
    SOURCE_BUCKETS,
    WINDOW_FULL_RUN,
)
from saorsa_db.models.host_metric_bucket import (
    METRIC_BYTES_IN_PER_SEC,
    METRIC_BYTES_OUT_PER_SEC,
    METRIC_DISK_USED_BYTES,
)

from saorsa_deploy.derived_metrics import (
    build_metric_rows,
    format_metric_value,
    infer_bucket_secs,
    sum_node_storage_growth_from_endpoints,
    sum_outbound_from_buckets,
    sum_traffic_gb,
)
from saorsa_deploy.registry import get_repository


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive timestamp.

    Postgres returns timezone-aware values, but SQLite (the test database, and
    any local inspection) drops the offset, and comparing the two raises. Every
    timestamp stored by this system is UTC, so attaching it is a restatement of
    fact rather than a conversion.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_ts(value: str, label: str, console: Console) -> datetime:
    """Parse an ISO-8601 timestamp, defaulting a naive one to UTC.

    Naive input is treated as UTC rather than as local time: every timestamp in
    the registry and in Influx is UTC, and silently reinterpreting an operator's
    "01:00" as their local hour would slice the wrong window.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        console.print(
            f"[bold red]Error:[/bold red] {label} '{value}' is not an ISO-8601 "
            "timestamp (e.g. 2026-07-25T09:00:00)."
        )
        sys.exit(1)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def cmd_derive_metrics(args) -> None:
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

    if args.label == WINDOW_FULL_RUN:
        # Refused rather than warned: `record-analysis` writes that label from
        # raw counter deltas, and this path would silently replace an exact
        # value with an integrated approximation under the same name -- the
        # precise confusion the `source` column exists to prevent.
        console.print(
            f"[bold red]Error:[/bold red] '{WINDOW_FULL_RUN}' is written by "
            "`deployments record-analysis` from exact raw counters. Choose "
            "another label (e.g. --label steady-state) so the two are not "
            "conflated."
        )
        sys.exit(1)

    span = repo.get_host_metric_bucket_span(deployment.id)
    if span is None:
        console.print(
            f"[bold red]Error:[/bold red] Deployment '{args.name}' has no host "
            "metric buckets, so no window can be re-derived. Either it was "
            "recorded before bucketing existed, its buckets were pruned, or "
            "`record-analysis` was run with --bucket-secs 0. No backfill is "
            "possible: the source InfluxDB is destroyed with the monitoring "
            "stack."
        )
        sys.exit(1)

    first_bucket, last_bucket = _as_utc(span[0]), _as_utc(span[1])

    # Left unbounded when the operator does not narrow that edge, rather than
    # pinned to the span. The bucket filter is half-open on `bucket_start`, so
    # `end = last_bucket` would silently drop the final bucket -- the whole run
    # minus its last five minutes, reported as the whole run.
    start = (
        _parse_ts(args.window_start, "--window-start", console)
        if args.window_start
        else None
    )
    end = (
        _parse_ts(args.window_end, "--window-end", console)
        if args.window_end
        else None
    )
    if start is not None and end is not None and end <= start:
        console.print(
            "[bold red]Error:[/bold red] --window-end must be after "
            "--window-start."
        )
        sys.exit(1)

    if (start is not None and start > last_bucket) or (
        end is not None and end <= first_bucket
    ):
        console.print(
            "[bold red]Error:[/bold red] That window does not overlap the "
            f"stored buckets ({first_bucket:%Y-%m-%d %H:%M:%S} — "
            f"{last_bucket:%Y-%m-%d %H:%M:%S} UTC)."
        )
        sys.exit(1)
    if (start is not None and start < first_bucket) or (
        end is not None and end > last_bucket
    ):
        # Not an error: a caller may deliberately pass the phase boundaries from
        # a runbook. But a window reaching outside the stored buckets silently
        # measures less than it appears to.
        console.print(
            f"[yellow]The requested window extends beyond the stored buckets "
            f"({first_bucket:%Y-%m-%d %H:%M:%S} — "
            f"{last_bucket:%Y-%m-%d %H:%M:%S} UTC). "
            "Only the overlapping part is measured.[/yellow]"
        )

    buckets = repo.get_host_metric_buckets_in_window(
        deployment_id=deployment.id, start=start, end=end
    )
    if not buckets:
        console.print(
            "[bold red]Error:[/bold red] No buckets fall inside that window. "
            f"Stored buckets span {first_bucket:%Y-%m-%d %H:%M:%S} — "
            f"{last_bucket:%Y-%m-%d %H:%M:%S} UTC."
        )
        sys.exit(1)

    # Inferred rather than assumed: the width the buckets were WRITTEN at is not
    # recorded on the row, and integrating an average at the wrong width is
    # wrong by exactly that factor with nothing to signal it.
    bucket_secs = args.bucket_secs or infer_bucket_secs(buckets)

    # What gets recorded on the row. An unbounded edge is reported as the extent
    # of the data it actually covered: the last bucket's coverage runs to its
    # start plus one width, not to its start.
    window_start = start or first_bucket
    window_end = end or (last_bucket + timedelta(seconds=bucket_secs))
    console.print(
        f"[bold]Re-deriving '{args.label}' for '{args.name}' over "
        f"{window_start:%Y-%m-%d %H:%M:%S} — {window_end:%Y-%m-%d %H:%M:%S} "
        f"UTC (bucket width {bucket_secs}s)...[/bold]"
    )

    outbound = sum_outbound_from_buckets(
        buckets, bucket_secs, METRIC_BYTES_OUT_PER_SEC
    )
    inbound = sum_outbound_from_buckets(
        buckets, bucket_secs, METRIC_BYTES_IN_PER_SEC
    )
    storage_growth = sum_node_storage_growth_from_endpoints(
        repo.get_host_metric_endpoints(
            deployment_id=deployment.id,
            metric=METRIC_DISK_USED_BYTES,
            start=start,
            end=end,
        )
    )

    uploads = repo.list_upload_records_for_deployment(deployment.id)
    downloads = repo.list_download_records_for_deployment(deployment.id)
    if not uploads and not downloads:
        console.print(
            "[yellow]No upload or download records for this deployment — the "
            "per-GB ratios have no denominator and will be omitted. Run "
            f"`deployments record-results --name {args.name}` while the "
            "monitoring stack is still up.[/yellow]"
        )

    rows = build_metric_rows(
        outbound=outbound,
        inbound=inbound,
        storage_growth=storage_growth,
        uploaded=sum_traffic_gb(uploads, start=start, end=end),
        downloaded=sum_traffic_gb(downloads, start=start, end=end),
        window_start=window_start,
        window_end=window_end,
        window_label=args.label,
        source=SOURCE_BUCKETS,
    )
    for row in rows:
        repo.upsert_derived_metric(deployment_id=deployment.id, **row)

    table = Table(title=f"Derived metrics for '{args.name}' (window: {args.label})")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_column("Denominator", justify="right")
    for row in rows:
        denominator = row.get("denominator")
        table.add_row(
            row["metric"],
            format_metric_value(row.get("value"), row["unit"]),
            f"{denominator:.2f} GB" if denominator is not None else "-",
        )
    console.print(table)

    missing = [m for m in RATIO_METRICS if m not in {r["metric"] for r in rows}]
    if missing:
        console.print(
            "[yellow]Not derived (no measurable value): "
            + ", ".join(missing)
            + "[/yellow]"
        )

    console.print(
        f"[bold green]Stored {len(rows)} metric(s) for window "
        f"'{args.label}'.[/bold green] These are re-derived from buckets and "
        "are marked source=buckets: outbound bytes are integrated from "
        "per-bucket averages rather than summed from raw counters, so do not "
        "compare them against a source=capture value."
    )
