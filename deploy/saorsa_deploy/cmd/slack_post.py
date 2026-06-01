"""`deployments slack-post` command.

Posts a formatted summary of a deployment to a Slack incoming webhook so
the team can see at a glance what infrastructure has been stood up. The
webhook URL is read from the ``ANT_DEPLOY_SLACK_WEBHOOK_URL`` env var; the command
errors out if it is not set.
"""

from __future__ import annotations

import os
import sys
from datetime import timezone
from pathlib import Path

import requests
from rich.console import Console

from saorsa_deploy.registry import get_repository
from saorsa_deploy.slack_api import SlackApiError, post_message
from saorsa_deploy.state import (
    load_client_state,
    load_deployment_state,
    load_elastic_state,
    load_monitoring_state,
)

_KIND_LABELS = {
    "testnet": "Testnet",
    "prod-uploaders": "Prod Uploaders",
    "prod-continuous-downloader": "Prod Continuous Downloader",
    "prod-downloaders": "Prod Downloaders",
    "prod-local-uploads": "Prod Uploads [Home]",
    "prod-local-downloads": "Prod Downloads [Home]",
}


def _kind_raw(deploy_args: dict) -> str:
    """Mirror ls.py: prod kinds are tagged explicitly; otherwise infer testnet
    from the presence of ``bootstrap_count``."""
    raw = deploy_args.get("kind")
    if raw:
        return raw
    if "bootstrap_count" in deploy_args:
        return "testnet"
    return ""


def _fmt_dt(dt) -> str:
    if dt is None:
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


# Slack Block Kit caps the mrkdwn text on a single section block at 3000
# characters. Above that the API rejects the whole post with
# ``invalid_blocks``. We pack chunks up to ~2800 chars to leave headroom
# for the header line plus any per-page suffix Slack might apply.
_SECTION_TEXT_LIMIT = 2800


def _chunked_sections(header: str, lines: list[str]) -> list[dict]:
    """Render ``lines`` under ``header`` as one or more section blocks.

    A single block is used when everything fits inside the per-section
    text cap. Otherwise the lines are split across multiple section
    blocks; from the second block onwards the header is suffixed with
    ``(i/N)`` so a reader can tell the list is paginated. Each line is
    kept whole — splits never happen mid-line.
    """
    if not lines:
        return [_section(header)]

    pages: list[list[str]] = [[]]
    page_len = len(header) + 1  # +1 for the newline after the header
    for line in lines:
        # +1 accounts for the joining newline before this line.
        added = len(line) + 1
        if pages[-1] and page_len + added > _SECTION_TEXT_LIMIT:
            pages.append([])
            page_len = len(header) + 1
        pages[-1].append(line)
        page_len += added

    if len(pages) == 1:
        return [_section(header + "\n" + "\n".join(pages[0]))]

    blocks: list[dict] = []
    total = len(pages)
    base_header = header.rstrip(":").rstrip("*")
    for i, page in enumerate(pages, start=1):
        paged_header = f"{base_header} ({i}/{total}):*"
        blocks.append(_section(paged_header + "\n" + "\n".join(page)))
    return blocks


def _fields_section(fields: list[tuple[str, str]]) -> dict:
    return {
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"*{label}:*\n{value}"}
            for label, value in fields
        ],
    }


