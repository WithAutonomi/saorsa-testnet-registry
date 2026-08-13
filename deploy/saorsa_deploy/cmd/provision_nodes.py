import sys

from rich.console import Console

from saorsa_deploy.binary_descriptor import build_binary_descriptor
from saorsa_deploy.cmd.infra_nodes import NO_VOLUME_PROVIDERS
from saorsa_deploy.cmd.provision_genesis import _resolve_binary_source
from saorsa_deploy.provisioning.clock_skew import select_skew_ips
from saorsa_deploy.provisioning.full_nodes import DEFAULT_FULL_HEADROOM_MB, select_full_ips
from saorsa_deploy.provisioning.nat import select_nat_ips
from saorsa_deploy.provisioning.node import AntNodeProvisioner
from saorsa_deploy.registry import (
    record_deploy_args_update,
    record_event,
    record_status,
)
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
    clock_skew_ips = _resolve_clock_skew_ips(args, state, vm_ips, console)
    full_ips = _resolve_full_ips(args, state, vm_ips, nat_ips, console)

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

    root_disk_ips = _resolve_root_disk_ips(vm_ips, all_ips)
    if root_disk_ips:
        console.print(
            f"  Root-disk storage (no volume): {len(root_disk_ips)} OVH 3-AZ VM(s)"
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

    extra_op_providers = []
    if getattr(args, "with_telegraf", False):
        from saorsa_deploy.cmd.telegraf_fold import build_node_telegraf_providers

        extra_op_providers = build_node_telegraf_providers(
            args.name,
            host_ips=all_ips,
            host_role="WAN_NODE",
            ssh_key_path=args.ssh_key_path,
            ssh_user=getattr(args, "ssh_user", "root"),
            drop_log_levels=getattr(args, "drop_log_levels", None),
            buffer_limit=getattr(args, "buffer_limit", None),
            collect_node_memory=getattr(args, "node_memory_metrics", False),
            node_memory_interval_secs=getattr(args, "node_memory_interval", 300),
            console=console,
        )

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
        "clock_skew_ips": clock_skew_ips,
        "full_ips": full_ips,
        "root_disk_ips": root_disk_ips,
        "full_headroom_mb": getattr(args, "full_headroom_mb", DEFAULT_FULL_HEADROOM_MB),
        "extra_op_providers": extra_op_providers,
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

    # Persist nodes_per_vm, nat_percent, and the resolved node binary
    # descriptor into deploy_args so DB consumers (e.g. `deployments ls`,
    # `deployments slack-post`) can render the deployment without reading
    # S3 state.
    try:
        nat_percent_value = getattr(args, "nat_percent", 0) or 0
        clock_skew_count_value = getattr(args, "clock_skew_count", 0) or 0
        full_percent_value = getattr(args, "full_percent", 0) or 0
        full_headroom_mb_value = getattr(args, "full_headroom_mb", DEFAULT_FULL_HEADROOM_MB)
        node_binary = build_binary_descriptor(
            getattr(args, "branch_name", None),
            getattr(args, "repo_owner", None),
            getattr(args, "node_version", None),
        )
        record_deploy_args_update(
            args.name,
            {
                "nodes_per_vm": args.node_count,
                "nat_percent": nat_percent_value,
                "clock_skew_count": clock_skew_count_value,
                "full_percent": full_percent_value,
                "full_headroom_mb": full_headroom_mb_value,
                "node_binary": node_binary,
                "node_log_level": getattr(args, "log_level", None),
            },
        )
    except Exception as e:
        console.print(
            "[yellow]Warning: Failed to record nodes_per_vm/nat_percent in "
            f"deploy_args: {e}[/yellow]"
        )


def _resolve_root_disk_ips(vm_ips: dict, all_ips: list[str]) -> set[str]:
    """IPs of node VMs that have NO attached volume and use the root disk for
    /mnt/ant-node-storage — the providers in ``NO_VOLUME_PROVIDERS`` (ovh_3az:
    OVH 3-AZ regions have no block storage; contabo: the provider has no
    attachable block-volume resource). ``vm_ips`` is keyed ``"provider/region"``
    (e.g. ``"contabo/EU"``), so match on the provider prefix. Intersected with
    ``all_ips`` so a single-region provision only flags hosts it is provisioning.
    """
    return {
        ip
        for region_key, ips in vm_ips.items()
        if region_key.split("/", 1)[0] in NO_VOLUME_PROVIDERS
        for ip in ips
    } & set(all_ips)


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


def _resolve_full_ips(args, state, vm_ips_by_region, nat_ips, console) -> set[str]:
    """Determine which VM IPs should be storage-full (data volume pre-filled).

    Mirrors :func:`_resolve_nat_ips`: uses the persisted selection from
    deployment state when available so re-running nodes provision never flips a
    VM between full and non-full unless ``--force-full-reselect`` is passed.

    The selection is always disjoint from NAT: ``nat_ips`` are excluded from the
    candidate pool at selection time, and subtracted from a locked-from-state
    selection as a belt-and-suspenders guard (so a later ``--force-nat-reselect``
    can never cause a NAT host to be filled).
    """
    full_percent = getattr(args, "full_percent", 0) or 0
    force_reselect = getattr(args, "force_full_reselect", False)
    existing = state.get("full_vm_ips")

    if existing is None or force_reselect:
        new_list = (
            select_full_ips(
                vm_ips_by_region, full_percent, exclude=nat_ips, seed=args.name
            )
            if full_percent > 0
            else []
        )
        if new_list or existing is not None:
            try:
                update_deployment_state(args.name, {"full_vm_ips": new_list})
            except Exception as e:
                console.print(
                    f"[yellow]Warning: failed to persist full_vm_ips to state: {e}[/yellow]"
                )
        if new_list:
            console.print(
                f"  Storage-full VMs: {len(new_list)} selected at {full_percent}% "
                "of non-NAT VMs (persisted to state)"
            )
        return set(new_list)

    locked = set(existing) - set(nat_ips)
    if full_percent > 0 and locked:
        console.print(
            f"  Storage-full VMs: {len(locked)} locked from state "
            "(--full-percent ignored; pass --force-full-reselect to recompute)"
        )
    elif locked:
        console.print(f"  Storage-full VMs: {len(locked)} from state")
    return locked


def _resolve_clock_skew_ips(args, state, vm_ips_by_region, console) -> set[str]:
    """Determine which VM IPs should have intentional clock skew injected.

    Mirrors :func:`_resolve_nat_ips`: uses the persisted selection from
    deployment state when available so re-running nodes provision never flips
    a VM between skewed and un-skewed unless ``--force-clock-skew-reselect`` is
    passed.
    """
    skew_count = getattr(args, "clock_skew_count", 0) or 0
    force_reselect = getattr(args, "force_clock_skew_reselect", False)
    existing = state.get("clock_skew_vm_ips")

    if existing is None or force_reselect:
        new_list = select_skew_ips(vm_ips_by_region, skew_count) if skew_count > 0 else []
        if new_list or existing is not None:
            try:
                update_deployment_state(args.name, {"clock_skew_vm_ips": new_list})
            except Exception as e:
                console.print(
                    f"[yellow]Warning: failed to persist clock_skew_vm_ips to state: {e}[/yellow]"
                )
        if new_list:
            console.print(
                f"  Clock-skew VMs: {len(new_list)} selected from "
                f"--clock-skew-count {skew_count} (persisted to state)"
            )
        return set(new_list)

    locked = set(existing)
    if skew_count > 0 and locked:
        console.print(
            f"  Clock-skew VMs: {len(locked)} locked from state "
            "(--clock-skew-count ignored; pass --force-clock-skew-reselect to recompute)"
        )
    elif locked:
        console.print(f"  Clock-skew VMs: {len(locked)} from state")
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
