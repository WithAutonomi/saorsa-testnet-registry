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

from saorsa_deploy.client_roles import is_pointer, is_upload, is_wasm
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
    "prod-continuous-uploader": "Prod Daily Write Test",
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
        "fields": [{"type": "mrkdwn", "text": f"*{label}:*\n{value}"} for label, value in fields],
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
        _section(f"*Node binary:* {_format_binary(deploy_args.get('node_binary'))}"),
        _section(f"*Client binary:* {_format_binary(deploy_args.get('ant_binary'))}"),
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

    full_ips = state.get("full_vm_ips") or []
    full_percent = deploy_args.get("full_percent")
    if full_ips and nodes_per_vm is not None:
        full_nodes = len(full_ips) * nodes_per_vm
        pct_str = f" ({full_percent}%)" if full_percent else ""
        full_value = f"{full_nodes} ({len(full_ips)} VMs{pct_str})"
    elif full_ips:
        full_value = f"_unknown_ ({len(full_ips)} full VMs)"
    else:
        full_value = "0"

    # Clock skew spans both tiers: node VMs (selected by --clock-skew-count,
    # persisted to state) and client VMs (a clock_skew duration on their spec).
    node_skew = len(state.get("clock_skew_vm_ips") or [])
    client_skew = sum(1 for c in ((client_state or {}).get("clients") or []) if c.get("clock_skew"))
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
                ("Storage-full nodes", full_value),
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
            lines.append(f"• *{provider}*: {vmc} VM(s) × {rc} region(s) — {regions_str}")
        blocks.append(_section("*Providers & regions:*\n" + "\n".join(lines)))

    node_log_level = deploy_args.get("node_log_level")
    if node_log_level:
        blocks.append(_section(f"*Node log level:* `{node_log_level}`"))

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
        debug_str = " — 🐞 debug build" if descriptor.get("debug") else ""
        return f"branch `{owner}/{branch}`{debug_str}"
    if kind == "latest":
        return "latest release"
    return "_unknown_"


def _bootstrap_cache_marker(value) -> str:
    """Inline per-client marker for the ant peer-cache mode.

    ``None`` (records predating the option) renders nothing; otherwise an
    explicit on/off so a mixed deployment is readable at a glance.
    """
    if value is None:
        return ""
    return " — bootstrap-cache: on" if value else " — bootstrap-cache: off"


def _forward_logs_marker(value) -> str:
    """Inline per-client marker for client→Elasticsearch log forwarding.

    Only rendered when on (the common case is off / unset, which would be
    noise on every line).
    """
    return " — 📤 forward-logs" if value else ""


def _rust_log_marker(value) -> str:
    """Inline per-client marker showing a custom RUST_LOG, if set."""
    return f" — RUST_LOG=`{value}`" if value else ""


def _memory_profiling_marker(value) -> str:
    """Inline per-client marker for heaptrack memory profiling, if enabled."""
    return " — 🧠 heaptrack" if value else ""


def _prod_uploaders_blocks(deploy_args: dict) -> list[dict]:
    uploaders = deploy_args.get("uploaders") or []
    native = [u for u in uploaders if (u.get("client_kind") or "native") != "wasm"]
    wasm = [u for u in uploaders if u.get("client_kind") == "wasm"]

    blocks: list[dict] = []
    # A WASM-only deployment installs no `ant` at all, so naming a client binary
    # would describe something that never ran.
    if native or not wasm:
        blocks.append(_section(f"*Client binary:* {_format_binary(deploy_args.get('ant_binary'))}"))
    if wasm:
        # The bundle commit is what identifies the browser-path code: the ref may
        # be a moving branch, so "built from web-support" does not say what ran.
        blocks.append(_section(f"*WASM bundle:* {_format_wasm_build(deploy_args)}"))
        blocks.append(_section(f"*Bootstraps:* {_format_wasm_bootstrap(deploy_args)}"))

    if not uploaders:
        blocks.append(_section("*Uploaders:* _(none recorded)_"))
        return blocks

    # Two headings rather than one mixed list, and the WASM one only when the
    # run has browser-path uploaders — a native-only post is byte-identical to
    # what it was before.
    for heading, group in (("Uploaders", native), ("WASM uploaders", wasm)):
        if not group:
            continue
        blocks.append(_section(f"*{heading}:*\n" + "\n".join(_prod_uploader_lines(group))))
    return blocks


