"""Print per-operation upload/download records for a deployment.

Two CLI entry points — ``cmd_print_uploads`` and ``cmd_print_downloads`` —
look up the deployment in the registry, derive its ``kind``, and dispatch
to the right Influx measurement / renderer:

* ``testnet`` uploaders → ``file_ops`` ``upload`` records, per-op tables
* ``testnet`` downloaders → ``file_ops`` ``download`` records, per-op tables
* ``prod-uploaders`` → ``prod_upload`` per-file detail tables (uploads only)
* ``prod-continuous-downloader`` → ``file_ops`` ``download`` records
  (downloads only)
* ``prod-local-uploads`` → no Influx exists for this kind; the user is
  pointed at the forthcoming ``deployments print-recorded-results``
  command which reads from Postgres.

Renderer behaviour (``--summary`` / ``--recent`` / ``--details``) varies
per kind; flags that don't apply to the resolved kind are silently
ignored.
"""

import sys
from datetime import datetime, timedelta, timezone
from statistics import median

from rich.console import Console
from rich.table import Table

from saorsa_deploy.analysis.json_out import console_for, emit_json
from saorsa_deploy.client_extension import client_extension_tiers, terraform_name
from saorsa_deploy.client_transport import (
    deployment_scoped_transport,
    transport_map_from_client_state,
)
from saorsa_deploy.gate_comparison import percentile
from saorsa_deploy.influx import (
    fetch_all_operation_points,
    fetch_all_prod_upload_points,
    fetch_browser_download_points,
)
from saorsa_deploy.influx_retention import binding_retention_seconds
from saorsa_deploy.provisioning.monitoring import INFLUXDB_WASM_BUCKET
from saorsa_deploy.registry import get_repository
from saorsa_deploy.state import load_client_state, load_monitoring_state

_TESTNET_KIND = "testnet"

#: Flux relative-duration units this command's `--window` can carry, in
#: seconds. `mo` and `y` are deliberately absent: they are CALENDAR units in
#: Flux and have no fixed length, so resolving them here would be a guess.
_WINDOW_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def _resolve_window_start(window, now=None) -> datetime | None:
    """The absolute instant a Flux ``start`` argument names, or None.

    Only the forms `--window` can take: a negative relative duration (`-30d`),
    `0` for the epoch, or an RFC3339 timestamp. Anything else yields None and
    the truncation signal below is simply not emitted -- claiming a run's
    history is short when it is not would be worse than saying nothing.
    """
    if window is None:
        return None
    text = str(window).strip()
    if not text:
        return None
    if text in ("0", "-0"):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    if text.startswith("-"):
        body = text[1:]
        for unit, seconds in _WINDOW_UNIT_SECONDS.items():
            if body.endswith(unit) and body[: -len(unit)].replace(".", "", 1).isdigit():
                now = now or datetime.now(timezone.utc)
                return now - timedelta(seconds=float(body[: -len(unit)]) * seconds)
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _resolve_deployment_start(name: str):
    """When the deployment's registry row was created, or None.

    The start of the SERIES, which is what this is for: `prod
    continuous-uploader deploy` deliberately reuses its row, so a redeploy does
    not move it.
    """
    repo = get_repository()
    if repo is None:
        return None
    deployment = repo.get_active_by_name(name) or repo.get_by_name(name)
    created = getattr(deployment, "created_at", None) if deployment else None
    if created is None:
        return None
    # SQLite hands back naive datetimes on some paths; everything stored is UTC.
    return created if created.tzinfo else created.replace(tzinfo=timezone.utc)


def _window_coverage(
    points: list[dict],
    window,
    retention_secs=None,
    now=None,
    deployment_started_at=None,
) -> dict:
    """How far back the stored history goes, against how far back it was asked for.

    A short answer has to SAY it is short. InfluxDB does not refuse a window
    wider than its retention -- it returns whatever is inside it -- so a `-30d`
    query against a 7-day bucket comes back looking like a complete 30 days and
    nothing in the output says otherwise. That is how nine days of PROD-WDL-01's
    download history read as present when it was not (INFRA-484).

    **The verdict comes from the bucket's RETENTION, not from the points.**
    Every query is already `range(start: <window>)`, so the earliest point it
    can return is at or after the window start: comparing the two made
    ``window_starts_before_data`` true on almost every non-empty answer,
    complete ones included, which left the signal firing when nothing was
    missing (review finding 3). Retention is the fact that actually bounds the
    history, and it is one cheap metadata read rather than a second query.

    It also settles the case item 8 asks for by construction: a bucket set to
    infinite can never truncate, so the four permanent deployments stop
    triggering the signal once their retention is `0`.

    **And the DEPLOYMENT'S AGE decides whether anything was actually lost.**
    Retention bounds what the bucket *can* hold; it says nothing about whether
    there was ever anything there to expire. A deployment created this morning,
    queried with the default `-30d` against a 7-day bucket, has its entire
    history present -- and was reported as truncated purely because the
    requested start precedes `now - 7d` (review finding 3). So the two facts
    are reported separately:

    * ``retention_limits_window`` -- the window asks for data the bucket cannot
      hold. A property of the request and the bucket, always computable.
    * ``window_starts_before_data`` -- the answer is ACTUALLY short, i.e. the
      deployment was already running before the retention horizon, so data that
      existed has since expired. This is the one the warning fires on and the
      one the issue asks for.

    ``retention_secs`` is 0 for infinite, a positive number of seconds, or None
    when it could not be read; an unknown retention or an unknown deployment
    start yields a verdict of None, because "could not look" is not "nothing
    there". ``oldest_point`` is still reported: it is what the warning names
    and what a reader compares against by eye.
    """
    now = now or datetime.now(timezone.utc)
    times = [p["_time"] for p in points if p.get("_time") is not None]
    oldest = min(times) if times else None
    start = _resolve_window_start(window, now=now)
    history_start = None
    if retention_secs is not None and retention_secs > 0:
        history_start = now - timedelta(seconds=retention_secs)

    if start is None or retention_secs is None:
        limited = None
    elif history_start is None:
        # Infinite retention: nothing can ever have expired.
        limited = False
    else:
        limited = start < history_start

    if limited is None:
        truncated = None
    elif not limited:
        # The bucket can hold everything that was asked for.
        truncated = False
    elif deployment_started_at is None:
        # The window outruns the bucket, but without knowing when the
        # deployment began there is no telling whether anything existed to be
        # lost. Unknown, not "short".
        truncated = None
    else:
        truncated = deployment_started_at < history_start

    return {
        "oldest_point": oldest.isoformat() if oldest is not None else None,
        "window_start": start.isoformat() if start is not None else None,
        "retention_secs": retention_secs,
        "history_starts_at": history_start.isoformat() if history_start else None,
        "deployment_started_at": (
            deployment_started_at.isoformat() if deployment_started_at else None
        ),
        "retention_limits_window": limited,
        "window_starts_before_data": truncated,
    }


