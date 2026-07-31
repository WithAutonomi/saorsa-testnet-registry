"""Record per-file/per-host upload+download results into the registry DB.

The CLI entry point ``cmd_record_results`` resolves the deployment's
``kind`` and dispatches to the correct Influx → Postgres pipeline:

* ``testnet`` and ``prod-continuous-downloader`` →
  per-(host, op_type) aggregates of the ``file_ops`` measurement, written
  to ``deployment_file_results``. Honours ``--window``.
* ``prod-uploaders`` → per-file rows of the ``prod_upload`` measurement,
  written to ``deployment_prod_upload_results``. ``--window`` is ignored
  (uploads are bounded runs; we always pull from epoch).
* ``prod-local-uploads`` / ``prod-local-downloads`` → rejected. Local
  uploads/downloads write directly to Postgres at run time; there is
  nothing to record.
"""
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from rich.console import Console
from rich.table import Table
from saorsa_db.models import FileOpType, OpRecordStatus, ProdUploadStatus

from saorsa_deploy.influx import (
    aggregate_file_results,
    fetch_operation_points,
    fetch_prod_upload_points,
)
from saorsa_deploy.registry import get_repository
from saorsa_deploy.slack_api import SlackApiError, post_reply
from saorsa_deploy.slack_results import (
    file_ops_result_blocks,
    prod_upload_result_blocks,
)
from saorsa_deploy.state import load_client_state, load_monitoring_state

_TESTNET_KIND = "testnet"


def cmd_record_results(args):
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

    kind = _resolve_kind(deployment.deploy_args)

    if kind in ("prod-local-uploads", "prod-local-downloads"):
        console.print(
            f"[yellow]'{args.name}' is a {kind} deployment — "
            f"its per-file results are written directly to Postgres at run "
            f"time. There is nothing to record.[/yellow]"
        )
        return

    creds = _resolve_influx(args, console)
    if creds is None:
        sys.exit(1)
    influx_url, influx_token, influx_org, influx_bucket = creds

    if kind == "prod-uploaders":
        results_blocks = _record_prod_upload(
            repo, deployment, args, console,
            influx_url, influx_token, influx_org, influx_bucket,
        )
    elif kind == "prod-canary":
        from saorsa_deploy.cmd.prod_canary_results import (
            record_prod_canary_results,
        )
        results_blocks = record_prod_canary_results(
            repo, deployment, args, console,
            influx_url, influx_token, influx_org, influx_bucket,
        )
    else:
        # testnet, prod-continuous-downloader, prod-downloaders, and any
        # unrecognised/empty kind fall through to the historical aggregate path.
        results_blocks = _record_file_ops(
            repo, deployment, args, console,
            influx_url, influx_token, influx_org, influx_bucket,
        )

    # Close the pool claims this deployment took. Only reached for the remote
    # kinds: prod-local-uploads resolves its claims inline as it uploads, and
    # returns above before ever getting here.
    _resolve_pool_claims(repo, deployment, kind, console)

    if getattr(args, "update_slack", False):
        _post_results_to_slack(repo, deployment, console, results_blocks)


