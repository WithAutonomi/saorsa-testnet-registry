"""`deployments print-upgrade-state` — the arming fields for an in-place client upgrade.

Everything needed to decide and record an in-place upgrade of a running
production client deployment: which registry row it is, what binary and which
WASM bundle it is currently recorded as running, what the last upgrade attempts
did, and which VMs run which systemd unit. One read, no SSH, no infrastructure.

It exists because the release train's `upgrade-production-monitors` arming step
named **library APIs** — `SqlAlchemyDeploymentRepository.get_active_by_name` and
`saorsa_deploy.state.load_client_state` — rather than commands, there being no
command that returned these fields. Following that meant importing the modules,
and **a bare import reads the wrong database**: the env file is loaded by the
CLI (:mod:`saorsa_deploy.env_file`), so `get_session_factory()` reached for its
own default and connected to a *local* Postgres. Measured on 2026-10-23 while
arming the 2026-10-22 train, through `uv run` and through a pinned
`uvx --from saorsa-deploy==1.11.0` alike — pinning the tool does not fix it,
because the fallback is in the library path and not in the version. It surfaced
only because the local copy was old enough to raise on a missing column; with
matching schemas it would have returned plausible, wrong state and the arming
would have written it into four production maintenance issues.

**Nothing this command prints can be a credential.** That is the reason it is a
new command rather than a use of `deployments print`, which emits the
deployment's InfluxDB API token in its normal output (observed on `--id 673`)
and so cannot be in the path of an agent that pastes what it reads into a Linear
issue. The property is structural rather than a matter of care: every value
comes from a named field — :data:`DEPLOY_ARGS_FIELDS` out of `deploy_args`,
:data:`CLIENT_FIELDS` out of each S3 client-state entry, and no monitoring or
elastic state is loaded at all — so a secret added to any of those sources in
future is absent from this output by default rather than newly exposed.

The unit comes from :mod:`saorsa_deploy.prod_client_units`, the one authority
for the kind-to-unit mapping, so this command cannot disagree with the
`collect-client-logs` and `upgrade-ant-binary` that act on the hosts it names.
"""

from __future__ import annotations

import sys

from rich.console import Console
from rich.table import Table

from saorsa_deploy.analysis.json_out import console_for, emit_json
from saorsa_deploy.prod_client_units import (
    LOCAL_KINDS,
    PROD_CLIENT_UNITS,
    UnknownProdClientKind,
    unit_for_role,
)
from saorsa_deploy.registry import get_repository
from saorsa_deploy.state import load_client_state

#: The `deploy_args` keys that are reported, each named here rather than the
#: object being passed through whole. `kind` and `client_kind` decide which
#: upgrade command applies; `ant_binary` and `wasm_build` are what it would be
#: replacing. Whole objects, because `wasm_build` grows fields (`features`
#: arrived with the 0.3.12 bundles) and a caller transcribing provenance wants
#: all of them.
DEPLOY_ARGS_FIELDS = ("kind", "client_kind", "ant_binary", "wasm_build")

#: The client-state keys that are reported. An allowlist, not an exclusion
#: list. Client state is a free-form dict whose shape is whatever provisioned
#: the tier put there: `update_client_wallet` writes a `wallet_secret_key` onto
#: an uploader's entry on the testnet path, and the prod kinds in scope here
#: keep their shared mainnet key out of state only as a matter of how they are
#: implemented today. A passthrough minus known-bad keys would leak the next
#: thing added; a named set cannot.
#:
#: No VM name is among them, deliberately. Client state does not carry
#: hostnames and the convention cannot be derived safely — PROD-UL-01's OVH
#: verifier reports `prod-ul-01-ant-client-download-gra9-2` in lower case while
#: its two siblings are upper case — so a caller wanting names reads them over
#: SSH. `region` and `client_index` are what distinguish two VMs of one role
#: without guessing one.
CLIENT_FIELDS = ("ip", "role", "region", "client_index", "provider", "client_kind")

#: Event types reported, by prefix. Written by `upgrade-ant-binary`
#: (`ant_binary_upgrade_started` / `_failed` / `_completed`) and
#: `upgrade-wasm-client` (`wasm_client_upgraded` / `wasm_client_upgrade_failed`).
UPGRADE_EVENT_PREFIXES = ("ant_binary_upgrade", "wasm_client_upgrade")

#: Detail keys whose VALUE is replaced by :data:`REDACTED`. Defence in depth
#: only: no event type under the prefixes above records anything secret today
#: (the details are URLs, commits, refs and host lists). But `details` is the
#: one free-form JSON object here, so a future event carrying a token must not
#: be able to reach an issue comment through this command. Matched as a
#: case-insensitive substring of the key.
SECRET_KEY_MARKERS = ("secret", "token", "password", "passwd", "credential")

