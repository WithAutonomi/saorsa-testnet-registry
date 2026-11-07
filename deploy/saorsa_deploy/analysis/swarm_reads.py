"""`analysis swarm-reads` — the report an ADR-0026 swarm run is judged by.

The ADR-0023 report (`analysis browser-reads`) answers "did the nodes spread one
hot record's reads"; this one answers the two questions ADR-0026 raises — **do
swarms form among readers on public cloud VMs, and do the nodes serve fewer GETs
for a hot file when they do** — over the two cohorts of one deployment that the
DEV-01 run is made of: a swarm arm (native readers on the branch `ant` plus
browsers on the branch bundle) on the EVEN rounds, and a control arm (native
readers on a released `ant`) on the ODD ones.

Three sources, and each is the only one that could serve its part:

* **Postgres**, for the rows: `deployment_hot_file_download_records` for both
  arms' native readers and `deployment_browser_download_records` for the swarm
  arm's browsers, each narrowed by ``extension``. The VMs are destroyed at the
  end of a run and InfluxDB retention is bounded, so a report that queried
  either could not be produced for the run it is about. The rows come from
  `deployments record-results`, which must therefore have been run first.
* **the run's Elasticsearch, client index**, for the hot file's **chunk set**.
  Nothing else in the registry says which records a file is made of, and the
  browser traces say it exactly: an ``rpc`` record's ``target`` is the chunk hex.
* **the run's Elasticsearch, node index**, for the fleet's own ``get_rpc``
  documents — one per native-path chunk GET on every node. That count, filtered
  to the chunk set and to each round's window, is the node side of the question
  and the numerator of G3.

So **this command must be run before teardown**: `elastic destroy` takes both
indices with it.

Exit 0 when the verdict is PASS, 1 when a gate failed, 2 when the report cannot
be produced at all — no rows for an extension, no trace documents, a chunk set
too small to be a file, an unreachable Elasticsearch, or a recorded parity that
contradicts its arm. The last distinction matters to an unattended runner: "the
swarm made the read path worse" and "I measured nothing" are different answers.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.table import Table

from saorsa_deploy.analysis.es import EsRun, quiet_tls
from saorsa_deploy.analysis.json_out import add_json_out_arg, console_for, emit_json
from saorsa_deploy.browser_reads import derive_period_secs, rounds_in_window
from saorsa_deploy.browser_records import recorded_browser_config
from saorsa_deploy.browser_traces import OP_RPC, fetch_trace_docs
from saorsa_deploy.hot_file_records import (
    HOT_FILE_CONFIG_KEY,
    recorded_hot_file_config,
    recorded_hot_files,
)
from saorsa_deploy.hot_files import hot_files_key, load_hot_files
from saorsa_deploy.node_gets import fetch_round_gets
from saorsa_deploy.registry import get_repository
from saorsa_deploy.state import S3_ADDRESSES_BUCKET
from saorsa_deploy.swarm_reads import (
    ARM_CONTROL,
    ARM_SWARM,
    GATE_FAIL,
    GATE_NOT_ASSESSED,
    GATE_WARN,
    MIN_CHUNK_SET,
    SwarmReadsError,
    arm_of_round,
    as_epoch_bounds,
    build_report,
    check_parities,
    chunk_set_from_traces,
    render_markdown,
    window_round_ids,
)

#: Exit code for "the report cannot be produced", kept apart from 1, which means
#: the report WAS produced and a gate failed.
EXIT_ERROR = 2
EXIT_GATE_FAIL = 1


def _parse_window(value: str, flag: str) -> datetime:
    """An ISO-8601 UTC bound. Raises :class:`SwarmReadsError` on anything else."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SwarmReadsError(
            f"{flag} must be ISO-8601 UTC, e.g. 2026-11-18T12:00:00Z. Got {value!r}."
        )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _resolve_deployment(repo, args):
    """The registry row this report is about.

    ``--id`` wins when given: staging and dev names are reused every train, so
    an id is the only thing that names one run. Without it the active row for
    the name is used, falling back to the most recent.
    """
    if getattr(args, "id", None) is not None:
        deployment = repo.get(args.id)
        if deployment is None:
            raise SwarmReadsError(f"no deployment with id={args.id} in the registry.")
        return deployment
    deployment = repo.get_active_by_name(args.name) or repo.get_by_name(args.name)
    if deployment is None:
        raise SwarmReadsError(
            f"'{args.name}' is not in the registry. Pass --id to point at a "
            f"specific historical row."
        )
    return deployment


