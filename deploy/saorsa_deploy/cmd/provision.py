import sys

from rich.console import Console

from saorsa_deploy.cmd.provision_genesis import _resolve_binary_source
from saorsa_deploy.provisioning.node import SaorsaNodeProvisioner
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_deployment_state, update_deployment_state


def cmd_provision(args):
    """Execute the provision command: provision nodes on all VMs."""
    console = Console()

    console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
    try:
        state = load_deployment_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    vm_ips = state.get("vm_ips")
    if not vm_ips:
        console.print(
            "[bold red]Error:[/bold red] No VM IPs found in deployment state. "
            "Was this deployment created with a recent version of the infra command?"
        )
        sys.exit(1)

    bootstrap_ip = state.get("bootstrap_ip")
    if not bootstrap_ip:
        console.print("[bold red]Error:[/bold red] No bootstrap IP found in deployment state.")
        sys.exit(1)

    bootstrap_port = state.get("bootstrap_port")
    if not bootstrap_port:
        console.print(
            "[bold red]Error:[/bold red] No bootstrap port found in deployment state. "
            "Has the provision-genesis command been run?"
        )
        sys.exit(1)

    binary_url, binary_is_archive = _resolve_binary_source(args, console)

    if args.region:
        if args.region not in vm_ips:
            available = ", ".join(sorted(vm_ips.keys()))
            console.print(
                f"[bold red]Error:[/bold red] Region '{args.region}' not found. "
                f"Available regions: {available}"
            )
            sys.exit(1)
        all_ips = vm_ips[args.region]
        console.print(f"[bold]Provisioning {len(all_ips)} VM(s) in {args.region}...[/bold]")
    else:
        all_ips = []
        for region_key in sorted(vm_ips.keys()):
            all_ips.extend(vm_ips[region_key])
        console.print(
            f"[bold]Provisioning {len(all_ips)} VM(s) across {len(vm_ips)} region(s)...[/bold]"
        )

    console.print(f"  Bootstrap: {bootstrap_ip}:{bootstrap_port}")
    console.print(f"  Node count per VM: {args.node_count}")
    console.print(f"  SSH key: {args.ssh_key_path}")
    if args.port:
        console.print(f"  Port range start: {args.port}")
    if args.ip_version:
        console.print(f"  IP version: {args.ip_version}")
    if args.log_level:
        console.print(f"  Log level: {args.log_level}")
    if args.testnet:
        console.print("  Testnet mode: enabled")
    console.print()

    clear_known_hosts(all_ips, console)

    kwargs = {
        "host_ips": all_ips,
        "bootstrap_ip": bootstrap_ip,
        "bootstrap_port": bootstrap_port,
        "ssh_key_path": args.ssh_key_path,
        "node_count": args.node_count,
        "initial_port": args.port,
        "log_level": args.log_level,
        "testnet": args.testnet,
        "console": console,
    }
    if args.ip_version:
        kwargs["ip_version"] = args.ip_version
    if binary_url:
        kwargs["binary_url"] = binary_url
        kwargs["binary_is_archive"] = binary_is_archive
    provisioner = SaorsaNodeProvisioner(**kwargs)

    try:
        provisioner.execute()
        console.print()
        console.print("[bold green]All nodes provisioned successfully.[/bold green]")
    except Exception as e:
        console.print(f"[bold red]Provisioning failed:[/bold red] {e}")
        sys.exit(1)

    try:
        update_deployment_state(args.name, {"node_count": args.node_count})
        console.print("[dim]Node count saved to deployment state.[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to save node count to state: {e}[/yellow]")
