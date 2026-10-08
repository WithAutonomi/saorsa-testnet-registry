"""Per-user write-only ingest credentials for a public Elasticsearch instance.

Handing every community participant the same key makes a leak unrecoverable
without re-keying everyone at once, and makes it impossible to say which machine
produced which logs. These commands mint one Elasticsearch API key per
participant instead, so a key can be revoked on its own and every document can
be attributed.

The privileges are deliberately narrow: ``create_doc`` and ``auto_configure`` on
the beta index pattern. There is no ``read`` — a leaked key can add logs and do
nothing else. It cannot pull another participant's logs, cannot overwrite an
existing document, and cannot touch any other index on the instance.

Keys are created against the instance over its own HTTPS endpoint using the
admin credentials from S3 state; nothing here needs SSH.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from saorsa_deploy.provisioning.elastic import BETA_INDEX_PATTERN
from saorsa_deploy.provisioning.es_log_filter import (
    LOG_FILTER_PIPELINE,
    build_pipeline_body,
    levels_to_drop,
)
from saorsa_deploy.state import load_elastic_state

#: Prefix on every key name, so keys minted here are distinguishable from any
#: other key on the instance and can be listed without guessing.
KEY_NAME_PREFIX = "beta-ingest"

#: No expiry by default. An expiring credential sounds prudent, but a beta
#: participant runs their node indefinitely, so a fixed lifetime means chasing
#: every participant to re-key on a rolling schedule — and a forwarder whose key
#: lapses fails silently from our side, since nobody is watching their logs stop.
#: Revocation is the control that matters here, and it is immediate and
#: per-participant. Pass --expiration explicitly to time-box a key.
DEFAULT_EXPIRATION = None


class EsAdminError(RuntimeError):
    """An administrative request to Elasticsearch failed."""


def _es_request(
    state: dict,
    method: str,
    path: str,
    data: dict | None = None,
    ssh_key_path: str = "~/.ssh/id_rsa",
) -> dict:
    """Run an administrative request against Elasticsearch, over SSH.

    Deliberately NOT through ``elasticsearch_url``. On a public-ingest instance
    that URL is the TLS proxy, and its allowlist exposes only the bulk/document
    write endpoints — ``/_security/*`` and ``/_ingest/*`` are 404, which is the
    entire point: the administrative surface is not reachable from the internet.

    Nor as the stored ``elasticsearch_username``: that is the log-writer, which
    holds index privileges only. Managing API keys and ingest pipelines are
    *cluster* privileges, so these run as the ``elastic`` superuser. Its password
    is read on the box from the credentials the provisioner wrote, so the secret
    never appears in an argument list here, in shell history, or in a log.
    """
    vm_ip = state.get("vm_ip")
    if not vm_ip:
        raise EsAdminError("No VM IP in Elasticsearch state.")

    remote = (
        "PW=$(cat /etc/elasticsearch/ant-credentials/password) && "
        f'curl -sS --fail-with-body -u "elastic:$PW" -X {method} '
        f"'http://localhost:9200{path}' -H 'Content-Type: application/json'"
    )
    payload = None
    if data is not None:
        remote += " --data-binary @-"
        payload = json.dumps(data)

    result = subprocess.run(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-i",
            str(Path(ssh_key_path).expanduser()),
            f"root@{vm_ip}",
            remote,
        ],
        input=payload,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise EsAdminError(
            (result.stdout or result.stderr or "").strip() or f"ssh/curl exited {result.returncode}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise EsAdminError(f"Unexpected response: {result.stdout[:300]}") from None


def _load(console: Console, name: str) -> dict:
    try:
        state = load_elastic_state(name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)
    if not state or not state.get("elasticsearch_url"):
        console.print(
            f"[bold red]Error:[/bold red] Elasticsearch '{name}' is not "
            "provisioned yet. Run `elastic provision` first."
        )
        sys.exit(1)
    if not state.get("public_ingest"):
        # Minting an ingest key for a testnet stack would be a category error:
        # that instance has no beta index, no TLS, and its whole point is that
        # only our own VMs reach it.
        console.print(
            f"[bold red]Error:[/bold red] '{name}' is not a public-ingest "
            "instance, so it has no community ingest credentials to mint. "
            "Deploy it with `public_ingest: true`."
        )
        sys.exit(1)
    return state


def _fail(console: Console, e: Exception) -> None:
    console.print(f"[bold red]Error:[/bold red] {e}")
    sys.exit(1)


def cmd_elastic_mint_key(args):
    """Mint a write-only ingest API key for one community user."""
    console = Console()
    state = _load(console, args.name)
    key_name = f"{KEY_NAME_PREFIX}-{args.user}"

    payload = {
        "name": key_name,
        # Inline role descriptors rather than a named role reference: an API key
        # carries its own privileges, so the key stays append-only even if the
        # named role on the instance is later widened.
        "role_descriptors": {
            "beta_ingest": {
                "indices": [
                    {
                        "names": [BETA_INDEX_PATTERN],
                        "privileges": ["create_doc", "auto_configure"],
                    }
                ]
            }
        },
        "metadata": {
            "beta_user": args.user,
            "issued_by": "saorsa-deploy",
            "issued_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    # Omitting `expiration` entirely is what makes a key never expire; there is
    # no way to clear it afterwards (the update API rejects a null), so this has
    # to be right at creation time.
    expiration = DEFAULT_EXPIRATION if args.expiration is None else args.expiration
    if expiration:
        payload["expiration"] = expiration

    try:
        resp = _es_request(
            state, "POST", "/_security/api_key", payload, ssh_key_path=args.ssh_key_path
        )
    except Exception as e:  # noqa: BLE001 - surfaced verbatim to the operator
        _fail(console, e)

    encoded = resp["encoded"]
    ingest_url = state["elasticsearch_url"].rstrip("/")
    console.print(
        Panel(
            "\n".join(
                [
                    f"  [bold]User:[/bold]       {args.user}",
                    f"  [bold]Key ID:[/bold]     {resp['id']}",
                    f"  [bold]Expires:[/bold]    {expiration or 'never'}",
                    f"  [bold]Endpoint:[/bold]   {ingest_url}",
                    f"  [bold]Index:[/bold]      {BETA_INDEX_PATTERN}",
                    "",
                    "  [bold]Credential (send to the user, it is not recoverable later):[/bold]",
                    f"  {encoded}",
                    "",
                    "  [dim]Used as:  Authorization: ApiKey <credential>[/dim]",
                    "  [dim]Write-only: it can add documents and nothing else — no reads,[/dim]",
                    "  [dim]no overwrites, no other index.[/dim]",
                    "",
                    "  [dim]Revoke with:[/dim]",
                    f"  saorsa-deploy elastic revoke-key --name {args.name} --user {args.user}",
                ]
            ),
            title=f"Ingest key: {args.user}",
            border_style="green",
        )
    )
    # Elasticsearch returns the encoded credential exactly once, so say so
    # rather than letting the operator discover it when they need it again.
    console.print(
        "[yellow]This credential is shown once and is not stored. If it is "
        "lost, revoke the key and mint a new one.[/yellow]"
    )


def cmd_elastic_list_keys(args):
    """List the ingest keys minted for this instance."""
    console = Console()
    state = _load(console, args.name)
    try:
        resp = _es_request(
            state, "GET", "/_security/api_key?active_only=true", ssh_key_path=args.ssh_key_path
        )
    except Exception as e:  # noqa: BLE001
        _fail(console, e)

    keys = [
        k
        for k in resp.get("api_keys", [])
        if k.get("name", "").startswith(KEY_NAME_PREFIX) and not k.get("invalidated")
    ]
    if not keys:
        console.print(f"No active ingest keys for '{args.name}'.")
        return

    table = Table(title=f"Ingest keys: {args.name}")
    table.add_column("User")
    table.add_column("Key ID")
    table.add_column("Created")
    table.add_column("Expires")
    for k in sorted(keys, key=lambda k: k.get("name", "")):
        meta = k.get("metadata") or {}
        table.add_row(
            meta.get("beta_user", k.get("name", "").removeprefix(f"{KEY_NAME_PREFIX}-")),
            k.get("id", ""),
            _fmt_ms(k.get("creation")),
            _fmt_ms(k.get("expiration")) if k.get("expiration") else "never",
        )
    console.print(table)


def _fmt_ms(value) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def cmd_elastic_revoke_key(args):
    """Invalidate a community user's ingest key."""
    console = Console()
    state = _load(console, args.name)
    try:
        resp = _es_request(
            state,
            "DELETE",
            "/_security/api_key",
            {"name": f"{KEY_NAME_PREFIX}-{args.user}"},
            ssh_key_path=args.ssh_key_path,
        )
    except Exception as e:  # noqa: BLE001
        _fail(console, e)

    invalidated = resp.get("invalidated_api_keys", [])
    if not invalidated:
        # Not an error worth failing on, but never report it as a revocation:
        # the operator would believe access was withdrawn when it was not.
        console.print(
            f"[yellow]No active key found for user '{args.user}' on "
            f"'{args.name}'. Nothing was revoked.[/yellow]"
        )
        return
    console.print(f"[bold green]Revoked {len(invalidated)} key(s) for '{args.user}'.[/bold green]")
    console.print(
        "[dim]Revocation is immediate; the user's forwarder will start getting "
        "401s on its next batch.[/dim]"
    )


def cmd_elastic_set_log_level(args):
    """Change the minimum log level this instance accepts, at runtime.

    Rewrites the ingest pipeline body only. The index template already names the
    pipeline as its ``final_pipeline``, so there is nothing to redeploy, nothing
    to restart and no index to recreate — the new policy applies to the next
    document written.

    Existing documents are untouched: this filters what is accepted from here
    on, and does not retroactively delete logs already indexed.
    """
    console = Console()
    state = _load(console, args.name)

    try:
        drop = levels_to_drop(args.min_level)
        body = build_pipeline_body(args.min_level)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    try:
        _es_request(
            state,
            "PUT",
            f"/_ingest/pipeline/{LOG_FILTER_PIPELINE}",
            body,
            ssh_key_path=args.ssh_key_path,
        )
    except Exception as e:  # noqa: BLE001
        _fail(console, e)

    kept = f"{args.min_level.upper()} and above"
    dropped = ", ".join(drop) if drop else "nothing"
    console.print(f"[bold green]Ingest level for '{args.name}' is now {kept}.[/bold green]")
    console.print(
        f"[dim]Dropping: {dropped}. Applies to the next document "
        f"written; already-indexed logs are unaffected.[/dim]"
    )
    if drop:
        # Community forwarders see a dropped document as accepted, so nobody
        # upstream will notice a tightening. Say so rather than let it be a
        # surprise when someone asks why their DEBUG lines never appear.
        console.print(
            "[dim]Participants are not notified: dropped documents return "
            "success to their forwarder, which is what stops it retrying.[/dim]"
        )