def _by_extension(rows, extension: str) -> list:
    return [r for r in rows if _extension_of(r) == extension]


def _extension_of(row):
    return row.get("extension") if isinstance(row, dict) else getattr(row, "extension", None)


def _periods_by_extension(*entry_lists) -> dict:
    """``extension -> {every period its VMs recorded}``, over both role configs.

    Both lists, and as a SET rather than a settled value: the hot-file readers
    and the browsers of one cohort must share the round clock, or "the even
    rounds" means two different things inside one arm. Collecting them here
    rather than deferring to
    :func:`hot_file_records.round_periods_by_extension` is what lets that
    disagreement be reported — that function drops an extension whose VMs
    disagree, which is right for a live view that can print `-` and wrong for a
    comparison that must refuse.
    """
    out: dict = {}
    for entries in entry_lists:
        for entry in entries or []:
            period = entry.get("round_period_secs")
            if not period:
                continue
            out.setdefault(entry.get("extension"), set()).add(int(period))
    return out


def _resolve_period(config, browser_config, rows, swarm: str, control: str) -> tuple[int, str]:
    """The round period both arms ran on, and where it came from.

    **One period, shared.** A round id is ``floor(epoch / period)``, so two
    cohorts on different periods do not share round numbers at all and "even
    rounds versus odd rounds" would be comparing two different clocks. Periods
    that disagree — between the arms, or between the VMs of one arm — are
    therefore a refusal, not an average.

    The recorded role config first (`clients provision` denormalises it into
    ``deploy_args`` precisely so this report survives teardown), then the rows
    themselves, whose ``started_at`` is the scheduled start and so gives the
    period exactly whenever every row agrees.
    """
    by_extension = _periods_by_extension(config, browser_config)
    named = {arm: by_extension[arm] for arm in (swarm, control) if by_extension.get(arm)}
    disagreeing = {arm: sorted(periods) for arm, periods in named.items() if len(periods) > 1}
    if disagreeing:
        raise SwarmReadsError(
            "the VMs of one cohort recorded different round periods ("
            + ", ".join(
                f"{arm}={'/'.join(f'{p}s' for p in periods)}"
                for arm, periods in sorted(disagreeing.items())
            )
            + "). A cohort's readers and browsers derive their round from the "
            "wall clock, so VMs on different periods are not one cohort at all "
            "and its own rounds would not line up. No report is produced."
        )
    settled = {arm: next(iter(periods)) for arm, periods in named.items()}
    if len(set(settled.values())) > 1:
        raise SwarmReadsError(
            "the two cohorts recorded different round periods ("
            + ", ".join(f"{arm}={period}s" for arm, period in sorted(settled.items()))
            + "). A round id is floor(epoch / period), so they do not share "
            "round numbers and the even/odd split is not a comparison. No "
            "report is produced."
        )
    if settled:
        return int(next(iter(settled.values()))), f"deploy_args.{HOT_FILE_CONFIG_KEY}"
    period = derive_period_secs(rows)
    if period:
        return period, "derived from the recorded boundaries"
    raise SwarmReadsError(
        f"the round period is unknown — deploy_args.{HOT_FILE_CONFIG_KEY} "
        f"records none for '{swarm}' or '{control}', and it could not be "
        f"derived from the recorded rounds. Re-run `clients provision` with "
        f"DATABASE_URL set to record it."
    )