def _post_results_to_slack(repo, deployment, console, results_blocks) -> None:
    """Post the per-kind results summary as a threaded reply to the
    deployment's original ``slack-post`` message. Best-effort: any problem
    (missing thread ref, missing token, Slack error) warns and returns without
    failing the run — mirroring the ``slack_post`` manifest tail semantics."""
    if not results_blocks:
        console.print(
            "[yellow]--update-slack: nothing was recorded, so there is no "
            "results summary to post.[/yellow]"
        )
        return

    channel = deployment.slack_channel
    thread_ts = deployment.slack_message_ts
    if not (channel and thread_ts):
        console.print(
            "[yellow]--update-slack: no Slack thread reference is recorded for "
            f"'{deployment.name}'. Post the original message with "
            "`deployments slack-post` while ANT_DEPLOY_SLACK_BOT_TOKEN is set so "
            "the (channel, ts) is captured, then re-run. Skipping.[/yellow]"
        )
        return

    bot_token = os.environ.get("ANT_DEPLOY_SLACK_BOT_TOKEN")
    if not bot_token:
        console.print(
            "[yellow]--update-slack: ANT_DEPLOY_SLACK_BOT_TOKEN is not set; "
            "cannot post a threaded reply. Skipping.[/yellow]"
        )
        return

    blocks = [_results_header_block(deployment)] + results_blocks
    text = f"Results for {deployment.name}"
    try:
        post_reply(bot_token, channel, thread_ts, text=text, blocks=blocks)
    except SlackApiError as e:
        console.print(
            f"[yellow]--update-slack: Slack reply failed: {e} "
            "(results were still recorded).[/yellow]"
        )
        return
    console.print(
        f"[bold green]Posted results summary to the Slack thread for "
        f"'{deployment.name}'.[/bold green]"
    )


def _results_header_block(deployment) -> dict:
    from saorsa_deploy.cmd.slack_post import _section

    recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return _section(f"*Results — {deployment.name}* _(recorded {recorded_at})_")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


logger = logging.getLogger(__name__)


def _resolve_kind(deploy_args) -> str:
    if not isinstance(deploy_args, dict):
        return ""
    explicit = deploy_args.get("kind")
    if explicit:
        return explicit
    if "bootstrap_count" in deploy_args:
        return _TESTNET_KIND
    return ""


def _resolve_influx(args, console: Console):
    influx_url = args.influx_url
    influx_token = args.influx_token
    influx_org = args.influx_org
    influx_bucket = args.influx_bucket

    if not (influx_url and influx_token):
        stack_name = args.monitoring_name or args.name
        console.print(
            f"[bold]Loading Influx credentials from monitoring state "
            f"'{stack_name}'...[/bold]"
        )
        try:
            state = load_monitoring_state(stack_name)
        except RuntimeError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            return None
        influx_url = influx_url or state.get("influxdb_url")
        influx_token = influx_token or state.get("influxdb_token")
        influx_org = influx_org or state.get("influxdb_org") or "ant"
        influx_bucket = (
            influx_bucket or state.get("influxdb_bucket") or "testnet-metrics"
        )

    if not (influx_url and influx_token):
        console.print(
            "[bold red]Error:[/bold red] InfluxDB URL and token are required. "
            "Provide --influx-url/--influx-token or --monitoring-name pointing "
            "to a provisioned monitoring stack."
        )
        return None

    return (
        influx_url,
        influx_token,
        influx_org or "ant",
        influx_bucket or "testnet-metrics",
    )


# ---------------------------------------------------------------------------
# testnet / prod-continuous-downloader: file_ops aggregate path
# ---------------------------------------------------------------------------


