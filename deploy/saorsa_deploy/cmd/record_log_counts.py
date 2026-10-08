"""Count named log strings in a run's Elasticsearch and record the counts (V2-842).

Connection errors are a standard measurement on every staging run, and the
counts are what gets kept: the log lines run to hundreds of millions and die
with the deployment's Elasticsearch at teardown, so a run that is not extracted
before its stack is destroyed can never be compared against another.

That is the one hard constraint on this command -- **run it while the run's
Elasticsearch is still up**. There is no backfill and there never can be.

The measurement is deliberately node-tier only. Client logs go to a separate
index with a different document shape, and whether they exist at all depends on
whether the run enabled client log forwarding.

Two structured events are counted alongside the phrases: ant-node's
``pointer_put_rpc`` and ``pointer_get_rpc`` (V2-1339), per outcome and with
latency percentiles. They are the node's own view of the pointer tier, so the
runner can tell "the client saw a stale state" apart from "a node answered
stale" -- a distinction the client-side records cannot make alone. Recorded into
the same table, keyed ``<event>:<outcome>``, so no migration and no second
command.
"""

import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from saorsa_db.models.derived_metric import WINDOW_FULL_RUN
from saorsa_db.models.log_event_count import SOURCE_ELASTICSEARCH

from saorsa_deploy.es_client import ESClient, ESCredentials, ESError
from saorsa_deploy.es_log_counts import (
    POINTER_RPC_EVENTS,
    RPC_LATENCY_PERCENTILES,
    build_count_query,
    build_rpc_latency_query,
    build_total_query,
    collapse_index_date_suffix,
    node_index_pattern,
    parse_count_response,
    parse_rpc_latency_response,
    rate_per_hour,
    summarise_counts,
)
from saorsa_deploy.log_patterns import LogPatternError, load_pattern_set
from saorsa_deploy.registry import get_repository
from saorsa_deploy.state import load_elastic_state


def _parse_iso(value: str | None, flag: str, console: Console) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        console.print(
            f"[bold red]Error:[/bold red] {flag} must be ISO-8601, e.g. "
            f"2026-08-11T20:00:00. Got {value!r}."
        )
        sys.exit(1)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _resolve_deployment(repo, args, console: Console):
    if getattr(args, "id", None) is not None:
        deployment = repo.get(args.id)
        if deployment is None:
            console.print(
                f"[bold red]Error:[/bold red] No deployment with id {args.id} in the registry."
            )
            sys.exit(1)
        return deployment
    deployment = repo.get_active_by_name(args.name) or repo.get_by_name(args.name)
    if deployment is None:
        console.print(
            f"[bold red]Error:[/bold red] No deployment named '{args.name}' in the registry."
        )
        sys.exit(1)
    return deployment


def _resolve_es(args, deployment_name: str, console: Console):
    """Elasticsearch credentials and the index glob to search.

    Prefers explicit flags so a retained stack can still be queried after its
    S3 state has been cleaned up, and falls back to the deployment's elastic
    state, which is the normal path.
    """
    url = getattr(args, "es_url", None)
    username = getattr(args, "es_username", None)
    password = getattr(args, "es_password", None)
    api_key = getattr(args, "es_api_key", None)
    index_pattern = getattr(args, "index_pattern", None)

    if not url:
        stack_name = args.elastic_name or deployment_name
        console.print(
            f"[bold]Loading Elasticsearch credentials from elastic state '{stack_name}'...[/bold]"
        )
        try:
            state = load_elastic_state(stack_name)
        except RuntimeError as exc:
            console.print(
                f"[bold red]Error:[/bold red] {exc}\n\nThese counts can only be "
                "taken while the run's Elasticsearch is alive. If the stack has "
                "been destroyed, the logs are gone and no backfill is possible."
            )
            sys.exit(1)
        url = state.get("elasticsearch_url")
        username = username or state.get("elasticsearch_username")
        password = password or state.get("elasticsearch_password")
        api_key = api_key or state.get("elasticsearch_api_key")
        index_pattern = index_pattern or state.get("elasticsearch_index_pattern")
        if not url:
            console.print(
                f"[bold red]Error:[/bold red] Elasticsearch state for "
                f"'{stack_name}' has no elasticsearch_url."
            )
            sys.exit(1)

    if not index_pattern:
        index_pattern = node_index_pattern(deployment_name)
    # The stored pattern is telegraf's strftime form and matches no index until
    # its date suffix is collapsed to a glob.
    index_pattern = collapse_index_date_suffix(index_pattern)

    try:
        creds = ESCredentials(url=url, username=username, password=password, api_key=api_key)
        creds.auth_header()
    except ValueError:
        console.print(
            "[bold red]Error:[/bold red] No Elasticsearch credentials. Provide "
            "--es-api-key, or --es-username with --es-password, or point "
            "--elastic-name at a deployment whose elastic state holds them."
        )
        sys.exit(1)
    return creds, index_pattern


