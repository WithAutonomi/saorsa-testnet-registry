"""Remove the telegraf-ant-metrics sidecar from node VMs.

Mirrors the input modes of provision-telegraf-metrics / clean-elastic:
  --inventory-path   read node_vms from inventory JSON
  --host-ips-file    read IPs from a text file (one per line)
  --name             load IPs from deployment state (bootstrap + regulars)

Does NOT touch the monitoring VM (InfluxDB + Grafana), the telegraf deb, or
the telegraf-ant-elasticsearch sidecar.
"""

import sys

from rich.console import Console

from saorsa_deploy.cmd.provision_telegraf import (
    group_by_credentials,
    read_inventory,
    read_ips_from_file,
)
from saorsa_deploy.provisioning.monitoring_clean import MonitoringCleanProvisioner
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_deployment_state


def cmd_clean_monitoring(args):
    """Remove telegraf-ant-metrics service + config from node VMs."""
    console = Console()

    if args.inventory_path:
        try:
            entries = read_inventory(args.inventory_path)
        except (OSError, ValueError, KeyError) as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

        all_ips = [e["ip"] for e in entries]
        console.print(
            f"[bold]Cleaning {len(entries)} VM(s) from inventory "
            f"{args.inventory_path}...[/bold]"
        )

        groups = group_by_credentials(entries)
        clear_known_hosts(all_ips, console)

        failed_any = False
        for (ssh_user, ssh_key), ips in groups.items():
            console.print(
                f"[bold]Cleaning {len(ips)} host(s) "
                f"(ssh_user={ssh_user})...[/bold]"
            )
            try:
                MonitoringCleanProvisioner(
                    host_ips=ips,
                    ssh_key_path=ssh_key,
                    ssh_user=ssh_user,
                    console=console,
                ).execute()
            except Exception as e:
                console.print(
                    f"[bold red]Cleanup failed for group "
                    f"(ssh_user={ssh_user}):[/bold red] {e}"
                )
                failed_any = True
        if failed_any:
            sys.exit(1)

    elif args.host_ips_file:
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
            MonitoringCleanProvisioner(
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

        bootstrap_ip = state.get("bootstrap_ip")
        vm_ips = state.get("vm_ips") or {}
        all_ips: list[str] = []
        if bootstrap_ip:
            all_ips.append(bootstrap_ip)
        for region_key in sorted(vm_ips.keys()):
            all_ips.extend(vm_ips[region_key])

        if not all_ips:
            console.print(
                "[bold red]Error:[/bold red] No hosts found in deployment state."
            )
            sys.exit(1)

        console.print(
            f"[bold]Cleaning {len(all_ips)} host(s) from deployment state "
            f"'{args.name}'...[/bold]"
        )
        clear_known_hosts(all_ips, console)
        try:
            MonitoringCleanProvisioner(
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
            "Either --name, --host-ips-file, or --inventory-path "
            "must be provided."
        )
        sys.exit(1)

    console.print()
    console.print(
        "[bold green]Telegraf-metrics cleanup finished.[/bold green]"
    )
