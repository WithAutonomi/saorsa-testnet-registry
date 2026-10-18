"""Compare a staging run against a baseline run and emit the gate verdict.

The release process requires that a deterministic tool -- not an agent, not a
human -- reads Postgres, compares the run against the baseline, applies the
thresholds and emits a per-gate PASS / FAIL / GRAY verdict plus a fixed-format
report. This command is that tool.

It is deliberately narrow. It evaluates only what the registry stores: the two
success floors, and the eight soft gates. Data integrity, crashes,
mixed-version chunk storage and the production canaries are observational, are
recorded nowhere in Postgres, and are supplied by the operator through the
accompanying skill. The report names them explicitly so their absence can never
read as a pass.

Both runs are named by **deployment id**, not name. Staging deployment names are
reused every train, and a name lookup would silently resolve to whichever run
happened to be most recent -- the same failure mode that once made
`record-analysis` a silent no-op.
"""

import json
import sys

from rich.console import Console
from rich.table import Table
from saorsa_db.models.derived_metric import WINDOW_FULL_RUN

from saorsa_deploy.gate_comparison import (
    FAIL,
    GRAY,
    NOT_ASSESSED,
    PASS,
    ComparisonError,
    RunMetrics,
    compare_runs,
    filter_ops_to_window,
)
from saorsa_deploy.gate_config import GateConfigError, load_gate_config
from saorsa_deploy.gate_report import render_report
from saorsa_deploy.log_patterns import LogPatternError, load_pattern_set
from saorsa_deploy.pointer_results import (
    filter_to_window as filter_pointer_records_to_window,
)
from saorsa_deploy.pointer_results import (
    normalise_records as normalise_pointer_records,
)
from saorsa_deploy.registry import get_repository

_VERDICT_STYLE = {
    PASS: "green",
    FAIL: "bold red",
    GRAY: "yellow",
    NOT_ASSESSED: "dim",
}

_OVERALL_STYLE = {
    "PASS": "bold green",
    "GRAY": "bold yellow",
    "SOFT FAIL": "bold red",
    "HARD FAIL": "bold red",
}


def _load_run(repo, deployment_id: int, window_label: str, console: Console):
    """Assemble everything the comparison needs about one run.

    All the I/O for one side of the comparison lives here so the engine itself
    stays a pure function of its inputs.
    """
    deployment = repo.get(deployment_id)
    if deployment is None:
        console.print(
            f"[bold red]Error:[/bold red] No deployment with id {deployment_id} in the registry."
        )
        sys.exit(1)

    derived_rows = repo.list_derived_metrics_for_deployment(
        deployment_id, window_label=window_label
    )
    derived = {
        row.metric: {
            "value": row.value,
            "source": row.source,
            "unit": row.unit,
            "numerator": row.numerator,
            "denominator": row.denominator,
            "window_start": row.window_start,
            "window_end": row.window_end,
        }
        for row in derived_rows
    }

    # The window comes from the derived rows rather than from the bucket span:
    # the derived row is what the operator explicitly labelled, and for a
    # full-run row the observed metric span is widened by the deliberate +-3h
    # node clock skew, so it is not a duration.
    window_start = min((r.window_start for r in derived_rows), default=None)
    window_end = max((r.window_end for r in derived_rows), default=None)

    host_metrics = []
    if window_label == WINDOW_FULL_RUN:
        # The rollup IS the whole run, and its percentiles are exact -- computed
        # from every raw sample at extraction time. Re-aggregating the buckets
        # for the same span would produce an estimated median for no reason.
        host_result = repo.get_host_result_for_deployment(deployment_id)
        if host_result is not None:
            host_metrics = host_result.host_metrics or []
        rss_buckets = repo.get_host_metric_buckets_in_window(deployment_id)
    else:
        host_metrics = repo.get_host_metric_window(
            deployment_id, start=window_start, end=window_end
        )
        rss_buckets = repo.get_host_metric_buckets_in_window(
            deployment_id, start=window_start, end=window_end
        )

    if not host_metrics:
        # Degrade rather than fail: a run recorded before bucketing exists only
        # as the whole-run rollup, and its file-op gates are still worth having.
        host_result = repo.get_host_result_for_deployment(deployment_id)
        if host_result is not None:
            host_metrics = host_result.host_metrics or []
            console.print(
                f"[yellow]Deployment {deployment_id} has no host metric buckets; "
                "falling back to the whole-run rollup. A windowed slice is not "
                "available for it, and the memory-leak check cannot run.[/yellow]"
            )

    # Looked up by the window label being compared on, like every other stored
    # measurement. A run whose counts were recorded only under `full-run` has
    # none for a `steady-state` comparison, and that reads NOT ASSESSED rather
    # than quietly borrowing a different window's number.
    log_event_counts = [
        {
            "event_key": row.event_key,
            "event_group": row.event_group,
            "pattern": row.pattern,
            "pattern_set": row.pattern_set,
            "pattern_version": row.pattern_version,
            "count": row.count,
            "hosts": row.hosts,
            "total_documents": row.total_documents,
            "observed_start": row.observed_start,
            "observed_end": row.observed_end,
        }
        for row in repo.list_log_event_counts_for_deployment(
            deployment_id, window_label=window_label
        )
    ]

    uploads = repo.list_upload_records_for_deployment(deployment_id)
    downloads = repo.list_download_records_for_deployment(deployment_id)
    # Normalised to plain dicts here, not in the engine: `pointer_results` is
    # the one place that knows how to read a pointer row, and both this and
    # `print-pointers` go through it so a gate and the table an operator reads
    # cannot disagree about a rate.
    pointers = normalise_pointer_records(repo.list_pointer_records_for_deployment(deployment_id))
    if window_label != WINDOW_FULL_RUN and window_start and window_end:
        uploads = filter_ops_to_window(uploads, window_start, window_end)
        downloads = filter_ops_to_window(downloads, window_start, window_end)
        pointers = filter_pointer_records_to_window(pointers, window_start, window_end)

    return RunMetrics(
        deployment_id=deployment.id,
        name=deployment.name,
        saorsa_deploy_version=deployment.saorsa_deploy_version,
        window_label=window_label,
        window_start=window_start,
        window_end=window_end,
        upload_records=uploads,
        download_records=downloads,
        host_metrics=host_metrics,
        rss_buckets=rss_buckets,
        derived=derived,
        log_event_counts=log_event_counts,
        pointer_records=pointers,
    )