def _resolve_retention(creds, buckets, console) -> int | None:
    """The retention bounding a query over ``buckets``, or None if unreadable.

    Never raises and never fails the command: this is a diagnostic printed
    beside the answer, not the answer.
    """
    influx_url, influx_token, influx_org, _bucket = creds
    try:
        return binding_retention_seconds(influx_url, influx_token, influx_org, buckets)
    except Exception:  # noqa: BLE001 — a diagnostic must not break the report
        return None


def _warn_if_truncated(coverage: dict, name: str, console) -> None:
    """Say on the console what the JSON already says.

    The JSON half matters more -- a warning on a console nobody reads is how
    this went unnoticed for nine days -- but the operator running the command by
    hand is also the one who can act on it. Named in terms of the retention,
    because that is the thing to change (`monitoring set-retention`) and the
    thing that explains the gap.
    """
    if not coverage.get("window_starts_before_data"):
        return
    days = (coverage.get("retention_secs") or 0) / 86400
    console.print(
        f"[yellow]Warning:[/yellow] this answer is short. The bucket keeps "
        f"{days:g} day(s), so it holds nothing before "
        f"{coverage['history_starts_at']}, and this deployment has been "
        f"running since {coverage['deployment_started_at']} — so operations "
        f"it performed before then have expired. The requested window starts "
        f"at {coverage['window_start']} and the oldest point returned is "
        f"{coverage['oldest_point']}. The durable history is in Postgres: "
        f"`saorsa-deploy deployments print-recorded-results --name {name}`. "
        f"`saorsa-deploy monitoring set-retention --name {name} --days 0` "
        f"removes the expiry."
    )


def cmd_print_uploads(args):
    _dispatch(args, op_type="upload")


def cmd_print_downloads(args):
    _dispatch(args, op_type="download")


def _dispatch(args, op_type: str) -> None:
    # `console_for` is quiet when the JSON is going to stdout, so a table or a
    # preamble cannot land in the caller's `jq` input.
    console = console_for(getattr(args, "json_out", None))
    kind = _resolve_kind(args.name, console)

    if kind == "prod-local-uploads":
        console.print(
            f"[yellow]'{args.name}' is a prod-local-uploads deployment — "
            f"its results are written directly to Postgres, not Influx. "
            f"Use `deployments print-recorded-results` to view them.[/yellow]"
        )
        return

    if kind == "prod-continuous-uploader":
        # Same `prod_upload` measurement as prod-uploaders, so the upload view
        # is shared. Its downloads are read-back *verifications*, which live in
        # Postgres rather than the `download` measurement this command reads —
        # falling through would query the wrong measurement and report "nothing
        # recorded" for a deployment that is working.
        if op_type == "download":
            console.print(
                f"[yellow]'{args.name}' is a prod-continuous-uploader — it "
                f"does not download, it re-reads what it uploaded.[/yellow]"
            )
            console.print(
                "Read-back verifications are written straight to Postgres; "
                "view them with [bold]deployments print-recorded-results "
                "--id <id>[/bold]."
            )
            return
        _run_prod_uploads(args, console)
        return

    if kind == "prod-uploaders":
        if op_type == "download":
            console.print(
                f"[yellow]'{args.name}' is a prod-uploaders deployment "
                f"and has no download records.[/yellow]"
            )
            return
        _run_prod_uploads(args, console)
        return

    if kind in ("prod-continuous-downloader", "prod-downloaders"):
        if op_type == "upload":
            console.print(
                f"[yellow]'{args.name}' is a {kind} deployment and has no upload records.[/yellow]"
            )
            return
        _run_file_ops(args, op_type, console)
        return

    # The browser cohort's own measurement, in place of the native/WASM
    # download operations. Checked after the prod kinds above because no prod
    # kind runs a browser tier, so `--browser` there would silently print an
    # empty table rather than the kind's own advisory.
    if op_type == "download" and getattr(args, "browser", False):
        _run_browser_rounds(args, console)
        return

    # Testnet (explicit "testnet" or unrecognised/empty kind — fall through
    # to the historical behaviour).
    _run_file_ops(args, op_type, console)


def _json_point(point: dict) -> dict:
    """One operation point as JSON-safe values.

    Only `_time` needs converting -- the rest of the pivoted record is ints,
    strings and Nones already. Unknown keys are passed through rather than
    allowlisted, so a field a client starts emitting reaches the consumer
    without a change here.
    """
    out = {}
    for key, value in point.items():
        out[key] = value.isoformat() if isinstance(value, datetime) else value
    return out


def _emit_ops_json(
    args,
    console,
    *,
    deployment: str,
    kind: str,
    op_type: str,
    source: str,
    points: list[dict],
    window=None,
    coverage: dict | None = None,
) -> None:
    """Write the per-operation records as JSON when `--json-out` was given.

    The consumer is a reporting tool that would otherwise re-derive cycles and
    run times by parsing rich tables -- box-drawing characters, truncated
    hostnames and dates wrapped onto a second line. The records go out
    unaggregated: a cycle boundary, a run window and a success rate are all
    derivable from them, and which of those a caller wants is not this
    command's business.

    An empty list is still emitted. "The query ran and returned nothing" and
    "the file is missing because the command never got that far" are different
    states, and a consumer can only tell them apart if the empty case writes.
    """
    json_out = getattr(args, "json_out", None)
    if not json_out:
        return
    emit_json(
        {
            "deployment": deployment,
            "kind": kind,
            "op_type": op_type,
            "source": source,
            "window": window,
            "queried_at": datetime.now(timezone.utc).isoformat(),
            "count": len(points),
            # How far back the answer actually reaches (INFRA-484). Emitted
            # even when it cannot be worked out, with nulls, so a consumer can
            # tell "the window was not resolvable" from "the key is missing
            # because this tool predates it".
            **(coverage or _window_coverage(points, window)),
            "ops": [_json_point(p) for p in points],
        },
        json_out,
        console,
    )


