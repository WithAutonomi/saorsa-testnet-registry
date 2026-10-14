"""`saorsa-deploy deployments print-pointers` — the pointer tier's results.

Two sources, one renderer:

* ``--name`` reads the deployment's **InfluxDB** and is the LIVE view, usable
  while the run is going on. It is the only source that exists before
  `record-results` has run, and the only one that exists at all if the run is
  never extracted.
* ``--id`` reads **Postgres** (``deployment_pointer_records``) and is the
  post-run view. It survives teardown, which Influx does not, and it is the one
  a comparison between two runs is made from.

Both go through :mod:`saorsa_deploy.pointer_results`, so the live table and the
recorded table cannot disagree about what a rate is.

The "Pointer handovers" section (V2-1354) reports the handover CYCLE rather than
any one op: a handover is nine steps landing in four ops, and what an unattended
run gates on is whether the cycle completed and whether the close group still
holds the final state later. Its two load-bearing conventions -- the silent-peer
refusals excluded from the completion denominator, and a take-back's success
being a refusal that cost nothing -- are explained on the output itself.

``--by-age`` is the second table, and the reason the age is stored per op:
whether a pointer decays -- becomes stale or unreadable as it gets older -- is
the question the deployment exists to answer, and it is only visible as a rate
against age. Sweeps only, own and foreign separately; see
:func:`~saorsa_deploy.pointer_results.age_buckets` for why.
"""

import sys
from datetime import datetime, timezone
from decimal import Decimal

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from saorsa_deploy.influx import fetch_pointer_points
from saorsa_deploy.pointer_results import (
    DEFAULT_AGE_BUCKET_SECS,
    FINALITY_STATUSES,
    OPS,
    OUTCOMES,
    SWEEP_FINALITY_MIN_AGE_SECS,
    SWEEP_OUTCOMES,
    age_buckets,
    filter_to_window,
    finality_age_buckets,
    normalise_records,
    summarise,
    summarise_by_host,
    summarise_handovers,
)
from saorsa_deploy.registry import get_repository
from saorsa_deploy.state import load_monitoring_state

_ATTO_PER_UNIT = Decimal(10) ** 18


def _parse_iso(value: str | None, flag: str, console: Console):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        console.print(
            f"[bold red]Error:[/bold red] {flag} must be ISO-8601, e.g. "
            f"2026-09-26T20:00:00. Got {value!r}."
        )
        sys.exit(1)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _fmt_rate(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}%"