def _fmt_int(value) -> str:
    return "-" if value is None else f"{value:,}"


def _fmt_rate(value) -> str:
    return "-" if value is None else f"{value:,.1f}/h"


def _fmt_share(count: int, total) -> str:
    if not total:
        return "-"
    return f"{100.0 * count / total:.3f}%"


def _fmt_ms(value) -> str:
    if value is None:
        return "-"
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{value:.0f}ms"


def _record_pointer_rpc_events(
    repo,
    client,
    deployment,
    index_pattern,
    pattern_set,
    args,
    window_start,
    window_end,
    total_documents,
    console,
) -> dict:
    """Count ant-node's pointer RPC latency events, per outcome, and store them.

    Best-effort by design, unlike the phrase counts: a run with no pointer
    clients emits none of these events, and a node build predating pointers has
    no such target at all. Neither is a failure of the recording -- so a query
    error warns and the phrase counts, which are the gated measurement, still
    land.

    Rows go into ``deployment_log_event_counts`` keyed ``<event>:<outcome>``,
    with the event as the group. That needs no migration and puts the node-side
    pointer signal exactly where the runner already looks for counts; the
    latency percentiles ride in ``details``, which is a JSON column.
    """
    out: dict = {}
    for event in POINTER_RPC_EVENTS:
        try:
            response = client.search(
                index_pattern, build_rpc_latency_query(event, window_start, window_end)
            )
        except ESError as exc:
            console.print(
                f"[yellow]Warning: could not count '{event}': {exc}. The phrase "
                f"counts above are unaffected.[/yellow]"
            )
            continue
        parsed = parse_rpc_latency_response(response, event)
        if parsed["count"] == 0:
            # Nothing to store. A zero here is not a finding the way a zero
            # phrase count is: it is what a run with no pointer clients looks
            # like, and storing zeros would read on the next comparison as a
            # pointer tier that stopped working.
            continue
        out[event] = parsed
        for outcome, values in parsed["by_outcome"].items():
            repo.upsert_log_event_count(
                deployment_id=deployment.id,
                event_key=f"{event}:{outcome}",
                event_group=event,
                pattern=f"{event} outcome={outcome}",
                pattern_set=pattern_set.name,
                pattern_version=pattern_set.version,
                window_label=args.label,
                count=values["count"],
                hosts=parsed["hosts"],
                total_documents=total_documents,
                observed_start=parsed["first_seen"],
                observed_end=parsed["last_seen"],
                source=SOURCE_ELASTICSEARCH,
                details={
                    "index_pattern": index_pattern,
                    "event": event,
                    "outcome": outcome,
                    "event_total": parsed["count"],
                    "latency_ms": {
                        str(p): values["latency"].get(p) for p in RPC_LATENCY_PERCENTILES
                    },
                    "event_latency_ms": {
                        str(p): parsed["latency"].get(p) for p in RPC_LATENCY_PERCENTILES
                    },
                    "scope_start": window_start.isoformat() if window_start else None,
                    "scope_end": window_end.isoformat() if window_end else None,
                },
            )
    return out