def _resolve_hot_files(deployment, name: str) -> tuple[dict, str]:
    """The hot-file selection, and where it came from.

    **The registry snapshot comes first, and the live S3 object is only the
    fallback.** The selection lives at
    ``addresses/<deployment>/browser-hot-files.json`` and the deployment's own
    `clients destroy` deletes that whole prefix, while this report may be run
    after a cohort has gone — so reading S3 first would make the verdict of a
    torn-down run unobtainable. `record-results` snapshots the document in the
    same pass that records the rows.
    """
    recorded = recorded_hot_files(deployment.deploy_args)
    if recorded is not None:
        return recorded, "deploy_args (hot-file selection snapshot)"
    try:
        live = load_hot_files(name)
    except Exception as e:  # noqa: BLE001 -- reported, never a traceback
        raise SwarmReadsError(
            f"no hot-file selection recorded for '{deployment.name}' "
            f"(id={deployment.id}), and s3://{S3_ADDRESSES_BUCKET}/"
            f"{hot_files_key(name)} could not be read: {e}"
        )
    if live is None:
        raise SwarmReadsError(
            f"no hot-file selection for '{name}' — nothing is recorded in "
            f"deploy_args and s3://{S3_ADDRESSES_BUCKET}/{hot_files_key(name)} "
            f"does not exist, so the report cannot say which file the rounds "
            f"read. The S3 object is deleted by the deployment's own `clients "
            f"destroy`; the snapshot is taken by `deployments record-results`, "
            f"so run that BEFORE tearing a cohort down."
        )
    return live, f"s3://{S3_ADDRESSES_BUCKET}/{hot_files_key(name)}"


def _load_trace_docs(run, rounds: list[int], console) -> list[dict]:
    """The swarm rounds' browser trace records, from the run's Elasticsearch.

    Every "cannot read them" condition is a :class:`SwarmReadsError`, so it
    comes out as exit 2 rather than as a traceback or — worse — as an empty
    chunk set, which would read as a file with no records.
    """
    try:
        index = run.client_index()
        docs, counts = fetch_trace_docs(run, rounds)
    except SwarmReadsError:
        raise
    except Exception as e:  # noqa: BLE001 -- reported as exit 2, never a traceback
        raise SwarmReadsError(
            f"the browser trace records could not be read from this "
            f"deployment's Elasticsearch: {e}. The hot file's chunk set comes "
            f"from there and nothing else in the registry carries it, so the "
            f"report cannot be produced. Run this BEFORE teardown — `elastic "
            f"destroy` takes the records with it."
        )
    if counts["unusable"]:
        console.print(
            f"[yellow]{counts['unusable']:,} trace document(s) were missing a "
            f"round, instance, seq or host and could not be attributed to a "
            f"download.[/yellow]"
        )
    if counts["duplicates"]:
        console.print(
            f"[yellow]{counts['duplicates']:,} duplicate trace document(s) "
            f"ignored (same round, host, instance and seq).[/yellow]"
        )
    if not any(d["operation"] == OP_RPC for d in docs):
        raise SwarmReadsError(
            f"no `{OP_RPC}` trace documents in index '{index}' for the swarm "
            f"rounds {rounds[0] if rounds else '-'}..{rounds[-1] if rounds else '-'}, "
            f"so the hot file's chunk set cannot be read and the fleet's GETs "
            f"cannot be filtered to the file. Either the swarm cohort's "
            f"`forward_logs` was off, its bundle was built without `test-utils` "
            f"(no trace at all), or the stack has already been destroyed."
        )
    return docs


