"""Higher-level orchestration of a prod (one-shot) downloaders deployment.

This is the one-shot sibling of ``prod continuous-downloader``: every
client VM runs the same ``prod_downloader.py`` service script, but the
service walks the address list a single time and then exits cleanly. No
inter-cycle sleep, no relooping.

Apart from the one-shot toggle (which propagates into the systemd unit's
``Restart=`` and the script's exit-after-cycle behaviour via the
``ONE_SHOT`` env var), the deploy / destroy flow mirrors
``prod continuous-downloader`` step-for-step so operators get the same
monitoring + addresses-handling story.
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
from saorsa_deploy.cmd.provision_clients import (
    _resolve_addresses_file_content,
    _resolve_ant_binary,
    parse_addresses,
)
from saorsa_deploy.prod_addresses import get_default_prod_addresses_path
from saorsa_deploy.prod_bootstraps import (
    PROD_BOOTSTRAPS_URL,
    ProdBootstrapsError,
    fetch_prod_bootstraps,
)
from saorsa_deploy.provisioning.clock_skew import parse_skew_duration
from saorsa_deploy.provisioning.prod_downloader import (
    SERVICE_NAME as PROD_DOWNLOADER_SERVICE_NAME,
)
from saorsa_deploy.provisioning.prod_downloader import (
    ProdDownloaderProvisioner,
)
from saorsa_deploy.registry import (
    record_deployment_created,
    record_event,
    record_status,
)
from saorsa_deploy.ssh import clear_known_hosts, wait_for_ssh
from saorsa_deploy.state import load_client_state, load_monitoring_state

MONITORING_REGION = "lon1"


def _monitoring_stack_exists(name: str) -> bool:
    try:
        load_monitoring_state(name)
        return True
    except RuntimeError:
        return False


def _client_stack_exists(name: str) -> bool:
    try:
        state = load_client_state(name)
        return bool(state.get("clients"))
    except RuntimeError:
        return False


def _ensure_monitoring(args, console: Console) -> None:
    from saorsa_deploy.cmd.monitoring_infra import cmd_monitoring_infra
    from saorsa_deploy.cmd.provision_monitoring import cmd_provision_monitoring

    if _monitoring_stack_exists(args.name):
        console.print(
            f"[dim]Monitoring stack '{args.name}' already exists — "
            "skipping monitoring infra / monitoring provision.[/dim]"
        )
        return

    console.print("[bold cyan]Step 1/5: creating monitoring VM[/bold cyan]")
    mon_infra_args = SimpleNamespace(
        name=args.name,
        region=MONITORING_REGION,
        volume_size=args.monitoring_volume_size,
        # A single client VM forwards little; keep the InfluxDB box small
        # rather than the larger testnet default.
        droplet_size="s-2vcpu-4gb",
        force=False,
    )
    cmd_monitoring_infra(mon_infra_args)

    console.print("[bold cyan]Step 2/5: provisioning InfluxDB + Grafana[/bold cyan]")
    mon_prov_args = SimpleNamespace(
        name=args.name,
        ssh_key_path=args.ssh_key_path,
    )
    cmd_provision_monitoring(mon_prov_args)


def _ensure_client_infra(args, console: Console) -> None:
    from saorsa_deploy.cmd.infra_clients import cmd_infra_clients

    if _client_stack_exists(args.name):
        console.print(
            f"[dim]Client stack '{args.name}' already exists — "
            "skipping clients infra.[/dim]"
        )
        return

    console.print(f"[bold cyan]Step 3/5: creating downloader VM ({args.spec})[/bold cyan]")
    # prod downloaders are Digital Ocean only (--spec carries no provider
    # field); prefix the provider parse_downloader_spec now requires.
    # bootstrap_cache is carried through so client_state records the real
    # per-VM cache mode (the print/slack surfaces read it from there).
    cache_token = (
        f"bootstrap_cache={str(getattr(args, 'bootstrap_cache', True)).lower()}"
    )
    infra_args = SimpleNamespace(
        name=args.name,
        uploader=[],
        downloader=[f"digital_ocean:{args.spec}:{cache_token}"],
        force=False,
    )
    cmd_infra_clients(infra_args)


def _provision_prod_downloader(
    args,
    addresses_file: str,
    console: Console,
) -> None:
    """Install the prod_downloader.py service in one-shot mode."""
    console.print(
        "[bold cyan]Step 4/5: provisioning prod-downloader (one-shot) "
        "service on client VM(s)[/bold cyan]"
    )

    try:
        client_state = load_client_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    clients = client_state.get("clients", [])
    if not clients:
        console.print(
            "[bold red]Error:[/bold red] No clients found in client state."
        )
        sys.exit(1)

    if any(c.get("role", "upload") == "upload" for c in clients):
        console.print(
            "[bold red]Error:[/bold red] prod downloaders does not support "
            "upload-role clients. Re-run clients infra with --downloader "
            "specs only."
        )
        sys.exit(1)

    addresses_args = SimpleNamespace(addresses_file=addresses_file)
    addresses_file_content = _resolve_addresses_file_content(
        addresses_args, console
    )
    if not addresses_file_content:
        console.print(
            "[bold red]Error:[/bold red] Addresses file is empty or missing."
        )
        sys.exit(1)

    console.print("[bold]Fetching production bootstrap peers...[/bold]")
    try:
        bootstrap_peers = fetch_prod_bootstraps()
    except ProdBootstrapsError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)
    console.print(f"  Using {len(bootstrap_peers)} production peers.")

    binary_args = SimpleNamespace(
        name=args.name,
        branch_name=args.branch_name,
        repo_owner=args.repo_owner,
        ant_version=args.ant_version,
    )
    binary_url, binary_is_archive = _resolve_ant_binary(binary_args, console)

    influx_url = ""
    influx_token = ""
    influx_org = "ant"
    influx_bucket = "testnet-metrics"
    try:
        mon_state = load_monitoring_state(args.name)
        influx_url = mon_state.get("influxdb_url", "") or ""
        influx_token = mon_state.get("influxdb_token", "") or ""
        influx_org = mon_state.get("influxdb_org", "ant") or "ant"
        influx_bucket = (
            mon_state.get("influxdb_bucket", "testnet-metrics")
            or "testnet-metrics"
        )
    except RuntimeError:
        console.print(
            "[yellow]No monitoring state found — Influx metric emission "
            "will be skipped.[/yellow]"
        )

    downloaders = [
        {
            "ip": c["ip"],
            "region": c.get("region", "unknown"),
            "addresses_file_content": addresses_file_content,
            "clock_skew": _resolve_clock_skew(args),
            "bootstrap_cache": getattr(args, "bootstrap_cache", True),
        }
        for c in clients
        if c.get("ip")
    ]

    host_ips = [d["ip"] for d in downloaders]
    clear_known_hosts(host_ips, console)
    not_ready = wait_for_ssh(host_ips, args.ssh_key_path, console=console)
    if not_ready:
        console.print(
            f"[bold red]Error:[/bold red] SSH never came up on: "
            f"{', '.join(not_ready)}"
        )
        sys.exit(1)

    provisioner = ProdDownloaderProvisioner(
        downloaders=downloaders,
        bootstrap_peers=bootstrap_peers,
        sleep_interval_secs=0,
        ssh_key_path=args.ssh_key_path,
        binary_url=binary_url,
        binary_is_archive=binary_is_archive,
        influx_url=influx_url,
        influx_token=influx_token,
        influx_org=influx_org,
        influx_bucket=influx_bucket,
        one_shot=True,
        console=console,
    )
    provisioner.execute()


def _start_prod_downloader(args, console: Console) -> None:
    console.print(
        "[bold cyan]Step 5/5: starting prod-downloader (one-shot) service[/bold cyan]"
    )
    _systemctl_action(
        args,
        console,
        action_label="Enable and start prod-downloader service",
        commands=[f"systemctl enable --now {PROD_DOWNLOADER_SERVICE_NAME}"],
    )


def _stop_prod_downloader(args, console: Console) -> None:
    console.print(
        "[bold cyan]Stopping ant-prod-downloader services...[/bold cyan]"
    )
    _systemctl_action(
        args,
        console,
        action_label="Stop and disable prod-downloader service",
        commands=[
            f"systemctl stop {PROD_DOWNLOADER_SERVICE_NAME} || true",
            f"systemctl disable {PROD_DOWNLOADER_SERVICE_NAME} || true",
        ],
    )


def _systemctl_action(
    args,
    console: Console,
    *,
    action_label: str,
    commands: list[str],
) -> None:
    try:
        client_state = load_client_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    host_ips = [c["ip"] for c in client_state.get("clients", []) if c.get("ip")]
    if not host_ips:
        console.print(
            "[bold red]Error:[/bold red] No client IPs found in state."
        )
        sys.exit(1)

    clear_known_hosts(host_ips, console)
    hosts_data = [
        (ip, {"ssh_user": "root", "ssh_key": args.ssh_key_path})
        for ip in host_ips
    ]
    state = State(inventory=Inventory((hosts_data, {})), config=Config())
    connect_all(state)

    try:
        add_op(state, server.shell, name=action_label, commands=commands)
        run_ops(state)
    finally:
        disconnect_all(state)

    failed = state.failed_hosts
    if failed:
        for host in failed:
            console.print(f"  [red]Failed: {host.name}[/red]")
        sys.exit(1)


def _resolve_clock_skew(args):
    """Resolve ``--clock-skew DURATION`` to signed seconds (or None when unset).

    Raises :class:`ValueError` for a malformed duration.
    """
    raw = getattr(args, "clock_skew", None)
    if not raw:
        return None
    return parse_skew_duration(raw)


def cmd_prod_downloaders_deploy(args):
    """Deploy a one-shot prod downloaders testbed against the Autonomi prod network."""
    console = Console()

    try:
        clock_skew_seconds = _resolve_clock_skew(args)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    addresses_file = args.addresses_path or get_default_prod_addresses_path()
    addresses_content = _resolve_addresses_file_content(
        SimpleNamespace(addresses_file=addresses_file), console
    )
    if not addresses_content:
        console.print(
            "[bold red]Error:[/bold red] Addresses file is empty or missing."
        )
        sys.exit(1)
    try:
        addresses_list = parse_addresses(addresses_content)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    console.print(f"[bold]prod downloaders deploy: {args.name}[/bold]")
    console.print(f"  Addresses file: {addresses_file}")
    console.print(f"  Prod bootstraps URL: {PROD_BOOTSTRAPS_URL}")
    console.print(f"  Downloader spec: {args.spec}")
    console.print("  Mode: one-shot (single pass, then exit)")
    console.print()

    deploy_args_payload = {
        "kind": "prod-downloaders",
        "prod_bootstraps_url": PROD_BOOTSTRAPS_URL,
        "addresses_file": addresses_file,
        "addresses": addresses_list,
        "monitoring_name": args.name,
        "spec": args.spec,
        "clock_skew": clock_skew_seconds,
        "bootstrap_cache": getattr(args, "bootstrap_cache", True),
        "purpose": args.purpose,
        "ant_binary": build_binary_descriptor(
            args.branch_name, args.repo_owner, args.ant_version
        ),
    }
    record_deployment_created(args.name, deploy_args_payload)
    record_event(args.name, "prod_downloaders_deploy_started", deploy_args_payload)

    try:
        _ensure_monitoring(args, console)
        _ensure_client_infra(args, console)
        _provision_prod_downloader(args, addresses_file, console)
        _start_prod_downloader(args, console)
        from saorsa_deploy.cmd.provision_telegraf_metrics import (
            provision_client_telegraf_best_effort,
        )
        provision_client_telegraf_best_effort(
            args.name, args.ssh_key_path, console=console
        )
    except SystemExit:
        record_event(args.name, "prod_downloaders_deploy_failed")
        record_status(args.name, DeploymentStatus.FAILED)
        raise
    except Exception as e:
        console.print(f"[bold red]Deploy failed:[/bold red] {e}")
        record_event(args.name, "prod_downloaders_deploy_failed", {"error": str(e)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    record_event(args.name, "prod_downloaders_deploy_completed")
    record_status(args.name, DeploymentStatus.RUNNING)
    console.print()
    console.print(
        f"[bold green]prod downloaders '{args.name}' is running.[/bold green]"
    )
    console.print(
        "[dim]Per-op logs: ssh root@<vm> ls /var/log/ant-prod-downloader/[/dim]"
    )
    console.print(
        f"[dim]Record download metrics with: saorsa-deploy deployments record-results "
        f"--name {args.name} --monitoring-name {args.name} --window -1h[/dim]"
    )


def cmd_prod_downloaders_destroy(args):
    """Destroy a prod downloaders testbed."""
    from saorsa_deploy.cmd.destroy_clients import cmd_destroy_clients
    from saorsa_deploy.cmd.destroy_monitoring import cmd_destroy_monitoring

    console = Console()
    console.print(f"[bold]prod downloaders destroy: {args.name}[/bold]")

    record_event(args.name, "prod_downloaders_destroy_started")
    record_status(args.name, DeploymentStatus.DESTROYING)

    try:
        if _client_stack_exists(args.name):
            try:
                _stop_prod_downloader(args, console)
            except (SystemExit, Exception) as e:
                console.print(
                    f"[yellow]prod-downloader stop reported a failure ({e!s}); "
                    "continuing with destroy.[/yellow]"
                )

            console.print("[bold cyan]Destroying client VMs...[/bold cyan]")
            cmd_destroy_clients(SimpleNamespace(name=args.name, force=True))
        else:
            console.print(
                f"[dim]No client stack found for '{args.name}' — skipping client destroy.[/dim]"
            )

        if args.keep_monitoring:
            console.print(
                "[dim]Leaving monitoring stack intact "
                "(--keep-monitoring); metrics remain queryable.[/dim]"
            )
        elif _monitoring_stack_exists(args.name):
            console.print("[bold cyan]Destroying monitoring stack...[/bold cyan]")
            cmd_destroy_monitoring(SimpleNamespace(name=args.name, force=True))
        else:
            console.print(
                f"[dim]No monitoring stack found for '{args.name}' — skipping.[/dim]"
            )
    except SystemExit:
        record_event(args.name, "prod_downloaders_destroy_failed")
        record_status(args.name, DeploymentStatus.FAILED)
        raise
    except Exception as e:
        console.print(f"[bold red]Destroy failed:[/bold red] {e}")
        record_event(args.name, "prod_downloaders_destroy_failed", {"error": str(e)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    record_event(args.name, "prod_downloaders_destroy_completed")
    record_status(args.name, DeploymentStatus.DESTROYED)
    console.print()
    console.print(
        f"[bold green]prod downloaders '{args.name}' destroyed.[/bold green]"
    )
