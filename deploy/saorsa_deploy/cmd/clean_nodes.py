"""Remove ant-node-* services from node VMs without destroying the VMs.

Leaves the mounted volume + fstab entry intact so the VM can be re-used by a
subsequent nodes provision run.

Input modes:
  --name             load IPs from deployment state vm_ips (optionally --region)
  --host-ips-file    read IPs from a text file (one per line)
"""

import sys

from rich.console import Console

from saorsa_deploy.cmd.provision_telegraf import read_ips_from_file
from saorsa_deploy.provisioning.node_clean import NodeCleanProvisioner
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_deployment_state


def cmd_clean_nodes(args):
    """Remove ant-node-* services + artefacts from node VMs."""
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
            NodeCleanProvisioner(
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
            f"[bold]Loading deployment state for '{args.name}'...[/bold]"
        )
        try:
            state = load_deployment_state(args.name)
        except RuntimeError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

        vm_ips = state.get("vm_ips") or {}
        if args.region:
            if args.region not in vm_ips:
                available = ", ".join(sorted(vm_ips.keys())) or "(none)"
                console.print(
                    f"[bold red]Error:[/bold red] Region '{args.region}' not "
                    f"found. Available regions: {available}"
                )
                sys.exit(1)
            all_ips = list(vm_ips[args.region])
            console.print(
                f"[bold]Cleaning {len(all_ips)} node host(s) in "
                f"{args.region}...[/bold]"
            )
        else:
            all_ips: list[str] = []
            for region_key in sorted(vm_ips.keys()):
                all_ips.extend(vm_ips[region_key])
            console.print(
                f"[bold]Cleaning {len(all_ips)} node host(s) across "
                f"{len(vm_ips)} region(s) from deployment state "
                f"'{args.name}'...[/bold]"
            )

        if not all_ips:
            console.print(
                "[bold red]Error:[/bold red] "
                "No node IPs found in deployment state."
            )
            sys.exit(1)

        clear_known_hosts(all_ips, console)
        try:
            NodeCleanProvisioner(
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
        "[bold green]Node cleanup finished.[/bold green]"
    )