def _collect_history(
    repo, history_ids: list[int], window_label: str, config, console: Console
) -> dict[str, list[float]]:
    """Gather prior accepted runs' values for the 2-sigma noise criterion.

    Which runs count as "accepted" is a judgment call and belongs to the
    operator (the skill prompts for it and confirms); only the arithmetic lives
    in the tool. Currently populated for the two per-GB ratios, which are the
    metrics with a single stored scalar per run.
    """
    history: dict[str, list[float]] = {}
    if not history_ids:
        return history

    from saorsa_db.models.derived_metric import METRIC_DISK_BYTES_PER_GB_STORED

    wanted = {
        "Outbound bytes per GB": config.outbound_ratio_metric,
        "Disk bytes per GB stored": METRIC_DISK_BYTES_PER_GB_STORED,
    }
    for gate, metric in wanted.items():
        values = []
        for run_id in history_ids:
            rows = repo.list_derived_metrics_for_deployment(run_id, window_label=window_label)
            for row in rows:
                if row.metric == metric and row.value is not None:
                    values.append(float(row.value))
        if values:
            history[gate] = values

    missing = [
        gate for gate in wanted if len(history.get(gate, [])) < config.noise_rule.min_history_runs
    ]
    if missing:
        console.print(
            f"[yellow]Fewer than {config.noise_rule.min_history_runs} history "
            "values for: "
            + ", ".join(sorted(missing))
            + ". The percentage threshold stands alone for those gates.[/yellow]"
        )
    return history


