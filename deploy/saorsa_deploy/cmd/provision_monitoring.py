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
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "BatchMode=yes",
                "-i",
                ssh_key_path,
                f"root@{ip}",
                "true",
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            console.print(f"[dim]SSH ready on {ip}.[/dim]")
            return
        time.sleep(5)
    console.print(f"[yellow]Warning: SSH not ready after {timeout}s, proceeding anyway...[/yellow]")


def print_connection_summary(name: str, details: dict, console: Console):
    """Print a formatted connection summary for the monitoring stack."""
    influxdb_url = details["influxdb_url"]
    grafana_url = details.get("grafana_url", "")
    org = details["influxdb_org"]
    bucket = details["influxdb_bucket"]
    token = details["influxdb_token"]

    grafana_password = details.get("grafana_password", "")
    grafana_line = f"{grafana_url}  (admin / {grafana_password})" if grafana_url else "disabled"

    lines = [
        f"  [bold]InfluxDB:[/bold]  {influxdb_url}",
        f"  [bold]Grafana:[/bold]   {grafana_line}",
        f"  [bold]Org:[/bold]       {org}",
        f"  [bold]Bucket:[/bold]    {bucket}",
        f"  [bold]API Token:[/bold] {token}",
        "",
        "  [dim]Telegraf command:[/dim]",
        f"  saorsa-deploy deployments provision-telegraf --name {name}",
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
        console.print("[bold red]Error:[/bold red] No VM IP found in monitoring state.")
        sys.exit(1)

    # Grafana is opt-in. The `grafana_enabled` flag (absent on legacy single-VM
    # stacks → defaults True) tells the three cases apart:
    #   - disabled (False):           grafana_ip = None → InfluxDB only, no Grafana
    #   - has its own VM (grafana_ip): provision Grafana on that VM
    #   - legacy single-VM (no ip):   co-locate Grafana on the InfluxDB VM
    grafana_enabled = state.get("grafana_enabled", True)
    if not grafana_enabled:
        grafana_ip = None
    else:
        grafana_ip = state.get("grafana_ip") or vm_ip

    console.print(f"  InfluxDB VM: {vm_ip}")
    console.print(f"  Grafana VM:  {grafana_ip or 'disabled'}")
    console.print()

    # The Grafana admin password is only needed when Grafana is actually
    # provisioned; an InfluxDB-only stack doesn't require it.
    grafana_password = os.environ.get("ANT_DEPLOY_GRAFANA_DEV_PASSWORD", "")
    if grafana_ip and not grafana_password:
        console.print(
            "[bold red]Error:[/bold red] "
            "ANT_DEPLOY_GRAFANA_DEV_PASSWORD environment variable is required."
        )
        sys.exit(1)

    retention_days = getattr(args, "retention_days", 0) or 0

    # Contabo has no attachable block storage → InfluxDB data on the root disk.
    root_disk = state.get("provider") == "contabo"

    if grafana_ip and grafana_ip != vm_ip:
        ssh_targets = [vm_ip, grafana_ip]
    else:
        ssh_targets = [vm_ip]
    for ip in ssh_targets:
        _wait_for_ssh(ip, args.ssh_key_path, console)
    clear_known_hosts(ssh_targets, console)

    try:
        provisioner = MonitoringProvisioner(
            influxdb_ip=vm_ip,
            grafana_ip=grafana_ip,
            ssh_key_path=args.ssh_key_path,
            grafana_password=grafana_password,
            retention_days=retention_days,
            console=console,
            root_disk=root_disk,
        )
        connection_details = provisioner.execute()
    except Exception as e:
        console.print(f"[bold red]Monitoring provisioning failed:[/bold red] {e}")
        sys.exit(1)

    # Update monitoring state with connection details
    try:
        update_monitoring_state(args.name, connection_details)
        console.print("[dim]Monitoring state updated in S3.[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to update monitoring state: {e}[/yellow]")

    console.print()
    console.print("[bold green]Monitoring stack provisioned successfully.[/bold green]")

    print_connection_summary(args.name, connection_details, console)