def _prod_uploader_lines(uploaders: list[dict]) -> list[str]:
    lines: list[str] = []
    for u in uploaders:
        region = u.get("region", "?")
        vm_size = u.get("vm_size", "?")
        payment = u.get("payment_type") or "default"
        file_names = u.get("file_names") or []
        skew_str = " — ⏰ clock-skew" if u.get("clock_skew") else ""
        net_str = _network_suffix(u)
        # Omitted on the browser path: the peer cache selects between ant's cache
        # and an explicit --bootstrap, and a WASM client does neither. The option
        # is REFUSED there, so reporting a value would describe a knob that had no
        # effect on the run.
        cache_str = (
            ""
            if u.get("client_kind") == "wasm"
            else _bootstrap_cache_marker(u.get("bootstrap_cache"))
        )
        fwd_str = _forward_logs_marker(u.get("forward_logs"))
        rust_str = _rust_log_marker(u.get("rust_log"))
        prof_str = _memory_profiling_marker(u.get("memory_profiling"))
        lines.append(
            f"• {region} ({vm_size}) — payment: {payment}"
            f"{cache_str}{skew_str}{net_str}{fwd_str}{rust_str}{prof_str}"
        )
        if file_names:
            for name in file_names:
                lines.append(f"    • `{name}`")
    return lines


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


def _prod_continuous_uploader_blocks(deploy_args: dict) -> list:
    """Summarise the daily write test.

    Leads with the cadence and the pool request, since "what does it upload and
    how often" is the question, and names the verifier regions because a single
    region confirming its own upload would not be a meaningful check.
    """
    blocks = []
    pool = deploy_args.get("pool_request") or {}
    if pool:
        summary = ", ".join(f"{count} {name}" for name, count in pool.items())
        blocks.append(_section(f"*Uploads each cycle:* {summary}"))
    interval = deploy_args.get("sleep_interval_secs")
    if interval:
        cadence = "once a day" if interval == 86400 else f"every {interval}s"
        blocks.append(_section(f"*Cadence:* {cadence}"))
    verifiers = deploy_args.get("verifiers") or []
    if verifiers:
        blocks.append(
            _section(
                f"*Read-back verification:* {len(verifiers)} region(s) — "
                + ", ".join(f"`{v}`" for v in verifiers)
                + "\n_Each checks every address; they do not divide the work._"
            )
        )
    bounds = []
    if deploy_args.get("pool_min_size"):
        bounds.append(f"≥ {deploy_args['pool_min_size']}")
    if deploy_args.get("pool_max_size"):
        bounds.append(f"< {deploy_args['pool_max_size']}")
    if bounds:
        blocks.append(_section(f"*Size bounds:* {' and '.join(bounds)}"))
    if "bootstrap_cache" in deploy_args:
        blocks.append(
            _section("*Bootstrap cache:* " + ("on" if deploy_args["bootstrap_cache"] else "off"))
        )
    if deploy_args.get("rust_log"):
        blocks.append(_section(f"*RUST_LOG:* `{deploy_args['rust_log']}`"))
    return blocks


def _prod_local_uploads_blocks(deploy_args: dict) -> list[dict]:
    input_path = deploy_args.get("input_path", "_unknown_")
    file_count = deploy_args.get("file_count")
    payment = deploy_args.get("payment_type") or "default"
    file_count_str = str(file_count) if file_count is not None else "_unknown_"
    blocks: list[dict] = [
        _section(f"*Client binary:* {_format_binary(deploy_args.get('ant_binary'))}"),
        _section(f"*Input path:* {input_path}"),
        _section(f"*File count:* {file_count_str}"),
        _section(f"*Payment:* {payment}"),
    ]
    if "bootstrap_cache" in deploy_args:
        blocks.append(
            _section("*Bootstrap cache:* " + ("on" if deploy_args["bootstrap_cache"] else "off"))
        )
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
                    files = [{"name": rel, "size_bytes": p.stat().st_size} for p, rel in pairs]
            except OSError:
                files = []
    if files:
        lines = [f"• `{f.get('name', '?')}` ({_format_size(f.get('size_bytes'))})" for f in files]
        blocks.extend(_chunked_sections("*Files:*", lines))
    return blocks


