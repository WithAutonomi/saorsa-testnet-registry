import sys

from rich.console import Console

from saorsa_deploy.binary_source import (
    check_custom_build_exists,
    check_release_exists,
    get_custom_build_url,
    get_release_url,
)
from saorsa_deploy.provisioning.bootstrap import AntBootstrapNodeProvisioner
from saorsa_deploy.registry import record_event, record_status
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_deployment_state, update_deployment_state


def _resolve_binary_source(args, console):
    """Resolve the binary URL and whether it's an archive based on CLI args.

    Returns (binary_url, binary_is_archive) or (None, True) for default behavior.
    """
    has_branch = getattr(args, "branch_name", None)
    has_owner = getattr(args, "repo_owner", None)
    has_version = getattr(args, "node_version", None)

    if has_branch and has_owner and has_version:
        console.print(
            "[bold red]Error:[/bold red] --node-version cannot be used with "
            "--branch-name/--repo-owner"
        )
        sys.exit(1)

    if (has_branch and not has_owner) or (has_owner and not has_branch):
        console.print(
            "[bold red]Error:[/bold red] --branch-name and --repo-owner must be used together"
        )
        sys.exit(1)

    if has_version:
        console.print(f"Checking release v{args.node_version} exists...")
        if not check_release_exists(args.node_version):
            console.print(
                f"[bold red]Error:[/bold red] Release v{args.node_version} not found on GitHub"
            )
            sys.exit(1)
        url = get_release_url(args.node_version)
        console.print(f"  Using release: v{args.node_version}")
        return url, True

    if has_branch and has_owner:
        console.print(
            f"Checking custom build for {args.repo_owner}/ant-node "
            f"({args.branch_name}) exists..."
        )
        if not check_custom_build_exists(args.name, args.repo_owner, args.branch_name):
            console.print(
                f"[bold red]Error:[/bold red] No custom build found for "
                f"{args.repo_owner}/{args.branch_name}. "
                f"Run 'build ant-node' first."
            )
            sys.exit(1)
        url = get_custom_build_url(args.name, args.repo_owner, args.branch_name)
        console.print(f"  Using custom build: {url}")
        return url, False

    return None, True


def cmd_provision_genesis(args):
    """Execute the nodes provision-genesis command: provision the genesis node."""
    console = Console()

    console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
    try:
        state = load_deployment_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    # Get genesis IP from bootstrap_nodes or fall back to legacy bootstrap_ip
    bootstrap_nodes = state.get("bootstrap_nodes", [])
    if bootstrap_nodes:
        bootstrap_ip = bootstrap_nodes[0]["ip"]
    else:
        bootstrap_ip = state.get("bootstrap_ip")
    if not bootstrap_ip:
        console.print(
            "[bold red]Error:[/bold red] No bootstrap IP found in deployment state. "
            "Was this deployment created with a recent version of the infra command?"
        )
        sys.exit(1)

    binary_url, binary_is_archive = _resolve_binary_source(args, console)

    console.print(f"[bold]Provisioning genesis node at {bootstrap_ip}...[/bold]")
    console.print(f"  SSH key: {args.ssh_key_path}")
    console.print(f"  Port: {args.port}")
    if args.log_level:
        console.print(f"  Log level: {args.log_level}")
    if args.testnet:
        console.print("  Testnet mode: enabled")
    if args.upgrade_channel:
        console.print(f"  Upgrade channel: {args.upgrade_channel}")
    console.print(f"  Rewards address: {args.rewards_address}")
    console.print()

    clear_known_hosts([bootstrap_ip], console)

    extra_op_providers = []
    if getattr(args, "with_telegraf", False):
        from saorsa_deploy.cmd.telegraf_fold import build_node_telegraf_providers

        extra_op_providers = build_node_telegraf_providers(
            args.name,
            host_ips=[bootstrap_ip],
            host_role="GENESIS",
            ssh_key_path=args.ssh_key_path,
            ssh_user=getattr(args, "ssh_user", "root"),
            drop_log_levels=getattr(args, "drop_log_levels", None),
            buffer_limit=getattr(args, "buffer_limit", None),
            collect_node_memory=getattr(args, "node_memory_metrics", False),
            console=console,
        )

    kwargs = {
        "ip": bootstrap_ip,
        "bootstrap_index": 1,
        "ssh_key_path": args.ssh_key_path,
        "port": args.port,
        "log_level": args.log_level,
        "testnet": args.testnet,
        "console": console,
        "rewards_address": args.rewards_address,
        "testnet_name": args.name,
        "extra_op_providers": extra_op_providers,
    }
    if args.upgrade_channel:
        kwargs["upgrade_channel"] = args.upgrade_channel
    if binary_url:
        kwargs["binary_url"] = binary_url
        kwargs["binary_is_archive"] = binary_is_archive
    node = AntBootstrapNodeProvisioner(**kwargs)

    record_event(args.name, "genesis_provisioning_started", {"ip": bootstrap_ip, "port": args.port})

    try:
        node.execute()
        console.print()
        console.print("[bold green]Genesis node provisioned successfully.[/bold green]")
    except Exception as e:
        console.print(f"[bold red]Failed to provision genesis node:[/bold red] {e}")
        from saorsa_db.models import DeploymentStatus

        record_event(args.name, "genesis_provisioning_failed", {"error": str(e)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    record_event(args.name, "genesis_provisioned", {"ip": bootstrap_ip, "port": args.port})

    try:
        state_updates = {"bootstrap_port": args.port}
        # Update the genesis node entry with its port
        if bootstrap_nodes:
            bootstrap_nodes[0]["port"] = args.port
            state_updates["bootstrap_nodes"] = bootstrap_nodes
        update_deployment_state(args.name, state_updates)
        console.print("[dim]Bootstrap port saved to deployment state.[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to save bootstrap port to state: {e}[/yellow]")
