import sys

from rich.console import Console

from saorsa_deploy.provisioning.telegraf import TelegrafProvisioner
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_deployment_state


def cmd_provision_telegraf(args):
    """Provision Telegraf for Elasticsearch log forwarding on all VMs."""
    console = Console()

    console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
    try:
        state = load_deployment_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    bootstrap_ip = state.get("bootstrap_ip")
    if not bootstrap_ip:
        console.print("[bold red]Error:[/bold red] No bootstrap IP found in deployment state.")
        sys.exit(1)

    vm_ips = state.get("vm_ips")
    if not vm_ips:
        console.print("[bold red]Error:[/bold red] No VM IPs found in deployment state.")
        sys.exit(1)

    if args.node_version:
        node_version = args.node_version
    elif args.branch_commit:
        node_version = f"{args.repo_owner}/{args.branch_commit}"
    else:
        node_version = "unknown"

    branch_commit = args.branch_commit or "unknown"

    console.print(f"  Elasticsearch URL: {args.elasticsearch_url}")
    console.print(f"  Node version: {node_version}")
    console.print(f"  Branch/commit: {branch_commit}")
    console.print()

    common_kwargs = {
        "ssh_key_path": args.ssh_key_path,
        "testnet_name": args.name,
        "elasticsearch_url": args.elasticsearch_url,
        "elasticsearch_username": args.elasticsearch_username,
        "elasticsearch_password": args.elasticsearch_password,
        "branch_commit": branch_commit,
        "node_version": node_version,
        "console": console,
    }

    # Provision genesis VM
    console.print(f"[bold]Provisioning Telegraf on genesis node ({bootstrap_ip})...[/bold]")
    clear_known_hosts([bootstrap_ip], console)
    try:
        TelegrafProvisioner(
            host_ips=[bootstrap_ip],
            host_role="GENESIS",
            **common_kwargs,
        ).execute()
    except Exception as e:
        console.print(f"[bold red]Telegraf provisioning failed on genesis:[/bold red] {e}")
        sys.exit(1)

    # Provision regular VMs
    all_ips = []
    for region_key in sorted(vm_ips.keys()):
        all_ips.extend(vm_ips[region_key])

    if all_ips:
        console.print(f"\n[bold]Provisioning Telegraf on {len(all_ips)} regular VM(s)...[/bold]")
        clear_known_hosts(all_ips, console)
        try:
            TelegrafProvisioner(
                host_ips=all_ips,
                host_role="WAN_NODE",
                **common_kwargs,
            ).execute()
        except Exception as e:
            console.print(f"[bold red]Telegraf provisioning failed:[/bold red] {e}")
            sys.exit(1)

    console.print()
    console.print("[bold green]Telegraf provisioned successfully on all VMs.[/bold green]")