REDACTED = "<redacted>"


def error_console() -> Console:
    """Where refusals go: stderr, always, whatever `--json-out` is.

    `console_for("-")` is quiet so a table cannot land in the caller's `jq`
    input, and an error printed through it would be swallowed — leaving an
    unattended caller a bare exit code for a wrong name. stderr keeps stdout
    parseable and the reason readable at the same time.
    """
    return Console(stderr=True)


def _redact(value):
    """``value`` with every secret-looking key's value replaced, recursively."""
    if isinstance(value, dict):
        return {
            key: REDACTED
            if any(marker in str(key).lower() for marker in SECRET_KEY_MARKERS)
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _status(deployment) -> str | None:
    """The row's status as a plain string, whether enum-valued or not."""
    status = getattr(deployment, "status", None)
    if status is None:
        return None
    return getattr(status, "value", status)


def _deploy_args(deployment) -> dict:
    args = getattr(deployment, "deploy_args", None)
    return args if isinstance(args, dict) else {}


def newest_upgrade_events(events) -> list[dict]:
    """The newest event of each upgrade type, newest first.

    Per *type* rather than the newest N: a failure and a success are different
    facts about the deployment and a reader needs both timestamps to order
    them, while the second-newest of either type says nothing an upgrade
    decision turns on. Ordering is by `created_at` with the row id as the
    tie-break, so two events recorded in the same transaction do not swap
    between runs.
    """
    newest: dict[str, object] = {}
    for event in events or []:
        event_type = getattr(event, "event_type", None) or ""
        if not event_type.startswith(UPGRADE_EVENT_PREFIXES):
            continue
        current = newest.get(event_type)
        if current is None or _event_sort_key(event) >= _event_sort_key(current):
            newest[event_type] = event
    ordered = sorted(newest.values(), key=_event_sort_key, reverse=True)
    return [
        {
            "event_type": event.event_type,
            "created_at": _isoformat(getattr(event, "created_at", None)),
            "details": _redact(getattr(event, "details", None)),
        }
        for event in ordered
    ]


def _event_sort_key(event):
    created_at = getattr(event, "created_at", None)
    return (
        created_at.timestamp() if hasattr(created_at, "timestamp") else 0.0,
        getattr(event, "id", 0) or 0,
    )


def _isoformat(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def describe_clients(kind: str, clients: list[dict]) -> list[dict]:
    """One allowlisted entry per client VM, with its unit resolved.

    Entries with no IP are kept and reported with `ip: null`: they are a VM
    that was specified and never created, and on this read that absence is the
    answer rather than noise — a deployment reporting two of three VMs is the
    arming's problem, and dropping the third would hide it.
    """
    described = []
    for client in clients or []:
        unit = unit_for_role(kind, client.get("role"))
        entry = {field: client.get(field) for field in CLIENT_FIELDS}
        entry["unit"] = unit.service
        entry["label"] = unit.label
        # Which VM pays, from the unit map rather than from a rule spelled
        # here: on a WASM `prod-continuous-uploader` it is the upload unit that
        # runs the bundle while the verifiers stay native, and both the
        # upgrade commands derive that from the same flag.
        entry["is_upload"] = unit.is_upload
        described.append(entry)
    return described


def build_report(
    deployment, kind: str, clients: list[dict], client_state_error: str | None
) -> dict:
    """The JSON document, assembled from the named fields and nothing else."""
    deploy_args = _deploy_args(deployment)
    report = {
        "deployment": deployment.name,
        "id": deployment.id,
        "status": _status(deployment),
        "saorsa_deploy_version": getattr(deployment, "saorsa_deploy_version", None),
    }
    for field in DEPLOY_ARGS_FIELDS:
        report[field] = _redact(deploy_args.get(field))
    # The caller's resolved kind wins over the raw field: it is the value the
    # units below were looked up with, so the two cannot disagree in output.
    report["kind"] = kind
    report["upgrade_events"] = newest_upgrade_events(getattr(deployment, "events", None))
    report["clients"] = describe_clients(kind, clients)
    report["client_state_error"] = client_state_error
    return report


def _resolve_deployment(args, errors: Console):
    """The row `--name` or `--id` asks for, or exit 1 naming what was asked.

    `--name` resolves the LIVE row and falls back to the most recent of that
    name, exactly as `print-recorded-results` does: these deployments are torn
    down and stood up again under the same name, so a pinned id goes stale
    while reading as a well-formed answer.
    """
    repo = get_repository()
    if repo is None:
        errors.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set or the "
            "database is unreachable. The kind, the transport and the recorded "
            "build all come from the registry, so there is nothing to report "
            "without it."
        )
        sys.exit(1)

    name = getattr(args, "name", None)
    if name:
        deployment = repo.get_active_by_name(name) or repo.get_by_name(name)
        if deployment is None:
            errors.print(
                f"[bold red]Error:[/bold red] No deployment named '{name}' in the registry."
            )
            sys.exit(1)
        return deployment

    deployment = repo.get(args.deployment_id)
    if deployment is None:
        errors.print(
            f"[bold red]Error:[/bold red] No deployment with id="
            f"{args.deployment_id} in the registry."
        )
        sys.exit(1)
    return deployment


def _load_clients(name: str, errors: Console) -> tuple[list[dict], str | None]:
    """The deployment's client VMs from S3, or why there are none.

    A MISSING key is tolerated and reported — a destroyed deployment's rows and
    upgrade history are still worth reading, and `clients: []` beside the
    reason says so plainly. Anything else refuses: "could not look" is not
    "nothing there", and an empty client list taken from a transport error
    would read as a deployment with no VMs to upgrade.
    """
    try:
        return load_client_state(name).get("clients") or [], None
    except RuntimeError as exc:
        return [], str(exc)
    except Exception as exc:  # noqa: BLE001 — anything else is unreadable, not absent
        errors.print(
            f"[bold red]Error:[/bold red] could not read client state for "
            f"'{name}': {exc}\n\nNo VM list can be reported, and an empty one "
            "would read as a deployment with nothing to upgrade."
        )
        sys.exit(1)


def _print_report(report: dict, console: Console) -> None:
    console.rule(
        f"[bold]Upgrade state — {report['deployment']} "
        f"(id={report['id']}, kind={report['kind'] or 'unknown'})[/bold]"
    )

    summary = Table(show_header=False, box=None)
    summary.add_column(style="bold")
    summary.add_column(overflow="fold")
    summary.add_row("status", str(report["status"]))
    summary.add_row("client_kind", str(report["client_kind"] or "native"))
    summary.add_row("ant_binary", _render_object(report["ant_binary"]))
    summary.add_row("wasm_build", _render_object(report["wasm_build"]))
    console.print(summary)

    console.print()
    if report["upgrade_events"]:
        console.print("[bold]Newest upgrade event per type[/bold]")
        # A list rather than a table: a commit, a release URL and an ISO
        # timestamp in three columns is elided to "…" at any terminal width,
        # and these are the values a reader transcribes.
        for event in report["upgrade_events"]:
            console.print(f"  [bold]{event['event_type']}[/bold]  {event['created_at']}")
            details = event["details"]
            if isinstance(details, dict):
                for key, value in details.items():
                    console.print(f"    {key}: {value}")
            elif details is not None:
                console.print(f"    {details}")
    else:
        console.print("[dim]No ant-binary or WASM-client upgrade events recorded.[/dim]")

    console.print()
    if report["clients"]:
        clients = Table(title="Client VMs")
        clients.add_column("ip")
        clients.add_column("role")
        # Folded, never truncated: the unit is what an operator pastes into a
        # `systemctl` command, and `ant-prod-continuous-…` is not a unit name.
        clients.add_column("unit", overflow="fold")
        clients.add_column("region")
        clients.add_column("idx")
        for client in report["clients"]:
            clients.add_row(
                str(client["ip"] or "-"),
                str(client["role"] or "-"),
                client["unit"],
                str(client["region"] or "-"),
                str(client["client_index"] if client["client_index"] is not None else "-"),
            )
        console.print(clients)
    else:
        console.print("[yellow]No client VMs in S3 state for this deployment.[/yellow]")
    if report["client_state_error"]:
        console.print(f"[yellow]{report['client_state_error']}[/yellow]")


def _render_object(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, dict):
        return ", ".join(f"{key}={item}" for key, item in value.items()) or "-"
    return str(value)


def cmd_print_upgrade_state(args) -> None:
    json_out = getattr(args, "json_out", None)
    console = console_for(json_out)
    errors = error_console()

    deployment = _resolve_deployment(args, errors)
    kind = _deploy_args(deployment).get("kind") or ""

    if kind in LOCAL_KINDS:
        errors.print(
            f"[bold red]Error:[/bold red] '{deployment.name}' is a '{kind}' "
            "deployment — it runs on this machine, so there is no VM, no unit "
            "and nothing to upgrade in place."
        )
        sys.exit(1)
    if kind not in PROD_CLIENT_UNITS:
        errors.print(
            f"[bold red]Error:[/bold red] no client units are known for kind "
            f"'{kind or 'unknown'}', so the unit each VM runs cannot be named. "
            f"Supported: {', '.join(sorted(PROD_CLIENT_UNITS))}."
        )
        sys.exit(1)

    clients, client_state_error = _load_clients(deployment.name, errors)
    try:
        report = build_report(deployment, kind, clients, client_state_error)
    except UnknownProdClientKind as exc:
        errors.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    _print_report(report, console)
    emit_json(report, json_out, console)
