import sys

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_client_state


def cmd_stop_clients(args):
    """Execute the stop-clients command: stop uploader/downloader services."""
    console = Console()

    console.print(f"[bold]Loading client state for '{args.name}'...[/bold]")
    try:
        client_state = load_client_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    clients = client_state.get("clients", [])
    host_ips = [c["ip"] for c in clients if c.get("ip")]
    if not host_ips:
        console.print("[bold red]Error:[/bold red] No client IPs found in state.")
        sys.exit(1)

    console.print(f"[bold]Stopping services on {len(host_ips)} client VM(s)...[/bold]")

    clear_known_hosts(host_ips, console)

    hosts_data = [
        (ip, {"ssh_user": "root", "ssh_key": args.ssh_key_path}) for ip in host_ips
    ]
    inventory = Inventory((hosts_data, {}))
    state = State(inventory=inventory, config=Config())
    connect_all(state)

    try:
        add_op(
            state,
            server.shell,
            name="Stop client services",
            commands=[
                "systemctl stop ant-uploader ant-downloader",
                "systemctl disable ant-uploader ant-downloader",
            ],
        )
        run_ops(state)
    finally:
        disconnect_all(state)

    failed = state.failed_hosts
    total = len(host_ips)
    succeeded = total - len(failed)
    console.print()
    console.print(
        f"[bold green]Client services stopped on {succeeded}/{total} host(s).[/bold green]"
    )
    if failed:
        for host in failed:
            console.print(f"  [red]Failed: {host.name}[/red]")
        sys.exit(1)
