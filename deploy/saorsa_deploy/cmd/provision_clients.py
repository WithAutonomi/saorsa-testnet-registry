import sys

from rich.console import Console

from saorsa_deploy.provisioning.client import AntClientProvisioner
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_client_state, load_deployment_state


def _resolve_influx_config(args, console):
    """Resolve InfluxDB configuration from args or monitoring state.

    Three-tier resolution:
    1. Explicit --influx-url and --influx-token args
    2. --monitoring-name (load from monitoring state in S3)
    3. No InfluxDB (returns empty strings)
    """
    if args.influx_url and args.influx_token:
        console.print(f"  InfluxDB: {args.influx_url} (explicit)")
        return (
            args.influx_url,
            args.influx_token,
            getattr(args, "influx_org", "ant"),
            getattr(args, "influx_bucket", "testnet-metrics"),
        )

    monitoring_name = getattr(args, "monitoring_name", None)
    if monitoring_name:
        try:
            from saorsa_deploy.state import load_monitoring_state

            mon_state = load_monitoring_state(monitoring_name)
            influx_url = mon_state.get("influxdb_url", "")
            influx_token = mon_state.get("influxdb_token", "")
            if influx_url and influx_token:
                console.print(
                    f"  InfluxDB: {influx_url} (from monitoring state '{monitoring_name}')"
                )
                return (
                    influx_url,
                    influx_token,
                    mon_state.get("influxdb_org", "ant"),
                    mon_state.get("influxdb_bucket", "testnet-metrics"),
                )
            else:
                console.print(
                    f"[yellow]Warning: Monitoring state '{monitoring_name}' "
                    f"has no InfluxDB config. Proceeding without metrics.[/yellow]"
                )
        except RuntimeError as e:
            console.print(f"[yellow]Warning: {e} Proceeding without metrics.[/yellow]")

    console.print("  InfluxDB: disabled (no --influx-url/--influx-token or --monitoring-name)")
    return ("", "", "ant", "testnet-metrics")


def _resolve_ant_binary(args, console):
    """Resolve the ant binary source. Returns (url, is_archive) or (None, True)."""
    if args.branch_name:
        repo_owner = getattr(args, "repo_owner", None) or "maidsafe"
        from saorsa_deploy.binary_source import (
            check_ant_custom_build_exists,
            get_ant_custom_build_url,
        )

        if not check_ant_custom_build_exists(args.name, repo_owner, args.branch_name):
            console.print(
                f"[bold red]Error:[/bold red] No custom ant build found for "
                f"{repo_owner}/{args.branch_name}. "
                f"Run 'build-ant-binary' first."
            )
            sys.exit(1)
        url = get_ant_custom_build_url(args.name, repo_owner, args.branch_name)
        console.print(f"  Ant binary: custom build ({repo_owner}/{args.branch_name})")
        return url, False

    if args.ant_version:
        from saorsa_deploy.binary_source import check_ant_release_exists, get_ant_release_url

        if not check_ant_release_exists(args.ant_version):
            console.print(
                f"[bold red]Error:[/bold red] Ant release v{args.ant_version} not found."
            )
            sys.exit(1)
        url = get_ant_release_url(args.ant_version)
        console.print(f"  Ant binary: release v{args.ant_version}")
        return url, True

    console.print("  Ant binary: latest release")
    return None, True


def _resolve_wallet_secret_key(args, console):
    """Resolve the wallet secret key from args or environment.

    Priority: --wallet-secret-key arg > ANT_DEPLOY_SECRET_KEY env var.
    Exits if neither is provided.
    """
    key = getattr(args, "wallet_secret_key", None)
    if key:
        console.print("  Wallet key: provided via --wallet-secret-key")
        return key

    import os

    key = os.environ.get("ANT_DEPLOY_SECRET_KEY")
    if key:
        console.print("  Wallet key: from ANT_DEPLOY_SECRET_KEY env var")
        return key

    console.print(
        "[bold red]Error:[/bold red] No wallet secret key provided. "
        "Use --wallet-secret-key or set ANT_DEPLOY_SECRET_KEY."
    )
    sys.exit(1)


def cmd_provision_clients(args):
    """Execute the provision-clients command: provision client VMs with uploader/downloader."""
    console = Console()

    wallet_secret_key = _resolve_wallet_secret_key(args, console)

    console.print(f"[bold]Loading client state for '{args.name}'...[/bold]")
    try:
        client_state = load_client_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    clients = client_state.get("clients", [])
    if not clients:
        console.print("[bold red]Error:[/bold red] No clients found in client state.")
        sys.exit(1)

    # Load bootstrap peers from deployment state
    console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
    try:
        deploy_state = load_deployment_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    bootstrap_nodes = deploy_state.get("bootstrap_nodes", [])
    bootstrap_port = deploy_state.get("bootstrap_port")
    if not bootstrap_port:
        console.print(
            "[bold red]Error:[/bold red] No bootstrap port found in deployment state. "
            "Has the provision-genesis command been run?"
        )
        sys.exit(1)

    bootstrap_peers = []
    for node in bootstrap_nodes:
        port = node.get("port", bootstrap_port)
        bootstrap_peers.append(f"{node['ip']}:{port}")

    if not bootstrap_peers:
        bootstrap_ip = deploy_state.get("bootstrap_ip")
        if not bootstrap_ip:
            console.print(
                "[bold red]Error:[/bold red] No bootstrap IP found in deployment state."
            )
            sys.exit(1)
        bootstrap_peers = [f"{bootstrap_ip}:{bootstrap_port}"]

    binary_url, binary_is_archive = _resolve_ant_binary(args, console)
    influx_url, influx_token, influx_org, influx_bucket = _resolve_influx_config(args, console)

    host_ips = [c["ip"] for c in clients if c.get("ip")]
    console.print(f"  Bootstrap peers: {', '.join(bootstrap_peers)}")
    console.print(f"  Client VMs: {len(host_ips)}")
    for c in clients:
        console.print(
            f"    #{c['client_index']}: {c.get('ip', 'N/A')} "
            f"({c['region']}, {c['file_size_mb']}MB)"
        )
    console.print()

    clear_known_hosts(host_ips, console)

    provisioner = AntClientProvisioner(
        clients=clients,
        bootstrap_peers=bootstrap_peers,
        ssh_key_path=args.ssh_key_path,
        console=console,
        binary_url=binary_url,
        binary_is_archive=binary_is_archive,
        influx_url=influx_url,
        influx_token=influx_token,
        influx_org=influx_org,
        influx_bucket=influx_bucket,
        wallet_secret_key=wallet_secret_key,
    )

    try:
        provisioner.execute()
        console.print()
        console.print("[bold green]All clients provisioned successfully.[/bold green]")
    except Exception as e:
        console.print(f"[bold red]Client provisioning failed:[/bold red] {e}")
        sys.exit(1)
