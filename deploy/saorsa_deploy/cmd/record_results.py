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

A ``testnet`` run carrying pointer clients ALSO records the ``pointer``
measurement into ``deployment_pointer_records``, in the same pass. It is not a
kind of its own: a pointer client rides an ordinary testnet alongside the
transfer clients, so its results have to be picked up by the same command
rather than by one an operator has to remember separately.
"""
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from rich.console import Console
from rich.table import Table
from saorsa_db.models import (
    FileOpType,
    OpRecordStatus,
    PointerOp,
    PointerOutcome,
    ProdUploadStatus,
)

from saorsa_deploy.client_roles import is_wasm
from saorsa_deploy.client_transport import (
    deployment_scoped_transport,
    transport_map_from_client_state,
    transport_map_from_deploy_args,
)
from saorsa_deploy.influx import (
    aggregate_file_results,
    fetch_all_operation_points,
    fetch_all_prod_upload_points,
    fetch_pointer_points,
)
from saorsa_deploy.provisioning.monitoring import INFLUXDB_WASM_BUCKET
from saorsa_deploy.registry import get_repository
from saorsa_deploy.slack_api import SlackApiError, post_reply
from saorsa_deploy.slack_results import (
    file_ops_result_blocks,
    pointer_result_blocks,
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

    if kind == "prod-continuous-uploader":
        # The uploader and verifier VMs hold their own database connections and
        # write every upload and verification as it happens, which is also how
        # a claim gets resolved. Pulling from Influx here could only produce a
        # less complete copy of what is already stored — and the Influx points
        # expire, while these rows do not.
        console.print(
            f"[yellow]'{args.name}' is a prod-continuous-uploader — its "
            "uploads and read-back verifications are written straight to "
            "Postgres by the VMs, so there is nothing to record.[/yellow]"
        )
        console.print(
            f"View them with [bold]saorsa-deploy deployments "
            f"print-recorded-results --name {args.name}[/bold]."
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
        # Pointer clients ride an ordinary testnet, so their records are picked
        # up in the same pass rather than by a command of their own. A run with
        # none finds no points and says nothing.
        pointer_blocks = _record_pointer_ops(
            repo, deployment, args, console,
            influx_url, influx_token, influx_org, influx_bucket,
        )
        if pointer_blocks:
            results_blocks = (results_blocks or []) + pointer_blocks

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


def _fetch_all_op_points(influx_url, influx_token, influx_org, influx_bucket, start):
    """Operation points from every bucket a client tier may write to.

    Thin wrapper over :func:`fetch_all_operation_points`, which is shared with
    `deployments print-uploads` / `print-downloads` so the reporting commands
    and the recording command can never disagree about which buckets a client
    tier writes to.
    """
    return fetch_all_operation_points(
        url=influx_url,
        token=influx_token,
        org=influx_org,
        bucket=influx_bucket,
        wasm_bucket=INFLUXDB_WASM_BUCKET,
        start=start,
    )


def _record_file_ops(
    repo, deployment, args, console,
    influx_url, influx_token, influx_org, influx_bucket,
) -> list[dict] | None:
    console.print(
        f"[bold]Querying {influx_url} "
        f"(bucket={influx_bucket}, window={args.window})...[/bold]"
    )
    try:
        points = _fetch_all_op_points(
            influx_url, influx_token, influx_org, influx_bucket, args.window
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
    table.add_column("Skip", justify="right")
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
            str(agg.get("skipped_count", 0)),
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


def _record_pointer_ops(
    repo, deployment, args, console,
    influx_url, influx_token, influx_org, influx_bucket,
) -> list[dict] | None:
    """Persist every `ant pointer` operation into ``deployment_pointer_records``.

    Always queried from epoch (``start="0"``), like the per-op file records:
    ``--window`` narrows the file-op AGGREGATE, but the per-op tables are the
    windowable ones and a re-run is expected to backfill the full history.

    An unrecognised op or outcome is counted as unusable rather than coerced.
    Coercing would put a value this build does not understand into a column the
    gates read, and reading `error` where the client wrote something else is
    worse than a gap that announces itself.
    """
    network_profile_for_host = _build_network_profile_lookup(deployment)
    try:
        points = fetch_pointer_points(
            influx_url, influx_token, influx_org, influx_bucket, "0"
        )
    except Exception as e:
        console.print(
            f"[yellow]Warning: pointer query failed, no pointer records "
            f"written: {e}[/yellow]"
        )
        return None
    if not points:
        return None

    recorded = 0
    unusable = 0
    records: list[dict] = []
    for point in points:
        start_ns = point.get("start_ns")
        if start_ns is None:
            unusable += 1
            continue
        try:
            op = PointerOp(point["op"])
            outcome = PointerOutcome(point["outcome"])
        except ValueError:
            unusable += 1
            continue
        started_at = datetime.fromtimestamp(
            start_ns / 1_000_000_000, tz=timezone.utc
        )
        repo.upsert_pointer_record(
            deployment_id=deployment.id,
            client_hostname=point["host"],
            region=point.get("region"),
            op=op,
            outcome=outcome,
            start_ns=start_ns,
            influx_time=point["_time"],
            started_at=started_at,
            finished_at=point["_time"],
            duration_ms=point.get("duration_ms"),
            pointer_address=point.get("address"),
            owned=point.get("owned", True),
            counter_expected=point.get("counter_expected"),
            counter_observed=point.get("counter_observed"),
            target_kind=point.get("target_kind"),
            age_secs=point.get("age_secs"),
            read_back=point.get("read_back"),
            attempts=point.get("attempts"),
            paid_attempts=point.get("paid_attempts"),
            ant_spend_atto=point.get("ant_spend_atto"),
            gas_spend_wei=point.get("gas_spend_wei"),
            error=point.get("error"),
            network_profile=network_profile_for_host(point["host"]),
        )
        recorded += 1
        records.append(
            {
                "client_hostname": point["host"],
                "op": op.value,
                "outcome": outcome.value,
                "owned": point.get("owned", True),
                "read_back": point.get("read_back"),
                "paid_attempts": point.get("paid_attempts"),
                "ant_spend_atto": point.get("ant_spend_atto"),
                "gas_spend_wei": point.get("gas_spend_wei"),
                "duration_ms": point.get("duration_ms"),
            }
        )

    console.print(
        f"[bold green]Recorded {recorded} pointer operation(s) "
        f"({unusable} unusable point(s)) for deployment '{args.name}'."
        f"[/bold green]"
    )
    return pointer_result_blocks(records)


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


def _warn_unclassifiable_wasm_hosts(repo, deployment, console) -> None:
    """Say which WASM hosts this run can no longer classify, and why.

    The transport is resolved from the deployment's S3 client state, and
    `clients destroy` deletes it -- so a `record-results` re-run after teardown
    resolves None for every host. The stored `client_kind` survives that (the
    upsert coalesces rather than overwriting), but silence would read as "these
    rows are native", which is exactly the misreading that pushes WASM failures
    into the native hard floors. Name the hosts instead.
    """
    if repo is None or console is None:
        return
    try:
        hosts = repo.list_wasm_client_hostnames(deployment.id)
    except Exception:
        return
    if not hosts:
        return
    console.print(
        "[yellow]Warning:[/yellow] this deployment's client state is gone, so "
        "the transport of each operation could not be resolved. "
        f"{len(hosts)} host(s) already recorded as WASM keep their existing "
        "client_kind (the upsert never overwrites a known transport with "
        "NULL), but any NEW operation recorded now is unlabelled: "
        + ", ".join(hosts)
    )


def _build_client_kind_lookup(deployment, repo=None, console=None):
    """Return host -> "native" | "wasm", or a constant None.

    Both transports emit the same `upload`/`download` Influx measurements (so the
    whole results pipeline works on WASM ops unchanged), which means the
    measurement cannot say which one produced a point. The client VM's ROLE can,
    and the deployment's client state already records it per VM -- the same
    mechanism `_network_map_from_client_state` uses, keyed on the same hostname
    the Influx `host` tag carries.

    Returns None per host when the state is unreadable or the deployment has no
    WASM clients at all: an unannotated row is right for a native-only run, and
    guessing would be worse than saying nothing.
    """
    # A production client kind's transport belongs to the DEPLOYMENT, and the
    # rule lives in `client_transport` so the reporting commands resolve it the
    # same way. It survives teardown, unlike the S3 client state below.
    scoped = deployment_scoped_transport(getattr(deployment, "deploy_args", None))
    if scoped is not None:
        return lambda host: scoped

    name = getattr(deployment, "name", "") or ""
    if not name:
        return lambda host: None
    try:
        state = load_client_state(name)
    except (RuntimeError, FileNotFoundError):
        _warn_unclassifiable_wasm_hosts(repo, deployment, console)
        return lambda host: None

    clients = state.get("clients", [])
    if not any(is_wasm(c.get("role", "upload")) for c in clients):
        # Native-only deployment: leave every row NULL, exactly as it was before
        # this column existed.
        return lambda host: None

    by_host = transport_map_from_client_state(name, state)
    return lambda host: by_host.get(host)


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
    client_kind_for_host = _build_client_kind_lookup(deployment, repo, console)
    console.print(
        "[bold]Fetching per-operation records (full range)...[/bold]"
    )
    try:
        points = _fetch_all_op_points(
            influx_url, influx_token, influx_org, influx_bucket, "0"
        )
    except Exception as e:
        console.print(
            f"[bold red]Error:[/bold red] InfluxDB per-op query failed: {e}"
        )
        sys.exit(1)

    upload_recorded = 0
    download_recorded = 0
    # Points that could not be turned into a row at all (a missing key, an
    # unrecognised status). Distinct from a `skipped` OPERATION, which IS a row.
    unusable = 0
    op_records: list[dict] = []
    for p in points:
        start_ns = p.get("start_ns")
        status_code = p.get("status")
        file_size = p.get("file_size")
        measurement = p.get("_measurement")
        if start_ns is None or file_size is None or measurement not in (
            "upload", "download"
        ):
            unusable += 1
            continue
        if status_code == 0:
            status = OpRecordStatus.SUCCESS
        elif status_code == 1:
            status = OpRecordStatus.FAILURE
        elif status_code == 2:
            # A transport refusal (an over-cap file on the browser path), not a
            # network failure. Stored so the skip is visible, and excluded from
            # every success-rate denominator downstream.
            status = OpRecordStatus.SKIPPED
        else:
            unusable += 1
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
            client_kind=client_kind_for_host(p["host"]),
        )
        if measurement == "upload":
            repo.upsert_upload_record(**kwargs)
            upload_recorded += 1
        else:
            # Downloads only: an upload's address is its RESULT and lives in the
            # prod-upload tables, not in the op record.
            repo.upsert_download_record(**kwargs, address=p.get("address"))
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
                "client_kind": client_kind_for_host(p["host"]),
                "address": p.get("address"),
                "error_kind": p.get("error_kind"),
            }
        )

    console.print(
        f"[bold green]Recorded {upload_recorded} upload record(s) and "
        f"{download_recorded} download record(s) "
        f"({unusable} unusable point(s)) for deployment '{args.name}'."
        f"[/bold green]"
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
    # 3 and 4 are browser-path only. OVER_CAP means the transport refused the
    # file and nothing was attempted; FAILED means the upload failed before any
    # payment went out, so nothing reached the network. Both release the pool
    # claim, where PARTIAL_UPLOAD spends it.
    if code == 3:
        return ProdUploadStatus.OVER_CAP
    if code == 4:
        return ProdUploadStatus.FAILED
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
    network_profile_for_host = _build_network_profile_lookup(deployment)
    # Per-host rather than per-deployment: a prod-uploaders run may carry both
    # transports, and it is read from the registry row (not the S3 client state
    # `clients destroy` deletes) so a re-run after teardown still labels them.
    transport_by_host = transport_map_from_deploy_args(
        getattr(deployment, "deploy_args", None)
    )
    console.print(
        f"[bold]Querying {influx_url} (buckets={influx_bucket}, "
        f"{INFLUXDB_WASM_BUCKET})...[/bold]"
    )
    try:
        points = fetch_all_prod_upload_points(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
            wasm_bucket=INFLUXDB_WASM_BUCKET,
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
        # Annotated onto the point as well as the row, so the Slack reply built
        # from these points can split its totals per transport.
        p["client_kind"] = transport_by_host.get(p["host"])
        repo.upsert_prod_upload_result(
            deployment_id=deployment.id,
            client_hostname=p["host"],
            network_profile=network_profile_for_host(p["host"]),
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
            client_kind=p["client_kind"],
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

    # Indexed twice. The locator is the reliable key: both upload tables record
    # the `source_url` they fetched, and the claim records the `locator_used`
    # it handed out, so the two sides hold the same string by construction.
    #
    # The name is NOT reliable, and matching on it alone was V2-1058. An
    # uploader records the raw percent-encoded URL basename
    # (`NBC%20Atlanta1%2083.avi`) while the pool catalogues the decoded name
    # (`NBC Atlanta1 83.avi`), so every file whose name contains a space or `#`
    # failed to match and its claim was left open — reported as "no matching
    # upload result", which reads as "the run never touched this file" while
    # the file was in fact on the network and paid for. It stayed a name match
    # as a *fallback* only, for a row predating `source_url` or a locator that
    # was rewritten after the upload.
    by_locator: dict[str, dict] = {}
    by_name: dict[str, dict] = {}

    # Precedence when the same file has several result rows: anything that put
    # chunks on the network outranks anything that did not, and a success
    # outranks a partial. A file uploaded successfully on one attempt and
    # partially (or not at all) on another IS on the network.
    _OUTCOME_RANK = {None: 0, False: 1, True: 2}

    def _index(locator: str | None, name: str | None, payload: dict) -> None:
        for table, key in ((by_locator, locator), (by_name, name)):
            if not key:
                continue
            existing = table.get(key)
            if existing is not None and _OUTCOME_RANK[existing["complete"]] > (
                _OUTCOME_RANK[payload["complete"]]
            ):
                continue
            table[key] = payload

    if kind == "prod-uploaders":
        for row in repo.list_prod_upload_results_for_deployment(deployment.id):
            # Three outcomes, and the difference between them is what the file's
            # claim becomes:
            #
            #   SUCCESS                -> uploaded (the entry is spent)
            #   PARTIAL_UPLOAD         -> partial  (chunks ARE in production, so
            #                             the file goes back to uploaders but is
            #                             excluded from canary selection)
            #   FAILED / OVER_CAP /
            #   DOWNLOAD_FAILED        -> released (nothing reached the network:
            #                             no payment went out, or nothing was
            #                             attempted at all, so the file is
            #                             untouched and must not be spent)
            #
            # Leaving that last group's claims open — which is what happened
            # before the pre-payment statuses existed — reported them as "no
            # matching upload result", i.e. as though the run had never touched
            # the file, while permanently withholding it from the pool.
            if row.status is ProdUploadStatus.SUCCESS:
                complete = True
            elif row.status is ProdUploadStatus.PARTIAL_UPLOAD:
                complete = False
            elif row.status in (
                ProdUploadStatus.FAILED,
                ProdUploadStatus.OVER_CAP,
                ProdUploadStatus.DOWNLOAD_FAILED,
            ):
                complete = None
            else:
                continue
            _index(
                row.source_url,
                row.file_name,
                {
                    "complete": complete,
                    "address": row.address,
                    "chunks_stored": row.chunks_stored,
                    "chunks_failed": row.chunks_failed,
                    "total_chunks": row.total_chunks,
                    "storage_cost_atto": row.storage_cost_atto,
                    "gas_cost_wei": row.gas_cost_wei,
                },
            )
    elif kind == "prod-canary":
        for row in repo.list_prod_canary_uploads_for_deployment(deployment.id):
            _index(
                row.source_url,
                row.file_name,
                {
                    "complete": True,
                    "address": row.top_address,
                    "chunks_stored": None,
                    "chunks_failed": None,
                    "total_chunks": row.num_chunks,
                    "storage_cost_atto": row.storage_cost_atto,
                    "gas_cost_wei": row.gas_cost_wei,
                },
            )
    else:
        return

    newly_resolved = 0
    refreshed = 0
    partial = 0
    released = 0
    still_open = 0
    for claim in claims:
        pool_file = pool_repo.get_pool_file(claim.file_id)
        if pool_file is None:
            continue
        # `locator_used` before the canonical row's own locator: a claim
        # attaches to the canonical file, but the content may be reachable two
        # ways (a local copy and an Internet Archive URL), and the run fetched
        # exactly one of them.
        match = (
            by_locator.get(claim.locator_used or "")
            or by_locator.get(pool_file.locator)
            or by_name.get(pool_file.name)
        )
        payload = dict(match or {})
        if not payload:
            if claim.is_open:
                still_open += 1
            continue
        complete = payload.pop("complete")
        was_open = claim.is_open
        if complete is None:
            # Nothing reached the network, so the file is untouched and the entry
            # must go back. Only an OPEN claim is released: one already resolved
            # to uploaded or partial records a real upload from an earlier pass,
            # and a later failed retry must never undo it.
            if was_open:
                pool_repo.release_pool_claim(
                    claim.id,
                    note="upload failed before any payment — file returned to the pool",
                )
                released += 1
            continue
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
    if released:
        console.print(
            f"[dim]Released {released} file-pool claim(s): the upload failed "
            "before any payment, so nothing reached the network and the "
            "file(s) are fully available again.[/dim]"
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
