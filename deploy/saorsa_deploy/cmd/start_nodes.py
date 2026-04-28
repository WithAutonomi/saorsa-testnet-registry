"""Start ant-node services on node VMs.

Separated from nodes provision so that port-restricted NAT rules (configured
during provisioning) are in place before the services come up. Runs a simple
parallel ``systemctl start`` across the target VMs — no staggering.
"""

import sys

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_deployment_state


def cmd_start_nodes(args):
    """Execute the nodes start command."""
    console = Console()

    console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
    try:
        state = load_deployment_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    vm_ips = state.get("vm_ips") or {}
    if not vm_ips:
        console.print(
            "[bold red]Error:[/bold red] No VM IPs found in deployment state. "
            "Run `saorsa-deploy nodes infra` and `nodes provision` first."
        )
        sys.exit(1)

    node_count = state.get("node_count")
    if not node_count:
        console.print(
            "[bold red]Error:[/bold red] No node_count found in deployment state. "
            "Run `saorsa-deploy nodes provision` first."
        )
        sys.exit(1)

    if args.region:
        if args.region not in vm_ips:
            available = ", ".join(sorted(vm_ips.keys()))
            console.print(
                f"[bold red]Error:[/bold red] Region '{args.region}' not found. "
                f"Available regions: {available}"
            )
            sys.exit(1)
        host_ips = list(vm_ips[args.region])
        console.print(
            f"[bold]Starting node services on {len(host_ips)} VM(s) in {args.region}...[/bold]"
        )
    else:
        host_ips = []
        for region_key in sorted(vm_ips.keys()):
            host_ips.extend(vm_ips[region_key])
        console.print(
            f"[bold]Starting node services on {len(host_ips)} VM(s) across "
            f"{len(vm_ips)} region(s)...[/bold]"
        )

    if not host_ips:
        console.print("[bold red]Error:[/bold red] No host IPs to start.")
        sys.exit(1)

    clear_known_hosts(host_ips, console)

    service_list = " ".join(f"ant-node-{i + 1}" for i in range(node_count))
    start_command = (
        f"systemctl start {service_list} && "
        f"systemctl is-active --quiet {service_list} && "
        f"echo 'ANT_NODES:STARTED'"
    )

    hosts_data = [
        (ip, {"ssh_user": "root", "ssh_key": args.ssh_key_path}) for ip in host_ips
    ]
    inventory = Inventory((hosts_data, {}))
    pyinfra_state = State(inventory=inventory, config=Config())
    connect_all(pyinfra_state)

    try:
        add_op(
            pyinfra_state,
            server.shell,
            name=f"Start ant-node-1..{node_count}",
            commands=[start_command],
        )
        run_ops(pyinfra_state)
    finally:
        disconnect_all(pyinfra_state)

    failed = pyinfra_state.failed_hosts
    total = len(host_ips)
    succeeded = total - len(failed)
    console.print()
    console.print(
        f"[bold green]Node services started on {succeeded}/{total} host(s) "
        f"({node_count} service(s) per host).[/bold green]"
    )
    if failed:
        for host in failed:
            console.print(f"  [red]Failed: {host.name}[/red]")
        sys.exit(1)