def _prod_local_downloads_blocks(deploy_args: dict) -> list[dict]:
    addresses_path = deploy_args.get("addresses_path", "_unknown_")
    address_count = deploy_args.get("address_count")
    output_mode = deploy_args.get("output_mode") or "_unknown_"
    output_dir = deploy_args.get("output_dir", "_unknown_")
    count_str = str(address_count) if address_count is not None else "_unknown_"
    blocks: list[dict] = [
        _section(f"*Client binary:* {_format_binary(deploy_args.get('ant_binary'))}"),
        _section(f"*Addresses path:* {addresses_path}"),
        _section(f"*Address count:* {count_str}"),
        _section(f"*Output mode:* {output_mode} ({output_dir})"),
    ]
    if "bootstrap_cache" in deploy_args:
        blocks.append(
            _section("*Bootstrap cache:* " + ("on" if deploy_args["bootstrap_cache"] else "off"))
        )
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

    # Which transport measured this run. Stated whenever it is WASM, because
    # every number below means something different then: the browser path
    # measured ~4x slower on download, and it refuses files over 1 GB.
    client_kind = deploy_args.get("client_kind") or "native"
    if client_kind == "wasm":
        fields.append(("Transport", "wasm (browser path, WebRTC-Direct) 🌐"))

    if deploy_args.get("clock_skew"):
        fields.append(("Clock skew", "enabled ⏰"))
    network = deploy_args.get("network")
    if network:
        fields.append(("Network", _format_network_field(network)))

    # Omitted on the browser path: the peer cache selects between ant's cache and
    # an explicit --bootstrap, and a WASM client does neither. The option is
    # REFUSED there, so reporting a value for it would describe a knob that had
    # no effect on the run.
    if "bootstrap_cache" in deploy_args and client_kind != "wasm":
        fields.append(("Bootstrap cache", "on" if deploy_args["bootstrap_cache"] else "off"))

    if deploy_args.get("forward_logs"):
        fields.append(("Log forwarding", "Elasticsearch 📤"))
    if deploy_args.get("rust_log"):
        fields.append(("RUST_LOG", f"`{deploy_args['rust_log']}`"))
    if deploy_args.get("memory_profiling"):
        fields.append(("Memory profiling", "heaptrack 🧠"))

    if "sleep_interval_secs" in deploy_args:
        sleep_interval_secs = deploy_args.get("sleep_interval_secs")
        if sleep_interval_secs is None:
            sleep_label = "_unset_"
        elif sleep_interval_secs == 0:
            sleep_label = "0s (back-to-back)"
        else:
            sleep_label = f"{sleep_interval_secs}s"
        fields.append(("Sleep interval", sleep_label))

    if client_kind == "wasm":
        blocks: list[dict] = [
            _section(f"*WASM bundle:* {_format_wasm_build(deploy_args)}"),
            _section(f"*Bootstraps:* {_format_wasm_bootstrap(deploy_args)}"),
            _fields_section(fields),
        ]
    else:
        blocks = [
            _section(f"*Client binary:* {_format_binary(deploy_args.get('ant_binary'))}"),
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


def _format_wasm_build(deploy_args: dict) -> str:
    """The bundle's provenance, or a note that it was not recorded.

    The commit is what identifies the code: `--wasm-branch-name` may name a
    moving branch, so "built from web-support" does not say what ran.
    """
    build = deploy_args.get("wasm_build")
    if not build:
        return "_no build provenance recorded_ (bundle predates `build-info.json`)"
    ref = build.get("ref", "_unknown_")
    owner = build.get("repo_owner", "_unknown_")
    commit = (build.get("commit") or "")[:12] or "_unknown_"
    nd = build.get("node_datachannel_version")
    extra = f", node-datachannel {nd}" if nd else ""
    return f"`{owner}/{ref}` @ `{commit}`{extra}"


def _format_wasm_bootstrap(deploy_args: dict) -> str:
    """How many bootstraps answered the preflight probe.

    Load-bearing for reading the run: ant-core cools a failed endpoint down for
    30 minutes, so a partial answer is a viable but degraded deployment and the
    results have to be read in that light.
    """
    bootstrap = deploy_args.get("wasm_bootstrap")
    if not bootstrap:
        return "_not recorded_"
    total = bootstrap.get("total", 0)
    if bootstrap.get("probe_skipped"):
        return f"{total} address(es), probe skipped"
    answered = bootstrap.get("answered", 0)
    mark = "✅" if answered == total and total else "⚠️"
    return f"{answered}/{total} answered an ICE probe {mark}"


def _client_line(c: dict) -> str:
    """One rendered client entry."""
    role = c.get("role", "upload")
    region = c.get("region", "?")
    vm_size = c.get("vm_size", "?")
    skew_str = " — ⏰ clock-skew" if c.get("clock_skew") else ""
    net_str = _network_suffix(c)
    # The ant peer cache does not exist on the WASM path — a browser client
    # bootstraps from the deployment's webrtc-direct addresses — so rendering a
    # cache marker there would describe a setting that has no effect.
    cache_str = "" if is_wasm(role) else _bootstrap_cache_marker(c.get("bootstrap_cache"))
    fwd_str = _forward_logs_marker(c.get("forward_logs"))
    rust_str = _rust_log_marker(c.get("rust_log"))
    if is_pointer(role):
        # Tested BEFORE is_upload: a pointer client IS an upload-type role (it
        # holds a funded wallet), but it uploads no files, so the uploader line
        # would report a payment mode and a file size that mean nothing here.
        # What matters is the workload rates, since they are what the run costs.
        create = c.get("create_interval_secs")
        update = c.get("update_interval_secs")
        sweep = c.get("sweep_interval_secs")
        rates = (
            f", create/{create}s update/{update}s sweep/{sweep}s"
            if create and update and sweep
            else ""
        )
        chain = c.get("chain_fraction")
        chain_str = f", chain {chain:.0%}" if chain else ""
        # Rendered only when the handover loop is on: 0 is "no handovers", and a
        # `transfer/0s` would read as a rate rather than as its absence.
        transfer = c.get("transfer_interval_secs")
        transfer_str = f", transfer/{transfer}s" if transfer else ""
        return (
            f"• pointer — {region} ({vm_size}{rates}{transfer_str}{chain_str})"
            f"{cache_str}{skew_str}{net_str}{fwd_str}{rust_str}"
        )
    if is_upload(role):
        file_size = c.get("file_size_mb")
        # file_size_mb is 0 for prod uploaders (real files of arbitrary
        # size) — only render the size for testnet uploaders, where it
        # is the configured upload size.
        size_str = f", {file_size}MB files" if file_size else ""
        payment = c.get("payment_type") or "default"
        return (
            f"• upload — {region} ({vm_size}{size_str}) — "
            f"payment: {payment}{cache_str}{skew_str}{net_str}{fwd_str}{rust_str}"
        )
    return f"• download — {region} ({vm_size}){cache_str}{skew_str}{net_str}{fwd_str}{rust_str}"


def _clients_block(clients: list[dict]) -> dict:
    """Render the client tier, splitting the two transports.

    A WASM client drives ant-core compiled to WASM over WebRTC-Direct; a native
    one drives the `ant` binary over QUIC. Their results are not comparable
    like-for-like, so a post that ran both and listed them as one undifferentiated
    set would misreport the run. (Worse, before this split every WASM client fell
    through the `role == "upload"` test and was rendered as a downloader.)

    The headings appear ONLY when the deployment actually has WASM clients, so
    every existing testnet's post is unchanged.
    """
    pointers = [c for c in clients if is_pointer(c.get("role", "upload"))]
    wasm = [c for c in clients if is_wasm(c.get("role", "upload"))]
    native = [c for c in clients if c not in pointers and c not in wasm]

    if not (wasm or pointers):
        return _section("*Clients:*\n" + "\n".join(_client_line(c) for c in native))

    lines: list[str] = []
    if native:
        # Only headed when there is something to tell it apart from. A run with
        # native clients and nothing else renders exactly as it always did.
        lines.append("_Native (QUIC):_")
        lines.extend(_client_line(c) for c in native)
    if wasm:
        lines.append("_WASM (WebRTC-Direct):_")
        lines.extend(_client_line(c) for c in wasm)
    if pointers:
        # Its own heading because a pointer client measures something else
        # entirely: whether a paid mutable reference can be read back, not
        # whether a file transfers.
        lines.append("_Pointers (paid mutable references):_")
        lines.extend(_client_line(c) for c in pointers)
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
    elif kind_raw == "prod-continuous-uploader":
        blocks.extend(_prod_continuous_uploader_blocks(deploy_args))
    elif kind_raw == "prod-local-uploads":
        blocks.extend(_prod_local_uploads_blocks(deploy_args))
    elif kind_raw == "prod-local-downloads":
        blocks.extend(_prod_local_downloads_blocks(deploy_args))
    elif kind_raw == "prod-canary":
        if "bootstrap_cache" in deploy_args:
            blocks.append(
                _section(
                    "*Bootstrap cache:* " + ("on" if deploy_args["bootstrap_cache"] else "off")
                )
            )
        if deploy_args.get("forward_logs"):
            blocks.append(_section("*Log forwarding:* Elasticsearch 📤"))
        if deploy_args.get("rust_log"):
            blocks.append(_section(f"*RUST_LOG:* `{deploy_args['rust_log']}`"))

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
            "[bold red]Error:[/bold red] DATABASE_URL is not set or the database is unreachable."
        )
        sys.exit(1)

    deployment = repo.get_active_by_name(args.name) or repo.get_by_name(args.name)
    if deployment is None:
        console.print(
            f"[bold red]Error:[/bold red] No deployment named '{args.name}' found in the registry."
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
        channel = getattr(args, "channel", None) or os.environ.get("ANT_DEPLOY_SLACK_CHANNEL")
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
            f"[bold red]Slack rejected the post:[/bold red] {response.status_code} {response.text}"
        )
        sys.exit(1)

    console.print(
        f"[bold green]Posted '{args.name}' summary to Slack.[/bold green] "
        "(via incoming webhook — no thread reference recorded; set "
        "ANT_DEPLOY_SLACK_BOT_TOKEN to enable threaded result updates.)"
    )


