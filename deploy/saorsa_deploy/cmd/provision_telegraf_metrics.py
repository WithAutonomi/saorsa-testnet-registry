import sys

from rich.console import Console

from saorsa_deploy.provisioning.telegraf_metrics import TelegrafMetricsProvisioner
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_deployment_state, load_monitoring_state


def cmd_provision_telegraf_metrics(args):
    """Provision Telegraf for InfluxDB metrics forwarding on testnet VMs."""
    console = Console()

    # Resolve host IPs: either from --host-ips or from deployment state
    if args.host_ips:
        all_ips = [ip.strip() for ip in args.host_ips.split(",") if ip.strip()]
        console.print(f"[bold]Using {len(all_ips)} provided host IP(s)...[/bold]")
    elif args.name:
        console.print(
            f"[bold]Loading deployment state for '{args.name}'...[/bold]"
        )
        try:
            state = load_deployment_state(args.name)
        except RuntimeError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

        all_ips = []
        bootstrap_ip = state.get("bootstrap_ip")
        if bootstrap_ip:
            all_ips.append(bootstrap_ip)

        vm_ips = state.get("vm_ips", {})
        for region_key in sorted(vm_ips.keys()):
            all_ips.extend(vm_ips[region_key])
    else:
        console.print(
            "[bold red]Error:[/bold red] "
            "Either --name or --host-ips must be provided."
        )
        sys.exit(1)

    if not all_ips:
        console.print("[bold red]Error:[/bold red] No host IPs to provision.")
        sys.exit(1)

    # Resolve InfluxDB connection details: explicit args take precedence,
    # otherwise load from monitoring state if --name is provided
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

    console.print(f"  InfluxDB URL: {influxdb_url}")
    console.print(f"  Target hosts: {len(all_ips)}")
    console.print()

    clear_known_hosts(all_ips, console)

    try:
        TelegrafMetricsProvisioner(
            host_ips=all_ips,
            ssh_key_path=args.ssh_key_path,
            influxdb_url=influxdb_url,
            influxdb_token=influxdb_token,
            influxdb_org=influxdb_org,
            influxdb_bucket=influxdb_bucket,
            testnet_name=args.name or "",
            console=console,
        ).execute()
    except Exception as e:
        console.print(
            f"[bold red]Telegraf metrics provisioning failed:[/bold red] {e}"
        )
        sys.exit(1)

    console.print()
    console.print(
        "[bold green]Telegraf metrics provisioned successfully on all hosts.[/bold green]"
    )
