import os
import shutil
import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.cmd.infra_nodes import OVH_REQUIRED_ENV_VARS
from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.providers import PROVIDERS
from saorsa_deploy.registry import record_event, record_status
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    delete_deployment_state,
    delete_terraform_state,
    list_terraform_state_keys,
    load_deployment_state,
    load_terraform_state,
    update_deployment_state,
)
from saorsa_deploy.terraform import TerraformRunConfig


def _orphan_volume_size(state_key: str) -> str:
    """Return the node-storage volume size recorded in an orphan tfstate.

    Reading the real size (rather than a hardcoded default) keeps the destroy
    plan from computing a volume "shrink", which DO/OVH reject. Takes the max
    across instances and falls back to "20" when no volume size is recorded or
    the state can't be read.
    """
    try:
        tfstate = load_terraform_state(state_key)
    except Exception:
        return "20"
    sizes: list[int] = []
    for resource in tfstate.get("resources", []):
        if resource.get("name") != "node_storage":
            continue
        rtype = resource.get("type", "")
        if "volume" not in rtype and "block_storage" not in rtype:
            continue
        for inst in resource.get("instances") or []:
            attrs = inst.get("attributes") or {}
            size = attrs.get("size", attrs.get("size_gb"))
            if isinstance(size, (int, float)) and size > 0:
                sizes.append(int(size))
    return str(max(sizes)) if sizes else "20"


def _apply_provider_size_vars(
    variables: dict, provider_name: str, p_cfg: dict | None = None
) -> None:
    """Populate the provider-specific Terraform variables for a destroy run.

    Falls back to each provider's default size when the recorded provider
    config (``terraform_variables.providers``) is missing or incomplete.
    """
    p_cfg = p_cfg or {}
    if provider_name == "digitalocean":
        variables["droplet_size"] = p_cfg.get("droplet_size", "s-2vcpu-4gb")
    elif provider_name == "hetzner":
        variables["server_type"] = p_cfg.get("server_type", "cpx22")
    elif provider_name == "vultr":
        variables["plan"] = p_cfg.get("plan", "vc2-2c-4gb")
    elif provider_name in ("ovh", "ovh_3az"):
        variables["flavor_name"] = p_cfg.get("flavor_name", "b3-16")
        variables["service_name"] = os.environ.get("OVH_CLOUD_PROJECT_SERVICE", "")
        variables["name_template"] = "ant-node"


