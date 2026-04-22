import sys

from rich.console import Console

from saorsa_deploy.cmd.provision_genesis import _resolve_binary_source
from saorsa_deploy.provisioning.bootstrap import AntBootstrapNodeProvisioner
from saorsa_deploy.registry import record_event
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_deployment_state, update_deployment_state


def cmd_provision_bootstraps(args):
    """Provision additional bootstrap nodes (index 2+) after genesis is ready.

    Reads bootstrap_nodes from deployment state to find the IPs.
    Uses the genesis node (bootstrap #1) as the --bootstrap peer.
    """
    console = Console()

    console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
    try:
        state = load_deployment_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    bootstrap_nodes = state.get("bootstrap_nodes", [])
    if len(bootstrap_nodes) < 2:
        console.print(
            "[yellow]Only one bootstrap node configured — nothing to provision.[/yellow]"
        )
        return

    # Genesis must already be provisioned (bootstrap_port in state)
    genesis_ip = bootstrap_nodes[0]["ip"]
    bootstrap_port = state.get("bootstrap_port")
    if not bootstrap_port:
        console.print(
            "[bold red]Error:[/bold red] No bootstrap port found in deployment state. "
            "Has the provision-genesis command been run?"
        )
        sys.exit(1)

    genesis_peer = f"{genesis_ip}:{bootstrap_port}"
    additional_nodes = [n for n in bootstrap_nodes if n["bootstrap_index"] > 1]

    binary_url, binary_is_archive = _resolve_binary_source(args, console)

    console.print(
        f"[bold]Provisioning {len(additional_nodes)} additional bootstrap node(s)...[/bold]"
    )
    console.print(f"  Genesis peer: {genesis_peer}")
    console.print(f"  Port: {args.port}")
    console.print()

    all_ips = [n["ip"] for n in additional_nodes]
    clear_known_hosts(all_ips, console)

    record_event(
        args.name,
        "bootstrap_provisioning_started",
        {"count": len(additional_nodes), "genesis_peer": genesis_peer},
    )

    for node in additional_nodes:
        console.print(
            f"[bold]Provisioning bootstrap #{node['bootstrap_index']} "
            f"at {node['ip']} ({node['region']})...[/bold]"
        )
        kwargs = {
            "ip": node["ip"],
            "bootstrap_index": node["bootstrap_index"],
            "ssh_key_path": args.ssh_key_path,
            "bootstrap_peers": [genesis_peer],
            "port": args.port,
            "log_level": args.log_level,
            "testnet": args.testnet,
            "console": console,
            "rewards_address": args.rewards_address,
            "testnet_name": args.name,
        }
        if args.upgrade_channel:
            kwargs["upgrade_channel"] = args.upgrade_channel
        if binary_url:
            kwargs["binary_url"] = binary_url
            kwargs["binary_is_archive"] = binary_is_archive

        provisioner = AntBootstrapNodeProvisioner(**kwargs)
        try:
            provisioner.execute()
        except Exception as e:
            console.print(
                f"[bold red]Failed to provision bootstrap "
                f"#{node['bootstrap_index']}:[/bold red] {e}"
            )
            from saorsa_db.models import DeploymentStatus

            from saorsa_deploy.registry import record_status

            record_event(
                args.name,
                "bootstrap_provisioning_failed",
                {"bootstrap_index": node["bootstrap_index"], "error": str(e)},
            )
            record_status(args.name, DeploymentStatus.FAILED)
            sys.exit(1)

    # Update state with all bootstrap ports
    all_bootstrap_nodes = []
    for node in bootstrap_nodes:
        all_bootstrap_nodes.append({
            **node,
            "port": args.port,
        })
    try:
        update_deployment_state(args.name, {"bootstrap_nodes": all_bootstrap_nodes})
        console.print("[dim]Bootstrap node ports saved to deployment state.[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to save state: {e}[/yellow]")

    record_event(
        args.name,
        "bootstraps_provisioned",
        {"count": len(additional_nodes)},
    )

    console.print()
    console.print(
        f"[bold green]All {len(additional_nodes)} additional bootstrap node(s) "
        "provisioned successfully.[/bold green]"
    )
