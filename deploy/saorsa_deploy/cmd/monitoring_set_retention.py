"""Change the InfluxDB retention of a running monitoring stack.

``monitoring provision --retention-days`` applies at FIRST provision only -- it
is baked into the bucket-creation calls in ``influxdb_setup.py``, which exits
early on a stack that already has a token -- and nothing else edited a live
bucket. So there was no way at all to change retention on a deployment that is
already up, which is exactly the deployments it matters for: the four permanent
production transfer kinds run at 7 days, and that window was the whole reason
their download history looked lost (INFRA-484).

It reads each bucket back and prints what it NOW is, rather than reporting the
value it asked for. The maintenance run that uses this needs a step whose
success can be checked, and "the PATCH returned 200" is not that.

Applied to every bucket the stack creates, because they expire independently:
the node-tier bucket from `/api/v2/setup`, the client-tier bucket and the WASM
client bucket from their own creates. Leaving one behind would quietly keep
expiring half a deployment's measurements.
"""

from __future__ import annotations

import sys

import requests
from rich.console import Console
from rich.table import Table

from saorsa_deploy.influx_retention import (
    _headers,
)
from saorsa_deploy.influx_retention import (
    find_bucket as _find_bucket,
)
from saorsa_deploy.influx_retention import (
    retention_seconds as _retention_seconds,
)
from saorsa_deploy.provisioning.monitoring import (
    INFLUXDB_BUCKET,
    INFLUXDB_CLIENT_BUCKET,
    INFLUXDB_WASM_BUCKET,
)
from saorsa_deploy.state import load_monitoring_state

#: Every bucket a monitoring stack creates, in the order they are reported.
#: Named constants rather than "whatever the org happens to hold", so a bucket
#: somebody made by hand is left alone and a bucket this tool creates and then
#: forgets to update is a test failure rather than a silent expiry.
STACK_BUCKETS = (INFLUXDB_BUCKET, INFLUXDB_CLIENT_BUCKET, INFLUXDB_WASM_BUCKET)

_TIMEOUT = 30


def _set_retention(url: str, token: str, bucket_id: str, seconds: int) -> dict:
    rules = [] if seconds == 0 else [{"type": "expire", "everySeconds": seconds}]
    resp = requests.patch(
        f"{url.rstrip('/')}/api/v2/buckets/{bucket_id}",
        json={"retentionRules": rules},
        headers=_headers(token),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _fmt(seconds: int) -> str:
    if seconds == 0:
        return "infinite"
    days = seconds / 86400
    if days == int(days):
        return f"{int(days)}d ({seconds}s)"
    return f"{days:.2f}d ({seconds}s)"


def cmd_monitoring_set_retention(args) -> None:
    console = Console()

    if args.days < 0:
        console.print("[bold red]Error:[/bold red] --days cannot be negative (0 = infinite).")
        sys.exit(1)

    try:
        state = load_monitoring_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    # Resolved exactly as `monitoring info` does, so the two can never disagree
    # about which stack is being talked to.
    url = state.get("influxdb_url") or ""
    token = state.get("influxdb_token") or ""
    org = state.get("influxdb_org") or "ant"
    if not (url and token):
        console.print(
            "[bold red]Error:[/bold red] monitoring state for "
            f"'{args.name}' has no InfluxDB URL/token. Run `monitoring "
            "provision` first."
        )
        sys.exit(1)

    seconds = args.days * 86400
    console.print(
        f"[bold]Setting InfluxDB retention on '{args.name}' to {_fmt(seconds)}[/bold] ({url})"
    )

    table = Table(title=f"InfluxDB retention for '{args.name}'")
    table.add_column("Bucket")
    table.add_column("Before")
    table.add_column("Now")
    failures: list[str] = []
    for name in STACK_BUCKETS:
        try:
            bucket = _find_bucket(url, token, org, name)
        except requests.RequestException as e:
            failures.append(f"{name}: could not be read ({e})")
            table.add_row(name, "[red]unreadable[/red]", "[red]unknown[/red]")
            continue
        if bucket is None:
            # Not an error: the WASM bucket does not exist on a stack
            # provisioned before that tier did, and a stack that never had one
            # is not misconfigured.
            table.add_row(name, "[dim]absent[/dim]", "[dim]absent[/dim]")
            continue
        before = _retention_seconds(bucket)
        try:
            _set_retention(url, token, bucket["id"], seconds)
            # Read back rather than trusting the PATCH's own echo: this is the
            # step the maintenance run checks.
            after_bucket = _find_bucket(url, token, org, name)
        except requests.RequestException as e:
            # 401/403 here is the one failure worth naming precisely: the token
            # in monitoring state is the org-scoped Telegraf token, so a stack
            # whose token was reminted more narrowly cannot edit a bucket.
            hint = ""
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (401, 403):
                hint = (
                    " — the token in monitoring state is not authorised to "
                    "write buckets; the stack's admin token is on the "
                    "InfluxDB VM at /etc/influxdb2/admin-token"
                )
            failures.append(f"{name}: {e}{hint}")
            table.add_row(name, _fmt(before), "[red]unchanged[/red]")
            continue
        after = _retention_seconds(after_bucket) if after_bucket else None
        rendered = "[red]unknown[/red]" if after is None else _fmt(after)
        if after is not None and after != seconds:
            rendered = f"[red]{_fmt(after)}[/red]"
            failures.append(f"{name}: still reads {_fmt(after)} after the change")
        table.add_row(name, _fmt(before), rendered)

    console.print(table)

    if failures:
        console.print("[bold red]Retention was not fully applied:[/bold red]")
        for failure in failures:
            console.print(f"  [red]{failure}[/red]")
        sys.exit(1)

    if seconds == 0:
        console.print(
            "[green]Retention is now infinite.[/green] Nothing expires, so "
            "`deployments archive` no longer has a deadline — but it is still "
            "what puts the per-operation records in Postgres, which is the "
            "only copy that survives the monitoring stack."
        )
    else:
        console.print(
            f"[green]Retention is now {_fmt(seconds)}.[/green] Run "
            f"`deployments archive --name {args.name}` at least that often, or "
            f"the measurements outside the window are gone."
        )