def _record_file_ops(
    repo, deployment, args, console,
    influx_url, influx_token, influx_org, influx_bucket,
) -> list[dict] | None:
    console.print(
        f"[bold]Querying {influx_url} "
        f"(bucket={influx_bucket}, window={args.window})...[/bold]"
    )
    try:
        points = fetch_operation_points(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
            start=args.window,
        )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] InfluxDB query failed: {e}")
        sys.exit(1)

    aggregates = aggregate_file_results(points)
    if not aggregates:
        console.print(
            f"[yellow]No upload/download points found for deployment "
            f"'{args.name}' in window {args.window}. "
            f"Nothing to record.[/yellow]"
        )
        return None

    for agg in aggregates:
        repo.upsert_file_result(
            deployment_id=deployment.id,
            client_hostname=agg["client_hostname"],
            op_type=FileOpType(agg["op_type"]),
            file_size_bytes=agg["file_size_bytes"],
            first_started_at=agg["first_started_at"],
            last_finished_at=agg["last_finished_at"],
            success_count=agg["success_count"],
            failure_count=agg["failure_count"],
            avg_duration_ms=agg["avg_duration_ms"],
            min_duration_ms=agg["min_duration_ms"],
            max_duration_ms=agg["max_duration_ms"],
        )

    table = Table(
        title=f"File results for deployment '{args.name}' (id={deployment.id})"
    )
    table.add_column("Host")
    table.add_column("Op")
    table.add_column("Size (MB)", justify="right")
    table.add_column("OK", justify="right")
    table.add_column("Fail", justify="right")
    table.add_column("Avg ms", justify="right")
    table.add_column("Min ms", justify="right")
    table.add_column("Max ms", justify="right")
    table.add_column("First start (UTC)")
    table.add_column("Last end (UTC)")
    for agg in aggregates:
        table.add_row(
            agg["client_hostname"],
            agg["op_type"],
            f"{agg['file_size_bytes'] / (1024 * 1024):.1f}",
            str(agg["success_count"]),
            str(agg["failure_count"]),
            "-" if agg["avg_duration_ms"] is None else str(agg["avg_duration_ms"]),
            "-" if agg["min_duration_ms"] is None else str(agg["min_duration_ms"]),
            "-" if agg["max_duration_ms"] is None else str(agg["max_duration_ms"]),
            agg["first_started_at"].strftime("%Y-%m-%d %H:%M:%S"),
            agg["last_finished_at"].strftime("%Y-%m-%d %H:%M:%S"),
        )
    console.print(table)
    console.print(
        f"[bold green]Recorded {len(aggregates)} row(s) "
        f"for deployment '{args.name}'.[/bold green]"
    )

    op_records = _record_file_op_records(
        repo, deployment, args, console,
        influx_url, influx_token, influx_org, influx_bucket,
    )

    return file_ops_result_blocks(aggregates, op_records=op_records)


def _build_network_profile_lookup(deployment):
    """Return host -> network profile name for a deployment, or a constant None.

    The shaping applied to a client VM is recorded in ``deploy_args`` at deploy
    time (that is the authoritative record, holding the full knob set); this
    denormalises just the *name* onto each op row so a run's transfers can be
    filtered and compared without joining through JSON.

    Two shapes are handled: a single ``network`` entry (the single-VM download
    kinds) applies to every host, while ``network_by_host`` maps each client VM
    to its own profile for the multi-VM kinds where a shaped uploader may run
    alongside an unshaped control. Anything unrecognised yields None, i.e. the
    row simply records no profile — never a wrong one.
    """
    deploy_args = getattr(deployment, "deploy_args", None) or {}

    by_host = deploy_args.get("network_by_host")
    if isinstance(by_host, dict) and by_host:
        return lambda host: (by_host.get(host) or {}).get("name") if isinstance(
            by_host.get(host), dict
        ) else by_host.get(host)

    single = deploy_args.get("network")
    if isinstance(single, dict):
        name = single.get("name")
        return lambda host: name
    if isinstance(single, str) and single:
        return lambda host: single

    # Multi-VM kinds (prod uploaders, testnet clients) keep their per-VM spec in
    # the deployment's client state rather than deploy_args, so a shaped uploader
    # and an unshaped control in the same run stay distinguishable. Hostname is
    # derived exactly as `_build_payment_map` in print_file_ops does, from the
    # client terraform module's naming.
    by_host = _network_map_from_client_state(getattr(deployment, "name", "") or "")
    if by_host:
        return lambda host: by_host.get(host)

    return lambda host: None


