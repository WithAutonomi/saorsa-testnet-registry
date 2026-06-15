"""``saorsa-deploy prod nodes`` — profiling ant-node VMs against PRODUCTION.

Stands up one (or more) node VMs running ``node_count`` ant-node services each
that join the **production** Autonomi network — the same prod-mode node units
the migration flow produces (no ``--evm-network`` → arbitrum-one,
``Restart=always``, ``LimitNOFILE=65536``, ``--stop-on-upgrade`` with no upgrade
channel so a pinned build keeps running) — but with **heaptrack memory
profiling** wrapped around every service. Intended for chasing the intermittent
ant-node memory leak with a **debug build** under real prod load.

Vehicle: this reuses the whole testnet node lifecycle. We seed the deployment's
S3 state with the production bootstrap peers, then drive the existing
``nodes infra`` (multi-provider VM + data-volume creation) and ``nodes start``
commands; provisioning is a dedicated prod-mode pass (mirroring
``migration provision``) so it can also fold in heaptrack and, optionally,
InfluxDB host-metric forwarding.

Companion to ``prod uploaders`` / ``prod continuous-downloader``: registers a
``Deployment`` row with ``deploy_args.kind = "prod-nodes"`` and supports the
declarative ``deployments up/plan/down --config`` path (kind ``prod-nodes``).
"""

import sys
from types import SimpleNamespace

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console
from saorsa_db.models import DeploymentStatus

from saorsa_deploy.binary_descriptor import build_binary_descriptor
from saorsa_deploy.cmd.provision_genesis import _resolve_binary_source
from saorsa_deploy.prod_bootstraps import (
    PROD_BOOTSTRAPS_URL,
    ProdBootstrapsError,
    fetch_prod_bootstraps,
)
from saorsa_deploy.provisioning.node import HEAPTRACK_DIR, AntNodeProvisioner
from saorsa_deploy.registry import (
    record_deploy_args_update,
    record_deployment_created,
    record_event,
    record_status,
)
from saorsa_deploy.ssh import clear_known_hosts, wait_for_ssh
from saorsa_deploy.state import (
    delete_deployment_state,
    load_deployment_state,
    load_monitoring_state,
    save_deployment_state,
    update_deployment_state,
)

# Default monitoring InfluxDB VM region/size. A node VM forwards 15 services'
# worth of host+procstat metrics, so keep the box at the testnet node default
# rather than the smaller single-client prod default.
MONITORING_REGION = "lon1"
MONITORING_DROPLET_SIZE = "s-4vcpu-8gb"

# Provider → (vm-count attr prefix, size Terraform var, prod-appropriate default
# VM size). The node TF modules already back all four providers.
_PROVIDER_INFO = {
    "digitalocean": ("do", "do_droplet_size", "s-4vcpu-8gb"),
    "hetzner": ("hetzner", "hetzner_server_type", "cpx42"),
    "vultr": ("vultr", "vultr_plan", "vhf-4c-16gb"),
    "ovh": ("ovh", "ovh_flavor", "b3-16"),
}


def _parse_prod_bootstrap_nodes(peers: list[str]) -> tuple[list[dict], int]:
    """Turn ``["ip:port", ...]`` prod peers into ``bootstrap_nodes`` state dicts.

    Returns ``(bootstrap_nodes, bootstrap_port)`` where ``bootstrap_port`` is the
    first peer's port (a fallback for any state consumer that reads it; each node
    dict also carries its own ``port``).
    """
    bootstrap_nodes: list[dict] = []
    first_port = 0
    for idx, peer in enumerate(peers):
        host, _, port_str = peer.rpartition(":")
        if not host or not port_str.isdigit():
            raise ProdBootstrapsError(f"Malformed prod bootstrap peer: {peer!r}")
        port = int(port_str)
        if idx == 0:
            first_port = port
        bootstrap_nodes.append(
            {
                "ip": host,
                "port": port,
                "region": "prod",
                "bootstrap_index": idx + 1,
            }
        )
    return bootstrap_nodes, first_port


def _node_infra_exists(name: str) -> bool:
    try:
        state = load_deployment_state(name)
    except RuntimeError:
        return False
    return bool(state.get("vm_ips"))


def _monitoring_stack_exists(name: str) -> bool:
    try:
        load_monitoring_state(name)
        return True
    except RuntimeError:
        return False


def _monitoring_has_influx(name: str) -> bool:
    try:
        state = load_monitoring_state(name)
    except RuntimeError:
        return False
    return bool(state.get("influxdb_url") and state.get("influxdb_token"))


