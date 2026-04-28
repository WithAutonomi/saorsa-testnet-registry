import sys

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_client_state


def cmd_start_clients(args):
    """Execute the clients start command: start uploader/downloader services."""
    console = Console()

    console.print(f"[bold]Loading client state for '{args.name}'...[/bold]")
    try:
        client_state = load_client_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    clients = client_state.get("clients", [])
    hosts_with_role = [
        (c["ip"], c.get("role", "upload"))
        for c in clients
        if c.get("ip")
    ]
    if not hosts_with_role:
        console.print("[bold red]Error:[/bold red] No client IPs found in state.")
        sys.exit(1)

    host_ips = [ip for ip, _ in hosts_with_role]
    console.print(f"[bold]Starting services on {len(host_ips)} client VM(s)...[/bold]")

    clear_known_hosts(host_ips, console)

    hosts_data = [
        (ip, {"ssh_user": "root", "ssh_key": args.ssh_key_path}) for ip in host_ips
    ]
    inventory = Inventory((hosts_data, {}))
    state = State(inventory=inventory, config=Config())
    connect_all(state)

    case_entries = "\n".join(
        f'    "{ip}") UNIT=ant-{role}er ;;' for ip, role in hosts_with_role
    )
    start_script = f"""\
MY_IP=$(hostname -I | awk '{{print $1}}')
case "$MY_IP" in
{case_entries}
    *) UNIT=ant-uploader ;;
esac
systemctl enable --now "$UNIT\""""

    try:
        add_op(
            state,
            server.shell,
            name="Start client services (role-aware)",
            commands=[start_script],
        )
        run_ops(state)
    finally:
        disconnect_all(state)

    failed = state.failed_hosts
    total = len(host_ips)
    succeeded = total - len(failed)
    console.print()
    console.print(
        f"[bold green]Client services started on {succeeded}/{total} host(s).[/bold green]"
    )
    if failed:
        for host in failed:
            console.print(f"  [red]Failed: {host.name}[/red]")
        sys.exit(1)