def _resolve_kind(name: str, console: Console) -> str:
    """Look up the deployment's ``kind`` from the registry.

    Returns an empty string if the deployment isn't recorded — callers
    treat that as testnet for backwards compatibility with rows that
    pre-date the ``kind`` field.
    """
    repo = get_repository()
    if repo is None:
        return ""
    deployment = repo.get_active_by_name(name) or repo.get_by_name(name)
    if deployment is None:
        return ""
    deploy_args = deployment.deploy_args or {}
    if not isinstance(deploy_args, dict):
        return ""
    explicit = deploy_args.get("kind")
    if explicit:
        return explicit
    if "bootstrap_count" in deploy_args:
        return _TESTNET_KIND
    return ""


# ---------------------------------------------------------------------------
# Influx credential resolution (shared between testnet file_ops and prod_upload)
# ---------------------------------------------------------------------------


def _resolve_influx(args, console: Console):
    """Resolve (url, token, org, bucket) from CLI args + monitoring state.

    Returns ``None`` if credentials can't be assembled (and prints the
    appropriate yellow advisory itself).
    """
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
                f"Run `monitoring infra` + `monitoring provision` first if "
                f"you expected to query Influx.[/yellow]"
            )
            return None
        influx_url = influx_url or state.get("influxdb_url")
        influx_token = influx_token or state.get("influxdb_token")
        influx_org = influx_org or state.get("influxdb_org") or "ant"
        influx_bucket = influx_bucket or state.get("influxdb_bucket") or "testnet-metrics"

    if not (influx_url and influx_token):
        console.print(
            f"[yellow]Monitoring state exists for '{args.name}' but InfluxDB "
            f"isn't provisioned yet. Run `monitoring provision` first.[/yellow]"
        )
        return None

    return (
        influx_url,
        influx_token,
        influx_org or "ant",
        influx_bucket or "testnet-metrics",
    )


# ---------------------------------------------------------------------------
# Testnet / prod-continuous-downloader: per-op file_ops tables
# ---------------------------------------------------------------------------


def _run_file_ops(args, op_type: str, console: Console) -> None:
    creds = _resolve_influx(args, console)
    if creds is None:
        return
    influx_url, influx_token, influx_org, influx_bucket = creds

    buckets = influx_bucket
    if influx_bucket != INFLUXDB_WASM_BUCKET:
        buckets = f"{influx_bucket} + {INFLUXDB_WASM_BUCKET}"
    console.print(f"[bold]Querying {influx_url} (bucket={buckets}, window={args.window})...[/bold]")
    try:
        # Both buckets: WASM clients write the same upload/download
        # measurements to a bucket of their own, so querying only the native
        # bucket would render a dead WASM tier indistinguishable from one that
        # was never configured.
        points = fetch_all_operation_points(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
            wasm_bucket=INFLUXDB_WASM_BUCKET,
            start=args.window,
        )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] InfluxDB query failed: {e}")
        sys.exit(1)

    filtered = [p for p in points if p.get("_measurement") == op_type]
    coverage = _window_coverage(
        filtered,
        args.window,
        _resolve_retention(creds, (influx_bucket, INFLUXDB_WASM_BUCKET), console),
        deployment_started_at=_resolve_deployment_start(args.name),
    )
    _emit_ops_json(
        args,
        console,
        deployment=args.name,
        kind=_resolve_kind(args.name, console),
        op_type=op_type,
        source="influx",
        points=filtered,
        window=args.window,
        coverage=coverage,
    )
    _warn_if_truncated(coverage, args.name, console)
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

    payment_by_host = _build_payment_map(args.name) if op_type == "upload" else {}
    cache_by_host = _build_bootstrap_cache_map(args.name)
    transport_by_host = _build_transport_map(args.name)

    for host in sorted(by_host.keys()):
        ops = sorted(by_host[host], key=lambda p: p.get("start_ns") or 0)
        total = len(ops)
        skew_label = _clock_skew_label(ops)
        if args.recent:
            ops = ops[-10:]
        title_count = (
            f"{len(ops)} of {total} op(s)"
            if args.recent and total > len(ops)
            else f"{len(ops)} op(s)"
        )
        title_parts = [host]
        transport = transport_by_host.get(host)
        if transport is not None:
            title_parts.append(f"transport={transport}")
        if op_type == "upload":
            payment = payment_by_host.get(host, "default")
            title_parts.append(f"payment={payment}")
        # Not shown for a WASM host: the peer cache has no browser-path meaning
        # and the option is refused there, so a value would describe a knob that
        # had no effect.
        cache = cache_by_host.get(host)
        if cache is not None and transport != "wasm":
            title_parts.append(f"bootstrap-cache={'on' if cache else 'off'}")
        if skew_label:
            title_parts.append(f"clock skew {skew_label}")
        title = " — ".join(title_parts) + f" ({title_count})"
        table = Table(
            title=title,
            title_style="bold",
        )
        # Only downloads verify content, and only against an uploader that
        # publishes a hash. Omitted entirely when nothing in the table has one,
        # so an upload table -- and a run predating verification -- renders as
        # it did rather than gaining a column of dashes.
        show_verify = any(op.get("verify_status") is not None for op in ops)
        table.add_column("#", justify="right")
        table.add_column("Started (UTC)")
        table.add_column("Completed (UTC)")
        table.add_column("Size", justify="right")
        table.add_column("Status")
        if show_verify:
            table.add_column("Verify")
        table.add_column("Duration", justify="right")
        for i, op in enumerate(ops, start=1):
            cells = [
                str(i),
                _fmt_started(op),
                _fmt_time(op.get("_time")),
                _fmt_size(op.get("file_size")),
                _fmt_status(op.get("status")),
            ]
            if show_verify:
                cells.append(_fmt_verify(op.get("verify_status")))
            cells.append(_fmt_duration(op.get("total_ms")))
            table.add_row(*cells)
        console.print(table)

    _print_transport_totals(op_type, by_host, transport_by_host, console)


