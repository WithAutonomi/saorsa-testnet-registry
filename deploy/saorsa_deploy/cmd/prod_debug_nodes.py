"""``saorsa-deploy prod debug-nodes`` — crash-debug ant-node VMs against PRODUCTION.

Sibling of ``prod nodes`` (the heaptrack memory-profiling kind). Stands up a
small, isolated, **permanent** set of node VMs that join the **production**
Autonomi network configured for panic/crash debugging rather than heap
profiling:

- ``RUST_BACKTRACE`` on every service (actionable panic traces),
- core dumps enabled + size/age-pruned (post-crash inspection),
- global DEBUG ``--log-level`` (ant-node has no per-module env filter, so a
  single global level is the only option — the requested per-module targeting is
  not achievable at the deploy layer),
- its OWN dedicated small Elasticsearch (short ILM retention) and small InfluxDB,
  not the main prod stack.

Runs the same prod-mode node units as ``prod nodes`` / the migration flow (no
``--evm-network`` → arbitrum-one, ``Restart=always``, ``LimitNOFILE=65536``,
``--stop-on-upgrade`` with no upgrade channel so the pinned debug build keeps
running). Node and monitoring/logging VMs each choose their own provider+region;
Contabo (root-disk only) is supported alongside the four existing providers.

Registers a ``Deployment`` row with ``deploy_args.kind = "prod-debug-nodes"`` and
supports the declarative ``deployments up/plan/down --config`` path.
"""

import sys
from types import SimpleNamespace

from rich.console import Console
from saorsa_db.models import DeploymentStatus

from saorsa_deploy.binary_descriptor import build_binary_descriptor
from saorsa_deploy.cmd.prod_nodes import (
    _elastic_has_creds,
    _monitoring_has_influx,
    _monitoring_stack_exists,
    _node_infra_exists,
    _resolve_node_drop_log_levels,
    _seed_prod_bootstraps,
)
from saorsa_deploy.cmd.provision_genesis import _resolve_binary_source
from saorsa_deploy.cmd.provision_nodes import _resolve_root_disk_ips
from saorsa_deploy.prod_bootstraps import PROD_BOOTSTRAPS_URL
from saorsa_deploy.provisioning.node import COREDUMP_DIR, AntNodeProvisioner
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
    update_deployment_state,
)

# Provider → (vm-count attr prefix, size Terraform var, prod-appropriate default
# VM size). Contabo carries a product id in place of a size slug. The debug
# fleet is small so these defaults are modest; override with --vm-size.
_PROVIDER_INFO = {
    "digitalocean": ("do", "do_droplet_size", "s-4vcpu-8gb"),
    "hetzner": ("hetzner", "hetzner_server_type", "cpx42"),
    "vultr": ("vultr", "vultr_plan", "vhf-4c-16gb"),
    "ovh": ("ovh", "ovh_flavor", "b3-16"),
    # A VDS gives ample fixed root disk (Contabo has no attachable/growable
    # volume) for a permanent node that accumulates chunk data + core dumps.
    "contabo": ("contabo", "contabo_product_id", "V10"),
}


def _build_node_infra_args(args) -> SimpleNamespace:
    """Build the ``SimpleNamespace`` ``cmd_infra_nodes`` expects for one
    provider/region run (all other providers' vm_count zeroed)."""
    kwargs = {
        "name": args.name,
        "force": False,
        "attached_volume_size": args.volume_size,
        "do_vm_count": 0,
        "hetzner_vm_count": 0,
        "vultr_vm_count": 0,
        "ovh_vm_count": 0,
        "ovh_3az_vm_count": 0,
        "contabo_vm_count": 0,
    }
    if args.provider == "contabo":
        _, _, default_product = _PROVIDER_INFO["contabo"]
        kwargs["contabo_vm_count"] = args.vm_count
        kwargs["contabo_regions_list"] = args.region
        kwargs["contabo_product_id"] = args.vm_size or default_product
        kwargs["contabo_image_id"] = args.contabo_image_id
        kwargs["contabo_ssh_public_key_path"] = args.contabo_ssh_public_key_path
    else:
        prefix, size_var, default_size = _PROVIDER_INFO[args.provider]
        kwargs[f"{prefix}_vm_count"] = args.vm_count
        kwargs[f"{prefix}_regions_list"] = args.region
        kwargs[size_var] = args.vm_size or default_size
    return SimpleNamespace(**kwargs)


