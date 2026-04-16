import shutil
import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.bootstrap import (
    destroy_bootstrap_vms,
    list_stale_bootstrap_state_keys,
    purge_stale_bootstrap_state,
)
from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.providers import PROVIDERS
from saorsa_deploy.registry import record_event, record_status
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    delete_deployment_state,
    delete_terraform_state,
    list_terraform_state_keys,
    load_deployment_state,
)
from saorsa_deploy.terraform import TerraformRunConfig


def cmd_destroy(args):
    """Execute the destroy command: tear down all infrastructure for a deployment."""
    console = Console()

    # Load deployment state from S3
    console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
    try:
        state = load_deployment_state(args.name)
    except RuntimeError:
        _destroy_without_deployment_state(args.name, args.force, console)
        return

    region_pairs = [(r[0], r[1]) for r in state["regions"]]
    terraform_variables = state["terraform_variables"]

    console.print(f"[dim]Found {len(region_pairs)} region(s) to destroy.[/dim]")
    for provider, region in region_pairs:
        console.print(f"  [dim]{provider}/{region}[/dim]")
    console.print()

    # Confirmation prompt
    if not args.force:
        confirm = input(
            f"This will destroy all infrastructure for '{args.name}'. Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            console.print("[yellow]Aborted.[/yellow]")
            sys.exit(0)
        console.print()

    from saorsa_db.models import DeploymentStatus

    record_event(args.name, "destroy_started")
    record_status(args.name, DeploymentStatus.DESTROYING)

    # Build Terraform configs for each region
    resources_dir = get_resources_dir()
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"

    # Extract per-provider vm_count from state
    providers_config = terraform_variables.get("providers", {})
    # Fallback for old-format state with flat vm_count
    fallback_vm_count = terraform_variables.get("vm_count")

    configs = []
    for provider_name, region in region_pairs:
        provider = PROVIDERS[provider_name]
        tf_source = resources_dir / provider.tf_dir
        name = terraform_variables["name"]
        workspace_dir = workspace_base / f"{provider_name}-{name}-{region}"
        state_key = f"{provider.state_key_prefix}-{name}-{region}.tfstate"

        if provider_name in providers_config:
            vm_count = str(providers_config[provider_name]["vm_count"])
        elif fallback_vm_count:
            vm_count = str(fallback_vm_count)
        else:
            console.print(
                f"[bold red]Error:[/bold red] Cannot determine vm_count for {provider_name}"
            )
            sys.exit(1)

        variables = {
            "name": name,
            "region": region,
            "vm_count": vm_count,
            "attached_volume_size": terraform_variables.get("attached_volume_size", "20"),
        }
        if provider_name in providers_config:
            p_cfg = providers_config[provider_name]
            if provider_name == "digitalocean":
                variables["droplet_size"] = p_cfg.get("droplet_size", "s-2vcpu-4gb")
            elif provider_name == "hetzner":
                variables["server_type"] = p_cfg.get("server_type", "cpx22")
            elif provider_name == "vultr":
                variables["plan"] = p_cfg.get("plan", "vc2-2c-4gb")

        config = TerraformRunConfig(
            provider=provider_name,
            region=region,
            tf_source_dir=tf_source,
            workspace_dir=workspace_dir,
            state_key=state_key,
            variables=variables,
        )
        configs.append(config)

    if configs:
        # Run terraform destroy in parallel
        console.print(f"[bold]Destroying infrastructure across {len(configs)} region(s)...[/bold]")
        console.print()

        results = execute_terraform_runs(configs, action="destroy")

        failures = [r for r in results if not r.success]
        if failures:
            console.print(
                f"[bold red]{len(failures)} region(s) failed to destroy.[/bold red]"
            )
            console.print(
                "[yellow]Bootstrap VM was NOT destroyed due to Terraform failures.[/yellow]"
            )
            record_event(args.name, "destroy_failed", {"failures": len(failures)})
            record_status(args.name, DeploymentStatus.FAILED)
            sys.exit(1)

        console.print(
            f"[bold green]All {len(results)} region(s) destroyed successfully.[/bold green]"
        )
        console.print()

        # Delete Terraform state files from S3
        for config in configs:
            try:
                delete_terraform_state(config.state_key)
                console.print(f"[dim]Deleted Terraform state: {config.state_key}[/dim]")
            except Exception as e:
                console.print(
                    f"[yellow]Warning: Failed to delete Terraform state "
                    f"{config.state_key}: {e}[/yellow]"
                )

    # Destroy bootstrap VM(s) via Terraform
    bootstrap_nodes = state.get("bootstrap_nodes", [])
    if bootstrap_nodes:
        bootstrap_regions = [n["region"] for n in bootstrap_nodes]
    else:
        # Legacy fallback: single bootstrap in lon1
        bootstrap_regions = ["lon1"]
    console.print(
        f"[bold]Destroying {len(bootstrap_regions)} bootstrap VM(s)...[/bold]"
    )
    try:
        volume_size = int(terraform_variables.get("attached_volume_size", "20"))
        destroy_bootstrap_vms(
            args.name,
            regions=bootstrap_regions,
            volume_size=volume_size,
            console=console,
        )
        console.print("[green]Bootstrap VM(s) destroyed.[/green]")
    except Exception as e:
        console.print(f"[bold red]Failed to destroy bootstrap VMs:[/bold red] {e}")
        sys.exit(1)

    # Delete deployment state from S3
    try:
        delete_deployment_state(args.name)
        console.print("[dim]Deployment state removed from S3.[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to delete deployment state: {e}[/yellow]")

    # Clean up local workspace directories
    for config in configs:
        if config.workspace_dir.exists():
            shutil.rmtree(config.workspace_dir)

    record_event(args.name, "destroyed")
    record_status(args.name, DeploymentStatus.DESTROYED)

    console.print()
    console.print(f"[bold green]Deployment '{args.name}' fully destroyed.[/bold green]")


def _destroy_without_deployment_state(name: str, force: bool, console: Console) -> None:
    """Recovery path when no deployment state exists in S3.

    Scans for bootstrap and main-VM tfstate files matching the deployment name
    and destroys whatever is found. This lets destroy clean up after a crashed
    or partial infra run that didn't persist deployment state.
    """
    bootstrap_keys = list_stale_bootstrap_state_keys(name)
    main_keys: list[tuple[str, str]] = []
    for provider_cfg in PROVIDERS.values():
        prefix = f"{provider_cfg.state_key_prefix}-{name}-"
        for key in list_terraform_state_keys(prefix):
            # For the DO provider, the prefix `saorsa-deploy/do-` is also a
            # prefix of `saorsa-deploy/do-bootstrap-`, but for DEV-02 the name
            # segment disambiguates. Still, guard explicitly so a deployment
            # name starting with "bootstrap-" wouldn't confuse us.
            if key.startswith(f"saorsa-deploy/do-bootstrap-{name}-"):
                continue
            main_keys.append((provider_cfg.name, key))

    if not bootstrap_keys and not main_keys:
        console.print(
            f"[bold red]Error:[/bold red] No deployment state or Terraform state "
            f"found for '{name}'."
        )
        sys.exit(1)

    console.print(
        "[yellow]No deployment state in S3 — scanning for orphaned Terraform "
        "state files...[/yellow]"
    )
    if bootstrap_keys:
        console.print(f"[dim]Found {len(bootstrap_keys)} bootstrap tfstate(s):[/dim]")
        for key in bootstrap_keys:
            console.print(f"  [dim]{key}[/dim]")
    if main_keys:
        console.print(f"[dim]Found {len(main_keys)} main-VM tfstate(s):[/dim]")
        for _, key in main_keys:
            console.print(f"  [dim]{key}[/dim]")
    console.print()

    if not force:
        confirm = input(
            f"Destroy orphaned infrastructure tracked by these state files for "
            f"'{name}'? Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            console.print("[yellow]Aborted.[/yellow]")
            sys.exit(0)
        console.print()

    # Destroy main-VM tfstate with dummy vars (destroy works off the state).
    if main_keys:
        resources_dir = get_resources_dir()
        workspace_base = Path.cwd() / ".saorsa" / "workspaces"
        main_configs = []
        for provider_name, state_key in main_keys:
            provider = PROVIDERS[provider_name]
            prefix = f"{provider.state_key_prefix}-{name}-"
            region = state_key[len(prefix) : -len(".tfstate")]
            tf_source = resources_dir / provider.tf_dir
            workspace_dir = workspace_base / f"{provider_name}-{name}-{region}"
            variables = {
                "name": name,
                "region": region,
                "vm_count": "1",
                "attached_volume_size": "20",
            }
            if provider_name == "digitalocean":
                variables["droplet_size"] = "s-2vcpu-4gb"
            elif provider_name == "hetzner":
                variables["server_type"] = "cpx22"
            elif provider_name == "vultr":
                variables["plan"] = "vc2-2c-4gb"
            main_configs.append(
                TerraformRunConfig(
                    provider=provider_name,
                    region=region,
                    tf_source_dir=tf_source,
                    workspace_dir=workspace_dir,
                    state_key=state_key,
                    variables=variables,
                )
            )
        console.print(
            f"[bold]Destroying {len(main_configs)} orphaned main-VM tfstate(s)...[/bold]"
        )
        results = execute_terraform_runs(main_configs, action="destroy")
        failures = [r for r in results if not r.success]
        if failures:
            console.print(
                f"[bold red]{len(failures)} main-VM region(s) failed to destroy.[/bold red]"
            )
            sys.exit(1)
        for c in main_configs:
            try:
                delete_terraform_state(c.state_key)
            except Exception:
                pass
            if c.workspace_dir.exists():
                shutil.rmtree(c.workspace_dir)

    if bootstrap_keys:
        try:
            purge_stale_bootstrap_state(name, console=console)
        except Exception as e:
            console.print(f"[bold red]Failed to destroy bootstrap VMs:[/bold red] {e}")
            sys.exit(1)

    console.print()
    console.print(
        f"[bold green]Orphaned infrastructure for '{name}' destroyed.[/bold green]"
    )