def _print_transport_totals(
    op_type: str, by_host: dict, transport_by_host: dict, console: Console
) -> None:
    """Success counts split by transport.

    A blended figure describes neither transport: the native and WASM tiers run
    different client code over different transports, so their rates are not
    comparable and their union moves with whatever VM mix a run happened to
    use. Printed only when the run actually has both, so a native-only
    deployment renders exactly as it did before.
    """
    tiers: dict[str, list[int]] = {}
    for host, ops in by_host.items():
        tier = transport_by_host.get(host)
        if tier is None:
            continue
        slot = tiers.setdefault(tier, [0, 0, 0, 0])
        for op in ops:
            # A skip is neither a success nor an attempt: the transport refused
            # the file before the network was involved, so it stays out of the
            # denominator. It is still reported, beside the rate.
            if op.get("status") == 2:
                slot[2] += 1
                continue
            slot[1] += 1
            if op.get("status") == 0:
                slot[0] += 1
            if op.get("error_kind") == "crash":
                slot[3] += 1
    if len(tiers) < 2:
        return
    console.print(f"[bold]{op_type} success by transport[/bold]")
    for tier in ("native", "wasm"):
        if tier not in tiers:
            continue
        ok, total, skipped, crashes = tiers[tier]
        rate = f"{100.0 * ok / total:.2f}%" if total else "n/a"
        colour = "green" if tier == "native" else "cyan"
        extra = ""
        if skipped:
            extra += f", {skipped} skipped (over-cap)"
        if crashes:
            extra += f", {crashes} crashed"
        console.print(f"  [{colour}]{tier:<7}[/{colour}] {ok}/{total} = {rate}{extra}")


def _client_tiers(name: str) -> list[tuple[str | None, dict]]:
    """``(extension, client_state)`` for every client tier of ``name``.

    The deployment's own tier first (extension ``None``), then each client
    extension's. Every per-host label below is derived from this, so an
    extension's VMs get the same payment / transport / cache annotations the
    deployment's own do — they are rendering the same run's operations.
    """
    tiers: list[tuple[str | None, dict]] = []
    try:
        tiers.append((None, load_client_state(name)))
    except RuntimeError:
        pass
    tiers.extend(client_extension_tiers(name))
    return tiers


def _build_payment_map(name: str) -> dict[str, str]:
    """Map hostname → payment_type label for the deployment's upload clients.

    Hostname matches the client VM's actual hostname (set by the digitalocean-
    client terraform module): ``{name}-ant-client-{role}-{region}-{client_index}``.
    Missing client state returns an empty map — the caller falls back to
    rendering "default" for every host rather than blocking the print.
    """
    out: dict[str, str] = {}
    for extension, state in _client_tiers(name):
        prefix = terraform_name(name, extension)
        for c in state.get("clients", []):
            if c.get("role") != "upload":
                continue
            host = f"{prefix}-ant-client-{c['role']}-{c['region']}-{c['client_index']}"
            out[host] = c.get("payment_type") or "default"
    return out


def _build_transport_map(name: str) -> dict[str, str]:
    """Map hostname → "native" | "wasm" for every client in the deployment.

    A production client kind's transport is a property of the DEPLOYMENT, not of
    the VM: its role is always the native `download`, so reading the role
    reported every production WASM run as `transport=native` — recorded
    correctly, printed wrong. `client_transport` holds both rules so the
    recording and reporting paths cannot drift again.

    An empty map means the caller omits the label rather than guessing a
    transport, which would be worse than saying nothing.
    """
    from saorsa_deploy.registry import get_repository

    try:
        repo = get_repository()
        if repo is not None:
            deployment = repo.get_active_by_name(name) or repo.get_by_name(name)
            if deployment is not None:
                scoped = deployment_scoped_transport(getattr(deployment, "deploy_args", None))
                if scoped is not None:
                    # One transport for the whole deployment; every host it wrote
                    # points from ran it.
                    return _ConstantTransport(scoped)
    except Exception:
        # Reporting must never fail because the registry is unreachable; fall
        # back to the per-VM rule below.
        pass

    out: dict[str, str] = {}
    for extension, state in _client_tiers(name):
        out.update(transport_map_from_client_state(terraform_name(name, extension), state))
    return out


class _ConstantTransport(dict):
    """A map answering the same transport for every host.

    A dict subclass so the two call sites keep using `.get(host)` unchanged, and
    so `len(...) < 2` still reports a single-transport run correctly.
    """

    def __init__(self, transport: str):
        super().__init__()
        self._transport = transport

    def get(self, _host, default=None):  # noqa: D102
        return self._transport


def _build_bootstrap_cache_map(name: str) -> dict[str, bool]:
    """Map hostname → bootstrap_cache flag for every client in the deployment.

    Covers both roles (the cache mode applies to uploaders and downloaders
    alike). Hosts whose client-state entry predates the option are simply
    absent from the map — the caller omits the label rather than guessing.
    """
    out: dict[str, bool] = {}
    for extension, state in _client_tiers(name):
        prefix = terraform_name(name, extension)
        for c in state.get("clients", []):
            if c.get("bootstrap_cache") is None:
                continue
            role = c.get("role", "upload")
            host = f"{prefix}-ant-client-{role}-{c['region']}-{c['client_index']}"
            out[host] = bool(c["bootstrap_cache"])
    return out