def _ensure_node_infra(args, console: Console) -> None:
    # Contabo bypasses Terraform entirely (its provider's 20-min create timeout
    # can't absorb Contabo's slow async "orders"): create/adopt via the REST API.
    if args.provider == "contabo":
        from saorsa_deploy.cmd.contabo_infra import ensure_contabo_node_infra

        console.print(
            f"[bold cyan]Ensuring {args.vm_count} Contabo node VM(s) via API "
            f"({args.region}/{args.vm_size})[/bold cyan]"
        )
        ensure_contabo_node_infra(args, console)
        return

    from saorsa_deploy.cmd.infra_nodes import cmd_infra_nodes

    if _node_infra_exists(args.name):
        console.print(
            f"[dim]Node VMs for '{args.name}' already exist — skipping nodes infra.[/dim]"
        )
        return

    _, _, default_size = _PROVIDER_INFO[args.provider]
    console.print(
        f"[bold cyan]Creating {args.vm_count} node VM(s) "
        f"({args.provider}/{args.region}, {args.vm_size or default_size})[/bold cyan]"
    )
    cmd_infra_nodes(_build_node_infra_args(args))


def _ensure_monitoring(args, console: Console) -> None:
    """Stand up (and provision) the dedicated small InfluxDB stack.

    Provider/region are selectable (``--monitoring-provider`` /
    ``--monitoring-region``); Grafana is off by default.
    """
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
            SimpleNamespace(
                name=args.name,
                ssh_key_path=args.ssh_key_path,
                retention_days=args.influx_retention_days,
            )
        )
        return

    console.print(
        f"[bold cyan]Creating monitoring (InfluxDB) VM on "
        f"{args.monitoring_provider}[/bold cyan]"
    )
    if args.monitoring_provider == "contabo":
        from saorsa_deploy.cmd.contabo_infra import ensure_contabo_monitoring_infra

        ensure_contabo_monitoring_infra(args, console)
    else:
        cmd_monitoring_infra(
            SimpleNamespace(
                name=args.name,
                provider=args.monitoring_provider,
                region=args.monitoring_region,
                volume_size=args.monitoring_volume_size,
                droplet_size=args.monitoring_droplet_size,
                grafana_size=None,
                volume_type=None,
                force=False,
                grafana=getattr(args, "grafana", False),
                contabo_image_id=args.contabo_image_id,
                contabo_ssh_public_key_path=args.contabo_ssh_public_key_path,
            )
        )
    console.print("[bold cyan]Provisioning InfluxDB[/bold cyan]")
    cmd_provision_monitoring(
        SimpleNamespace(
            name=args.name,
            ssh_key_path=args.ssh_key_path,
            retention_days=args.influx_retention_days,
        )
    )


def _ensure_elastic(args, console: Console) -> None:
    """Stand up (and provision) the dedicated small Elasticsearch (short ILM)."""
    from saorsa_deploy.cmd.elastic_infra import cmd_elastic_infra
    from saorsa_deploy.cmd.prod_log_forwarding import elastic_state_exists
    from saorsa_deploy.cmd.provision_elastic import cmd_provision_elastic

    if elastic_state_exists(args.name):
        if _elastic_has_creds(args.name):
            console.print(
                f"[dim]Elasticsearch '{args.name}' already exists — "
                "skipping elastic infra / provision.[/dim]"
            )
            return
        console.print(
            f"[yellow]Elasticsearch VM for '{args.name}' exists but has no creds "
            "(a prior provision didn't finish) — re-provisioning.[/yellow]"
        )
        cmd_provision_elastic(
            SimpleNamespace(
                name=args.name,
                ssh_key_path=args.ssh_key_path,
                retention_days=args.elastic_retention_days,
                shards=args.elastic_shards,
            )
        )
        return

    console.print(
        f"[bold cyan]Creating dedicated Elasticsearch VM on "
        f"{args.elastic_provider} (retention {args.elastic_retention_days}d)"
        "[/bold cyan]"
    )
    if args.elastic_provider == "contabo":
        from saorsa_deploy.cmd.contabo_infra import ensure_contabo_elastic_infra

        ensure_contabo_elastic_infra(args, console)
    else:
        cmd_elastic_infra(
            SimpleNamespace(
                name=args.name,
                provider=args.elastic_provider,
                region=args.elastic_region,
                droplet_size=args.elastic_droplet_size,
                volume_size=args.elastic_volume_size,
                volume_type=None,
                force=False,
                contabo_image_id=args.contabo_image_id,
                contabo_ssh_public_key_path=args.contabo_ssh_public_key_path,
            )
        )
    console.print("[bold cyan]Provisioning Elasticsearch for node log forwarding[/bold cyan]")
    cmd_provision_elastic(
        SimpleNamespace(
            name=args.name,
            ssh_key_path=args.ssh_key_path,
            retention_days=args.elastic_retention_days,
            shards=args.elastic_shards,
        )
    )