def _check_ovh_destroy_env(
    provider_names: list[str], console: Console
) -> None:
    """Exit early when OVH regions are present but the env lacks credentials.

    Terraform re-reads the module's data sources on destroy, so the OVH API
    credentials and project id are needed just like on create.
    """
    if "ovh" not in provider_names and "ovh_3az" not in provider_names:
        return
    missing = [v for v in OVH_REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        console.print(
            f"[bold red]Error:[/bold red] OVH node region(s) present but "
            f"required environment variable(s) not set: {', '.join(missing)}"
        )
        sys.exit(1)


def cmd_destroy_nodes(args):
    """Tear down the node (main) VMs for a deployment.

    Leaves bootstrap state alone. Falls back to scanning S3 for orphan
    main-VM tfstate files when no deployment state is found.
    """
    console = Console()

    console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
    try:
        state = load_deployment_state(args.name)
    except RuntimeError:
        _destroy_nodes_without_state(args.name, args.force, console)
        return

    region_pairs = [(r[0], r[1]) for r in (state.get("regions") or [])]
    terraform_variables = state.get("terraform_variables") or {}

    # Belt-and-braces: even if `regions` is empty, scan S3 for orphan main-VM
    # tfstate keys that may have been created and then lost. This used to leak
    # cloud resources when an older infra command crashed between the early
    # state save and the terraform apply.
    orphan_main_keys = _scan_orphan_main_vm_keys(args.name)

    if not region_pairs and not orphan_main_keys:
        console.print(
            f"[yellow]No node regions recorded for '{args.name}' and no "
            "orphan main-VM tfstate found. Nothing to destroy.[/yellow]"
        )
        return

    if region_pairs:
        console.print(
            f"[dim]Found {len(region_pairs)} node region(s) to destroy.[/dim]"
        )
        for provider, region in region_pairs:
            console.print(f"  [dim]{provider}/{region}[/dim]")
    if orphan_main_keys:
        console.print(
            f"[dim]Found {len(orphan_main_keys)} orphan main-VM tfstate(s) "
            "not covered by state:[/dim]"
        )
        for _, key in orphan_main_keys:
            console.print(f"  [dim]{key}[/dim]")
    console.print()

    _check_ovh_destroy_env(
        [p for p, _ in region_pairs] + [p for p, _ in orphan_main_keys], console
    )

    if not args.force:
        confirm = input(
            f"This will destroy all node VMs for '{args.name}'. Type 'yes' to "
            "confirm: "
        )
        if confirm.strip().lower() != "yes":
            console.print("[yellow]Aborted.[/yellow]")
            sys.exit(0)
        console.print()

    from saorsa_db.models import DeploymentStatus

    record_event(args.name, "destroy_nodes_started")
    record_status(args.name, DeploymentStatus.DESTROYING)

    resources_dir = get_resources_dir()
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"
    providers_config = terraform_variables.get("providers", {})
    fallback_vm_count = terraform_variables.get("vm_count")

    configs: list[TerraformRunConfig] = []
    seen_keys: set[str] = set()

    for provider_name, region in region_pairs:
        provider = PROVIDERS[provider_name]
        tf_source = resources_dir / provider.tf_dir
        name = terraform_variables.get("name", args.name)
        workspace_dir = workspace_base / f"{provider_name}-{name}-{region}"
        state_key = f"{provider.state_key_prefix}-{name}-{region}.tfstate"

        if provider_name in providers_config:
            vm_count = str(providers_config[provider_name]["vm_count"])
        elif fallback_vm_count:
            vm_count = str(fallback_vm_count)
        else:
            vm_count = "1"

        variables = {
            "name": name,
            "region": region,
            "vm_count": vm_count,
        }
        # ovh_3az has no attached volume (3-AZ regions have no block storage), so
        # its module doesn't declare attached_volume_size.
        if provider_name != "ovh_3az":
            variables["attached_volume_size"] = terraform_variables.get(
                "attached_volume_size", "20"
            )
        _apply_provider_size_vars(
            variables, provider_name, providers_config.get(provider_name)
        )

        configs.append(
            TerraformRunConfig(
                provider=provider_name,
                region=region,
                tf_source_dir=tf_source,
                workspace_dir=workspace_dir,
                state_key=state_key,
                variables=variables,
            )
        )
        seen_keys.add(state_key)

    for provider_name, state_key in orphan_main_keys:
        if state_key in seen_keys:
            continue
        provider = PROVIDERS[provider_name]
        prefix = f"{provider.state_key_prefix}-{args.name}-"
        region = state_key[len(prefix) : -len(".tfstate")]
        tf_source = resources_dir / provider.tf_dir
        workspace_dir = workspace_base / f"{provider_name}-{args.name}-{region}"
        # Read the real volume size from the orphan's own tfstate. A hardcoded
        # value smaller than the recorded volume makes Terraform's destroy plan
        # compute a volume "shrink", which DO/OVH reject ("size can only be
        # expanded and not shrunk") — failing the destroy. Fall back to "20"
        # only when no volume size is recorded.
        variables = {
            "name": args.name,
            "region": region,
            "vm_count": "1",
            "attached_volume_size": _orphan_volume_size(state_key),
        }
        _apply_provider_size_vars(variables, provider_name)
        configs.append(
            TerraformRunConfig(
                provider=provider_name,
                region=region,
                tf_source_dir=tf_source,
                workspace_dir=workspace_dir,
                state_key=state_key,
                variables=variables,
            )
        )
        seen_keys.add(state_key)

    console.print(
        f"[bold]Destroying node infrastructure across {len(configs)} region(s)...[/bold]"
    )
    console.print()

    results = execute_terraform_runs(configs, action="destroy")
    failures = [r for r in results if not r.success]
    if failures:
        console.print(
            f"[bold red]{len(failures)} region(s) failed to destroy.[/bold red]"
        )
        record_event(args.name, "destroy_nodes_failed", {"failures": len(failures)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    console.print(
        f"[bold green]All {len(results)} region(s) destroyed successfully.[/bold green]"
    )
    console.print()

    for config in configs:
        try:
            delete_terraform_state(config.state_key)
            console.print(f"[dim]Deleted Terraform state: {config.state_key}[/dim]")
        except Exception as e:
            console.print(
                f"[yellow]Warning: Failed to delete Terraform state "
                f"{config.state_key}: {e}[/yellow]"
            )
        if config.workspace_dir.exists():
            shutil.rmtree(config.workspace_dir)

    # Clear node slice from deployment state. If bootstraps are gone too,
    # delete the state file entirely.
    if state.get("bootstrap_nodes"):
        try:
            update_deployment_state(
                args.name,
                {
                    "regions": [],
                    "terraform_variables": {
                        "name": args.name,
                        "attached_volume_size": terraform_variables.get(
                            "attached_volume_size", "20"
                        ),
                        "providers": {},
                    },
                    "vm_ips": {},
                    "vm_ids": {},
                },
            )
        except Exception as e:
            console.print(
                f"[yellow]Warning: Failed to update deployment state: {e}[/yellow]"
            )
    else:
        try:
            delete_deployment_state(args.name)
            console.print("[dim]Deployment state removed from S3.[/dim]")
        except Exception as e:
            console.print(
                f"[yellow]Warning: Failed to delete deployment state: {e}[/yellow]"
            )

    record_event(args.name, "destroy_nodes_completed")
    console.print(
        f"[bold green]Node VMs for '{args.name}' destroyed.[/bold green]"
    )


def _scan_orphan_main_vm_keys(name: str) -> list[tuple[str, str]]:
    """Return (provider_name, state_key) for main-VM tfstate keys in S3."""
    orphans: list[tuple[str, str]] = []
    for provider_cfg in PROVIDERS.values():
        prefix = f"{provider_cfg.state_key_prefix}-{name}-"
        for key in list_terraform_state_keys(prefix):
            # Guard against the DO "do-" prefix also matching "do-bootstrap-".
            if key.startswith(f"saorsa-deploy/do-bootstrap-{name}-"):
                continue
            orphans.append((provider_cfg.name, key))
    return orphans


def _destroy_nodes_without_state(name: str, force: bool, console: Console) -> None:
    """Fallback: destroy node VMs by scanning S3 for orphan tfstate files."""
    orphans = _scan_orphan_main_vm_keys(name)
    if not orphans:
        console.print(
            f"[yellow]No node infrastructure found for '{name}'. "
            "Nothing to destroy.[/yellow]"
        )
        return

    console.print(
        "[yellow]No deployment state in S3 — scanning for orphaned main-VM "
        "tfstate files...[/yellow]"
    )
    console.print(f"[dim]Found {len(orphans)} main-VM tfstate(s):[/dim]")
    for _, key in orphans:
        console.print(f"  [dim]{key}[/dim]")
    console.print()

    _check_ovh_destroy_env([p for p, _ in orphans], console)

    if not force:
        confirm = input(
            f"Destroy orphaned node resources for '{name}'? Type 'yes' to "
            "confirm: "
        )
        if confirm.strip().lower() != "yes":
            console.print("[yellow]Aborted.[/yellow]")
            sys.exit(0)
        console.print()

    resources_dir = get_resources_dir()
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"
    configs = []
    for provider_name, state_key in orphans:
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
        _apply_provider_size_vars(variables, provider_name)
        configs.append(
            TerraformRunConfig(
                provider=provider_name,
                region=region,
                tf_source_dir=tf_source,
                workspace_dir=workspace_dir,
                state_key=state_key,
                variables=variables,
            )
        )

    results = execute_terraform_runs(configs, action="destroy")
    failures = [r for r in results if not r.success]
    if failures:
        console.print(
            f"[bold red]{len(failures)} region(s) failed to destroy.[/bold red]"
        )
        sys.exit(1)
    for c in configs:
        try:
            delete_terraform_state(c.state_key)
        except Exception:
            pass
        if c.workspace_dir.exists():
            shutil.rmtree(c.workspace_dir)

    console.print(
        f"[bold green]Orphaned node infrastructure for '{name}' destroyed.[/bold green]"
    )
