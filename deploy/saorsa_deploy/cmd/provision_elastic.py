import subprocess
import sys
import time

from rich.console import Console
from rich.panel import Panel

from saorsa_deploy.provisioning.elastic import ElasticProvisioner
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_elastic_state, update_elastic_state


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
    """Print a formatted connection summary for the Elasticsearch stack."""
    es_url = details["elasticsearch_url"]
    es_user = details["elasticsearch_username"]
    es_pass = details["elasticsearch_password"]
    es_api_key = details["elasticsearch_api_key"]
    es_index = details["elasticsearch_index_pattern"]

    lines = [
        f"  [bold]URL:[/bold]           {es_url}",
        f"  [bold]Username:[/bold]      {es_user}",
        f"  [bold]Password:[/bold]      {es_pass}",
        f"  [bold]API Key:[/bold]       {es_api_key}",
        f"  [bold]Index Pattern:[/bold] {es_index}",
        "",
        "  [dim]Telegraf command:[/dim]",
        f"  saorsa-deploy provision-telegraf-logs --name {name}",
        "",
        "  [dim]Query with API key:[/dim]",
        f'  curl -H "Authorization: ApiKey {es_api_key}" {es_url}/{es_index}/_search',
    ]

    console.print()
    console.print(
        Panel(
            "\n".join(lines),
            title=f"Elasticsearch: {name}",
            border_style="green",
        )
    )


def cmd_provision_elastic(args):
    """Provision Elasticsearch on the Elasticsearch VM."""
    console = Console()

    console.print(f"[bold]Loading Elasticsearch state for '{args.name}'...[/bold]")
    try:
        state = load_elastic_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    vm_ip = state.get("vm_ip")
    if not vm_ip:
        console.print(
            "[bold red]Error:[/bold red] No VM IP found in Elasticsearch state."
        )
        sys.exit(1)

    console.print(f"  Elasticsearch VM: {vm_ip}")
    console.print()

    _wait_for_ssh(vm_ip, args.ssh_key_path, console)
    clear_known_hosts([vm_ip], console)

    try:
        provisioner = ElasticProvisioner(
            host_ip=vm_ip,
            ssh_key_path=args.ssh_key_path,
            testnet_name=args.name,
            console=console,
        )
        connection_details = provisioner.execute()
    except Exception as e:
        console.print(
            f"[bold red]Elasticsearch provisioning failed:[/bold red] {e}"
        )
        sys.exit(1)

    # Update Elasticsearch state with connection details
    try:
        update_elastic_state(args.name, connection_details)
        console.print("[dim]Elasticsearch state updated in S3.[/dim]")
    except Exception as e:
        console.print(
            f"[yellow]Warning: Failed to update Elasticsearch state: {e}[/yellow]"
        )

    console.print()
    console.print(
        "[bold green]Elasticsearch provisioned successfully.[/bold green]"
    )

    print_connection_summary(args.name, connection_details, console)
