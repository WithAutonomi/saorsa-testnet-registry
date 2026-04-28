import sys

from rich.console import Console

from saorsa_deploy.cmd.provision_telegraf import (
    group_by_role_and_credentials,
    read_inventory,
    read_ips_from_file,
    set_hostnames,
)
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
                "or create a monitoring stack with monitoring infra first.[/dim]"
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
    if args.inventory_path:
        try:
            entries = read_inventory(args.inventory_path)
        except (OSError, ValueError, KeyError) as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

        all_ips = [e["ip"] for e in entries]
        console.print(
            f"[bold]Using {len(entries)} VM(s) from inventory "
            f"{args.inventory_path}...[/bold]"
        )
        console.print(f"  InfluxDB URL: {influxdb_url}")
        console.print(f"  Target hosts: {len(entries)}")
        console.print()

        groups = group_by_role_and_credentials(entries)
        clear_known_hosts(all_ips, console)
        set_hostnames(entries, console)

        for (role, ssh_user, ssh_key), ips in groups.items():
            # Bootstrap VMs run a single service; node VMs run many.
            # node_count=0 makes telegraf_metrics auto-detect based on
            # actual services present, so it's safe for either.
            console.print(
                f"[bold]Provisioning {len(ips)} host(s) "
                f"(role={role}, ssh_user={ssh_user})...[/bold]"
            )
            try:
                TelegrafMetricsProvisioner(
                    host_ips=ips,
                    ssh_key_path=ssh_key,
                    ssh_user=ssh_user,
                    node_count=0,
                    host_role=role,
                    influxdb_url=influxdb_url,
                    influxdb_token=influxdb_token,
                    influxdb_org=influxdb_org,
                    influxdb_bucket=influxdb_bucket,
                    testnet_name=args.name or "",
                    console=console,
                ).execute()
            except Exception as e:
                console.print(
                    f"[bold red]Telegraf metrics provisioning failed:"
                    f"[/bold red] {e}"
                )
                sys.exit(1)
    elif args.host_ips_file:
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
        bootstrap_nodes = state.get("bootstrap_nodes") or []
        bootstrap_ip = state.get("bootstrap_ip")
        vm_ips = state.get("vm_ips", {})

        if bootstrap_nodes:
            genesis_ips = [
                n["ip"] for n in bootstrap_nodes
                if n.get("bootstrap_index") == 1 and n.get("ip")
            ]
            secondary_ips = [
                n["ip"] for n in bootstrap_nodes
                if n.get("bootstrap_index", 0) >= 2 and n.get("ip")
            ]
        else:
            genesis_ips = [bootstrap_ip] if bootstrap_ip else []
            secondary_ips = []

        regular_ips = []
        for region_key in sorted(vm_ips.keys()):
            regular_ips.extend(vm_ips[region_key])

        all_ips = list(genesis_ips) + list(secondary_ips) + list(regular_ips)

        console.print(f"  InfluxDB URL: {influxdb_url}")
        console.print(f"  Target hosts: {len(all_ips)}")
        if node_count:
            console.print(f"  Node count per VM: {node_count}")
        console.print()

        clear_known_hosts(all_ips, console)

        # Provision bootstrap VMs (genesis + any secondary bootstraps)
        for role, is_genesis, ips in [
            ("GENESIS", True, genesis_ips),
            ("BOOTSTRAP", True, secondary_ips),
        ]:
            if not ips:
                continue
            console.print(
                f"[bold]Provisioning Telegraf on {len(ips)} {role} "
                f"node(s): {', '.join(ips)}...[/bold]"
            )
            try:
                TelegrafMetricsProvisioner(
                    host_ips=ips,
                    node_count=1,
                    is_genesis=is_genesis,
                    host_role=role,
                    **common_kwargs,
                ).execute()
            except Exception as e:
                console.print(
                    f"[bold red]Telegraf metrics provisioning failed on {role}:"
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
            "Either --name, --host-ips, --host-ips-file, or --inventory-path "
            "must be provided."
        )
        sys.exit(1)

    console.print()
    console.print(
        "[bold green]Telegraf metrics provisioned successfully on all hosts.[/bold green]"
    )