def _seed_prod_bootstraps(args, console: Console) -> None:
    """Fetch prod bootstrap peers and seed deployment state with them.

    ``nodes infra`` requires ``bootstrap_nodes`` to already exist in state; for a
    production join those peers come from the WithAutonomi bootstrap list rather
    than a genesis node we created. Idempotent: an existing deployment keeps its
    regions/vm_ips and just has its bootstrap list refreshed.
    """
    console.print("[bold]Fetching production bootstrap peers...[/bold]")
    try:
        peers = fetch_prod_bootstraps()
    except ProdBootstrapsError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)
    bootstrap_nodes, bootstrap_port = _parse_prod_bootstrap_nodes(peers)
    console.print(f"  Using {len(bootstrap_nodes)} production peers.")

    try:
        load_deployment_state(args.name)
        update_deployment_state(
            args.name,
            {"bootstrap_nodes": bootstrap_nodes, "bootstrap_port": bootstrap_port},
        )
    except RuntimeError:
        save_deployment_state(
            name=args.name,
            regions=[],
            terraform_variables={},
            bootstrap_nodes=bootstrap_nodes,
            vm_ips={},
        )
        update_deployment_state(args.name, {"bootstrap_port": bootstrap_port})


def _ensure_node_infra(args, console: Console) -> None:
    from saorsa_deploy.cmd.infra_nodes import cmd_infra_nodes

    if _node_infra_exists(args.name):
        console.print(
            f"[dim]Node VMs for '{args.name}' already exist — skipping nodes infra.[/dim]"
        )
        return

    prefix, size_var, default_size = _PROVIDER_INFO[args.provider]
    vm_size = args.vm_size or default_size
    console.print(
        f"[bold cyan]Creating {args.vm_count} node VM(s) "
        f"({args.provider}/{args.region}, {vm_size})[/bold cyan]"
    )
    infra_args = SimpleNamespace(
        name=args.name,
        force=False,
        attached_volume_size=args.volume_size,
        do_vm_count=0,
        hetzner_vm_count=0,
        vultr_vm_count=0,
        ovh_vm_count=0,
        **{
            f"{prefix}_vm_count": args.vm_count,
            f"{prefix}_regions_list": args.region,
            size_var: vm_size,
        },
    )
    cmd_infra_nodes(infra_args)


def _ensure_monitoring(args, console: Console) -> None:
    from saorsa_deploy.cmd.monitoring_infra import cmd_monitoring_infra
    from saorsa_deploy.cmd.provision_monitoring import cmd_provision_monitoring

    if _monitoring_stack_exists(args.name):
        if _monitoring_has_influx(args.name):
            console.print(
                f"[dim]Monitoring stack '{args.name}' already exists — "
                "skipping monitoring infra / provision.[/dim]"
            )
            return
        console.print(
            f"[yellow]Monitoring VM for '{args.name}' exists but has no InfluxDB "
            "creds (a prior provision didn't finish) — re-provisioning.[/yellow]"
        )
        cmd_provision_monitoring(
            SimpleNamespace(name=args.name, ssh_key_path=args.ssh_key_path)
        )
        return

    console.print("[bold cyan]Creating monitoring (InfluxDB) VM[/bold cyan]")
    cmd_monitoring_infra(
        SimpleNamespace(
            name=args.name,
            region=MONITORING_REGION,
            volume_size=args.monitoring_volume_size,
            droplet_size=MONITORING_DROPLET_SIZE,
            force=False,
            grafana=getattr(args, "grafana", False),
        )
    )
    console.print("[bold cyan]Provisioning InfluxDB + Grafana[/bold cyan]")
    cmd_provision_monitoring(
        SimpleNamespace(name=args.name, ssh_key_path=args.ssh_key_path)
    )


