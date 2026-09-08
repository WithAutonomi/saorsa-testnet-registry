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

from saorsa_deploy.client_roles import is_wasm
from saorsa_deploy.influx import (
    fetch_all_operation_points,
    fetch_prod_upload_points,
)
from saorsa_deploy.provisioning.monitoring import INFLUXDB_WASM_BUCKET
from saorsa_deploy.registry import get_repository
from saorsa_deploy.state import load_client_state, load_monitoring_state

_TESTNET_KIND = "testnet"


def cmd_print_uploads(args):
    _dispatch(args, op_type="upload")


def cmd_print_downloads(args):
    _dispatch(args, op_type="download")


def _dispatch(args, op_type: str) -> None:
    console = Console()
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
                f"[yellow]'{args.name}' is a {kind} deployment and has no "
                f"upload records.[/yellow]"
            )
            return
        _run_file_ops(args, op_type, console)
        return

    # Testnet (explicit "testnet" or unrecognised/empty kind — fall through
    # to the historical behaviour).
    _run_file_ops(args, op_type, console)


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
        influx_bucket = (
            influx_bucket or state.get("influxdb_bucket") or "testnet-metrics"
        )

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
    console.print(
        f"[bold]Querying {influx_url} "
        f"(bucket={buckets}, window={args.window})...[/bold]"
    )
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

    payment_by_host = (
        _build_payment_map(args.name) if op_type == "upload" else {}
    )
    cache_by_host = _build_bootstrap_cache_map(args.name)
    role_by_host = _build_role_map(args.name)

    for host in sorted(by_host.keys()):
        ops = sorted(
            by_host[host], key=lambda p: p.get("start_ns") or 0
        )
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
        role = role_by_host.get(host)
        if role is not None:
            title_parts.append(
                f"transport={'wasm' if is_wasm(role) else 'native'}"
            )
        if op_type == "upload":
            payment = payment_by_host.get(host, "default")
            title_parts.append(f"payment={payment}")
        cache = cache_by_host.get(host)
        if cache is not None:
            title_parts.append(f"bootstrap-cache={'on' if cache else 'off'}")
        if skew_label:
            title_parts.append(f"clock skew {skew_label}")
        title = " — ".join(title_parts) + f" ({title_count})"
        table = Table(
            title=title,
            title_style="bold",
        )
        table.add_column("#", justify="right")
        table.add_column("Started (UTC)")
        table.add_column("Completed (UTC)")
        table.add_column("Size", justify="right")
        table.add_column("Status")
        table.add_column("Duration", justify="right")
        for i, op in enumerate(ops, start=1):
            table.add_row(
                str(i),
                _fmt_started(op),
                _fmt_time(op.get("_time")),
                _fmt_size(op.get("file_size")),
                _fmt_status(op.get("status")),
                _fmt_duration(op.get("total_ms")),
            )
        console.print(table)

    _print_transport_totals(op_type, by_host, role_by_host, console)