def _network_map_from_client_state(name: str) -> dict[str, str]:
    """Map hostname -> network profile name from the deployment's client state.

    Missing or unreadable state yields an empty map, so recording degrades to "no
    profile recorded" rather than failing — the results themselves matter far more
    than this annotation.
    """
    if not name:
        return {}
    try:
        state = load_client_state(name)
    except (RuntimeError, FileNotFoundError):
        return {}
    out: dict[str, str] = {}
    for c in state.get("clients", []):
        network = c.get("network")
        if not network:
            continue
        label = network.get("name") if isinstance(network, dict) else str(network)
        if not label or "=" in label:
            # An inline (ad-hoc) set has no name worth denormalising; the full
            # definition stays in deploy_args.
            label = "inline"
        out[
            f"{name}-ant-client-{c.get('role', 'upload')}-"
            f"{c.get('region')}-{c.get('client_index')}"
        ] = label
    return out


def _record_file_op_records(
    repo, deployment, args, console,
    influx_url, influx_token, influx_org, influx_bucket,
) -> list[dict]:
    """Persist every individual upload/download op as a row in
    ``deployment_upload_records`` / ``deployment_download_records``.

    Unlike the summary above, this always queries from epoch (``start="0"``);
    ``--window`` is ignored so re-runs naturally backfill the full history.
    Idempotent via the ``(deployment_id, host, start_ns)`` upsert key.
    """
    network_profile_for_host = _build_network_profile_lookup(deployment)
    console.print(
        "[bold]Fetching per-operation records (full range)...[/bold]"
    )
    try:
        points = fetch_operation_points(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
            start="0",
        )
    except Exception as e:
        console.print(
            f"[bold red]Error:[/bold red] InfluxDB per-op query failed: {e}"
        )
        sys.exit(1)

    upload_recorded = 0
    download_recorded = 0
    skipped = 0
    op_records: list[dict] = []
    for p in points:
        start_ns = p.get("start_ns")
        status_code = p.get("status")
        file_size = p.get("file_size")
        measurement = p.get("_measurement")
        if start_ns is None or file_size is None or measurement not in (
            "upload", "download"
        ):
            skipped += 1
            continue
        if status_code == 0:
            status = OpRecordStatus.SUCCESS
        elif status_code == 1:
            status = OpRecordStatus.FAILURE
        else:
            skipped += 1
            continue

        started_at = datetime.fromtimestamp(start_ns / 1_000_000_000, tz=timezone.utc)
        duration_ms = p.get("total_ms")
        finished_at = p["_time"]

        kwargs = dict(
            deployment_id=deployment.id,
            client_hostname=p["host"],
            region=p.get("region"),
            file_size_bytes=file_size,
            status=status,
            start_ns=start_ns,
            influx_time=p["_time"],
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            network_profile=network_profile_for_host(p["host"]),
        )
        if measurement == "upload":
            repo.upsert_upload_record(**kwargs)
            upload_recorded += 1
        else:
            repo.upsert_download_record(**kwargs)
            download_recorded += 1

        # Collected for the optional --update-slack per-operation detail.
        op_records.append(
            {
                "client_hostname": p["host"],
                "op_type": measurement,
                "status": status.value,
                "file_size_bytes": file_size,
                "duration_ms": duration_ms,
                "started_at": started_at,
            }
        )

    console.print(
        f"[bold green]Recorded {upload_recorded} upload record(s) and "
        f"{download_recorded} download record(s) "
        f"({skipped} skipped) for deployment '{args.name}'.[/bold green]"
    )

    return op_records


# ---------------------------------------------------------------------------
# prod-uploaders: prod_upload per-file path
# ---------------------------------------------------------------------------


def _status_from_int(code) -> ProdUploadStatus | None:
    if code == 0:
        return ProdUploadStatus.SUCCESS
    if code == 1:
        return ProdUploadStatus.PARTIAL_UPLOAD
    if code == 2:
        return ProdUploadStatus.DOWNLOAD_FAILED
    return None


def _started_at(point: dict) -> datetime:
    start_ns = point.get("start_ns")
    if start_ns is not None:
        return datetime.fromtimestamp(start_ns / 1_000_000_000, tz=timezone.utc)
    return point["_time"]