def _network_rates(network) -> tuple[str, str | None, str | None]:
    """Resolve a client's ``network`` value to ``(name, rate_down, rate_up)``.

    Three shapes reach here: a resolved dict (from ``deploy_args``), a bare
    profile name, or an inline comma-delimited set (both from client state, which
    stores the value unresolved). Names and inline sets are resolved against the
    shipped profile file to recover the rates.

    Best-effort by design — a profile since renamed or deleted from the file must
    not stop a deployment summary being posted, so resolution failure degrades to
    the label alone rather than raising.
    """
    if isinstance(network, dict):
        return (
            network.get("name") or "inline",
            network.get("rate_down"),
            network.get("rate_up"),
        )

    label = str(network)
    try:
        from saorsa_deploy.provisioning.network_profile import resolve_network

        profile = resolve_network(label)
    except Exception:
        profile = None
    if profile is None:
        # An inline set's raw token is unreadable in a summary; the name at least
        # says "this VM was shaped".
        return ("inline" if "=" in label else label, None, None)
    return (profile.name, profile.rate_down, profile.rate_up)


def _network_suffix(client: dict) -> str:
    """Render a client's simulated link conditions for the per-VM listing.

    Includes the rates, not just the profile name: "home-adsl" means nothing to a
    reader who has not memorised the profile set, and on a post announcing a
    low-bandwidth run the rate is the most interesting fact. It matters most for
    an inline set, which is precisely the one nobody can look up.

    Empty string when unshaped, so an ordinary run's post is unchanged.
    """
    network = client.get("network")
    if not network:
        return ""
    name, down, up = _network_rates(network)
    if down and up:
        return f" — 🐌 {name} ({down}/{up})"
    return f" — 🐌 {name}"


def _format_network_field(network) -> str:
    """Render the spec-level network profile, including its rates when known.

    Spelt out more fully than the per-VM suffix — this is a standalone field with
    room for it, rather than one marker among several on a crowded line.
    """
    name, down, up = _network_rates(network)
    if down and up:
        return f"🐌 {name} ({down} down / {up} up)"
    return f"🐌 {name}"
