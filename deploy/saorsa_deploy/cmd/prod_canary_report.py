"""CLI entry point for ``prod canary report``.

Renders the canary digest **locally, to the terminal, without posting anything**.

The daily Slack digest is produced by a systemd timer on the canary's monitoring
VM (see ``provision-reporter``), which reads Influx over localhost and posts Block
Kit to an incoming webhook. That is the only place the report existed, so reading
it meant reading Slack. This command runs the same analysis from here instead.

Two rules it must not break:

* **Never post.** The timer already posts the daily message; posting again would
  duplicate it in the channel.
* **Never share the timer's state file.** The report describes what changed since
  the previous run, computed from a stored snapshot. Writing to the VM's snapshot
  would corrupt the deltas in the team's daily message. This keeps its own local
  snapshot, which also makes the deltas mean "since *you* last looked".
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from saorsa_deploy.state import load_monitoring_state

REPORTER_SCRIPT = (
    Path(__file__).resolve().parent.parent / "resources" / "scripts" / "prod_canary_reporter.py"
)


def _load_reporter():
    """Import the reporter script as a module.

    It lives under resources/ because it is deployed to a VM as a standalone
    file, so it is not importable by package path. Loading it here rather than
    copying its ~750 lines keeps one implementation of the analysis: whatever the
    team sees in Slack is exactly what this prints.
    """
    spec = importlib.util.spec_from_file_location("prod_canary_reporter", REPORTER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _local_state_path(name: str) -> Path:
    path = Path.home() / ".local" / "share" / "saorsa-deploy" / "canary-report"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{name}.json"


def _flatten(blocks: list[dict]) -> str:
    """Turn Block Kit into plain text.

    The reporter emits only header/section/context blocks, so this is a faithful
    rendering rather than a lossy summary. Slack's mrkdwn bold (*text*) is left
    as-is; it reads fine in a terminal.
    """
    lines = []
    for block in blocks:
        kind = block.get("type")
        if kind == "header":
            text = block.get("text", {}).get("text", "")
            lines.append("")
            lines.append(text)
            lines.append("=" * len(text))
        elif kind == "section":
            if "text" in block:
                lines.append(block["text"].get("text", ""))
            for field in block.get("fields", []) or []:
                lines.append("  " + field.get("text", ""))
            lines.append("")
        elif kind == "context":
            for element in block.get("elements", []) or []:
                lines.append(element.get("text", ""))
            lines.append("")
        elif kind == "divider":
            lines.append("-" * 60)
    return "\n".join(lines).strip()


def cmd_prod_canary_report(args):
    console = Console()
    reporter = _load_reporter()

    try:
        mon_state = load_monitoring_state(args.name)
    except RuntimeError:
        console.print(
            f"[bold red]Error:[/bold red] No monitoring stack found for "
            f"'{args.name}'. The canary's Influx lives on its monitoring VM."
        )
        sys.exit(1)

    # Unlike the on-VM reporter, which reads Influx over localhost, this runs
    # from a workstation and needs the externally reachable URL.
    influx_url = (mon_state.get("influxdb_url") or "").rstrip("/")
    influx_token = mon_state.get("influxdb_token") or ""
    influx_org = mon_state.get("influxdb_org", "ant") or "ant"
    influx_bucket = mon_state.get("influxdb_bucket", "testnet-metrics") or "testnet-metrics"
    if not influx_url or not influx_token:
        console.print(
            "[bold red]Error:[/bold red] monitoring state has no Influx "
            "URL/token — run monitoring provision first."
        )
        sys.exit(1)

    now = datetime.now(tz=timezone.utc)
    try:
        uploads = reporter.fetch_uploads(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
            deployment_name=args.name,
        )
        probes = reporter.fetch_probes(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
            deployment_name=args.name,
        )
    except urllib.error.URLError as e:
        console.print(f"[bold red]Error:[/bold red] Influx query failed: {e}")
        sys.exit(1)

    if not uploads:
        console.print(
            f"No `prod_canary_upload` points for '{args.name}'. The upload "
            "service may not have started, or no URLs have been processed."
        )
        return

    state_path = _local_state_path(args.name)
    prev_state = reporter.read_prev_state(state_path)
    state = reporter.derive_state(uploads, probes)
    blocks = reporter.build_blocks(
        deployment_name=args.name,
        now=now,
        state=state,
        probes=probes,
        prev_state=prev_state,
    )

    print(_flatten(blocks))

    if args.no_save_state:
        console.print(
            '\n[dim](state not saved — next run\'s "since last" comparison is unchanged)[/dim]'
        )
    else:
        reporter.write_state(state_path, state, now)