def _provision_prod_nodes(args, console: Console) -> None:
    console.print("[bold cyan]Provisioning ant-node services (prod mode)[/bold cyan]")
    try:
        state = load_deployment_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    vm_ips = state.get("vm_ips") or {}
    if not vm_ips:
        console.print("[bold red]Error:[/bold red] No VM IPs in deployment state.")
        sys.exit(1)

    all_ips: list[str] = []
    for region_key in sorted(vm_ips.keys()):
        all_ips.extend(vm_ips[region_key])

    bootstrap_nodes = state.get("bootstrap_nodes") or []
    bootstrap_port = state.get("bootstrap_port")
    bootstrap_peers = [
        f"{n['ip']}:{n.get('port', bootstrap_port)}" for n in bootstrap_nodes
    ]
    if not bootstrap_peers:
        console.print(
            "[bold red]Error:[/bold red] No prod bootstrap peers in state."
        )
        sys.exit(1)

    binary_url, binary_is_archive = _resolve_binary_source(args, console)

    clear_known_hosts(all_ips, console)
    not_ready = wait_for_ssh(all_ips, args.ssh_key_path, console=console)
    if not_ready:
        console.print(
            f"[bold red]Error:[/bold red] SSH never came up on: {', '.join(not_ready)}"
        )
        sys.exit(1)

    extra_op_providers = []
    if args.monitoring and _monitoring_has_influx(args.name):
        from saorsa_deploy.cmd.telegraf_fold import build_node_telegraf_providers

        extra_op_providers = build_node_telegraf_providers(
            args.name,
            host_ips=all_ips,
            host_role="WAN_NODE",
            node_count=args.node_count,
            is_genesis=False,
            ssh_key_path=args.ssh_key_path,
            ssh_user="root",
            drop_log_levels=None,
            buffer_limit=None,
            console=console,
        )

    kwargs = {
        "host_ips": all_ips,
        "bootstrap_peers": bootstrap_peers,
        "ssh_key_path": args.ssh_key_path,
        "node_count": args.node_count,
        "initial_port": args.port,
        "log_level": args.log_level,
        "testnet": False,
        "console": console,
        "rewards_address": args.rewards_address,
        # Match the prod fleet's units (see migration provision): no
        # --evm-network (ant-node defaults to arbitrum-one), Restart=always
        # with 10s back-off, LimitNOFILE=65536.
        "evm_network": None,
        "restart_policy": "always",
        "restart_sec": 10,
        "limit_nofile": 65536,
        "heaptrack": args.heaptrack,
        "extra_op_providers": extra_op_providers,
    }
    if binary_url:
        kwargs["binary_url"] = binary_url
        kwargs["binary_is_archive"] = binary_is_archive

    provisioner = AntNodeProvisioner(**kwargs)
    try:
        provisioner.execute()
    except Exception as e:
        console.print(f"[bold red]Provisioning failed:[/bold red] {e}")
        raise

    update_deployment_state(args.name, {"node_count": args.node_count})


def _install_trace_logprune(args, console: Console) -> None:
    """Install a daily systemd timer that prunes heaptrack traces older than N days.

    Mirrors the prod-downloader log-retention timer. ``--log-retention-days 0``
    keeps traces forever (no timer installed).
    """
    if args.log_retention_days <= 0:
        return
    try:
        state = load_deployment_state(args.name)
    except RuntimeError:
        return
    vm_ips = state.get("vm_ips") or {}
    host_ips: list[str] = []
    for region_key in sorted(vm_ips.keys()):
        host_ips.extend(vm_ips[region_key])
    if not host_ips:
        return

    console.print(
        f"[dim]Installing heaptrack trace prune timer "
        f"(delete traces older than {args.log_retention_days}d).[/dim]"
    )
    service_unit = (
        "[Unit]\n"
        "Description=Prune old ant-node heaptrack traces\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart=/usr/bin/find {HEAPTRACK_DIR} -type f -name '*.zst' "
        f"-mtime +{args.log_retention_days} -delete\n"
    )
    timer_unit = (
        "[Unit]\n"
        "Description=Daily ant-node heaptrack trace prune\n\n"
        "[Timer]\n"
        "OnCalendar=daily\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    commands = [
        f"cat > /etc/systemd/system/ant-node-heaptrack-logprune.service "
        f"<< 'SVC_EOF'\n{service_unit}SVC_EOF",
        f"cat > /etc/systemd/system/ant-node-heaptrack-logprune.timer "
        f"<< 'TMR_EOF'\n{timer_unit}TMR_EOF",
        "systemctl daemon-reload",
        "systemctl enable --now ant-node-heaptrack-logprune.timer",
    ]

    clear_known_hosts(host_ips, console)
    hosts_data = [
        (ip, {"ssh_user": "root", "ssh_key": args.ssh_key_path}) for ip in host_ips
    ]
    state_obj = State(inventory=Inventory((hosts_data, {})), config=Config())
    connect_all(state_obj)
    try:
        add_op(
            state_obj,
            server.shell,
            name="Install heaptrack trace prune timer",
            commands=commands,
        )
        run_ops(state_obj)
    finally:
        disconnect_all(state_obj)