def _print_transport_totals(
    op_type: str, by_host: dict, role_by_host: dict, console: Console
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
        role = role_by_host.get(host)
        if role is None:
            continue
        tier = "wasm" if is_wasm(role) else "native"
        slot = tiers.setdefault(tier, [0, 0])
        for op in ops:
            slot[1] += 1
            if op.get("status") == 0:
                slot[0] += 1
    if len(tiers) < 2:
        return
    console.print(f"[bold]{op_type} success by transport[/bold]")
    for tier in ("native", "wasm"):
        if tier not in tiers:
            continue
        ok, total = tiers[tier]
        rate = f"{100.0 * ok / total:.2f}%" if total else "n/a"
        colour = "green" if tier == "native" else "cyan"
        console.print(f"  [{colour}]{tier:<7}[/{colour}] {ok}/{total} = {rate}")


def _build_payment_map(name: str) -> dict[str, str]:
    """Map hostname → payment_type label for the deployment's upload clients.

    Hostname matches the client VM's actual hostname (set by the digitalocean-
    client terraform module): ``{name}-ant-client-{role}-{region}-{client_index}``.
    Missing client state returns an empty map — the caller falls back to
    rendering "default" for every host rather than blocking the print.
    """
    try:
        state = load_client_state(name)
    except RuntimeError:
        return {}
    out: dict[str, str] = {}
    for c in state.get("clients", []):
        if c.get("role") != "upload":
            continue
        host = (
            f"{name}-ant-client-{c['role']}-{c['region']}-{c['client_index']}"
        )
        out[host] = c.get("payment_type") or "default"
    return out


def _build_role_map(name: str) -> dict[str, str]:
    """Map hostname → client role for every client in the deployment.

    Used to label which transport each host's operations came from. Derived
    from client state rather than by sniffing the hostname, so the canonical
    role names in `client_roles` stay the single source of truth. Missing
    client state returns an empty map — the caller omits the label rather than
    guessing a transport, which would be worse than saying nothing.
    """
    try:
        state = load_client_state(name)
    except RuntimeError:
        return {}
    out: dict[str, str] = {}
    for c in state.get("clients", []):
        role = c.get("role")
        if not role:
            continue
        host = f"{name}-ant-client-{role}-{c['region']}-{c['client_index']}"
        out[host] = role
    return out


def _build_bootstrap_cache_map(name: str) -> dict[str, bool]:
    """Map hostname → bootstrap_cache flag for every client in the deployment.

    Covers both roles (the cache mode applies to uploaders and downloaders
    alike). Hosts whose client-state entry predates the option are simply
    absent from the map — the caller omits the label rather than guessing.
    """
    try:
        state = load_client_state(name)
    except RuntimeError:
        return {}
    out: dict[str, bool] = {}
    for c in state.get("clients", []):
        if c.get("bootstrap_cache") is None:
            continue
        host = (
            f"{name}-ant-client-{c.get('role', 'upload')}-"
            f"{c['region']}-{c['client_index']}"
        )
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
    uploader_specs = (
        deploy_args.get("uploaders") if isinstance(deploy_args, dict) else None
    )
    if not uploader_specs:
        return {}

    try:
        state = load_client_state(name)
    except RuntimeError:
        return {}
    upload_clients = sorted(
        (
            c for c in state.get("clients", [])
            if c.get("role", "upload") == "upload"
        ),
        key=lambda c: c.get("client_index") or 0,
    )
    if len(upload_clients) != len(uploader_specs):
        return {}

    out: dict[str, str] = {}
    for spec, client in zip(uploader_specs, upload_clients):
        host = (
            f"{name}-ant-client-upload-{client['region']}-{client['client_index']}"
        )
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
        started_at = datetime.fromtimestamp(
            min(starts) / 1_000_000_000, tz=timezone.utc
        )
        elapsed_s = (now - started_at).total_seconds()
    else:
        started_at = None
        elapsed_s = None

    total = len(ops)
    successes = sum(1 for o in ops if o.get("status") == 0)
    failures = sum(1 for o in ops if o.get("status") == 1)

    success_durations = [
        o["total_ms"] for o in ops
        if o.get("status") == 0 and o.get("total_ms") is not None
    ]
    overall_avg_ms = (
        sum(success_durations) / len(success_durations)
        if success_durations else None
    )

    return {
        "started_at": started_at,
        "elapsed_s": elapsed_s,
        "total": total,
        "successes": successes,
        "failures": failures,
        "overall_avg_ms": overall_avg_ms,
    }


def _print_summary(op_type: str, by_host: dict, console: Console) -> None:
    label = op_type.capitalize() + "s"

    all_starts = [
        o["start_ns"]
        for ops in by_host.values()
        for o in ops
        if o.get("start_ns") is not None
    ]
    if all_starts:
        started_at = datetime.fromtimestamp(
            min(all_starts) / 1_000_000_000, tz=timezone.utc
        )
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
        table.add_row("Avg duration", _fmt_duration(summary["overall_avg_ms"]))
        console.print(table)


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
_PROD_STATUS_LABELS = {
    0: "[green]ok[/green]",
    1: "[red]partial[/red]",
    2: "[yellow]src-fail[/yellow]",
}

_PROD_STATUS_TEXT = {
    0: "ok",
    1: "partial",
    2: "src-fail",
}


def _run_prod_uploads(args, console: Console) -> None:
    creds = _resolve_influx(args, console)
    if creds is None:
        return
    influx_url, influx_token, influx_org, influx_bucket = creds

    console.print(
        f"[bold]Querying {influx_url} (bucket={influx_bucket})...[/bold]"
    )
    points = fetch_prod_upload_points(
        url=influx_url,
        token=influx_token,
        org=influx_org,
        bucket=influx_bucket,
    )
    if not points:
        console.print(
            f"[yellow]No prod_upload records yet for '{args.name}'.[/yellow]"
        )
        return

    by_host: dict[str, list[dict]] = {}
    for p in points:
        by_host.setdefault(p["host"], []).append(p)

    payment_by_host = _build_prod_payment_map(args.name)
    cache_by_host = _build_bootstrap_cache_map(args.name)

    if getattr(args, "details", False):
        _prod_print_details(by_host, payment_by_host, cache_by_host, console)
    else:
        _prod_print_table(by_host, payment_by_host, cache_by_host, console)


def _bootstrap_cache_segment(cache_by_host: dict[str, bool], host: str) -> str:
    cache = cache_by_host.get(host)
    if cache is None:
        return ""
    return f" — bootstrap-cache={'on' if cache else 'off'}"


def _prod_print_table(
    by_host: dict[str, list[dict]],
    payment_by_host: dict[str, str],
    cache_by_host: dict[str, bool],
    console: Console,
) -> None:
    for host in sorted(by_host.keys()):
        ops = sorted(by_host[host], key=lambda p: p.get("start_ns") or 0)
        payment = payment_by_host.get(host, "default")
        cache_seg = _bootstrap_cache_segment(cache_by_host, host)
        skew_label = _clock_skew_label(ops)
        skew_seg = f" — clock skew {skew_label}" if skew_label else ""
        table = Table(
            title=(
                f"prod_upload — {host} — payment={payment}{cache_seg}"
                f"{skew_seg} ({len(ops)} record(s))"
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
    console: Console,
) -> None:
    for host in sorted(by_host.keys()):
        ops = sorted(by_host[host], key=lambda p: p.get("start_ns") or 0)
        payment = payment_by_host.get(host, "default")
        cache_seg = _bootstrap_cache_segment(cache_by_host, host)
        skew_label = _clock_skew_label(ops)
        skew_seg = f" — clock skew {skew_label}" if skew_label else ""
        console.rule(
            f"[bold]{host} — payment={payment}{cache_seg}{skew_seg} — "
            f"{len(ops)} record(s)[/bold]"
        )
        for i, op in enumerate(ops, start=1):
            console.print(f"[bold]#{i}[/bold]")
            console.print(f"  Started (UTC):     {_fmt_started(op)}")
            console.print(f"  Finished (UTC):    {_fmt_time(op.get('_time'))}")
            console.print(f"  File:              {op.get('file_name') or '-'}")
            console.print(f"  Source URL:        {op.get('source_url') or '-'}")
            console.print(f"  Size (bytes):      {op.get('file_size') or 0}")
            console.print(
                f"  Status:            {_PROD_STATUS_TEXT.get(op.get('status'), '-')}"
            )
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
    gb = num_bytes / (1024 ** 3)
    mb = num_bytes / (1024 ** 2)
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