def _print_rpc_table(rpc_results: dict, total_documents, console: Console) -> None:
    table = Table(title="Pointer RPC events (node-side, ant_node::storage::rpc_latency)")
    table.add_column("Event")
    table.add_column("Outcome")
    table.add_column("Count", justify="right")
    table.add_column("Share of event", justify="right")
    table.add_column("p50", justify="right")
    table.add_column("p95", justify="right")
    for event, parsed in rpc_results.items():
        for outcome, values in parsed["by_outcome"].items():
            table.add_row(
                event,
                outcome,
                _fmt_int(values["count"]),
                _fmt_share(values["count"], parsed["count"]),
                _fmt_ms(values["latency"].get(50.0)),
                _fmt_ms(values["latency"].get(95.0)),
            )
        table.add_row(
            event,
            "[bold]all[/bold]",
            _fmt_int(parsed["count"]),
            _fmt_share(parsed["count"], total_documents),
            _fmt_ms(parsed["latency"].get(50.0)),
            _fmt_ms(parsed["latency"].get(95.0)),
        )
    console.print(table)
    console.print(
        "[dim]These are the NODE's view. `outcome=error` as a fraction of the "
        "event total is the one to read; `stale` here is a node refusing a write "
        "that lost the merge, which is not the same event as a client being "
        "served a stale state (that is `deployments print-pointers`).[/dim]"
    )


