"""`analysis browser-reads` — the report a hot-read run is judged by (V2-1618).

Reads **Postgres only**, and that is the point: the browser VMs are destroyed at
the end of a run and their InfluxDB retention is bounded, so a report that
queried either could not be produced for the run it is about. The rows come from
`deployments record-results`, which must therefore have been run first.

Four gates, and they are separate from the staging gates in `deployments
compare` on purpose: this one answers whether hot-record read spreading
(ADR-0023) is working, which is a question about ONE file read by many browsers
at once — nothing the native or WASM tiers measure. Exit 0 when all four pass, 1
when any fails, 2 when the report cannot be produced at all (no rows in the
window, no hot-file selection, no round period).

The holder-share gate (G3) reads ``first_reads`` and nothing else. The holder a
chunk's FIRST read went to is the one the spreading change moves; a fallback to a
second holder has already paid the first one's `NotFound` or silence, so counting
it would credit the read path for a retry.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.table import Table

from saorsa_deploy.analysis.json_out import add_json_out_arg, console_for, emit_json
from saorsa_deploy.browser_reads import (
    GATE_FAIL,
    BrowserReadsError,
    build_report,
    derive_period_secs,
    render_markdown,
    rounds_in_window,
)
from saorsa_deploy.browser_records import (
    BROWSER_HOT_FILES_KEY,
    expected_instances,
    recorded_browser_config,
    recorded_hot_files,
    round_period_from_config,
)
from saorsa_deploy.hot_files import hot_files_key, load_hot_files
from saorsa_deploy.registry import get_repository
from saorsa_deploy.state import S3_ADDRESSES_BUCKET

#: Exit code for "the report cannot be produced", kept apart from 1, which means
#: the report WAS produced and a gate failed. An unattended runner has to tell
#: "the release made the read path worse" from "I measured nothing".
EXIT_ERROR = 2
EXIT_GATE_FAIL = 1


def _parse_window(value: str, flag: str) -> datetime:
    """An ISO-8601 UTC bound. Raises :class:`BrowserReadsError` on anything else."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise BrowserReadsError(
            f"{flag} must be ISO-8601 UTC, e.g. 2026-10-18T12:00:00Z. Got {value!r}."
        )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _resolve_deployment(repo, args):
    """The registry row this report is about.

    ``--id`` wins when given: staging and dev names are reused every train, so
    an id is the only thing that names one run. Without it the active row for
    the name is used, falling back to the most recent — which is what every
    other `--name` command here does.
    """
    if getattr(args, "id", None) is not None:
        deployment = repo.get(args.id)
        if deployment is None:
            raise BrowserReadsError(f"no deployment with id={args.id} in the registry.")
        return deployment
    deployment = repo.get_active_by_name(args.name) or repo.get_by_name(args.name)
    if deployment is None:
        raise BrowserReadsError(
            f"'{args.name}' is not in the registry. Pass --id to point at a "
            f"specific historical row."
        )
    return deployment


def _resolve_period(config: list[dict], rows) -> tuple[int, str]:
    """The round period, and where it came from.

    The recorded role config first (`clients provision` denormalises it into
    ``deploy_args`` precisely so this report survives teardown), then the rows
    themselves: ``started_at`` IS the boundary, so ``started_at / round_id``
    gives the period exactly whenever every row agrees. Neither is an error, not
    a guess — the whole window selection is defined in terms of the period.
    """
    period = round_period_from_config(config)
    if period:
        return period, "deploy_args.browser_downloaders"
    period = derive_period_secs(rows)
    if period:
        return period, "derived from the recorded boundaries"
    raise BrowserReadsError(
        "the round period is unknown — deploy_args.browser_downloaders records "
        "none (or the VMs disagree), and it could not be derived from the "
        "recorded rounds. Re-run `clients provision` with DATABASE_URL set to "
        "record it."
    )


def _resolve_hot_files(deployment, name: str) -> tuple[dict, str]:
    """The hot-file selection, and where it came from.

    **The registry snapshot comes first, and the live S3 object is only the
    fallback.** The selection lives at
    ``addresses/<deployment>/browser-hot-files.json`` and the deployment's own
    `clients destroy` deletes that WHOLE prefix -- while this report is meant to
    be run after teardown, so reading S3 first would make the primary verdict of
    a torn-down run unobtainable. `record-results` snapshots the document in the
    same pass that records the rows, which is also the more coherent pairing: a
    selection that was `--replace`d since says nothing about the rounds in hand.

    The S3 read is guarded, so a credentials or connectivity failure becomes the
    "no selection" error rather than a traceback.
    """
    recorded = recorded_hot_files(deployment.deploy_args)
    if recorded is not None:
        return recorded, f"deploy_args.{BROWSER_HOT_FILES_KEY}"
    try:
        live = load_hot_files(name)
    except Exception as e:  # noqa: BLE001 -- reported, never a traceback
        raise BrowserReadsError(
            f"no hot-file selection recorded for '{deployment.name}' "
            f"(id={deployment.id}), and s3://{S3_ADDRESSES_BUCKET}/"
            f"{hot_files_key(name)} could not be read: {e}"
        )
    if live is None:
        raise BrowserReadsError(
            f"no hot-file selection for '{name}' — nothing is recorded in "
            f"deploy_args.{BROWSER_HOT_FILES_KEY} and "
            f"s3://{S3_ADDRESSES_BUCKET}/{hot_files_key(name)} does not exist, "
            f"so the report cannot say which file each round read. The S3 object "
            f"is deleted by the deployment's own `clients destroy`; the snapshot "
            f"is taken by `deployments record-results`, so run that BEFORE "
            f"tearing a cohort down."
        )
    return live, f"s3://{S3_ADDRESSES_BUCKET}/{hot_files_key(name)}"


