"""Remove ant-uploader/ant-downloader + artefacts from client VMs without
destroying the VMs.

Input modes:
  --name             load IPs from client state
  --host-ips-file    read IPs from a text file (one per line)
"""

import sys

from rich.console import Console

from saorsa_deploy.cmd.provision_telegraf import read_ips_from_file
from saorsa_deploy.provisioning.client_clean import ClientCleanProvisioner
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_client_state


def cmd_clean_clients(args):
    """Remove client services + artefacts from client VMs."""
    console = Console()

    if args.host_ips_file:
        try:
            all_ips = read_ips_from_file(args.host_ips_file)
        except (OSError, ValueError) as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

        console.print(
            f"[bold]Cleaning {len(all_ips)} host(s) from "
            f"{args.host_ips_file}...[/bold]"
        )
        clear_known_hosts(all_ips, console)
        try:
            ClientCleanProvisioner(
                host_ips=all_ips,
                ssh_key_path=args.ssh_key_path,
                ssh_user=args.ssh_user,
                console=console,
            ).execute()
        except Exception as e:
            console.print(f"[bold red]Cleanup failed:[/bold red] {e}")
            sys.exit(1)

    elif args.name:
        console.print(
            f"[bold]Loading client state for '{args.name}'...[/bold]"
        )
        try:
            client_state = load_client_state(args.name)
        except RuntimeError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

        clients = client_state.get("clients") or []
        all_ips = [c["ip"] for c in clients if c.get("ip")]

        if not all_ips:
            console.print(
                "[bold red]Error:[/bold red] "
                "No client IPs found in client state."
            )
            sys.exit(1)

        console.print(
            f"[bold]Cleaning {len(all_ips)} client host(s) from "
            f"client state '{args.name}'...[/bold]"
        )
        clear_known_hosts(all_ips, console)
        try:
            ClientCleanProvisioner(
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
            "[bold red]Error:[/bold red] "
            "Either --name or --host-ips-file must be provided."
        )
        sys.exit(1)

    console.print()
    console.print(
        "[bold green]Client cleanup finished.[/bold green]"
    )
