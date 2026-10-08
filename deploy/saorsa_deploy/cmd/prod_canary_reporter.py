"""CLI entry points for ``prod canary provision-reporter`` /
``prod canary unprovision-reporter``.

Both look up the canary's paired monitoring stack (one stack per canary
deployment, name-equal-to-deployment-name per the canary deploy command),
SSH to its VM, and invoke
:class:`~saorsa_deploy.provisioning.prod_canary_reporter.ProdCanaryReporterProvisioner`
or :class:`~.ProdCanaryReporterRemover`.
"""

from __future__ import annotations

import os
import sys

from rich.console import Console

from saorsa_deploy.provisioning.prod_canary_reporter import (
    ProdCanaryReporterProvisioner,
    ProdCanaryReporterRemover,
)
from saorsa_deploy.ssh import clear_known_hosts, wait_for_ssh
from saorsa_deploy.state import load_monitoring_state


def _load_monitoring(name: str, console: Console) -> dict:
    try:
        return load_monitoring_state(name)
    except RuntimeError:
        console.print(
            f"[bold red]Error:[/bold red] No monitoring stack found for "
            f"'{name}'. The reporter runs on the canary's monitoring VM — "
            "provision the canary first."
        )
        sys.exit(1)


def _resolve_webhook(args, console: Console) -> str:
    candidate = getattr(args, "slack_webhook_url", None) or os.environ.get("SLACK_WEBHOOK_URL", "")
    candidate = (candidate or "").strip()
    if not candidate:
        console.print(
            "[bold red]Error:[/bold red] --slack-webhook-url is required "
            "(or set SLACK_WEBHOOK_URL in the environment)."
        )
        sys.exit(1)
    return candidate


def cmd_prod_canary_provision_reporter(args):
    console = Console()
    console.print(f"[bold]prod canary provision-reporter: {args.name}[/bold]")

    mon_state = _load_monitoring(args.name, console)
    host_ip = mon_state.get("vm_ip", "")
    influx_url = (mon_state.get("influxdb_url") or "").rstrip("/")
    influx_token = mon_state.get("influxdb_token", "") or ""
    influx_org = mon_state.get("influxdb_org", "ant") or "ant"
    influx_bucket = mon_state.get("influxdb_bucket", "testnet-metrics") or "testnet-metrics"

    if not host_ip:
        console.print("[bold red]Error:[/bold red] monitoring state has no vm_ip.")
        sys.exit(1)
    if not influx_url or not influx_token:
        console.print(
            "[bold red]Error:[/bold red] monitoring state has no Influx "
            "URL/token — run monitoring provision first."
        )
        sys.exit(1)

    # The reporter reads Influx via localhost on the monitoring VM. The
    # external influx_url in monitoring state points at the public IP; swap
    # it for localhost so we don't take a hairpin route. The bucket / org /
    # token are unchanged.
    reporter_influx_url = "http://localhost:8086"

    slack_webhook = _resolve_webhook(args, console)

    clear_known_hosts([host_ip], console)
    not_ready = wait_for_ssh([host_ip], args.ssh_key_path, console=console)
    if not_ready:
        console.print(f"[bold red]Error:[/bold red] SSH never came up on monitoring VM {host_ip}.")
        sys.exit(1)

    console.print(
        f"  Monitoring VM: {host_ip}\n"
        f"  Deployment:    {args.name}\n"
        f"  Influx (local): {reporter_influx_url}\n"
        f"  On calendar:   {args.on_calendar}\n"
        f"  Run now:       {args.run_now}"
    )

    provisioner = ProdCanaryReporterProvisioner(
        host_ip=host_ip,
        deployment_name=args.name,
        slack_webhook_url=slack_webhook,
        influx_url=reporter_influx_url,
        influx_token=influx_token,
        influx_org=influx_org,
        influx_bucket=influx_bucket,
        on_calendar=args.on_calendar,
        run_now=args.run_now,
        ssh_key_path=args.ssh_key_path,
        console=console,
    )
    try:
        provisioner.execute()
    except Exception as e:
        console.print(f"[bold red]Reporter provisioning failed:[/bold red] {e}")
        sys.exit(1)

    console.print(f"[bold green]Reporter for '{args.name}' is installed.[/bold green]")
    if args.run_now:
        console.print(
            "[dim]An immediate run was triggered — check the Slack channel "
            "for a confirmation message.[/dim]"
        )


def cmd_prod_canary_unprovision_reporter(args):
    console = Console()
    console.print(f"[bold]prod canary unprovision-reporter: {args.name}[/bold]")
    mon_state = _load_monitoring(args.name, console)
    host_ip = mon_state.get("vm_ip", "")
    if not host_ip:
        console.print("[bold red]Error:[/bold red] monitoring state has no vm_ip.")
        sys.exit(1)

    clear_known_hosts([host_ip], console)
    not_ready = wait_for_ssh([host_ip], args.ssh_key_path, console=console)
    if not_ready:
        console.print(f"[bold red]Error:[/bold red] SSH never came up on monitoring VM {host_ip}.")
        sys.exit(1)

    remover = ProdCanaryReporterRemover(
        host_ip=host_ip,
        purge_state=args.purge_state,
        ssh_key_path=args.ssh_key_path,
        console=console,
    )
    try:
        remover.execute()
    except Exception as e:
        console.print(f"[bold red]Reporter removal failed:[/bold red] {e}")
        sys.exit(1)
    console.print(f"[bold green]Reporter for '{args.name}' removed.[/bold green]")