def _fmt_ms(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{value:.0f}ms"


def _fmt_token(value: Decimal | None, symbol: str) -> str:
    if value is None:
        return "-"
    return f"{(value / _ATTO_PER_UNIT).normalize():f} {symbol}"


def _resolve_influx(args, console: Console):
    """Influx credentials, from flags or the deployment's monitoring state."""
    url = getattr(args, "influx_url", None)
    token = getattr(args, "influx_token", None)
    org = getattr(args, "influx_org", None) or "ant"
    bucket = getattr(args, "influx_bucket", None) or "testnet-metrics"
    if url and token:
        return url, token, org, bucket
    stack = getattr(args, "monitoring_name", None) or args.name
    try:
        state = load_monitoring_state(stack)
    except RuntimeError as exc:
        console.print(
            f"[bold red]Error:[/bold red] {exc}\n\nPointer operations are read "
            "from the run's InfluxDB. If the monitoring stack has been "
            "destroyed, read the recorded table instead: "
            "`deployments print-pointers --id <id>`."
        )
        sys.exit(1)
    url = url or state.get("influxdb_url")
    token = token or state.get("influxdb_token")
    if not (url and token):
        console.print(
            f"[bold red]Error:[/bold red] monitoring state for '{stack}' has no InfluxDB URL/token."
        )
        sys.exit(1)
    return url, token, state.get("influxdb_org") or org, bucket


def _print_summary_table(title: str, summaries: dict, console: Console) -> None:
    table = Table(title=title)
    table.add_column("Host")
    table.add_column("Op")
    table.add_column("Attempts", justify="right")
    for outcome in OUTCOMES:
        table.add_column(outcome, justify="right")
    table.add_column("ok %", justify="right")
    for host, summary in summaries.items():
        for op in OPS:
            counts = summary.by_op[op]
            if not counts.attempts:
                continue
            table.add_row(
                host,
                op,
                str(counts.attempts),
                *(str(counts.outcomes.get(o, 0)) for o in OUTCOMES),
                _fmt_rate(counts.success_rate),
            )
    console.print(table)


def _print_headline(pooled, console: Console) -> None:
    table = Table(title="Pointer totals", show_header=False)
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row(
        "Write success",
        f"{pooled.write_ok}/{pooled.write_attempts} ({_fmt_rate(pooled.write_success_rate)})",
    )
    table.add_row(
        "Read success",
        f"{pooled.read_ok}/{pooled.read_attempts} ({_fmt_rate(pooled.read_success_rate)})",
    )
    table.add_row("Stale reads", f"{pooled.stale_reads} ({_fmt_rate(pooled.stale_read_rate)})")
    table.add_row("Lost writes", f"{pooled.lost_writes} ({_fmt_rate(pooled.lost_write_rate)})")
    table.add_row(
        "Read-your-writes matched",
        f"{pooled.read_your_writes_ok}/{pooled.read_your_writes_total}",
    )
    table.add_row("RPC-error writes", str(pooled.rpc_error_writes))
    table.add_row("Double payments", str(pooled.double_payments))
    table.add_row("Paid but not stored", str(pooled.paid_but_failed_writes))
    table.add_row(
        "Write latency p50 / p95",
        f"{_fmt_ms(pooled.write_latency(0.50))} / {_fmt_ms(pooled.write_latency(0.95))}",
    )
    table.add_row(
        "Read latency p50 / p95",
        f"{_fmt_ms(pooled.read_latency(0.50))} / {_fmt_ms(pooled.read_latency(0.95))}",
    )
    table.add_row(
        "Mean cost per write",
        f"{_fmt_token(pooled.mean_ant_atto_per_write, 'ANT')} + "
        f"{_fmt_token(pooled.mean_gas_wei_per_write, 'ETH')}",
    )
    console.print(table)
    console.print(
        "[dim]A non-zero stale-read rate is EXPECTED (ADR-0016: an eclipsed "
        "reader can be served an older, validly signed state and cannot tell). "
        "Only its trend against the baseline is a signal.[/dim]"
    )


def _print_handovers(records: list[dict], console: Console) -> None:
    """The "Pointer handovers" section (V2-1354).

    Printed whenever a `transfer` record exists, and announced as absent when
    none does: a section that vanishes reads as a measurement nobody took, which
    on a train whose RC predates handovers is exactly the wrong impression.
    """
    handovers = summarise_handovers(records)
    if not handovers.attempts:
        console.print("[dim]no handovers recorded[/dim]")
        return

    table = Table(title="Pointer handovers", show_header=False)
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Attempts", str(handovers.attempts))
    table.add_row(
        "Completed",
        f"{handovers.completed}/{handovers.completion_denominator} "
        f"({_fmt_rate(handovers.completion_rate)})",
    )
    table.add_row(
        "Refused before paying — silent peer",
        f"{handovers.refused_silent_peer} "
        f"({_fmt_rate(handovers.silent_peer_refusal_rate)} of attempts)",
    )
    table.add_row(
        "Refused before paying — rival final",
        str(handovers.refused_rival_final),
    )
    table.add_row("Lost writes", str(handovers.lost_writes))
    table.add_row("Errors", str(handovers.errors))
    table.add_row(
        "Handover latency p50 / p95",
        f"{_fmt_ms(handovers.transfer_latency(0.50))} / "
        f"{_fmt_ms(handovers.transfer_latency(0.95))}",
    )
    table.add_row(
        "Mean cost per handover",
        f"{_fmt_token(handovers.mean_ant_atto_per_handover, 'ANT')} + "
        f"{_fmt_token(handovers.mean_gas_wei_per_handover, 'ETH')}",
    )
    table.add_row(
        "Redirect reads ok",
        f"{handovers.redirect_ok}/{handovers.redirect_attempts} "
        f"({_fmt_rate(handovers.redirect_ok_rate)})",
    )
    table.add_row(
        "Controller ok",
        f"{handovers.controller_ok}/{handovers.controller_attempts} "
        f"({_fmt_rate(handovers.controller_ok_rate)})",
    )
    table.add_row(
        "Resolve ok",
        f"{handovers.resolve_ok}/{handovers.resolve_attempts} "
        f"({_fmt_rate(handovers.resolve_ok_rate)})",
    )
    console.print(table)
    console.print(
        "[dim]The silent-peer refusals are EXCLUDED from the completion "
        "denominator: `transfer` declining to act when a close-group peer did "
        "not answer is the pre-payment guard working, not a handover that "
        "failed. A handover cannot be taken back, so it is refused rather than "
        "risked.[/dim]"
    )

    finality = Table(title="Finality at the check immediately after the transfer")
    finality.add_column("Status")
    finality.add_column("Handovers", justify="right")
    finality.add_column("%", justify="right")
    for status in FINALITY_STATUSES:
        finality.add_row(
            status,
            str(handovers.first_finality.get(status, 0)),
            _fmt_rate(handovers.first_finality_rate(status)),
        )
    console.print(finality)
    console.print(
        f"[dim]One row per handed-over address (its FIRST finality check). "
        f"`settling` here is a close group that has not converged yet, not a "
        f"fault; the aged view is the `--by-age` table and the gate, which read "
        f"checks at least {SWEEP_FINALITY_MIN_AGE_SECS}s old "
        f"({_fmt_rate(handovers.sweep_finality_ok_rate)} final over "
        f"{handovers.aged_finality_total} aged check(s)).[/dim]"
    )

    takebacks = Table(title="Take-backs (a refusal is the PASS)")
    takebacks.add_column("Attempt")
    takebacks.add_column("Attempts", justify="right")
    takebacks.add_column("Refused", justify="right")
    takebacks.add_column("Accepted or spent", justify="right")
    takebacks.add_column("Errors", justify="right")
    takebacks.add_column("refused %", justify="right")
    for counts in handovers.takebacks.values():
        takebacks.add_row(
            counts.attempt,
            str(counts.attempts),
            str(counts.refused),
            str(counts.accepted_or_spent),
            str(counts.errors),
            _fmt_rate(counts.refused_rate),
        )
    console.print(takebacks)
    console.print(
        "[dim]`Accepted or spent` pools the two failures on purpose: a write "
        "that was refused but still cost ANT or gas is not a refusal, and the "
        "ADR's claim is that a handover cannot be taken back at all. A take-back "
        "whose wallet could not be read is an ERROR, never a pass — unknown is "
        "not zero.[/dim]"
    )


def _print_finality_age_table(records: list[dict], bucket_secs: int, console: Console) -> None:
    """Finality by age since handover. Only `finality` points count."""
    buckets = finality_age_buckets(records, bucket_secs)
    if not buckets:
        return
    table = Table(title=(f"Finality by age since handover ({bucket_secs // 60}-minute buckets)"))
    table.add_column("Owner")
    table.add_column("Age")
    table.add_column("Reads", justify="right")
    for status in FINALITY_STATUSES:
        table.add_column(status, justify="right")
    table.add_column("final %", justify="right")
    for bucket in buckets:
        table.add_row(
            "own" if bucket.owned else "foreign",
            bucket.label,
            str(bucket.attempts),
            *(str(bucket.outcomes.get(s, 0)) for s in FINALITY_STATUSES),
            _fmt_rate(bucket.rate("final")),
        )
    console.print(table)
    console.print(
        "[dim]`finality` asks the WHOLE close group and never stops early, which "
        "is what makes it different from a read. Own and foreign are separate "
        "because whether a handover looks the same from the other side of the "
        "world is the question the second pointer client answers — and because "
        "a FOREIGN row's age is measured from when this client first saw the "
        "published pair, not from the handover: the transfers file carries no "
        "timestamp, so foreign ages are a LOWER BOUND and those rows sit in a "
        "younger bucket than they belong in.[/dim]"
    )


def _print_age_table(records: list[dict], bucket_secs: int, console: Console) -> None:
    buckets = age_buckets(records, bucket_secs)
    if not buckets:
        console.print(
            "[yellow]No sweep records carry an age, so there is nothing to "
            "bucket. Sweeps begin one sweep_interval_secs after the service "
            "starts.[/yellow]"
        )
        return
    table = Table(
        title=(f"Sweep outcomes by pointer age at read ({bucket_secs // 60}-minute buckets)")
    )
    table.add_column("Owner")
    table.add_column("Age")
    table.add_column("Reads", justify="right")
    # The seven read outcomes, not every outcome: the three finality statuses
    # cannot occur on a sweep row, and three always-zero columns in an already
    # wide table buy nothing.
    for outcome in SWEEP_OUTCOMES:
        table.add_column(outcome, justify="right")
    table.add_column("ok %", justify="right")
    table.add_column("stale %", justify="right")
    for bucket in buckets:
        table.add_row(
            "own" if bucket.owned else "foreign",
            bucket.label,
            str(bucket.attempts),
            *(str(bucket.outcomes.get(o, 0)) for o in SWEEP_OUTCOMES),
            _fmt_rate(bucket.ok_rate),
            _fmt_rate(bucket.rate("stale")),
        )
    console.print(table)
    console.print(
        "[dim]Sweeps only: a `get` is the read that immediately confirms a "
        "write, so its age is always ~0 and it is not an aged sample. Own and "
        "foreign are separate because only for an owned pointer is the expected "
        "state known.[/dim]"
    )


def cmd_print_pointers(args) -> None:
    console = Console()
    window_start = _parse_iso(getattr(args, "window_start", None), "--window-start", console)
    window_end = _parse_iso(getattr(args, "window_end", None), "--window-end", console)

    if getattr(args, "id", None) is not None:
        records, source = _read_from_postgres(args, console)
    else:
        records, source = _read_from_influx(args, console)

    if window_start or window_end:
        before = len(records)
        records = filter_to_window(records, window_start, window_end)
        console.print(
            f"[dim]Window: {before} operation(s) narrowed to {len(records)} on finished_at.[/dim]"
        )

    if not records:
        console.print(
            "[yellow]No pointer operations found. A run with no "
            "clients.pointers[] entries has none; otherwise check that the "
            "ant-pointer service is running (`systemctl status ant-pointer`) "
            "and that monitoring is enabled.[/yellow]"
        )
        return

    console.print(Panel(source, border_style="dim"))
    _print_headline(summarise(records), console)
    _print_summary_table("Per-host pointer operations", summarise_by_host(records), console)
    _print_handovers(records, console)
    if getattr(args, "by_age", False):
        bucket_secs = getattr(args, "age_bucket_secs", None) or DEFAULT_AGE_BUCKET_SECS
        _print_age_table(records, bucket_secs, console)
        _print_finality_age_table(records, bucket_secs, console)


def _read_from_postgres(args, console: Console) -> tuple[list[dict], str]:
    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set, so --id "
            "cannot read the recorded table."
        )
        sys.exit(1)
    deployment = repo.get(args.id)
    if deployment is None:
        console.print(
            f"[bold red]Error:[/bold red] No deployment with id {args.id} in the registry."
        )
        sys.exit(1)
    rows = repo.list_pointer_records_for_deployment(deployment.id)
    return (
        normalise_records(rows),
        f"Source: Postgres deployment_pointer_records — {deployment.name} "
        f"(id={deployment.id}), {len(rows)} row(s). Run `deployments "
        f"record-results` first if this is short of what Influx holds.",
    )


def _read_from_influx(args, console: Console) -> tuple[list[dict], str]:
    url, token, org, bucket = _resolve_influx(args, console)
    console.print(f"[bold]Querying {url} (bucket={bucket})...[/bold]")
    try:
        points = fetch_pointer_points(url, token, org, bucket, "0")
    except Exception as exc:
        console.print(f"[bold red]Error:[/bold red] InfluxDB query failed: {exc}")
        sys.exit(1)
    # Influx's own server-receive time is the op's finish time, and is what the
    # window filter needs. The client's `start_ns` is deliberately not used for
    # it: on a clock-skewed VM it can be hours out.
    for point in points:
        point["finished_at"] = point["_time"]
    return (
        points,
        f"Source: InfluxDB {url} (bucket={bucket}) — live, {len(points)} "
        f"operation(s). Read the recorded table with --id after "
        f"`record-results`; Influx dies with the monitoring stack.",
    )