def cmd_record_log_counts(args) -> None:
    console = Console()

    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set. "
            "This command requires the registry database."
        )
        sys.exit(1)

    try:
        pattern_set = load_pattern_set(args.patterns)
    except LogPatternError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    window_start = _parse_iso(args.window_start, "--window-start", console)
    window_end = _parse_iso(args.window_end, "--window-end", console)

    # Same rule `derive-metrics` applies to its own label: `full-run` names the
    # whole of what the index holds, so attaching it to a narrower slice would
    # overwrite the whole-run count with a partial one under a name that claims
    # otherwise.
    if (window_start or window_end) and args.label == WINDOW_FULL_RUN:
        console.print(
            "[bold red]Error:[/bold red] --window-start/--window-end narrow the "
            f"count, so it is not the '{WINDOW_FULL_RUN}' figure. Give the slice "
            "its own --label (e.g. steady-state), matching the label used for "
            "`deployments derive-metrics`."
        )
        sys.exit(1)

    deployment = _resolve_deployment(repo, args, console)
    creds, index_pattern = _resolve_es(args, deployment.name, console)
    client = ESClient(creds, timeout=args.timeout)

    console.print(
        f"[bold]Counting {len(pattern_set.patterns)} pattern(s) from "
        f"'{pattern_set.name}' v{pattern_set.version} in {index_pattern}...[/bold]"
    )

    try:
        total_response = client.search(index_pattern, build_total_query(window_start, window_end))
    except ESError as exc:
        console.print(f"[bold red]Error:[/bold red] Elasticsearch query failed: {exc}")
        sys.exit(1)
    totals = parse_count_response(total_response)
    total_documents = totals["count"]

    if total_documents == 0:
        # Not a finding about the release: an index with no documents at all
        # means the query is pointed at the wrong place, or log forwarding was
        # never enabled. Recording zeros here would look like a spectacular
        # improvement on the next comparison.
        console.print(
            f"[bold red]Error:[/bold red] {index_pattern} contains no documents "
            "in this scope. Nothing was recorded — a zero count from an empty "
            "index is indistinguishable from a real absence, and would read as "
            "a total elimination of every error on the next comparison.\n\n"
            "Check that log forwarding was enabled for this run and that the "
            "index pattern is right."
        )
        sys.exit(1)

    results: dict[str, dict] = {}
    for pattern in pattern_set.patterns:
        try:
            response = client.search(
                index_pattern, build_count_query(pattern.phrase, window_start, window_end)
            )
        except ESError as exc:
            console.print(f"[bold red]Error:[/bold red] query for '{pattern.key}' failed: {exc}")
            sys.exit(1)
        results[pattern.key] = parse_count_response(response)

    for pattern in pattern_set.patterns:
        parsed = results[pattern.key]
        repo.upsert_log_event_count(
            deployment_id=deployment.id,
            event_key=pattern.key,
            event_group=pattern.group,
            pattern=pattern.phrase,
            pattern_set=pattern_set.name,
            pattern_version=pattern_set.version,
            window_label=args.label,
            count=parsed["count"],
            hosts=parsed["hosts"],
            total_documents=total_documents,
            observed_start=parsed["first_seen"],
            observed_end=parsed["last_seen"],
            source=SOURCE_ELASTICSEARCH,
            details={
                "module": pattern.module,
                "index_pattern": index_pattern,
                "by_level": parsed["by_level"],
                "top_hosts": parsed["top_hosts"],
                "scope_start": window_start.isoformat() if window_start else None,
                "scope_end": window_end.isoformat() if window_end else None,
                "scope_observed_start": (
                    totals["first_seen"].isoformat() if totals["first_seen"] else None
                ),
                "scope_observed_end": (
                    totals["last_seen"].isoformat() if totals["last_seen"] else None
                ),
            },
        )

    table = Table(
        title=(f"Log event counts — {deployment.name} (id={deployment.id}, window: {args.label})")
    )
    table.add_column("Group")
    table.add_column("Pattern")
    table.add_column("Count", justify="right")
    table.add_column("Hosts", justify="right")
    table.add_column("Rate", justify="right")
    table.add_column("Share of lines", justify="right")
    table.add_column("Levels")
    for pattern in pattern_set.patterns:
        parsed = results[pattern.key]
        levels = ", ".join(
            f"{level}:{count:,}"
            for level, count in sorted(parsed["by_level"].items(), key=lambda kv: -kv[1])
        )
        table.add_row(
            pattern.group,
            pattern.key,
            _fmt_int(parsed["count"]),
            _fmt_int(parsed["hosts"]),
            _fmt_rate(rate_per_hour(parsed["count"], parsed["first_seen"], parsed["last_seen"])),
            _fmt_share(parsed["count"], total_documents),
            levels or "-",
        )
    console.print(table)

    rpc_results = _record_pointer_rpc_events(
        repo,
        client,
        deployment,
        index_pattern,
        pattern_set,
        args,
        window_start,
        window_end,
        total_documents,
        console,
    )
    if rpc_results:
        _print_rpc_table(rpc_results, total_documents, console)

    summary = summarise_counts(results, pattern_set)
    console.print(
        f"[bold green]Recorded {len(results)} log event count(s) across "
        f"{len(summary)} group(s) for deployment '{deployment.name}' "
        f"(id={deployment.id}), against {total_documents:,} forwarded node log "
        f"line(s).[/bold green]"
    )

    zeroed = [key for key, parsed in results.items() if parsed["count"] == 0]
    if zeroed:
        console.print(
            Panel(
                "These patterns matched nothing:\n\n  "
                + "\n  ".join(sorted(zeroed))
                + "\n\nA zero is NOT self-explaining. 'The string is gone because "
                "the bug is fixed' and\n'the string was renamed' look identical "
                "from Elasticsearch, and so does 'the string is\nemitted at DEBUG "
                "and the node forwarder drops DEBUG and TRACE by default'. Check "
                "the\npinned source for the literal before reporting a zero as an "
                "improvement.",
                title="[yellow]Zero counts[/yellow]",
                border_style="yellow",
            )
        )

    if window_start or window_end:
        console.print(
            Panel(
                "A time-filtered count is taken against each host's OWN clock. "
                "Testnet manifests\nskew node clocks deliberately (±3h in a "
                "per-host random direction), and Telegraf\nstamps a document "
                "with the host's clock, so a skewed host contributes a "
                "different\nstretch of its run than its peers do. An unfiltered "
                "count over the whole index has\nno such problem, which is why "
                "it is the default.",
                title="[yellow]Windowed count and clock skew[/yellow]",
                border_style="yellow",
            )
        )