def _regions_by_provider(state: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for entry in state.get("regions") or []:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        provider, region = entry
        out.setdefault(provider, []).append(region)
    return out


def _testnet_blocks(deploy_args: dict, state: dict, client_state: dict | None) -> list[dict]:
    blocks: list[dict] = [
        _section(
            f"*Node binary:* {_format_binary(deploy_args.get('node_binary'))}"
        ),
        _section(
            f"*Client binary:* {_format_binary(deploy_args.get('ant_binary'))}"
        ),
    ]

    vm_count = deploy_args.get("vm_count")
    nodes_per_vm = deploy_args.get("nodes_per_vm")
    if vm_count is not None and nodes_per_vm is not None:
        total_nodes = vm_count * nodes_per_vm
        node_value = f"{total_nodes} ({vm_count} VMs × {nodes_per_vm}/VM)"
    elif vm_count is not None:
        node_value = f"_unknown_ ({vm_count} VMs, nodes-per-VM not yet recorded)"
    else:
        node_value = "_unknown_"

    nat_ips = state.get("nat_vm_ips") or []
    nat_percent = deploy_args.get("nat_percent")
    if nat_ips and nodes_per_vm is not None:
        nat_nodes = len(nat_ips) * nodes_per_vm
        pct_str = f" ({nat_percent}%)" if nat_percent else ""
        nat_value = f"{nat_nodes} ({len(nat_ips)} VMs{pct_str})"
    elif nat_ips:
        nat_value = f"_unknown_ ({len(nat_ips)} NAT VMs)"
    else:
        nat_value = "0"

    # Clock skew spans both tiers: node VMs (selected by --clock-skew-count,
    # persisted to state) and client VMs (clock_skew=true on their spec).
    node_skew = len(state.get("clock_skew_vm_ips") or [])
    client_skew = sum(
        1 for c in ((client_state or {}).get("clients") or []) if c.get("clock_skew")
    )
    total_skew = node_skew + client_skew
    if total_skew and node_skew and client_skew:
        skew_value = f"{total_skew} ({node_skew} node, {client_skew} client)"
    elif client_skew:
        skew_value = f"{total_skew} ({client_skew} client)"
    elif node_skew:
        skew_value = f"{total_skew} ({node_skew} node)"
    else:
        skew_value = "0"

    blocks.append(
        _fields_section(
            [
                ("Total nodes", node_value),
                ("NAT-simulated nodes", nat_value),
                ("Clock-skewed VMs", skew_value),
            ]
        )
    )

    node_specs = deploy_args.get("node_specs") or []
    region_map = _regions_by_provider(state)
    if node_specs:
        lines = []
        for spec in node_specs:
            provider = spec.get("provider", "?")
            vmc = spec.get("vm_count", "?")
            rc = spec.get("region_count", "?")
            actual_regions = region_map.get(provider, [])
            regions_str = ", ".join(actual_regions) if actual_regions else "_pending_"
            lines.append(
                f"• *{provider}*: {vmc} VM(s) × {rc} region(s) — {regions_str}"
            )
        blocks.append(_section("*Providers & regions:*\n" + "\n".join(lines)))

    return blocks


def _format_binary(descriptor: dict | None) -> str:
    if not descriptor:
        return "_unknown_"
    kind = descriptor.get("kind")
    if kind == "release":
        version = descriptor.get("version", "?")
        return f"release v{version}"
    if kind == "branch":
        owner = descriptor.get("repo_owner", "?")
        branch = descriptor.get("branch", "?")
        return f"branch `{owner}/{branch}`"
    if kind == "latest":
        return "latest release"
    return "_unknown_"


def _prod_uploaders_blocks(deploy_args: dict) -> list[dict]:
    blocks: list[dict] = [
        _section(
            f"*Client binary:* {_format_binary(deploy_args.get('ant_binary'))}"
        ),
    ]
    uploaders = deploy_args.get("uploaders") or []
    if not uploaders:
        blocks.append(_section("*Uploaders:* _(none recorded)_"))
        return blocks
    lines = []
    for u in uploaders:
        region = u.get("region", "?")
        vm_size = u.get("vm_size", "?")
        payment = u.get("payment_type") or "default"
        file_names = u.get("file_names") or []
        skew_str = " — ⏰ clock-skew" if u.get("clock_skew") else ""
        lines.append(f"• {region} ({vm_size}) — payment: {payment}{skew_str}")
        if file_names:
            for name in file_names:
                lines.append(f"    • `{name}`")
    blocks.append(_section("*Uploaders:*\n" + "\n".join(lines)))
    return blocks


def _format_size(num_bytes: int | None) -> str:
    """Render a byte count as a short human-readable string (e.g. '4.2 MB')."""
    if num_bytes is None:
        return "_unknown_"
    val = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024:
            return f"{int(val)} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} TB"


def _prod_local_uploads_blocks(deploy_args: dict) -> list[dict]:
    input_path = deploy_args.get("input_path", "_unknown_")
    file_count = deploy_args.get("file_count")
    payment = deploy_args.get("payment_type") or "default"
    file_count_str = str(file_count) if file_count is not None else "_unknown_"
    blocks: list[dict] = [
        _section(
            f"*Client binary:* {_format_binary(deploy_args.get('ant_binary'))}"
        ),
        _section(f"*Input path:* {input_path}"),
        _section(f"*File count:* {file_count_str}"),
        _section(f"*Payment:* {payment}"),
    ]
    files = deploy_args.get("files") or []
    if not files and deploy_args.get("input_mode") == "local-dir":
        # The deployment was recorded by an older version of the command
        # that didn't snapshot the file list, or the field was lost.
        # Fall back to scanning the local directory now — for a local-dir
        # run the source files should still be on this machine.
        raw_path = deploy_args.get("input_path")
        if raw_path:
            try:
                directory = Path(raw_path)
                if directory.is_dir():
                    pairs = sorted(
                        (
                            (p, p.relative_to(directory).as_posix())
                            for p in directory.rglob("*")
                            if p.is_file()
                        ),
                        key=lambda item: item[1],
                    )
                    files = [
                        {"name": rel, "size_bytes": p.stat().st_size}
                        for p, rel in pairs
                    ]
            except OSError:
                files = []
    if files:
        lines = [
            f"• `{f.get('name', '?')}` ({_format_size(f.get('size_bytes'))})"
            for f in files
        ]
        blocks.extend(_chunked_sections("*Files:*", lines))
    return blocks


def _prod_local_downloads_blocks(deploy_args: dict) -> list[dict]:
    addresses_path = deploy_args.get("addresses_path", "_unknown_")
    address_count = deploy_args.get("address_count")
    output_mode = deploy_args.get("output_mode") or "_unknown_"
    output_dir = deploy_args.get("output_dir", "_unknown_")
    count_str = (
        str(address_count) if address_count is not None else "_unknown_"
    )
    blocks: list[dict] = [
        _section(
            f"*Client binary:* {_format_binary(deploy_args.get('ant_binary'))}"
        ),
        _section(f"*Addresses path:* {addresses_path}"),
        _section(f"*Address count:* {count_str}"),
        _section(f"*Output mode:* {output_mode} ({output_dir})"),
    ]
    addresses = deploy_args.get("addresses") or []
    if addresses:
        lines = [f"• `{addr}`" for addr in addresses]
        header = f"*Addresses ({len(addresses)}):*"
        blocks.extend(_chunked_sections(header, lines))
    return blocks


def _prod_downloader_blocks(deploy_args: dict) -> list[dict]:
    """Render the prod-downloaders / prod-continuous-downloader detail blocks.

    Shared by both kinds. The Sleep-interval field is omitted when the kind
    has no sleep concept (i.e. one-shot ``prod-downloaders``). Addresses are
    listed inline from ``deploy_args["addresses"]`` when present; older rows
    that only stored ``addresses_file`` fall back to printing the path.
    """
    spec = deploy_args.get("spec", "_unknown_")
    fields: list[tuple[str, str]] = [("Downloader spec", spec)]

    if deploy_args.get("clock_skew"):
        fields.append(("Clock skew", "enabled ⏰"))

    if "sleep_interval_secs" in deploy_args:
        sleep_interval_secs = deploy_args.get("sleep_interval_secs")
        if sleep_interval_secs is None:
            sleep_label = "_unset_"
        elif sleep_interval_secs == 0:
            sleep_label = "0s (back-to-back)"
        else:
            sleep_label = f"{sleep_interval_secs}s"
        fields.append(("Sleep interval", sleep_label))

    blocks: list[dict] = [
        _section(
            f"*Client binary:* {_format_binary(deploy_args.get('ant_binary'))}"
        ),
        _fields_section(fields),
    ]

    addresses = deploy_args.get("addresses") or []
    if addresses:
        lines = [f"• `{addr}`" for addr in addresses]
        header = f"*Addresses ({len(addresses)}):*"
        blocks.extend(_chunked_sections(header, lines))
    else:
        addresses_file = deploy_args.get("addresses_file", "_unknown_")
        blocks.append(_section(f"*Addresses file:* {addresses_file}"))

    return blocks


def _clients_block(clients: list[dict]) -> dict:
    lines = []
    for c in clients:
        role = c.get("role", "upload")
        region = c.get("region", "?")
        vm_size = c.get("vm_size", "?")
        skew_str = " — ⏰ clock-skew" if c.get("clock_skew") else ""
        if role == "upload":
            file_size = c.get("file_size_mb")
            # file_size_mb is 0 for prod uploaders (real files of arbitrary
            # size) — only render the size for testnet uploaders, where it
            # is the configured upload size.
            size_str = f", {file_size}MB files" if file_size else ""
            payment = c.get("payment_type") or "default"
            lines.append(
                f"• upload — {region} ({vm_size}{size_str}) — payment: {payment}{skew_str}"
            )
        else:
            lines.append(f"• download — {region} ({vm_size}){skew_str}")
    return _section("*Clients:*\n" + "\n".join(lines))


def _monitoring_blocks(state: dict) -> list[dict]:
    influxdb_url = state.get("influxdb_url", "")
    if not influxdb_url:
        return []
    grafana_url = state.get("grafana_url", "")
    grafana_password = state.get("grafana_password", "")
    grafana_line = grafana_url
    if grafana_url and grafana_password:
        grafana_line = f"{grafana_url} (admin / {grafana_password})"
    lines = [
        "*Monitoring (InfluxDB + Grafana):*",
        f"• *InfluxDB:* {influxdb_url}",
        f"• *Grafana:* {grafana_line or '_unknown_'}",
        f"• *Org:* {state.get('influxdb_org', '') or '_unknown_'}",
        f"• *Bucket:* {state.get('influxdb_bucket', '') or '_unknown_'}",
        f"• *API token:* `{state.get('influxdb_token', '') or '_unknown_'}`",
    ]
    return [_section("\n".join(lines))]


def _elastic_blocks(state: dict) -> list[dict]:
    es_url = state.get("elasticsearch_url", "")
    if not es_url:
        return []
    lines = [
        "*Elasticsearch:*",
        f"• *URL:* {es_url}",
        f"• *Username:* {state.get('elasticsearch_username', '') or '_unknown_'}",
        f"• *Password:* `{state.get('elasticsearch_password', '') or '_unknown_'}`",
        f"• *API key:* `{state.get('elasticsearch_api_key', '') or '_unknown_'}`",
        f"• *Index pattern:* `{state.get('elasticsearch_index_pattern', '') or '_unknown_'}`",
    ]
    return [_section("\n".join(lines))]


def build_slack_blocks(
    deployment,
    state: dict | None,
    client_state: dict | None,
    monitoring_state: dict | None = None,
    elastic_state: dict | None = None,
) -> dict:
    """Build a Slack Block-Kit payload for ``deployment``.

    ``state`` and ``client_state`` are the dicts returned by
    ``load_deployment_state`` / ``load_client_state``. ``monitoring_state``
    and ``elastic_state`` come from ``load_monitoring_state`` /
    ``load_elastic_state`` when the deployment has those paired stacks. Any
    may be None or empty if S3 state is unavailable; the renderer degrades
    gracefully.
    """
    deploy_args = deployment.deploy_args or {}
    state = state or {}
    kind_raw = _kind_raw(deploy_args)
    kind_label = _KIND_LABELS.get(kind_raw, kind_raw or "Unknown")

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{deployment.name} — {kind_label}",
            },
        },
        _section(f"*Deployed:* {_fmt_dt(deployment.created_at)}"),
    ]

    purpose = deploy_args.get("purpose")
    if purpose:
        blocks.append(_section(f"*Purpose:*\n{purpose}"))
    else:
        blocks.append(_section("*Purpose:*\n_(none recorded)_"))

    if kind_raw == "testnet":
        blocks.extend(_testnet_blocks(deploy_args, state, client_state))
    elif kind_raw == "prod-uploaders":
        blocks.extend(_prod_uploaders_blocks(deploy_args))
    elif kind_raw in ("prod-continuous-downloader", "prod-downloaders"):
        blocks.extend(_prod_downloader_blocks(deploy_args))
    elif kind_raw == "prod-local-uploads":
        blocks.extend(_prod_local_uploads_blocks(deploy_args))
    elif kind_raw == "prod-local-downloads":
        blocks.extend(_prod_local_downloads_blocks(deploy_args))

    if client_state:
        clients = client_state.get("clients") or []
        if clients:
            blocks.append(_clients_block(clients))

    if monitoring_state:
        blocks.extend(_monitoring_blocks(monitoring_state))
    if elastic_state:
        blocks.extend(_elastic_blocks(elastic_state))

    return {
        "text": f"Deployment {deployment.name} — {kind_label}",
        "blocks": blocks,
    }