def cmd_compare_runs(args) -> None:
    console = Console()

    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set. "
            "This command requires the registry database."
        )
        sys.exit(1)

    new_baseline = bool(getattr(args, "new_baseline", False))
    if not new_baseline and args.id == args.baseline_id:
        console.print(
            "[bold red]Error:[/bold red] --id and --baseline-id are the same "
            "deployment. A run compared against itself is not a gate."
        )
        sys.exit(1)

    try:
        config = load_gate_config(args.thresholds)
    except GateConfigError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    history_ids = []
    if args.history:
        try:
            history_ids = [int(x) for x in args.history.split(",") if x.strip()]
        except ValueError:
            console.print(
                "[bold red]Error:[/bold red] --history must be a comma-separated "
                "list of deployment ids."
            )
            sys.exit(1)
        if args.id in history_ids or (not new_baseline and args.baseline_id in history_ids):
            console.print(
                "[bold red]Error:[/bold red] --history must not include the "
                "candidate or the baseline. Including either makes the variance "
                "band partly a function of the run being judged."
            )
            sys.exit(1)

    # The catalogue names the connection-error groups and says how each one's
    # patterns combine. Loaded here rather than in the engine so the comparison
    # stays a pure function of its inputs.
    try:
        pattern_set = load_pattern_set(args.patterns)
    except LogPatternError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    candidate = _load_run(repo, args.id, args.window_label, console)
    baseline = (
        None if new_baseline else _load_run(repo, args.baseline_id, args.window_label, console)
    )
    history = _collect_history(repo, history_ids, args.window_label, config, console)

    try:
        verdict = compare_runs(
            candidate,
            baseline,
            config,
            history,
            pattern_set=pattern_set,
            new_baseline=new_baseline,
        )
    except ComparisonError as exc:
        console.print(f"[bold red]These runs cannot be compared.[/bold red]\n\n{exc}")
        sys.exit(1)

    report = render_report(verdict)

    for warning in verdict.get("warnings", []):
        console.print(f"[yellow]Warning: {warning}[/yellow]")

    against = (
        "a NEW BASELINE (no prior run)"
        if new_baseline
        else f"{baseline.name} (#{baseline.deployment_id})"
    )
    # Context for reading the gates, never a gate itself: a difference between
    # two runs produced by different tool versions may be the tool's, and that
    # is for the reader to weigh.
    cand_tool = candidate.saorsa_deploy_version or "not recorded"
    if new_baseline:
        console.print(f"[dim]saorsa-deploy: candidate {cand_tool}[/dim]")
    else:
        base_tool = baseline.saorsa_deploy_version or "not recorded"
        console.print(f"[dim]saorsa-deploy: candidate {cand_tool}, baseline {base_tool}[/dim]")

    table = Table(
        title=(
            f"Gate comparison: {candidate.name} (#{candidate.deployment_id}) vs "
            f"{against} [window: {args.window_label}]"
        )
    )
    table.add_column("Gate")
    table.add_column("Kind")
    table.add_column("Verdict")
    table.add_column("Delta", justify="right")
    for gate in verdict["gates"]:
        delta = gate.get("delta_percent")
        unit = "pp" if gate.get("unit") == "pp" else "%"
        table.add_row(
            gate["gate"],
            gate["kind"],
            f"[{_VERDICT_STYLE.get(gate['verdict'], '')}]{gate['verdict']}[/]",
            f"{delta:+.2f} {unit}" if delta is not None else "-",
        )
    console.print(table)

    counts = verdict["counts"]
    style = _OVERALL_STYLE.get(verdict["overall"], "bold")
    console.print(
        f"[{style}]Overall: {verdict['overall']}[/] — {counts[PASS]} pass, "
        f"{counts[FAIL]} fail, {counts[GRAY]} gray, {counts[NOT_ASSESSED]} not "
        f"assessed (threshold config v{config.version})."
    )
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(verdict, f, indent=2, sort_keys=True)
            f.write("\n")
        console.print(f"Wrote JSON verdict to {args.json_out}")
    if args.markdown_out:
        with open(args.markdown_out, "w") as f:
            f.write(report)
        console.print(f"Wrote markdown report to {args.markdown_out}")
    if args.print_markdown:
        console.print()
        print(report)

    if args.no_persist:
        console.print("[dim]--no-persist: verdict not written to the registry.[/dim]")
    else:
        repo.upsert_gate_verdict(
            deployment_id=candidate.deployment_id,
            baseline_deployment_id=None if new_baseline else baseline.deployment_id,
            window_label=args.window_label,
            overall=verdict["overall"],
            threshold_config_version=config.version,
            verdict=verdict,
            report_markdown=report,
        )
        console.print(
            f"[green]Stored verdict against deployment {candidate.deployment_id}.[/green]"
        )

    # A hard-gate failure blocks the ship with no override, so it is worth a
    # non-zero exit for anything scripting this. A soft fail or GRAY is a
    # judgment call and deliberately exits 0.
    if verdict["overall"] == "HARD FAIL":
        sys.exit(2)
