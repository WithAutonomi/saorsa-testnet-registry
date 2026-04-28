import os
import subprocess
import sys
import time

from rich.console import Console
from rich.panel import Panel

from saorsa_deploy.provisioning.monitoring import MonitoringProvisioner
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_monitoring_state, update_monitoring_state


def _wait_for_ssh(ip: str, ssh_key_path: str, console: Console, timeout: int = 120):
    """Wait until SSH is accepting connections on the host."""
    console.print(f"[dim]Waiting for SSH on {ip}...[/dim]")
    start = time.time()
    while time.time() - start < timeout:
        result = subprocess.run(
            [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=5",
                "-o", "BatchMode=yes",
                "-i", ssh_key_path,
                f"root@{ip}",
                "true",
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            console.print(f"[dim]SSH ready on {ip}.[/dim]")
            return
        time.sleep(5)
    console.print(
        f"[yellow]Warning: SSH not ready after {timeout}s, proceeding anyway...[/yellow]"
    )


def print_connection_summary(name: str, details: dict, console: Console):
    """Print a formatted connection summary for the monitoring stack."""
    influxdb_url = details["influxdb_url"]
    grafana_url = details["grafana_url"]
    org = details["influxdb_org"]
    bucket = details["influxdb_bucket"]
    token = details["influxdb_token"]

    grafana_password = details.get("grafana_password", "admin")

    lines = [
        f"  [bold]InfluxDB:[/bold]  {influxdb_url}",
        f"  [bold]Grafana:[/bold]   {grafana_url}  (admin / {grafana_password})",
        f"  [bold]Org:[/bold]       {org}",
        f"  [bold]Bucket:[/bold]    {bucket}",
        f"  [bold]API Token:[/bold] {token}",
        "",
        "  [dim]Telegraf command:[/dim]",
        f"  saorsa-deploy monitoring provision-telegraf --name {name}",
    ]

    console.print()
    console.print(
        Panel(
            "\n".join(lines),
            title=f"Monitoring Stack: {name}",
            border_style="green",
        )
    )


def cmd_provision_monitoring(args):
    """Provision InfluxDB + Grafana on the monitoring VM."""
    console = Console()

    console.print(f"[bold]Loading monitoring state for '{args.name}'...[/bold]")
    try:
        state = load_monitoring_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    vm_ip = state.get("vm_ip")
    if not vm_ip:
        console.print(
            "[bold red]Error:[/bold red] No VM IP found in monitoring state."
        )
        sys.exit(1)

    console.print(f"  Monitoring VM: {vm_ip}")
    console.print()

    grafana_password = os.environ.get("ANT_DEPLOY_GRAFANA_DEV_PASSWORD")
    if not grafana_password:
        console.print(
            "[bold red]Error:[/bold red] "
            "ANT_DEPLOY_GRAFANA_DEV_PASSWORD environment variable is required."
        )
        sys.exit(1)

    _wait_for_ssh(vm_ip, args.ssh_key_path, console)
    clear_known_hosts([vm_ip], console)

    try:
        provisioner = MonitoringProvisioner(
            host_ip=vm_ip,
            ssh_key_path=args.ssh_key_path,
            grafana_password=grafana_password,
            console=console,
        )
        connection_details = provisioner.execute()
    except Exception as e:
        console.print(
            f"[bold red]Monitoring provisioning failed:[/bold red] {e}"
        )
        sys.exit(1)

    # Update monitoring state with connection details
    try:
        update_monitoring_state(args.name, connection_details)
        console.print("[dim]Monitoring state updated in S3.[/dim]")
    except Exception as e:
        console.print(
            f"[yellow]Warning: Failed to update monitoring state: {e}[/yellow]"
        )

    console.print()
    console.print(
        "[bold green]Monitoring stack provisioned successfully.[/bold green]"
    )

    print_connection_summary(args.name, connection_details, console)