def render_payload_text(payload: dict) -> str:
    """Flatten a Slack Block-Kit payload to plain text for previewing.

    Used by ``--dry-run``: the JSON payload is hard to read, but the team
    just wants to see what the post will *look like*. Headers become a
    rule-bracketed line; sections render as their mrkdwn body; field
    sections render as ``Label: value`` lines.
    """
    lines: list[str] = []
    for block in payload.get("blocks") or []:
        btype = block.get("type")
        if btype == "header":
            text = (block.get("text") or {}).get("text", "")
            lines.append("")
            lines.append(f"=== {text} ===")
        elif btype == "section":
            text_obj = block.get("text")
            if text_obj and text_obj.get("text"):
                lines.append(text_obj["text"])
            for field in block.get("fields") or []:
                lines.append(field.get("text", ""))
        elif btype == "divider":
            lines.append("---")
    return "\n".join(lines).lstrip("\n") + "\n"


def _try_load_state(name: str) -> dict | None:
    try:
        return load_deployment_state(name)
    except RuntimeError:
        return None


def _try_load_client_state(name: str) -> dict | None:
    try:
        result = load_client_state(name)
    except Exception:
        return None
    return result or None


def _try_load_monitoring_state(name: str) -> dict | None:
    try:
        return load_monitoring_state(name)
    except RuntimeError:
        return None