def _load_round_gets(run, chunks: list[str], round_ids: list[int], period: int) -> dict[int, dict]:
    """One ``get_rpc`` query per round, keyed by round id.

    Per round rather than one query bucketed by time: the round boundaries are
    the only windows that matter and a ``date_histogram`` would have to be
    offset to them, which is a second place for the period to be got wrong.
    """
    out: dict[int, dict] = {}
    for round_id in round_ids:
        start, end = as_epoch_bounds(round_id, period)
        try:
            out[round_id] = fetch_round_gets(run, chunks, start, end)
        except Exception as e:  # noqa: BLE001 -- reported as exit 2, never a traceback
            raise SwarmReadsError(
                f"the fleet's `get_rpc` documents could not be counted for "
                f"round {round_id} ([{start}, {end})): {e}. That count is G3's "
                f"numerator, so a partial answer is not a report."
            )
    return out


def _render_console(report: dict, console) -> None:
    """A compact view for the transcript; the markdown carries every table."""
    totals = report["totals"]
    header = Table(
        show_header=False,
        title=(
            f"{report['deployment']} swarm reads, "
            f"{report['window_start']} -> {report['window_end']}"
        ),
    )
    header.add_column("measure")
    header.add_column("value", justify="right")
    header.add_row("rounds (missing)", f"{totals['rounds']:,} ({totals['rounds_missing']:,})")
    header.add_row("chunk set", f"{report['chunk_set']['count']:,}")
    for arm in (ARM_SWARM, ARM_CONTROL):
        summary = report["arms"][arm]
        native = summary["native"]
        header.add_row(
            f"{arm} native ok/att",
            f"{native['success']:,} / {native['attempted']:,}",
        )
        header.add_row(
            f"{arm} peer share",
            "n/a" if native["peer_share"] is None else f"{native['peer_share'] * 100:.1f}%",
        )
        header.add_row(
            f"{arm} served GET median",
            "n/a"
            if summary["served_gets_median"] is None
            else f"{summary['served_gets_median']:,.1f}",
        )
    browser = report["arms"][ARM_SWARM]["browser"]
    header.add_row("swarm browser ok/att", f"{browser['success']:,} / {browser['attempted']:,}")
    header.add_row("trace records", f"{totals['trace_records']:,}")
    console.print(header)

    gates = Table(title="Gates")
    gates.add_column("gate")
    gates.add_column("verdict")
    gates.add_column("figure")
    gates.add_column("threshold")
    for gate in report["gates"]:
        if gate["status"] == GATE_FAIL:
            colour = "red"
        elif gate["status"] in (GATE_WARN, GATE_NOT_ASSESSED):
            colour = "yellow"
        else:
            colour = "green"
        gates.add_row(
            gate["name"],
            f"[{colour}]{gate['status']}[/{colour}]",
            gate["figure"],
            gate["threshold"],
        )
    console.print(gates)


def cmd_swarm_reads(args) -> int:
    """Produce the report and return its exit code.

    Every "cannot produce the report" condition raises :class:`SwarmReadsError`
    and comes out as exit 2 here, in one place, so there is no path that exits
    with a code the dispatcher did not choose.
    """
    console = console_for(args.json_out)
    try:
        return _run(args, console)
    except SwarmReadsError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        return EXIT_ERROR