def _build_prod_payment_map(name: str) -> dict[str, str]:
    """Map hostname → payment_type label for a prod-uploaders deployment.

    Unlike testnet uploaders, the ``prod uploaders deploy`` flow does not
    propagate ``payment_type`` into client state — it passes it straight
    through to the provisioner. The authoritative source is therefore
    ``deploy_args.uploaders`` on the registry row. Match those specs to
    the client_state's upload VMs by sorted ``client_index`` (which
    ``clients infra`` assigns 1..N in spec order).
    """
    repo = get_repository()
    if repo is None:
        return {}
    deployment = repo.get_active_by_name(name) or repo.get_by_name(name)
    if deployment is None:
        return {}
    deploy_args = deployment.deploy_args or {}
    uploader_specs = deploy_args.get("uploaders") if isinstance(deploy_args, dict) else None
    if not uploader_specs:
        return {}

    try:
        state = load_client_state(name)
    except RuntimeError:
        return {}
    upload_clients = sorted(
        (c for c in state.get("clients", []) if c.get("role", "upload") == "upload"),
        key=lambda c: c.get("client_index") or 0,
    )
    if len(upload_clients) != len(uploader_specs):
        return {}

    out: dict[str, str] = {}
    for spec, client in zip(uploader_specs, upload_clients):
        host = f"{name}-ant-client-upload-{client['region']}-{client['client_index']}"
        out[host] = spec.get("payment_type") or "default"
    return out


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
    """
    if now is None:
        now = datetime.now(timezone.utc)

    starts = [o["start_ns"] for o in ops if o.get("start_ns") is not None]
    if starts:
        started_at = datetime.fromtimestamp(min(starts) / 1_000_000_000, tz=timezone.utc)
        elapsed_s = (now - started_at).total_seconds()
    else:
        started_at = None
        elapsed_s = None

    total = len(ops)
    successes = sum(1 for o in ops if o.get("status") == 0)
    failures = sum(1 for o in ops if o.get("status") == 1)
    skipped = sum(1 for o in ops if o.get("status") == 2)
    # An OOM kill of the transfer process, which on a client VM is what a
    # browser-path transfer too large for the box looks like. Counted separately
    # from a plain failure because the fix is a bigger VM, not the network.
    crashes = sum(1 for o in ops if o.get("error_kind") == "crash")
    # The distribution of WHY, which is what makes a failure count actionable
    # (INFRA-484). Every row with a kind, skips included: a skip carries
    # `over_cap` and leaving it out would make the table disagree with the
    # counts beside it. A diagnostic and nothing more -- no gate reads it.
    error_kinds: dict[str, int] = {}
    for o in ops:
        kind = o.get("error_kind")
        if kind:
            error_kinds[kind] = error_kinds.get(kind, 0) + 1

    success_durations = [
        o["total_ms"] for o in ops if o.get("status") == 0 and o.get("total_ms") is not None
    ]
    overall_avg_ms = sum(success_durations) / len(success_durations) if success_durations else None

    return {
        "started_at": started_at,
        "elapsed_s": elapsed_s,
        "total": total,
        "successes": successes,
        "failures": failures,
        "skipped": skipped,
        "crashes": crashes,
        "error_kinds": error_kinds,
        "overall_avg_ms": overall_avg_ms,
    }


def _print_summary(op_type: str, by_host: dict, console: Console) -> None:
    label = op_type.capitalize() + "s"

    all_starts = [
        o["start_ns"] for ops in by_host.values() for o in ops if o.get("start_ns") is not None
    ]
    if all_starts:
        started_at = datetime.fromtimestamp(min(all_starts) / 1_000_000_000, tz=timezone.utc)
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
        if summary["skipped"]:
            table.add_row("Skipped (over-cap)", str(summary["skipped"]))
        if summary["crashes"]:
            table.add_row("Crashes (killed by signal)", str(summary["crashes"]))
        table.add_row("Avg duration", _fmt_duration(summary["overall_avg_ms"]))
        console.print(table)
        _print_error_kinds(summary["error_kinds"], console)


def _print_error_kinds(kinds: dict, console: Console) -> None:
    """Counts by `error_kind`, most frequent first.

    Printed only when something has a kind, so a clean run and every run
    recorded before the field existed render exactly as they did. The per-record
    `reason` behind each of these is in the `--details` view.
    """
    if not kinds:
        return
    table = Table(title="By error_kind")
    table.add_column("error_kind")
    table.add_column("ops", justify="right")
    for kind, count in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])):
        table.add_row(kind, f"{count:,}")
    console.print(table)


# ---------------------------------------------------------------------------
# Browser downloaders: per-round browser_download tables
# ---------------------------------------------------------------------------


def summarise_browser_rounds(points: list[dict]) -> list[dict]:
    """One summary per `round_id`, oldest first.

    Pure, so the renderer below is testable without an InfluxDB. The boundary
    comes from ``start_ns``, which the harness stamps as the round's boundary
    rather than as the moment its browser launched — so every instance of a
    round reports the same one, and a round with instances scattered over a
    minute still has one time.

    ``skipped`` rows are counted and reported but excluded from the percentiles
    and from the success denominator: a round that ran before the operator chose
    a hot file is part of the series, not a failure.
    """
    by_round: dict[int, list[dict]] = {}
    for point in points:
        round_id = point.get("round_id")
        if round_id is None:
            continue
        by_round.setdefault(round_id, []).append(point)

    out: list[dict] = []
    for round_id in sorted(by_round):
        rows = by_round[round_id]
        boundaries = [p["start_ns"] for p in rows if p.get("start_ns") is not None]
        successes = [p for p in rows if p.get("status") == "success"]
        failures = [p for p in rows if p.get("status") == "failure"]
        skipped = [p for p in rows if p.get("status") == "skipped"]
        durations = [float(p["total_ms"]) for p in successes if p.get("total_ms") is not None]
        kinds: dict[str, int] = {}
        for point in failures:
            kind = point.get("error_kind") or "unknown"
            kinds[kind] = kinds.get(kind, 0) + 1
        out.append(
            {
                "round_id": round_id,
                "boundary": (
                    datetime.fromtimestamp(min(boundaries) / 1_000_000_000, tz=timezone.utc)
                    if boundaries
                    else None
                ),
                # Distinct (host, instance): one point per instance per round,
                # so a count of rows would hide a duplicate and a count of
                # instances would hide a VM that reported nothing.
                "instances": len({(p.get("host"), p.get("instance")) for p in rows}),
                "hosts": len({p.get("host") for p in rows}),
                "success": len(successes),
                "failure": len(failures),
                "skipped": len(skipped),
                "p50_ms": percentile(durations, 0.50),
                "p95_ms": percentile(durations, 0.95),
                "error_kinds": kinds,
                # Summed over every row that carried the field, skips included:
                # a skip launches no browser and so contributes 0, which is the
                # truth rather than a gap.
                "stalls": sum(p["stalls"] for p in rows if p.get("stalls") is not None),
                "refused_dials": sum(
                    p["refused_dials"] for p in rows if p.get("refused_dials") is not None
                ),
            }
        )
    return out


def _fmt_error_kinds(kinds: dict[str, int]) -> str:
    if not kinds:
        return "-"
    return ", ".join(f"{k}={v}" for k, v in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))


def _run_browser_rounds(args, console: Console) -> None:
    creds = _resolve_influx(args, console)
    if creds is None:
        return
    influx_url, influx_token, influx_org, _ = creds

    console.print(
        f"[bold]Querying {influx_url} (bucket={INFLUXDB_WASM_BUCKET}, "
        f"measurement=browser_download, window={args.window})...[/bold]"
    )
    try:
        points = fetch_browser_download_points(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=INFLUXDB_WASM_BUCKET,
            start=args.window,
        )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] InfluxDB query failed: {e}")
        sys.exit(1)

    _emit_ops_json(
        args,
        console,
        deployment=args.name,
        kind="browser",
        op_type="download",
        source="influx:browser_download",
        points=points,
        window=args.window,
        coverage=_window_coverage(
            points,
            args.window,
            _resolve_retention(creds, (INFLUXDB_WASM_BUCKET,), console),
            deployment_started_at=_resolve_deployment_start(args.name),
        ),
    )
    if not points:
        console.print(
            f"[yellow]No browser-download rounds recorded yet for "
            f"'{args.name}' (window {args.window}).[/yellow]"
        )
        return

    rounds = summarise_browser_rounds(points)
    if args.recent:
        rounds = rounds[-10:]

    if args.summary:
        _print_browser_summary(rounds, args.name, console)
        return

    table = Table(
        title=f"{args.name} — browser-download rounds ({len(rounds)} round(s))",
        title_style="bold",
    )
    table.add_column("Round", justify="right")
    table.add_column("Boundary (UTC)")
    table.add_column("Inst", justify="right")
    table.add_column("OK", justify="right")
    table.add_column("Fail", justify="right")
    table.add_column("Skip", justify="right")
    table.add_column("p50", justify="right")
    table.add_column("p95", justify="right")
    table.add_column("Stalls", justify="right")
    table.add_column("Refused", justify="right")
    table.add_column("Failures by kind")
    for entry in rounds:
        table.add_row(
            str(entry["round_id"]),
            _fmt_time(entry["boundary"]),
            str(entry["instances"]),
            str(entry["success"]),
            str(entry["failure"]),
            str(entry["skipped"]),
            _fmt_duration(entry["p50_ms"]),
            _fmt_duration(entry["p95_ms"]),
            str(entry["stalls"]),
            str(entry["refused_dials"]),
            _fmt_error_kinds(entry["error_kinds"]),
        )
    console.print(table)
    _print_browser_summary(rounds, args.name, console)


def _print_browser_summary(rounds: list[dict], name: str, console: Console) -> None:
    attempted = sum(r["success"] + r["failure"] for r in rounds)
    ok = sum(r["success"] for r in rounds)
    table = Table(show_header=False, title=f"{name} — browser cohort totals")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Rounds", f"{len(rounds):,}")
    table.add_row("Downloads", f"{attempted + sum(r['skipped'] for r in rounds):,}")
    # The completion rate excludes skips, exactly as the report's G1 gate does.
    table.add_row("Completion", f"{100.0 * ok / attempted:.2f}%" if attempted else "n/a")
    table.add_row("Skipped", f"{sum(r['skipped'] for r in rounds):,}")
    table.add_row("Stalls", f"{sum(r['stalls'] for r in rounds):,}")
    table.add_row("Refused dials", f"{sum(r['refused_dials'] for r in rounds):,}")
    console.print(table)
    console.print(
        "[dim]Per-chunk holder share, the stall distribution and the gates come "
        "from `analysis browser-reads`, which reads the recorded Postgres rows "
        "and so survives teardown.[/dim]"
    )


# ---------------------------------------------------------------------------
# prod-uploaders: per-file prod_upload tables
# ---------------------------------------------------------------------------


# `src-fail`, not `dl-fail`: status 2 means the uploader could not fetch
# the SOURCE file from its origin (an Internet Archive URL, typically),
# so no upload was ever attempted. In a deployment that also downloads
# its uploads back to verify them, "dl-fail" reads as "the read-back
# failed" — i.e. as a production fault — when it is the opposite: a
# problem fetching our own test material, saying nothing about the
# network.
# `failed` and `over-cap` are browser-path only. `failed` is deliberately NOT
# spelled "partial": it means no payment went out, so nothing is stored — the
# opposite of a partial upload, and the difference decides whether the file's
# pool entry is spent. `over-cap` is not a failure at all: the transport refuses
# the file and never asks the network.
_PROD_STATUS_LABELS = {
    0: "[green]ok[/green]",
    1: "[red]partial[/red]",
    2: "[yellow]src-fail[/yellow]",
    3: "[yellow]over-cap[/yellow]",
    4: "[red]failed[/red]",
}

_PROD_STATUS_TEXT = {
    0: "ok",
    1: "partial",
    2: "src-fail",
    3: "over-cap",
    4: "failed",
}


def _run_prod_uploads(args, console: Console) -> None:
    creds = _resolve_influx(args, console)
    if creds is None:
        return
    influx_url, influx_token, influx_org, influx_bucket = creds

    console.print(
        f"[bold]Querying {influx_url} (bucket={influx_bucket}, window={args.window})...[/bold]"
    )
    # Both buckets: a browser-path uploader writes the same `prod_upload`
    # measurement into its own, so a single-bucket read would make a WASM tier
    # that failed every upload look like one that was never configured.
    #
    # `--window` is PASSED, where this used to call with no `start` and so
    # query from the epoch -- which is why a prod-upload JSON recorded
    # `"window": null` (INFRA-484). With a deployment's retention set to
    # infinite, epoch-to-now grows without bound, so this stopped being
    # tidiness.
    points = fetch_all_prod_upload_points(
        url=influx_url,
        token=influx_token,
        org=influx_org,
        bucket=influx_bucket,
        wasm_bucket=INFLUXDB_WASM_BUCKET,
        start=args.window,
    )
    coverage = _window_coverage(
        points,
        args.window,
        _resolve_retention(creds, (influx_bucket, INFLUXDB_WASM_BUCKET), console),
        deployment_started_at=_resolve_deployment_start(args.name),
    )
    _emit_ops_json(
        args,
        console,
        deployment=args.name,
        kind=_resolve_kind(args.name, console),
        op_type="upload",
        source="influx:prod_upload",
        points=points,
        window=args.window,
        coverage=coverage,
    )
    _warn_if_truncated(coverage, args.name, console)
    if not points:
        console.print(f"[yellow]No prod_upload records yet for '{args.name}'.[/yellow]")
        return

    by_host: dict[str, list[dict]] = {}
    for p in points:
        by_host.setdefault(p["host"], []).append(p)

    payment_by_host = _build_prod_payment_map(args.name)
    cache_by_host = _build_bootstrap_cache_map(args.name)
    transport_by_host = _build_prod_upload_transport_map(args.name)

    if getattr(args, "details", False):
        _prod_print_details(by_host, payment_by_host, cache_by_host, transport_by_host, console)
    else:
        _prod_print_table(by_host, payment_by_host, cache_by_host, transport_by_host, console)
    _prod_print_transport_totals(by_host, transport_by_host, console)


def _build_prod_upload_transport_map(name: str) -> dict[str, str]:
    """Map hostname -> transport for a prod-uploaders deployment.

    Read from the registry row's persisted uploader list, not the S3 client
    state: the state is deleted by `clients destroy`, and this command is most
    useful right up to (and after) teardown.
    """
    repo = get_repository()
    if repo is None:
        return {}
    deployment = repo.get_active_by_name(name) or repo.get_by_name(name)
    if deployment is None:
        return {}
    from saorsa_deploy.client_transport import transport_map_from_deploy_args

    return transport_map_from_deploy_args(deployment.deploy_args or {})


def _transport_segment(transport_by_host: dict[str, str], host: str) -> str:
    """`` — [wasm]`` for a browser-path host, nothing for a native one.

    Absent rather than ``[native]`` when the deployment has no WASM uploader, so
    a native-only run's output is exactly what it was before. The bracket is
    escaped for Rich, which would otherwise read ``[wasm]`` as a style tag and
    render the label as nothing at all.
    """
    if not any(kind == "wasm" for kind in transport_by_host.values()):
        return ""
    return f" — \\[{transport_by_host.get(host) or 'native'}]"


def _prod_print_transport_totals(
    by_host: dict[str, list[dict]],
    transport_by_host: dict[str, str],
    console: Console,
) -> None:
    """Per-transport success counts, with over-cap files excluded and stated.

    The native and browser paths are not the same instrument, so one blended
    success rate describes neither. Printed only when the run carries a WASM
    uploader. An over-cap file never enters the denominator: the transport
    refused it, and the network was never asked.
    """
    if not any(kind == "wasm" for kind in transport_by_host.values()):
        return
    console.print()
    console.print("[bold]Upload totals by transport[/bold]")
    for transport in ("native", "wasm"):
        ops = [
            op
            for host, host_ops in by_host.items()
            if (transport_by_host.get(host) or "native") == transport
            for op in host_ops
        ]
        if not ops:
            continue
        over_cap = sum(1 for op in ops if op.get("status") == 3)
        attempted = [op for op in ops if op.get("status") != 3]
        ok = sum(1 for op in attempted if op.get("status") == 0)
        total = len(attempted)
        rate = f"{100.0 * ok / total:.2f}%" if total else "n/a"
        colour = "green" if transport == "native" else "cyan"
        extra = f", {over_cap} over-cap (not uploaded)" if over_cap else ""
        console.print(f"  [{colour}]{transport:<7}[/{colour}] {ok}/{total} = {rate}{extra}")


def _bootstrap_cache_segment(cache_by_host: dict[str, bool], host: str) -> str:
    cache = cache_by_host.get(host)
    if cache is None:
        return ""
    return f" — bootstrap-cache={'on' if cache else 'off'}"


def _prod_print_table(
    by_host: dict[str, list[dict]],
    payment_by_host: dict[str, str],
    cache_by_host: dict[str, bool],
    transport_by_host: dict[str, str],
    console: Console,
) -> None:
    for host in sorted(by_host.keys()):
        ops = sorted(by_host[host], key=lambda p: p.get("start_ns") or 0)
        payment = payment_by_host.get(host, "default")
        cache_seg = _bootstrap_cache_segment(cache_by_host, host)
        skew_label = _clock_skew_label(ops)
        skew_seg = f" — clock skew {skew_label}" if skew_label else ""
        transport_seg = _transport_segment(transport_by_host, host)
        table = Table(
            title=(
                f"prod_upload — {host}{transport_seg} — payment={payment}"
                f"{cache_seg}{skew_seg} ({len(ops)} record(s))"
            ),
            title_style="bold",
            show_lines=True,
        )
        table.add_column("#", justify="right")
        # When an upload happened, not just that it did. A permanent daily run
        # is a time series, and a table of it with no timestamp cannot answer
        # "did today's cycle run?" — the question the whole deployment exists
        # to answer. The download table has carried this from the start.
        #
        # Completion rather than start: `_time` is stamped by InfluxDB's own
        # clock when the point lands, which is immediately after the upload
        # finishes, so it is trustworthy even on a clock-skewed VM. The
        # client's own start is in `start_ns` and is shown by `--details`.
        table.add_column("Completed (UTC)")
        table.add_column("File")
        table.add_column("Size", justify="right")
        table.add_column("Status")
        table.add_column("Chunks Uploaded", justify="right")
        table.add_column("Failed", justify="right")
        table.add_column("Duration", justify="right")
        for i, op in enumerate(ops, start=1):
            table.add_row(
                str(i),
                _fmt_time(op.get("_time")),
                op.get("file_name") or "-",
                _fmt_size(op.get("file_size")),
                _PROD_STATUS_LABELS.get(op.get("status"), "-"),
                f"{op.get('chunks_stored') or 0}/{op.get('total_chunks') or 0}",
                str(op.get("chunks_failed") or 0),
                _fmt_duration(op.get("total_ms")),
            )
        console.print(table)


def _prod_print_details(
    by_host: dict[str, list[dict]],
    payment_by_host: dict[str, str],
    cache_by_host: dict[str, bool],
    transport_by_host: dict[str, str],
    console: Console,
) -> None:
    for host in sorted(by_host.keys()):
        ops = sorted(by_host[host], key=lambda p: p.get("start_ns") or 0)
        payment = payment_by_host.get(host, "default")
        cache_seg = _bootstrap_cache_segment(cache_by_host, host)
        skew_label = _clock_skew_label(ops)
        skew_seg = f" — clock skew {skew_label}" if skew_label else ""
        transport_seg = _transport_segment(transport_by_host, host)
        console.rule(
            f"[bold]{host}{transport_seg} — payment={payment}{cache_seg}"
            f"{skew_seg} — {len(ops)} record(s)[/bold]"
        )
        for i, op in enumerate(ops, start=1):
            console.print(f"[bold]#{i}[/bold]")
            console.print(f"  Started (UTC):     {_fmt_started(op)}")
            console.print(f"  Finished (UTC):    {_fmt_time(op.get('_time'))}")
            console.print(f"  File:              {op.get('file_name') or '-'}")
            console.print(f"  Source URL:        {op.get('source_url') or '-'}")
            console.print(f"  Size (bytes):      {op.get('file_size') or 0}")
            console.print(f"  Status:            {_PROD_STATUS_TEXT.get(op.get('status'), '-')}")
            console.print(
                f"  Chunks uploaded:   {op.get('chunks_stored') or 0} / "
                f"{op.get('total_chunks') or 0}"
            )
            console.print(f"  Chunks failed:     {op.get('chunks_failed') or 0}")
            console.print(f"  Address:           {op.get('address') or '-'}")
            console.print(f"  Duration (ms):     {op.get('total_ms') or 0}")
            console.print(f"  Upload secs:       {op.get('upload_secs') or 0}")
            console.print(f"  Reason:            {op.get('reason') or '-'}")
            console.print()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


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
    gb = num_bytes / (1024**3)
    mb = num_bytes / (1024**2)
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
    # Yellow, and its own word: the transport refused the file (over ant-core's
    # browser cap), the network did not. Rendering it as "fail" would report a
    # transport limitation as a network fault, and as "-" would hide the op.
    if code == 2:
        return "[yellow]skip[/yellow]"
    return "-"


def _fmt_verify(code) -> str:
    """Render a download's content-verification result.

    `-` covers three different absences -- a failed download, a line carrying
    no published hash, and a client that does not verify at all -- because none
    of them is a finding. A MISMATCH is, and is the loudest thing in the table.
    """
    if code == 0:
        return "[green]ok[/green]"
    if code == 1:
        return "[bold red]MISMATCH[/bold red]"
    if code == 2:
        return "[yellow]n/a[/yellow]"
    return "-"


def _fmt_time(dt) -> str:
    if dt is None:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_started(op: dict) -> str:
    """Render an op's start as ``Completed - Duration``.

    InfluxDB now stamps each point's ``_time`` with its own (correct) server
    clock, while the ``start_ns`` field carries the client's own — possibly
    skewed — clock. Deriving the start from the server-side completion time
    keeps the Started/Completed columns in the same clock frame; using the raw
    ``start_ns`` would show the client's skewed clock and could read *after*
    the Completed column on a clock-skewed host.
    """
    completed = op.get("_time")
    if completed is None:
        return "-"
    total_ms = op.get("total_ms")
    if total_ms is None:
        return _fmt_time(completed)
    return _fmt_time(completed - timedelta(milliseconds=total_ms))


# Below this magnitude a start_ns/_time gap is just normal drift + the tiny lag
# between op completion and the Influx write, not an injected clock skew.
_SKEW_THRESHOLD_S = 60.0


def _clock_skew_seconds(ops: list[dict]) -> float | None:
    """Estimate a host's clock skew from its recorded ops, or ``None``.

    Each point's ``_time`` is the server-assigned completion time while the
    ``start_ns`` field is the client's own start time. The client's start in
    the server frame is ``_time - total_ms``, so ``start_ns - (_time -
    total_ms)`` is how far the client's clock is shifted. Returns the median
    offset (seconds; +ve = client clock ahead) over all usable ops.
    """
    offsets: list[float] = []
    for op in ops:
        completed = op.get("_time")
        start_ns = op.get("start_ns")
        total_ms = op.get("total_ms")
        if completed is None or start_ns is None or total_ms is None:
            continue
        server_start = completed.timestamp() - total_ms / 1000.0
        offsets.append(start_ns / 1_000_000_000 - server_start)
    if not offsets:
        return None
    return median(offsets)


def _clock_skew_label(ops: list[dict]) -> str | None:
    """A ``+3h00m`` / ``-3h00m`` skew label for ``ops``, or ``None`` if the
    host's clock is effectively un-skewed (within ``_SKEW_THRESHOLD_S``)."""
    skew = _clock_skew_seconds(ops)
    if skew is None or abs(skew) < _SKEW_THRESHOLD_S:
        return None
    sign = "+" if skew >= 0 else "-"
    total_min = round(abs(skew) / 60)
    hours, minutes = divmod(total_min, 60)
    return f"{sign}{hours}h{minutes:02d}m"