def _try_load_elastic_state(name: str) -> dict | None:
    try:
        return load_elastic_state(name)
    except RuntimeError:
        return None


def cmd_slack_post(args):
    """Post a deployment summary to a Slack incoming webhook.

    Reads ``ANT_DEPLOY_SLACK_WEBHOOK_URL`` from the environment. With ``--dry-run``,
    prints a human-readable preview of the post instead of POSTing.
    """
    console = Console()

    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set or the database "
            "is unreachable."
        )
        sys.exit(1)

    deployment = repo.get_active_by_name(args.name) or repo.get_by_name(args.name)
    if deployment is None:
        console.print(
            f"[bold red]Error:[/bold red] No deployment named '{args.name}' "
            "found in the registry."
        )
        sys.exit(1)

    state = _try_load_state(args.name)
    client_state = _try_load_client_state(args.name)
    monitoring_state = _try_load_monitoring_state(args.name)
    elastic_state = _try_load_elastic_state(args.name)

    payload = build_slack_blocks(
        deployment,
        state,
        client_state,
        monitoring_state=monitoring_state,
        elastic_state=elastic_state,
    )

    if getattr(args, "dry_run", False):
        # Plain stdout so Rich doesn't reflow or interpret the mrkdwn markers.
        print(render_payload_text(payload), end="")
        return

    # Two posting paths. When a bot token is configured we post via the Web API
    # (chat.postMessage), which returns the message's (channel, ts) — we persist
    # that so `record-results --update-slack` can thread a results summary onto
    # this post. Otherwise we fall back to the legacy incoming webhook, which
    # returns no message reference (threading is then unavailable).
    bot_token = os.environ.get("ANT_DEPLOY_SLACK_BOT_TOKEN")
    if bot_token:
        channel = getattr(args, "channel", None) or os.environ.get(
            "ANT_DEPLOY_SLACK_CHANNEL"
        )
        if not channel:
            console.print(
                "[bold red]Error:[/bold red] ANT_DEPLOY_SLACK_BOT_TOKEN is set "
                "but no channel was provided. Pass --channel or set "
                "ANT_DEPLOY_SLACK_CHANNEL (a channel ID or name) so the Web API "
                "knows where to post."
            )
            sys.exit(1)
        try:
            ref = post_message(
                bot_token,
                channel,
                text=payload["text"],
                blocks=payload["blocks"],
            )
        except SlackApiError as e:
            console.print(f"[bold red]Slack post failed:[/bold red] {e}")
            sys.exit(1)
        repo.set_slack_message(deployment.id, ref.channel, ref.ts)
        console.print(
            f"[bold green]Posted '{args.name}' summary to Slack[/bold green] "
            f"(channel={ref.channel}, ts={ref.ts}); recorded for threaded "
            "result updates."
        )
        return

    webhook_url = os.environ.get("ANT_DEPLOY_SLACK_WEBHOOK_URL")
    if not webhook_url:
        console.print(
            "[bold red]Error:[/bold red] Neither ANT_DEPLOY_SLACK_BOT_TOKEN nor "
            "ANT_DEPLOY_SLACK_WEBHOOK_URL is set. Export one before running this "
            "command, or pass --dry-run to preview the payload without posting."
        )
        sys.exit(1)

    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
    except requests.RequestException as e:
        console.print(f"[bold red]Slack POST failed:[/bold red] {e}")
        sys.exit(1)

    if not response.ok:
        console.print(
            f"[bold red]Slack rejected the post:[/bold red] "
            f"{response.status_code} {response.text}"
        )
        sys.exit(1)

    console.print(
        f"[bold green]Posted '{args.name}' summary to Slack.[/bold green] "
        "(via incoming webhook — no thread reference recorded; set "
        "ANT_DEPLOY_SLACK_BOT_TOKEN to enable threaded result updates.)"
    )