def _provision_debug_nodes(args, console: Console) -> None:
    console.print(
        "[bold cyan]Provisioning ant-node services (prod crash-debug mode)[/bold cyan]"
    )
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
        console.print("[bold red]Error:[/bold red] No prod bootstrap peers in state.")
        sys.exit(1)

    binary_url, binary_is_archive = _resolve_binary_source(args, console)

    clear_known_hosts(all_ips, console)
    not_ready = wait_for_ssh(all_ips, args.ssh_key_path, console=console)
    if not_ready:
        console.print(
            f"[bold red]Error:[/bold red] SSH never came up on: {', '.join(not_ready)}"
        )
        sys.exit(1)

    # Root-disk hosts (contabo / ovh_3az have no attachable volume): data,
    # heaptrack and core dumps live under /mnt/ant-node-storage on the root disk.
    root_disk_ips = _resolve_root_disk_ips(vm_ips, all_ips)

    # Fold node telegraf forwarders in whenever a backend exists: InfluxDB host
    # metrics (--monitoring) and/or the dedicated ES node-log forwarder
    # (--forward-logs). A debug build runs at --log-level debug, so
    # _resolve_node_drop_log_levels yields "TRACE" (keep DEBUG, drop TRACE) and
    # the DEBUG logs actually reach ES.
    extra_op_providers = []
    monitoring_on = bool(getattr(args, "monitoring", False)) and _monitoring_has_influx(
        args.name
    )
    elastic_on = bool(getattr(args, "forward_logs", False)) and _elastic_has_creds(
        args.name
    )
    if monitoring_on or elastic_on:
        from saorsa_deploy.cmd.telegraf_fold import build_node_telegraf_providers

        extra_op_providers = build_node_telegraf_providers(
            args.name,
            host_ips=all_ips,
            host_role="WAN_NODE",
            node_count=args.node_count,
            is_genesis=False,
            ssh_key_path=args.ssh_key_path,
            ssh_user="root",
            drop_log_levels=_resolve_node_drop_log_levels(args),
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
        # Prod fleet units: arbitrum-one (no --evm-network), Restart=always,
        # 10s back-off, LimitNOFILE=65536.
        "evm_network": None,
        "restart_policy": "always",
        "restart_sec": 10,
        "limit_nofile": 65536,
        "heaptrack": args.heaptrack,
        "root_disk_ips": root_disk_ips,
        # The crash-debug knobs. COLORBT_SHOW_HIDDEN is always on for this kind:
        # ant-node's color-backtrace filters every frame otherwise (prints
        # <empty backtrace>), so a bare RUST_BACKTRACE yields nothing useful.
        "rust_backtrace": args.rust_backtrace,
        "coredumps": args.coredumps,
        "colorbt_show_hidden": True,
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


def _install_coredump_logprune(args, console: Console) -> None:
    """Install a daily systemd timer pruning core dumps older than N days.

    ``--coredump-retention-days 0`` keeps them forever (no timer).
    """
    from pyinfra.api import Config, Inventory, State
    from pyinfra.api.connect import connect_all, disconnect_all
    from pyinfra.api.operation import add_op
    from pyinfra.api.operations import run_ops
    from pyinfra.operations import server

    if args.coredump_retention_days <= 0:
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
        f"[dim]Installing core-dump prune timer "
        f"(delete cores older than {args.coredump_retention_days}d).[/dim]"
    )
    service_unit = (
        "[Unit]\n"
        "Description=Prune old ant-node core dumps\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart=/usr/bin/find {COREDUMP_DIR} -type f -name 'core.*' "
        f"-mtime +{args.coredump_retention_days} -delete\n"
    )
    timer_unit = (
        "[Unit]\n"
        "Description=Daily ant-node core-dump prune\n\n"
        "[Timer]\n"
        "OnCalendar=daily\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    commands = [
        f"cat > /etc/systemd/system/ant-node-coredump-logprune.service "
        f"<< 'SVC_EOF'\n{service_unit}SVC_EOF",
        f"cat > /etc/systemd/system/ant-node-coredump-logprune.timer "
        f"<< 'TMR_EOF'\n{timer_unit}TMR_EOF",
        "systemctl daemon-reload",
        "systemctl enable --now ant-node-coredump-logprune.timer",
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
            name="Install core-dump prune timer",
            commands=commands,
        )
        run_ops(state_obj)
    finally:
        disconnect_all(state_obj)