def _cost_to_decimal(value) -> Decimal | None:
    """Convert a string-encoded uint256 cost field to ``Decimal``.

    Cost fields (``storage_cost_atto`` / ``gas_cost_wei``) ride through Influx
    as strings to keep full precision; the Postgres columns are ``NUMERIC``.
    Empty/missing/unparseable stays ``None`` (NULL).
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _record_prod_upload(
    repo, deployment, args, console,
    influx_url, influx_token, influx_org, influx_bucket,
) -> list[dict] | None:
    console.print(f"[bold]Querying {influx_url} (bucket={influx_bucket})...[/bold]")
    try:
        points = fetch_prod_upload_points(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
        )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] InfluxDB query failed: {e}")
        sys.exit(1)

    if not points:
        console.print(
            f"[yellow]No prod_upload points found for deployment "
            f"'{args.name}'. Nothing to record.[/yellow]"
        )
        return None

    recorded = 0
    skipped = 0
    for p in points:
        status = _status_from_int(p.get("status"))
        if status is None:
            skipped += 1
            continue
        source_url = p.get("source_url") or ""
        if not source_url:
            skipped += 1
            continue
        repo.upsert_prod_upload_result(
            deployment_id=deployment.id,
            client_hostname=p["host"],
            region=p.get("region") or "",
            source_url=source_url,
            file_name=p.get("file_name") or "",
            file_size_bytes=p.get("file_size") or 0,
            status=status,
            address=p.get("address") or None,
            chunks_stored=p.get("chunks_stored") or 0,
            chunks_failed=p.get("chunks_failed") or 0,
            total_chunks=p.get("total_chunks") or 0,
            duration_ms=p.get("total_ms"),
            upload_secs=p.get("upload_secs"),
            started_at=_started_at(p),
            finished_at=p["_time"],
            reason=p.get("reason") or None,
            storage_cost_atto=_cost_to_decimal(p.get("storage_cost_atto")),
            gas_cost_wei=_cost_to_decimal(p.get("gas_cost_wei")),
        )
        recorded += 1

    table = Table(
        title=f"Prod uploads recorded for '{args.name}' (id={deployment.id})"
    )
    table.add_column("Recorded", justify="right")
    table.add_column("Skipped", justify="right")
    table.add_row(str(recorded), str(skipped))
    console.print(table)
    console.print(
        f"[bold green]Persisted {recorded} prod_upload row(s) "
        f"for deployment '{args.name}'.[/bold green]"
    )

    return prod_upload_result_blocks(points)


def _resolve_pool_claims(repo, deployment, kind: str, console) -> None:
    """Mark this deployment's pool claims as uploaded, with what the network gave back.

    The two source tables describe the same concepts under different names —
    ``address``/``top_address``, ``total_chunks``/``num_chunks`` — so each kind
    needs its own reader rather than one generic path.

    An **upsert**, not an insert-once: ``record-results`` is idempotent and
    re-runnable, and a later pass can carry revised costs. A claim whose file
    has no matching result row is left open rather than guessed at — an upload
    that did not happen must not be recorded as though it did.
    """
    from saorsa_deploy.registry import get_file_pool_repository

    pool_repo = get_file_pool_repository()
    if pool_repo is None:
        return

    try:
        _resolve_pool_claims_inner(repo, pool_repo, deployment, kind, console)
    except Exception:
        # Closing pool claims is auxiliary to recording results. A registry
        # that predates the pool migration, or any other read failure here,
        # must not cost the operator the results themselves — those live in an
        # InfluxDB that the teardown is about to destroy.
        logger.debug("Could not resolve file-pool claims", exc_info=True)
        console.print(
            "[dim]Could not resolve file-pool claims (the pool tables may not "
            "exist yet); results were recorded normally.[/dim]"
        )


def _resolve_pool_claims_inner(repo, pool_repo, deployment, kind: str, console) -> None:
    # Released claims are excluded, but already-uploaded ones are NOT: a later
    # pass can carry revised costs, and skipping them would make this
    # insert-once in practice however the resolve method behaves.
    from saorsa_db.models.prod_file_pool import ClaimStatus
    from saorsa_db.models.prod_upload_result import ProdUploadStatus

    claims = [
        claim
        for claim in pool_repo.list_pool_claims(deployment_id=deployment.id)
        if claim.status is not ClaimStatus.RELEASED
    ]
    if not claims:
        return

    by_name: dict[str, dict] = {}
    if kind == "prod-uploaders":
        for row in repo.list_prod_upload_results_for_deployment(deployment.id):
            # Partials matter as much as successes. A part-uploaded file put
            # real chunks in production; leaving its claim open would report it
            # as "no matching upload result", which reads as "the run never
            # touched this file" — the opposite of what happened.
            if row.status is ProdUploadStatus.SUCCESS:
                complete = True
            elif row.status is ProdUploadStatus.PARTIAL_UPLOAD:
                complete = False
            else:
                continue
            existing = by_name.get(row.file_name)
            if existing is not None and existing["complete"] and not complete:
                # A file uploaded successfully on one attempt and partially on
                # another is on the network: the success wins.
                continue
            by_name[row.file_name] = {
                "complete": complete,
                "address": row.address,
                "chunks_stored": row.chunks_stored,
                "chunks_failed": row.chunks_failed,
                "total_chunks": row.total_chunks,
                "storage_cost_atto": row.storage_cost_atto,
                "gas_cost_wei": row.gas_cost_wei,
            }
    elif kind == "prod-canary":
        for row in repo.list_prod_canary_uploads_for_deployment(deployment.id):
            by_name[row.file_name] = {
                "complete": True,
                "address": row.top_address,
                "chunks_stored": None,
                "chunks_failed": None,
                "total_chunks": row.num_chunks,
                "storage_cost_atto": row.storage_cost_atto,
                "gas_cost_wei": row.gas_cost_wei,
            }
    else:
        return

    newly_resolved = 0
    refreshed = 0
    partial = 0
    still_open = 0
    for claim in claims:
        pool_file = pool_repo.get_pool_file(claim.file_id)
        if pool_file is None:
            continue
        payload = dict(by_name.get(pool_file.name) or {})
        if not payload:
            if claim.is_open:
                still_open += 1
            continue
        complete = payload.pop("complete")
        was_open = claim.is_open
        if not complete:
            payload.pop("storage_cost_atto", None)
            payload.pop("gas_cost_wei", None)
            pool_repo.mark_pool_claim_partial(
                claim.id,
                note="upload was partial — file returned to the pool",
                **payload,
            )
            partial += 1
            continue
        pool_repo.resolve_pool_claim(claim.id, **payload)
        if was_open:
            newly_resolved += 1
        else:
            refreshed += 1

    if newly_resolved:
        console.print(
            f"[dim]Resolved {newly_resolved} file-pool claim(s) for this "
            "deployment.[/dim]"
        )
    if refreshed:
        console.print(
            f"[dim]Refreshed {refreshed} already-resolved file-pool "
            "claim(s).[/dim]"
        )
    if partial:
        # Said plainly: these files are back in the pool, but they are NOT
        # untouched — some of their chunks are in production.
        console.print(
            f"[yellow]{partial} file(s) uploaded only partially. They are "
            "available to uploaders again (completing them is useful work) but "
            "are excluded from canary selection, since chunks of them are "
            "already stored.[/yellow]"
        )
    if still_open:
        # Not silently ignored: an unresolved claim means a file was handed out
        # and has no recorded upload, which is exactly the state the operator
        # needs to see rather than have assumed away.
        console.print(
            f"[yellow]{still_open} file-pool claim(s) have no matching upload "
            "result and remain open. See `prod pool claims "
            f"--consumer {deployment.name}` or `prod pool claims "
            "--unresolvable`.[/yellow]"
        )
