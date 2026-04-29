"""Higher-level orchestration of a prod continuous-downloader deployment.

A prod continuous-downloader is a single-VM client deployment that runs
``ant-downloader`` against the **production** Autonomi network, seeded with a
curated static list of public file addresses. It ships with its own monitoring
VM (InfluxDB + Grafana) so the operator can measure production download
performance continuously.

This command is a thin orchestrator: it does not re-implement any of the
underlying infra / provisioning logic, it imports the existing ``cmd_*``
functions and calls them with a constructed args namespace. The
``prod continuous-downloader deploy`` and ``prod continuous-downloader destroy``
subcommands are the two entry points.
"""

import sys
from types import SimpleNamespace

from rich.console import Console
from saorsa_db.models import DeploymentStatus

from saorsa_deploy.binary_descriptor import build_binary_descriptor
from saorsa_deploy.prod_addresses import get_default_prod_addresses_path
from saorsa_deploy.prod_bootstraps import PROD_BOOTSTRAPS_URL
from saorsa_deploy.registry import (
    record_deployment_created,
    record_event,
    record_status,
)
from saorsa_deploy.state import load_client_state, load_monitoring_state


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


MONITORING_REGION = "lon1"


def _ensure_monitoring(args, console: Console) -> None:
    """Ensure a monitoring VM exists and is provisioned."""
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
    """Ensure the single client VM exists via clients infra."""
    from saorsa_deploy.cmd.infra_clients import cmd_infra_clients

    if _client_stack_exists(args.name):
        console.print(
            f"[dim]Client stack '{args.name}' already exists — "
            "skipping clients infra.[/dim]"
        )
        return

    console.print(f"[bold cyan]Step 3/5: creating downloader VM ({args.spec})[/bold cyan]")
    infra_args = SimpleNamespace(
        name=args.name,
        uploader=[],
        downloader=[args.spec],
        force=False,
    )
    cmd_infra_clients(infra_args)


def _provision_clients(args, addresses_file: str, console: Console) -> None:
    from saorsa_deploy.cmd.provision_clients import cmd_provision_clients

    console.print(
        "[bold cyan]Step 4/5: provisioning ant-downloader on client VMs[/bold cyan]"
    )
    prov_args = SimpleNamespace(
        name=args.name,
        branch_name=args.branch_name,
        repo_owner=args.repo_owner,
        ant_version=args.ant_version,
        influx_url=None,
        influx_token=None,
        influx_org="ant",
        influx_bucket="testnet-metrics",
        monitoring_name=args.name,
        wallet_secret_key=None,
        ssh_key_path=args.ssh_key_path,
        addresses_file=addresses_file,
        prod=True,
    )
    cmd_provision_clients(prov_args)


def _start_clients(args, console: Console) -> None:
    from saorsa_deploy.cmd.start_clients import cmd_start_clients

    console.print("[bold cyan]Step 5/5: starting ant-downloader services[/bold cyan]")
    start_args = SimpleNamespace(
        name=args.name,
        ssh_key_path=args.ssh_key_path,
    )
    cmd_start_clients(start_args)


def cmd_prod_continuous_downloader_deploy(args):
    """Deploy a prod continuous-downloader testbed against the Autonomi prod network."""
    console = Console()

    addresses_file = args.addresses_file or get_default_prod_addresses_path()
    console.print(f"[bold]prod continuous-downloader deploy: {args.name}[/bold]")
    console.print(f"  Addresses file: {addresses_file}")
    console.print(f"  Prod bootstraps URL: {PROD_BOOTSTRAPS_URL}")
    console.print(f"  Downloader spec: {args.spec}")
    console.print()

    deploy_args_payload = {
        "kind": "prod-continuous-downloader",
        "prod_bootstraps_url": PROD_BOOTSTRAPS_URL,
        "addresses_file": addresses_file,
        "monitoring_name": args.name,
        "spec": args.spec,
        "purpose": args.purpose,
        "ant_binary": build_binary_descriptor(
            args.branch_name, args.repo_owner, args.ant_version
        ),
    }
    record_deployment_created(args.name, deploy_args_payload)
    record_event(args.name, "prod_downloader_deploy_started", deploy_args_payload)

    try:
        _ensure_monitoring(args, console)
        _ensure_client_infra(args, console)
        _provision_clients(args, addresses_file, console)
        _start_clients(args, console)
    except SystemExit:
        record_event(args.name, "prod_downloader_deploy_failed")
        record_status(args.name, DeploymentStatus.FAILED)
        raise
    except Exception as e:
        console.print(f"[bold red]Deploy failed:[/bold red] {e}")
        record_event(args.name, "prod_downloader_deploy_failed", {"error": str(e)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    record_event(args.name, "prod_downloader_deploy_completed")
    record_status(args.name, DeploymentStatus.RUNNING)
    console.print()
    console.print(
        f"[bold green]prod continuous-downloader '{args.name}' is running.[/bold green]"
    )
    console.print(
        f"[dim]Record download metrics with: saorsa-deploy deployments record-file-results "
        f"--name {args.name} --monitoring-name {args.name} --window -1h[/dim]"
    )


def cmd_prod_continuous_downloader_destroy(args):
    """Destroy a prod continuous-downloader testbed."""
    from saorsa_deploy.cmd.destroy_clients import cmd_destroy_clients
    from saorsa_deploy.cmd.destroy_monitoring import cmd_destroy_monitoring
    from saorsa_deploy.cmd.stop_clients import cmd_stop_clients

    console = Console()
    console.print(f"[bold]prod continuous-downloader destroy: {args.name}[/bold]")

    record_event(args.name, "prod_downloader_destroy_started")
    record_status(args.name, DeploymentStatus.DESTROYING)

    try:
        if _client_stack_exists(args.name):
            console.print("[bold cyan]Stopping ant-downloader services...[/bold cyan]")
            try:
                cmd_stop_clients(SimpleNamespace(
                    name=args.name,
                    ssh_key_path=args.ssh_key_path,
                ))
            except (SystemExit, Exception) as e:
                # clients stop is best-effort: a half-provisioned VM may have no
                # systemd unit yet, and a dead/unreachable VM will never stop
                # cleanly. Either way we must still proceed to clients destroy
                # so the Terraform-managed VMs get torn down.
                console.print(
                    f"[yellow]clients stop reported a failure ({e!s}); "
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
        record_event(args.name, "prod_downloader_destroy_failed")
        record_status(args.name, DeploymentStatus.FAILED)
        raise
    except Exception as e:
        console.print(f"[bold red]Destroy failed:[/bold red] {e}")
        record_event(args.name, "prod_downloader_destroy_failed", {"error": str(e)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    record_event(args.name, "prod_downloader_destroy_completed")
    record_status(args.name, DeploymentStatus.DESTROYED)
    console.print()
    console.print(
        f"[bold green]prod continuous-downloader '{args.name}' destroyed.[/bold green]"
    )
