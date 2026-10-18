import sys

from rich.console import Console

from saorsa_deploy.cmd.obs_provider import (
    default_grafana_size,
    default_influxdb_size,
    default_region,
    require_ovh_service_name,
    resolve_provider,
    state_key_abbrev,
)
from saorsa_deploy.contabo import require_contabo_env
from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.paths import state_dir
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    check_terraform_state_exists,
    delete_terraform_state,
    load_monitoring_state,
    save_monitoring_state,
    update_monitoring_state,
)
from saorsa_deploy.terraform import TerraformRunConfig


def cmd_monitoring_infra(args):
    """Create monitoring VM infrastructure using Terraform."""
    console = Console()

    provider = resolve_provider(args)
    region = getattr(args, "region", None) or default_region(provider)
    volume_size = getattr(args, "volume_size", None) or 50
    droplet_size = getattr(args, "droplet_size", None) or default_influxdb_size(provider)
    grafana_size = getattr(args, "grafana_size", None) or default_grafana_size(provider)
    # Grafana is opt-in (off by default). The Terraform module still receives a
    # grafana size; it's simply ignored when enable_grafana is false.
    enable_grafana = bool(getattr(args, "grafana", False))
    volume_type = getattr(args, "volume_type", None) or "high-speed"
    keep_on_destroy = bool(getattr(args, "keep_on_destroy", False))

    resources_dir = get_resources_dir()
    tf_source = resources_dir / f"{provider}-monitoring"
    abbrev = state_key_abbrev(provider)
    workspace_base = state_dir() / "workspaces"
    workspace_dir = workspace_base / f"{provider}-monitoring-{args.name}-{region}"
    state_key = f"saorsa-deploy/{abbrev}-monitoring-{args.name}-{region}.tfstate"

    # Idempotent reuse: skip the apply if we already have a monitoring VM
    # recorded for this deployment. Mirrors `bootstraps infra` so re-running
    # a deploy runbook after a mid-pipeline failure doesn't trip on its own
    # previously-provisioned VM.
    try:
        existing = load_monitoring_state(args.name)
    except RuntimeError:
        existing = None

    if existing and existing.get("vm_ip"):
        existing_region = existing.get("region", region)
        if existing_region != region:
            console.print(
                f"[bold red]Error:[/bold red] Monitoring VM for '{args.name}' "
                f"already exists in region '{existing_region}', but the request "
                f"is for '{region}'. Run `monitoring destroy --name {args.name} "
                f"--force` first if you want to recreate in a different region."
            )
            sys.exit(1)
        console.print(
            f"[bold]Reusing existing monitoring VM for '{args.name}' "
            f"({existing['vm_ip']}, {existing_region}).[/bold]"
        )
        # The reuse branch returns before any state write, so the keep-on-destroy
        # intent has to be merged in here too — otherwise it is silently dropped
        # on every re-run and on a resumed `up`, and the deployment's teardown
        # would destroy a stack the manifest asked to keep. The manifest is the
        # desired state, so an omitted flag resets the stored value; report the
        # flip so it is never a silent behaviour change.
        if bool(existing.get("keep_on_destroy", False)) != keep_on_destroy:
            try:
                update_monitoring_state(args.name, {"keep_on_destroy": keep_on_destroy})
                console.print(
                    "[dim]Recorded keep_on_destroy="
                    f"{str(keep_on_destroy).lower()}: `deployments down` will "
                    f"{'leave this stack standing' if keep_on_destroy else 'destroy it'}"
                    ".[/dim]"
                )
            except Exception as e:
                console.print(f"[yellow]Warning: Failed to update monitoring state: {e}[/yellow]")
        return

    if check_terraform_state_exists(state_key):
        if not getattr(args, "force", False):
            console.print("[bold red]Error:[/bold red] Stale Terraform state found:")
            console.print(f"  {state_key}")
            console.print("Use --force to delete and proceed.")
            sys.exit(1)
        console.print(f"[yellow]Warning: Deleting stale Terraform state: {state_key}[/yellow]")
        delete_terraform_state(state_key)

    if provider == "ovh":
        service_name = require_ovh_service_name(console)
        variables = {
            "name": args.name,
            "region": region,
            "service_name": service_name,
            "flavor_name": droplet_size,
            "grafana_flavor_name": grafana_size,
            "attached_volume_size": str(volume_size),
            "volume_type": volume_type,
            "enable_grafana": "true" if enable_grafana else "false",
        }
    elif provider == "contabo":
        require_contabo_env(console)
        image_id = getattr(args, "contabo_image_id", None)
        if not image_id:
            console.print(
                "[bold red]Error:[/bold red] Contabo monitoring requires "
                "--contabo-image-id (an Ubuntu image UUID; resolve via "
                "`cntb get images`)."
            )
            sys.exit(1)
        # Contabo is root-disk only (no volume). droplet_size / grafana_size
        # carry Contabo product ids here (from obs_provider defaults or --*-size).
        variables = {
            "name": args.name,
            "region": region,
            "product_id": droplet_size,
            "grafana_product_id": grafana_size,
            "image_id": image_id,
            "ssh_public_key_path": getattr(
                args, "contabo_ssh_public_key_path", "~/.ssh/id_rsa.pub"
            ),
            "enable_grafana": "true" if enable_grafana else "false",
        }
    else:
        variables = {
            "name": args.name,
            "region": region,
            "volume_size": str(volume_size),
            "droplet_size": droplet_size,
            "grafana_droplet_size": grafana_size,
            "enable_grafana": "true" if enable_grafana else "false",
        }

    config = TerraformRunConfig(
        provider=provider,
        region=region,
        tf_source_dir=tf_source,
        workspace_dir=workspace_dir,
        state_key=state_key,
        variables=variables,
    )

    grafana_desc = grafana_size if enable_grafana else "disabled"
    console.print(
        f"[bold]Creating monitoring VMs for '{args.name}' on {provider}/{region} "
        f"(InfluxDB: {droplet_size}, Grafana: {grafana_desc}, "
        f"volume: {volume_size}GB)...[/bold]"
    )
    console.print()

    results = execute_terraform_runs([config])

    if not results[0].success:
        console.print("[bold red]Monitoring infrastructure creation failed.[/bold red]")
        console.print(results[0].stderr)
        sys.exit(1)

    monitoring_ip = results[0].outputs.get("monitoring_ip", "")
    grafana_ip = results[0].outputs.get("grafana_ip", "") if enable_grafana else ""
    grafana_report = grafana_ip if enable_grafana else "disabled"
    console.print(
        f"[bold green]Monitoring VMs created — InfluxDB: {monitoring_ip}, "
        f"Grafana: {grafana_report}[/bold green]"
    )

    try:
        save_monitoring_state(
            name=args.name,
            region=region,
            vm_ip=monitoring_ip,
            grafana_ip=grafana_ip,
            provider=provider,
            grafana_enabled=enable_grafana,
            keep_on_destroy=keep_on_destroy,
        )
        console.print("[dim]Monitoring state saved to S3.[/dim]")
        if keep_on_destroy:
            console.print(
                "[dim]keep_on_destroy is set: `deployments down` will leave this "
                "stack standing. Clean it up with `monitoring destroy --name "
                f"{args.name} --force`.[/dim]"
            )
    except Exception as e:
        console.print(f"[bold red]Error: Failed to save monitoring state: {e}[/bold red]")
        console.print("[bold red]The monitoring VM was created but state was not saved.[/bold red]")
        console.print(
            "[bold red]The VM must be destroyed manually to avoid orphaned resources.[/bold red]"
        )
        sys.exit(1)

    console.print()
    console.print(
        f"[dim]Next step: saorsa-deploy monitoring provision "
        f"--name {args.name} --ssh-key-path ~/.ssh/id_rsa[/dim]"
    )
