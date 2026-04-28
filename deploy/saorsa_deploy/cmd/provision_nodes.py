import sys

from rich.console import Console

from saorsa_deploy.cmd.provision_genesis import _resolve_binary_source
from saorsa_deploy.provisioning.nat import select_nat_ips
from saorsa_deploy.provisioning.node import AntNodeProvisioner
from saorsa_deploy.registry import record_event, record_status
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_deployment_state, update_deployment_state


def cmd_provision_nodes(args):
    """Execute the nodes provision command: provision nodes on all VMs."""
    console = Console()

    console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
    try:
        state = load_deployment_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    vm_ips = state.get("vm_ips")
    if not vm_ips:
        console.print(
            "[bold red]Error:[/bold red] No VM IPs found in deployment state. "
            "Was this deployment created with a recent version of the infra command?"
        )
        sys.exit(1)

    # Build bootstrap peers list from state
    bootstrap_nodes = state.get("bootstrap_nodes", [])
    bootstrap_port = state.get("bootstrap_port")
    if not bootstrap_port:
        console.print(
            "[bold red]Error:[/bold red] No bootstrap port found in deployment state. "
            "Has the nodes provision-genesis command been run?"
        )
        sys.exit(1)

    if bootstrap_nodes:
        bootstrap_peers = []
        for node in bootstrap_nodes:
            port = node.get("port", bootstrap_port)
            bootstrap_peers.append(f"{node['ip']}:{port}")
    else:
        # Backwards compat: old state format with single bootstrap_ip
        bootstrap_ip = state.get("bootstrap_ip")
        if not bootstrap_ip:
            console.print(
                "[bold red]Error:[/bold red] No bootstrap IP found in deployment state."
            )
            sys.exit(1)
        bootstrap_peers = [f"{bootstrap_ip}:{bootstrap_port}"]

    binary_url, binary_is_archive = _resolve_binary_source(args, console)

    nat_ips = _resolve_nat_ips(args, state, vm_ips, console)

    if args.region:
        if args.region not in vm_ips:
            available = ", ".join(sorted(vm_ips.keys()))
            console.print(
                f"[bold red]Error:[/bold red] Region '{args.region}' not found. "
                f"Available regions: {available}"
            )
            sys.exit(1)
        all_ips = vm_ips[args.region]
        console.print(f"[bold]Provisioning {len(all_ips)} VM(s) in {args.region}...[/bold]")
    else:
        all_ips = []
        for region_key in sorted(vm_ips.keys()):
            all_ips.extend(vm_ips[region_key])
        console.print(
            f"[bold]Provisioning {len(all_ips)} VM(s) across {len(vm_ips)} region(s)...[/bold]"
        )

    console.print(f"  Bootstrap peers: {', '.join(bootstrap_peers)}")
    console.print(f"  Node count per VM: {args.node_count}")
    console.print(f"  SSH key: {args.ssh_key_path}")
    if args.port:
        console.print(f"  Port range start: {args.port}")
    if args.log_level:
        console.print(f"  Log level: {args.log_level}")
    if args.testnet:
        console.print("  Testnet mode: enabled")
    if args.upgrade_channel:
        console.print(f"  Upgrade channel: {args.upgrade_channel}")
    console.print(f"  Rewards address: {args.rewards_address}")
    console.print()

    clear_known_hosts(all_ips, console)

    kwargs = {
        "host_ips": all_ips,
        "bootstrap_peers": bootstrap_peers,
        "ssh_key_path": args.ssh_key_path,
        "node_count": args.node_count,
        "initial_port": args.port,
        "log_level": args.log_level,
        "testnet": args.testnet,
        "console": console,
        "rewards_address": args.rewards_address,
        "nat_ips": nat_ips,
    }
    if args.upgrade_channel:
        kwargs["upgrade_channel"] = args.upgrade_channel
    if binary_url:
        kwargs["binary_url"] = binary_url
        kwargs["binary_is_archive"] = binary_is_archive
    provisioner = AntNodeProvisioner(**kwargs)

    record_event(
        args.name,
        "nodes_provisioning_started",
        {"vm_count": len(all_ips), "node_count": args.node_count},
    )

    try:
        provisioner.execute()
        console.print()
        console.print("[bold green]All nodes provisioned successfully.[/bold green]")
    except Exception as e:
        console.print(f"[bold red]Provisioning failed:[/bold red] {e}")
        from saorsa_db.models import DeploymentStatus

        record_event(args.name, "nodes_provisioning_failed", {"error": str(e)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    from saorsa_db.models import DeploymentStatus

    record_event(
        args.name,
        "nodes_provisioned",
        {"vm_count": len(all_ips), "node_count": args.node_count},
    )
    record_status(args.name, DeploymentStatus.RUNNING)

    try:
        state_updates = {"node_count": args.node_count}
        binary_info = _build_binary_info(args, binary_url, binary_is_archive)
        if binary_info:
            state_updates["binary"] = binary_info
        update_deployment_state(args.name, state_updates)
        console.print("[dim]Node count and binary info saved to deployment state.[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to save node count to state: {e}[/yellow]")


def _resolve_nat_ips(args, state, vm_ips_by_region, console) -> set[str]:
    """Determine which VM IPs should be NAT-simulated.

    Uses the persisted selection from deployment state when available —
    re-running nodes provision never flips a VM between NAT and non-NAT
    unless ``--force-nat-reselect`` is passed.
    """
    nat_percent = getattr(args, "nat_percent", 0) or 0
    force_reselect = getattr(args, "force_nat_reselect", False)
    existing = state.get("nat_vm_ips")

    if existing is None or force_reselect:
        new_list = select_nat_ips(vm_ips_by_region, nat_percent) if nat_percent > 0 else []
        if new_list or existing is not None:
            try:
                update_deployment_state(args.name, {"nat_vm_ips": new_list})
            except Exception as e:
                console.print(
                    f"[yellow]Warning: failed to persist nat_vm_ips to state: {e}[/yellow]"
                )
        if new_list:
            console.print(
                f"  NAT VMs: {len(new_list)} selected at {nat_percent}% "
                "(persisted to state)"
            )
        return set(new_list)

    locked = set(existing)
    if nat_percent > 0 and locked:
        console.print(
            f"  NAT VMs: {len(locked)} locked from state "
            "(--nat-percent ignored; pass --force-nat-reselect to recompute)"
        )
    elif locked:
        console.print(f"  NAT VMs: {len(locked)} from state")
    return locked


def _build_binary_info(args, binary_url, binary_is_archive):
    """Build the binary metadata dict persisted to deployment state.

    Returns None if no binary source was specified (defaults used).
    """
    version = getattr(args, "node_version", None)
    branch = getattr(args, "branch_name", None)
    owner = getattr(args, "repo_owner", None)
    if not version and not (branch and owner):
        return None
    info = {
        "url": binary_url or "",
        "is_archive": bool(binary_is_archive),
    }
    if version:
        info["version"] = version
    if branch and owner:
        info["branch"] = branch
        info["repo_owner"] = owner
    return info