def _render_console(report: dict, console) -> None:
    """A compact view for the transcript; the markdown carries every table."""
    totals = report["totals"]
    header = Table(
        show_header=False,
        title=(
            f"{report['deployment']} browser hot reads, "
            f"{report['window_start']} -> {report['window_end']}"
        ),
    )
    header.add_column("measure")
    header.add_column("value", justify="right")
    header.add_row("rounds", f"{totals['rounds']:,}")
    header.add_row("downloads", f"{totals['downloads']:,}")
    header.add_row(
        "ok / fail / skip",
        f"{totals['success']} / {totals['failure']} / {totals['skipped']}",
    )
    header.add_row(
        "completion",
        "n/a" if totals["completion"] is None else f"{totals['completion'] * 100:.2f}%",
    )
    header.add_row("chunks with first reads", f"{len(report['chunks']):,}")
    header.add_row("stalls", f"{report['stalls']['total']:,}")
    header.add_row("refused dials", f"{report['refusals']['total']:,}")
    header.add_row(
        "peak sessions/bootstrap",
        f"{report['bootstrap_load']['peak_sessions_per_bootstrap']:,}"
        f" of {report['bootstrap_load']['ceiling']}",
    )
    console.print(header)

    gates = Table(title="Gates")
    gates.add_column("gate")
    gates.add_column("verdict")
    gates.add_column("figure")
    gates.add_column("threshold")
    for gate in report["gates"]:
        colour = "red" if gate["status"] == GATE_FAIL else "green"
        gates.add_row(
            gate["name"],
            f"[{colour}]{gate['status']}[/{colour}]",
            gate["figure"],
            gate["threshold"],
        )
    console.print(gates)


def cmd_browser_reads(args) -> int:
    """Produce the report and return its exit code.

    Every "cannot produce the report" condition raises
    :class:`BrowserReadsError` and comes out as exit 2 here, in one place, so
    there is no path that exits with a code the dispatcher did not choose.
    """
    console = console_for(args.json_out)
    try:
        return _run(args, console)
    except BrowserReadsError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        return EXIT_ERROR


def _run(args, console) -> int:
    window_start = _parse_window(args.window_start, "--window-start")
    window_end = _parse_window(args.window_end, "--window-end")
    if window_end <= window_start:
        raise BrowserReadsError("--window-end must be after --window-start.")

    repo = get_repository()
    if repo is None:
        raise BrowserReadsError(
            "DATABASE_URL is not set or the database is unreachable. This "
            "report reads Postgres only."
        )
    deployment = _resolve_deployment(repo, args)

    all_rows = repo.list_browser_download_records_for_deployment(deployment.id)
    if not all_rows:
        raise BrowserReadsError(
            f"no browser-download rounds recorded for '{deployment.name}' "
            f"(id={deployment.id}). Run `deployments record-results --name "
            f"{deployment.name}` while the monitoring stack is still up."
        )

    config = recorded_browser_config(deployment.deploy_args)
    period, period_source = _resolve_period(config, all_rows)
    rows = rounds_in_window(all_rows, period, window_start, window_end)
    if not rows:
        raise BrowserReadsError(
            f"'{deployment.name}' has {len(all_rows)} recorded round row(s), "
            f"but none whose round boundary falls in "
            f"[{window_start.isoformat()}, {window_end.isoformat()}) at a "
            f"{period}s period."
        )

    hot_files, hot_files_source = _resolve_hot_files(deployment, args.name)

    report = build_report(
        deployment=deployment.name,
        deployment_id=deployment.id,
        rows=rows,
        hot_files=hot_files,
        hot_files_source=hot_files_source,
        period_secs=period,
        period_source=period_source,
        expected_instances=expected_instances(config),
        window_start=window_start,
        window_end=window_end,
        min_completion=args.min_completion,
        max_holder_share=args.max_holder_share,
        max_refused=args.max_refused,
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
        "browser-reads",
        help=(
            "The hot-read report: per round, per region and per chunk, with the "
            "busiest holder's share, stalls, refusals, bootstrap load and four "
            "gates. Reads recorded Postgres rows, so it survives teardown."
        ),
    )
    p.add_argument(
        "--name",
        required=True,
        help=(
            "Deployment name. Also where the hot-file selection is read from "
            "(s3://.../addresses/<name>/browser-hot-files.json)."
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
        help="Window start, ISO-8601 UTC. A round is in the window when its "
        "boundary (round_id x period) is at or after this.",
    )
    p.add_argument(
        "--window-end",
        required=True,
        help="Window end, ISO-8601 UTC, EXCLUSIVE — so two adjacent windows "
        "partition a run's rounds exactly once between them.",
    )
    p.add_argument(
        "--min-completion",
        type=float,
        default=0.99,
        help=(
            "G1: the floor on successes/attempts in EVERY round of the window "
            "(default 0.99). Per round, not over the window: a cohort that "
            "failed every read for one round and recovered has still had an "
            "outage, which a window-wide average hides."
        ),
    )
    p.add_argument(
        "--max-holder-share",
        type=float,
        default=0.25,
        help=(
            "G3: the ceiling on the busiest holder's share of a chunk's first "
            "reads (default 0.25), for every chunk with at least 20 of them. "
            "This is the verdict on hot-record read spreading."
        ),
    )
    p.add_argument(
        "--max-refused",
        type=int,
        default=0,
        help=(
            "G4: the ceiling on total reconstructed refused dials in the window "
            "(default 0). A refusal usually means the VM exceeded the per-IP "
            "WebRTC session quota, not that the fleet refused it."
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
    p.set_defaults(func=cmd_browser_reads)