def _run(args, console) -> int:
    window_start = _parse_window(args.window_start, "--window-start")
    window_end = _parse_window(args.window_end, "--window-end")
    if window_end <= window_start:
        raise SwarmReadsError("--window-end must be after --window-start.")
    if args.swarm_extension == args.control_extension:
        raise SwarmReadsError(
            "--swarm-extension and --control-extension name the same extension, "
            "so there are no two arms to compare."
        )

    repo = get_repository()
    if repo is None:
        raise SwarmReadsError(
            "DATABASE_URL is not set or the database is unreachable. The rows "
            "this report is built on are in Postgres."
        )
    deployment = _resolve_deployment(repo, args)

    all_native = repo.list_hot_file_download_records_for_deployment(deployment.id)
    all_browser = repo.list_browser_download_records_for_deployment(deployment.id)
    config = recorded_hot_file_config(deployment.deploy_args)
    browser_config = recorded_browser_config(deployment.deploy_args)
    check_parities([*config, *browser_config], args.swarm_extension, args.control_extension)

    period, period_source = _resolve_period(
        config,
        browser_config,
        [*all_native, *all_browser],
        args.swarm_extension,
        args.control_extension,
    )

    native = rounds_in_window(all_native, period, window_start, window_end)
    browser = rounds_in_window(all_browser, period, window_start, window_end)
    swarm_native = _by_extension(native, args.swarm_extension)
    swarm_browser = _by_extension(browser, args.swarm_extension)
    control_native = _by_extension(native, args.control_extension)
    kept = len(swarm_native) + len(swarm_browser) + len(control_native)
    ignored = len(native) + len(browser) - kept

    window = f"[{window_start.isoformat()}, {window_end.isoformat()}) at a {period}s period"
    if not swarm_native and not swarm_browser:
        raise SwarmReadsError(
            f"no row of the swarm extension '{args.swarm_extension}' falls in "
            f"{window}. '{deployment.name}' (id={deployment.id}) has "
            f"{len(all_native)} native and {len(all_browser)} browser row(s) in "
            f"all; check the extension name and the window, and that "
            f"`deployments record-results` has been run."
        )
    if not control_native:
        raise SwarmReadsError(
            f"no row of the control extension '{args.control_extension}' falls "
            f"in {window}. Without the control arm there is nothing for the "
            f"swarm arm's GET count to be compared against."
        )

    hot_files, hot_files_source = _resolve_hot_files(deployment, args.name)

    quiet_tls()
    run = EsRun(args.name)
    swarm_rounds = [
        round_id
        for round_id in window_round_ids(period, window_start, window_end)
        if arm_of_round(round_id) == ARM_SWARM
    ]
    trace_docs = _load_trace_docs(run, swarm_rounds, console)
    chunk_set = chunk_set_from_traces(trace_docs, swarm_rounds)
    if chunk_set["count"] < MIN_CHUNK_SET:
        raise SwarmReadsError(
            f"only {chunk_set['count']} distinct chunk address(es) in the swarm "
            f"rounds' trace records, which does not describe a file (a 300 MiB "
            f"one is hundreds of records). The fleet's GETs would be filtered "
            f"to almost nothing and G3 would divide one stray address by "
            f"another, so no report is produced. The floor is {MIN_CHUNK_SET}."
        )
    if len(hot_files.get("files") or []) > 1:
        console.print(
            f"[yellow]The selection carries "
            f"{len(hot_files['files'])} hot files, so the chunk set spans every "
            f"file the swarm rounds read and the GET counts are for all of "
            f"them together.[/yellow]"
        )

    gets_by_round = _load_round_gets(
        run,
        chunk_set["chunks"],
        window_round_ids(period, window_start, window_end),
        period,
    )

    report = build_report(
        deployment=deployment.name,
        deployment_id=deployment.id,
        swarm_extension=args.swarm_extension,
        control_extension=args.control_extension,
        swarm_native=swarm_native,
        swarm_browser=swarm_browser,
        control_native=control_native,
        ignored_rows=ignored,
        config_entries=config,
        browser_config_entries=browser_config,
        hot_files=hot_files,
        hot_files_source=hot_files_source,
        chunk_set=chunk_set,
        gets_by_round=gets_by_round,
        trace_docs=trace_docs,
        period_secs=period,
        period_source=period_source,
        window_start=window_start,
        window_end=window_end,
        min_completion=args.min_completion,
        min_peer_share=args.min_peer_share,
        max_get_ratio=args.max_get_ratio,
    )

    markdown = render_markdown(report)
    if args.markdown_out:
        with open(args.markdown_out, "w") as fh:
            fh.write(markdown)
        console.print(f"wrote {args.markdown_out}")
    if getattr(args, "print_markdown", False):
        # Through the console, so `--json-out -` suppresses it: the JSON is the
        # whole of stdout in that mode and a markdown table in front of it is
        # what makes `jq` abort.
        console.print(markdown, markup=False, highlight=False)
    else:
        _render_console(report, console)
    emit_json(report, args.json_out, console)

    return EXIT_GATE_FAIL if report["verdict"] == GATE_FAIL else 0


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "swarm-reads",
        help=(
            "The ADR-0026 swarm report: a swarm cohort (native + browser, even "
            "rounds) against a control cohort (released native, odd rounds) on "
            "one deployment — completion, peer share, the fleet's `get_rpc` "
            "GETs for the hot file per round, download times, and gates G1-G4. "
            "Reads the recorded Postgres rows and the run's own Elasticsearch, "
            "so RUN IT BEFORE TEARDOWN."
        ),
    )
    p.add_argument(
        "--name",
        required=True,
        help=(
            "Deployment name (e.g. DEV-01). Also what names the hot-file "
            "selection in S3 and the Elasticsearch indices the browser traces "
            "and the node `get_rpc` documents are read from."
        ),
    )
    p.add_argument(
        "--id",
        type=int,
        default=None,
        help=(
            "Deployment id, which is what names ONE run — staging and dev names "
            "are reused every train. Without it the active row for --name is "
            "used, falling back to the most recent."
        ),
    )
    p.add_argument(
        "--window-start",
        required=True,
        help=(
            "Window start, ISO-8601 UTC. A round is in the window when its "
            "boundary (round_id x period) is at or after this."
        ),
    )
    p.add_argument(
        "--window-end",
        required=True,
        help=(
            "Window end, ISO-8601 UTC, EXCLUSIVE — so two adjacent windows "
            "partition a run's rounds exactly once between them."
        ),
    )
    p.add_argument(
        "--swarm-extension",
        required=True,
        help=(
            "The `kind: clients` extension whose rows are the swarm arm: its "
            "native hot-file readers AND its browsers. It runs the EVEN rounds, "
            "and a recorded `round_parity: odd` on one of its VMs is refused."
        ),
    )
    p.add_argument(
        "--control-extension",
        required=True,
        help=(
            "The extension whose native rows are the control arm — the released "
            "`ant`, with no swarm. It runs the ODD rounds, and a recorded "
            "`round_parity: even` on one of its VMs is refused."
        ),
    )
    p.add_argument(
        "--min-completion",
        type=float,
        default=0.95,
        help=(
            "G1: the floor on successes/attempts over the window, for each arm's "
            "native readers and for the swarm arm's browsers (default 0.95). "
            "All three figures must pass; a cohort that attempted nothing is "
            "NOT ASSESSED rather than 0%%."
        ),
    )
    p.add_argument(
        "--min-peer-share",
        type=float,
        default=0.80,
        help=(
            "G2: the floor on the share of rows that took at least one record "
            "from a peer, in EVERY swarm round and for natives and browsers "
            "alike (default 0.80). Per round, not over the window: a round "
            "whose swarm failed to form and recovered has still had an outage."
        ),
    )
    p.add_argument(
        "--max-get-ratio",
        type=float,
        default=0.80,
        help=(
            "G3: the ceiling on the median served `get_rpc` for the file over "
            "the swarm rounds divided by the median over the control rounds "
            "(default 0.80). NOT ASSESSED below two present rounds in either "
            "arm, or when the control arm served none."
        ),
    )
    p.add_argument(
        "--markdown-out",
        default=None,
        help="Write the fixed-format markdown report here.",
    )
    p.add_argument(
        "--print-markdown",
        action="store_true",
        help=(
            "Print the markdown report instead of the compact tables. "
            "Suppressed when --json-out is `-`."
        ),
    )
    add_json_out_arg(p)
    p.set_defaults(func=cmd_swarm_reads)