def cmd_prod_nodes_deploy(args):
    """Deploy profiling ant-node VMs against the production Autonomi network."""
    console = Console()

    if args.provider not in _PROVIDER_INFO:
        console.print(
            f"[bold red]Error:[/bold red] Unknown provider '{args.provider}'. "
            f"Choose one of: {', '.join(sorted(_PROVIDER_INFO))}."
        )
        sys.exit(1)

    console.print(f"[bold]prod nodes deploy: {args.name}[/bold]")
    console.print(f"  Provider/region: {args.provider}/{args.region}")
    console.print(f"  VMs: {args.vm_count} × {args.node_count} node services")
    console.print(f"  Prod bootstraps URL: {PROD_BOOTSTRAPS_URL}")
    console.print(f"  heaptrack: {'on' if args.heaptrack else 'off'}")
    console.print(f"  Monitoring: {'on' if args.monitoring else 'off'}")
    console.print()

    deploy_args_payload = {
        "kind": "prod-nodes",
        "prod_bootstraps_url": PROD_BOOTSTRAPS_URL,
        "provider": args.provider,
        "region": args.region,
        "vm_size": args.vm_size or _PROVIDER_INFO[args.provider][2],
        "vm_count": args.vm_count,
        "node_count": args.node_count,
        "heaptrack": args.heaptrack,
        "monitoring": args.monitoring,
        "monitoring_name": args.name if args.monitoring else None,
        "log_retention_days": args.log_retention_days,
        "rewards_address": args.rewards_address,
        "log_level": args.log_level,
        "purpose": args.purpose,
        "node_binary": build_binary_descriptor(
            args.branch_name,
            args.repo_owner,
            args.node_version,
            debug=getattr(args, "debug", False),
        ),
    }
    record_deployment_created(args.name, deploy_args_payload)
    record_event(args.name, "prod_nodes_deploy_started", deploy_args_payload)

    try:
        _seed_prod_bootstraps(args, console)
        _ensure_node_infra(args, console)
        if args.monitoring:
            _ensure_monitoring(args, console)
        _provision_prod_nodes(args, console)

        from saorsa_deploy.cmd.start_nodes import cmd_start_nodes

        cmd_start_nodes(
            SimpleNamespace(
                name=args.name, ssh_key_path=args.ssh_key_path, region=None
            )
        )

        if args.heaptrack:
            _install_trace_logprune(args, console)

        record_deploy_args_update(
            args.name, {"nodes_per_vm": args.node_count}
        )
    except SystemExit:
        record_event(args.name, "prod_nodes_deploy_failed")
        record_status(args.name, DeploymentStatus.FAILED)
        raise
    except Exception as e:
        console.print(f"[bold red]Deploy failed:[/bold red] {e}")
        record_event(args.name, "prod_nodes_deploy_failed", {"error": str(e)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    record_event(args.name, "prod_nodes_deploy_completed")
    record_status(args.name, DeploymentStatus.RUNNING)
    console.print()
    console.print(f"[bold green]prod nodes '{args.name}' are running.[/bold green]")
    if args.heaptrack:
        console.print(
            f"[dim]heaptrack traces: ssh root@<vm> ls {HEAPTRACK_DIR}/ "
            "(ant-node-<i>.<pid>.zst). scp them off before destroy.[/dim]"
        )


def cmd_prod_nodes_destroy(args):
    """Destroy a prod profiling-nodes deployment (node VMs + optional monitoring)."""
    from saorsa_deploy.cmd.destroy_monitoring import cmd_destroy_monitoring
    from saorsa_deploy.cmd.destroy_nodes import cmd_destroy_nodes

    console = Console()
    console.print(f"[bold]prod nodes destroy: {args.name}[/bold]")

    record_event(args.name, "prod_nodes_destroy_started")
    record_status(args.name, DeploymentStatus.DESTROYING)

    try:
        console.print("[bold cyan]Destroying node VMs...[/bold cyan]")
        cmd_destroy_nodes(SimpleNamespace(name=args.name, force=True))
        # destroy_nodes keeps the state file while bootstrap_nodes remain; for a
        # prod-nodes deployment the seeded prod bootstraps are not real infra, so
        # remove the state entirely.
        try:
            delete_deployment_state(args.name)
        except Exception:
            pass

        if args.keep_monitoring:
            console.print(
                "[dim]Leaving monitoring stack intact (--keep-monitoring).[/dim]"
            )
        elif _monitoring_stack_exists(args.name):
            console.print("[bold cyan]Destroying monitoring stack...[/bold cyan]")
            cmd_destroy_monitoring(SimpleNamespace(name=args.name, force=True))
        else:
            console.print(
                f"[dim]No monitoring stack found for '{args.name}' — skipping.[/dim]"
            )
    except SystemExit:
        record_event(args.name, "prod_nodes_destroy_failed")
        record_status(args.name, DeploymentStatus.FAILED)
        raise
    except Exception as e:
        console.print(f"[bold red]Destroy failed:[/bold red] {e}")
        record_event(args.name, "prod_nodes_destroy_failed", {"error": str(e)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    record_event(args.name, "prod_nodes_destroy_completed")
    record_status(args.name, DeploymentStatus.DESTROYED)
    console.print()
    console.print(f"[bold green]prod nodes '{args.name}' destroyed.[/bold green]")
