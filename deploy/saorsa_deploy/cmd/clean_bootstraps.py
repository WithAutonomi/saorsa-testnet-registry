"""Remove ant-bootstrap-* services from bootstrap VMs without destroying the VMs.

Leaves the mounted volume + fstab entry intact so the VM can be re-used by a
subsequent nodes provision-genesis / bootstraps provision run. Targets all bootstrap
services on the VM, including the genesis node (bootstrap_index=1).

Input modes:
  --name             load IPs from deployment state (bootstrap_nodes)
  --host-ips-file    read IPs from a text file (one per line)
"""

import sys

from rich.console import Console

from saorsa_deploy.cmd.provision_telegraf import read_ips_from_file
from saorsa_deploy.provisioning.bootstrap_clean import BootstrapCleanProvisioner
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_deployment_state


def cmd_clean_bootstraps(args):
    """Remove ant-bootstrap-* services + artefacts from bootstrap VMs."""
    console = Console()

    if args.host_ips_file:
        try:
            all_ips = read_ips_from_file(args.host_ips_file)
        except (OSError, ValueError) as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

        console.print(f"[bold]Cleaning {len(all_ips)} host(s) from {args.host_ips_file}...[/bold]")
        clear_known_hosts(all_ips, console)
        try:
            BootstrapCleanProvisioner(
                host_ips=all_ips,
                ssh_key_path=args.ssh_key_path,
                ssh_user=args.ssh_user,
                console=console,
            ).execute()
        except Exception as e:
            console.print(f"[bold red]Cleanup failed:[/bold red] {e}")
            sys.exit(1)

    elif args.name:
        console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
        try:
            state = load_deployment_state(args.name)
        except RuntimeError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

        bootstrap_nodes = state.get("bootstrap_nodes") or []
        all_ips = [n["ip"] for n in bootstrap_nodes if n.get("ip")]

        if not all_ips:
            bootstrap_ip = state.get("bootstrap_ip")
            if bootstrap_ip:
                all_ips = [bootstrap_ip]

        if not all_ips:
            console.print("[bold red]Error:[/bold red] No bootstrap IPs found in deployment state.")
            sys.exit(1)

        console.print(
            f"[bold]Cleaning {len(all_ips)} bootstrap host(s) from "
            f"deployment state '{args.name}'...[/bold]"
        )
        clear_known_hosts(all_ips, console)
        try:
            BootstrapCleanProvisioner(
                host_ips=all_ips,
                ssh_key_path=args.ssh_key_path,
                ssh_user=args.ssh_user,
                console=console,
            ).execute()
        except Exception as e:
            console.print(f"[bold red]Cleanup failed:[/bold red] {e}")
            sys.exit(1)
    else:
        console.print(
            "[bold red]Error:[/bold red] Either --name or --host-ips-file must be provided."
        )
        sys.exit(1)

    console.print()
    console.print("[bold green]Bootstrap cleanup finished.[/bold green]")
