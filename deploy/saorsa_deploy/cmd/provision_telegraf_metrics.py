import sys

from rich.console import Console

from saorsa_deploy.cmd.provision_telegraf import read_ips_from_file
from saorsa_deploy.provisioning.telegraf_metrics import TelegrafMetricsProvisioner
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_deployment_state, load_monitoring_state


def cmd_provision_telegraf_metrics(args):
    """Provision Telegraf for InfluxDB metrics forwarding on testnet VMs."""
    console = Console()

    # Resolve InfluxDB connection details first
    influxdb_url = args.influxdb_url
    influxdb_token = args.influxdb_token
    influxdb_org = args.influxdb_org
    influxdb_bucket = args.influxdb_bucket

    if not influxdb_url and args.name:
        console.print(
            f"[dim]Loading monitoring state for '{args.name}'...[/dim]"
        )
        try:
            monitoring = load_monitoring_state(args.name)
        except RuntimeError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            console.print(
                "[dim]Provide --influxdb-url and --influxdb-token explicitly, "
                "or create a monitoring stack with monitoring-infra first.[/dim]"
            )
            sys.exit(1)
        influxdb_url = monitoring.get("influxdb_url", "")
        influxdb_token = monitoring.get("influxdb_token", "")
        influxdb_org = monitoring.get("influxdb_org", influxdb_org)
        influxdb_bucket = monitoring.get("influxdb_bucket", influxdb_bucket)

    if not influxdb_url or not influxdb_token:
        console.print(
            "[bold red]Error:[/bold red] "
            "InfluxDB URL and token are required. Provide --influxdb-url and "
            "--influxdb-token, or ensure a monitoring stack exists for --name."
        )
        sys.exit(1)

    common_kwargs = {
        "ssh_key_path": args.ssh_key_path,
        "ssh_user": args.ssh_user,
        "influxdb_url": influxdb_url,
        "influxdb_token": influxdb_token,
        "influxdb_org": influxdb_org,
        "influxdb_bucket": influxdb_bucket,
        "testnet_name": args.name or "",
        "console": console,
    }

    # Resolve host IPs and node counts
    if args.host_ips_file:
        try:
            all_ips = read_ips_from_file(args.host_ips_file)
        except (OSError, ValueError) as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

        console.print(
            f"[bold]Using {len(all_ips)} host IP(s) from "
            f"{args.host_ips_file}...[/bold]"
        )
        console.print(f"  InfluxDB URL: {influxdb_url}")
        console.print(f"  Target hosts: {len(all_ips)}")
        console.print(f"  SSH user: {args.ssh_user}")
        console.print()

        clear_known_hosts(all_ips, console)
        try:
            TelegrafMetricsProvisioner(
                host_ips=all_ips,
                node_count=0,
                host_role="WAN_NODE",
                **common_kwargs,
            ).execute()
        except Exception as e:
            console.print(
                f"[bold red]Telegraf metrics provisioning failed:[/bold red] {e}"
            )
            sys.exit(1)
    elif args.host_ips:
        all_ips = [ip.strip() for ip in args.host_ips.split(",") if ip.strip()]
        console.print(f"[bold]Using {len(all_ips)} provided host IP(s)...[/bold]")
        console.print(f"  InfluxDB URL: {influxdb_url}")
        console.print(f"  Target hosts: {len(all_ips)}")
        console.print()

        clear_known_hosts(all_ips, console)
        try:
            TelegrafMetricsProvisioner(
                host_ips=all_ips,
                node_count=0,
                host_role="WAN_NODE",
                **common_kwargs,
            ).execute()
        except Exception as e:
            console.print(
                f"[bold red]Telegraf metrics provisioning failed:[/bold red] {e}"
            )
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

        node_count = state.get("node_count", 0)
        bootstrap_ip = state.get("bootstrap_ip")
        vm_ips = state.get("vm_ips", {})

        regular_ips = []
        for region_key in sorted(vm_ips.keys()):
            regular_ips.extend(vm_ips[region_key])

        all_ips = []
        if bootstrap_ip:
            all_ips.append(bootstrap_ip)
        all_ips.extend(regular_ips)

        console.print(f"  InfluxDB URL: {influxdb_url}")
        console.print(f"  Target hosts: {len(all_ips)}")
        if node_count:
            console.print(f"  Node count per VM: {node_count}")
        console.print()

        clear_known_hosts(all_ips, console)

        # Provision genesis VM (1 node)
        if bootstrap_ip:
            console.print(
                f"[bold]Provisioning Telegraf on genesis node ({bootstrap_ip})...[/bold]"
            )
            try:
                TelegrafMetricsProvisioner(
                    host_ips=[bootstrap_ip],
                    node_count=1,
                    is_genesis=True,
                    host_role="GENESIS",
                    **common_kwargs,
                ).execute()
            except Exception as e:
                console.print(
                    f"[bold red]Telegraf metrics provisioning failed on genesis:"
                    f"[/bold red] {e}"
                )
                sys.exit(1)

        # Provision regular VMs (N nodes each)
        if regular_ips:
            console.print(
                f"\n[bold]Provisioning Telegraf on {len(regular_ips)}"
                f" regular VM(s)...[/bold]"
            )
            try:
                TelegrafMetricsProvisioner(
                    host_ips=regular_ips,
                    node_count=node_count,
                    host_role="WAN_NODE",
                    **common_kwargs,
                ).execute()
            except Exception as e:
                console.print(
                    f"[bold red]Telegraf metrics provisioning failed:[/bold red] {e}"
                )
                sys.exit(1)
    else:
        console.print(
            "[bold red]Error:[/bold red] "
            "Either --name, --host-ips, or --host-ips-file must be provided."
        )
        sys.exit(1)

    console.print()
    console.print(
        "[bold green]Telegraf metrics provisioned successfully on all hosts.[/bold green]"
    )