def cmd_prod_debug_nodes_deploy(args):
    """Deploy crash-debug ant-node VMs against the production Autonomi network."""
    console = Console()

    if args.provider not in _PROVIDER_INFO:
        console.print(
            f"[bold red]Error:[/bold red] Unknown provider '{args.provider}'. "
            f"Choose one of: {', '.join(sorted(_PROVIDER_INFO))}."
        )
        sys.exit(1)

    console.print(f"[bold]prod debug-nodes deploy: {args.name}[/bold]")
    console.print(f"  Provider/region: {args.provider}/{args.region}")
    console.print(f"  VMs: {args.vm_count} × {args.node_count} node services")
    console.print(f"  Log level: {args.log_level}")
    console.print(f"  RUST_BACKTRACE: {args.rust_backtrace or 'off'}")
    console.print(f"  Core dumps: {'on' if args.coredumps else 'off'}")
    console.print(f"  Monitoring: {args.monitoring_provider if args.monitoring else 'off'}")
    console.print(
        f"  Log forwarding (dedicated ES): "
        f"{args.elastic_provider if args.forward_logs else 'off'}"
    )
    console.print()

    deploy_args_payload = {
        "kind": "prod-debug-nodes",
        "prod_bootstraps_url": PROD_BOOTSTRAPS_URL,
        "provider": args.provider,
        "region": args.region,
        "vm_size": args.vm_size or _PROVIDER_INFO[args.provider][2],
        "vm_count": args.vm_count,
        "node_count": args.node_count,
        "log_level": args.log_level,
        "rust_backtrace": args.rust_backtrace,
        "coredumps": args.coredumps,
        "coredump_retention_days": args.coredump_retention_days,
        "heaptrack": args.heaptrack,
        "monitoring": args.monitoring,
        "monitoring_provider": args.monitoring_provider if args.monitoring else None,
        "monitoring_name": args.name if args.monitoring else None,
        "influx_retention_days": args.influx_retention_days if args.monitoring else None,
        "forward_logs": args.forward_logs,
        "elastic_provider": args.elastic_provider if args.forward_logs else None,
        "elastic_name": args.name if args.forward_logs else None,
        "elastic_retention_days": (
            args.elastic_retention_days if args.forward_logs else None
        ),
        "rewards_address": args.rewards_address,
        "purpose": args.purpose,
        "node_binary": build_binary_descriptor(
            args.branch_name,
            args.repo_owner,
            args.node_version,
            debug=getattr(args, "debug", False),
        ),
    }
    record_deployment_created(args.name, deploy_args_payload)
    record_event(args.name, "prod_debug_nodes_deploy_started", deploy_args_payload)

    try:
        _seed_prod_bootstraps(args, console)
        _ensure_node_infra(args, console)
        if args.monitoring:
            _ensure_monitoring(args, console)
        if args.forward_logs:
            _ensure_elastic(args, console)
        _provision_debug_nodes(args, console)

        from saorsa_deploy.cmd.start_nodes import cmd_start_nodes

        cmd_start_nodes(
            SimpleNamespace(
                name=args.name, ssh_key_path=args.ssh_key_path, region=None
            )
        )

        if args.coredumps:
            _install_coredump_logprune(args, console)

        record_deploy_args_update(args.name, {"nodes_per_vm": args.node_count})
    except SystemExit:
        record_event(args.name, "prod_debug_nodes_deploy_failed")
        record_status(args.name, DeploymentStatus.FAILED)
        raise
    except Exception as e:
        console.print(f"[bold red]Deploy failed:[/bold red] {e}")
        record_event(args.name, "prod_debug_nodes_deploy_failed", {"error": str(e)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    record_event(args.name, "prod_debug_nodes_deploy_completed")
    record_status(args.name, DeploymentStatus.RUNNING)
    console.print()
    console.print(
        f"[bold green]prod debug-nodes '{args.name}' are running.[/bold green]"
    )
    if args.coredumps:
        console.print(
            f"[dim]core dumps: ssh root@<vm> ls {COREDUMP_DIR}/ "
            "(core.<exe>.<pid>.<time>). scp them off for analysis.[/dim]"
        )


def cmd_prod_debug_nodes_destroy(args):
    """Destroy a prod crash-debug-nodes deployment (VMs + monitoring + ES)."""
    from saorsa_deploy.cmd.contabo_infra import (
        destroy_contabo_elastic,
        destroy_contabo_monitoring,
        destroy_contabo_nodes,
    )
    from saorsa_deploy.cmd.destroy_monitoring import cmd_destroy_monitoring
    from saorsa_deploy.cmd.destroy_nodes import cmd_destroy_nodes
    from saorsa_deploy.cmd.prod_log_forwarding import destroy_client_elastic

    console = Console()
    console.print(f"[bold]prod debug-nodes destroy: {args.name}[/bold]")

    record_event(args.name, "prod_debug_nodes_destroy_started")
    record_status(args.name, DeploymentStatus.DESTROYING)

    try:
        console.print("[bold cyan]Destroying node VMs...[/bold cyan]")
        # Contabo instances are API-managed (no tfstate); cancel them via the API.
        # destroy_contabo_nodes deletes the deployment state itself when it applies.
        if not destroy_contabo_nodes(args.name, console):
            cmd_destroy_nodes(SimpleNamespace(name=args.name, force=True))
            # The seeded prod bootstraps are not real infra, so remove state.
            try:
                delete_deployment_state(args.name)
            except Exception:
                pass

        if args.keep_monitoring:
            console.print(
                "[dim]Leaving monitoring stack intact (--keep-monitoring).[/dim]"
            )
        elif destroy_contabo_monitoring(args.name, console):
            pass  # handled via the Contabo API
        elif _monitoring_stack_exists(args.name):
            console.print("[bold cyan]Destroying monitoring stack...[/bold cyan]")
            cmd_destroy_monitoring(SimpleNamespace(name=args.name, force=True))
        else:
            console.print(
                f"[dim]No monitoring stack found for '{args.name}' — skipping.[/dim]"
            )

        if getattr(args, "keep_elastic", False):
            console.print("[dim]Leaving Elasticsearch intact (--keep-elastic).[/dim]")
        elif not destroy_contabo_elastic(args.name, console):
            destroy_client_elastic(args.name, False, console)
    except SystemExit:
        record_event(args.name, "prod_debug_nodes_destroy_failed")
        record_status(args.name, DeploymentStatus.FAILED)
        raise
    except Exception as e:
        console.print(f"[bold red]Destroy failed:[/bold red] {e}")
        record_event(args.name, "prod_debug_nodes_destroy_failed", {"error": str(e)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    record_event(args.name, "prod_debug_nodes_destroy_completed")
    record_status(args.name, DeploymentStatus.DESTROYED)
    console.print()
    console.print(f"[bold green]prod debug-nodes '{args.name}' destroyed.[/bold green]")
