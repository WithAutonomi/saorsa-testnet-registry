import shutil
from pathlib import Path

from rich.console import Console

from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.providers import PROVIDERS
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import check_terraform_state_exists, delete_terraform_state
from saorsa_deploy.terraform import TerraformRunConfig

BOOTSTRAP_STATE_KEY_PREFIX = "saorsa-deploy/do-bootstrap"
BOOTSTRAP_DROPLET_SIZE = "s-2vcpu-4gb"


def _select_bootstrap_regions(count: int) -> list[str]:
    """Select geographically distributed DO regions for bootstrap nodes.

    Returns the first `count` regions from the DO provider's region list.
    """
    do_regions = PROVIDERS["digitalocean"].regions
    if count > len(do_regions):
        raise ValueError(
            f"Requested {count} bootstrap nodes, but only {len(do_regions)} "
            "Digital Ocean regions are available"
        )
    return do_regions[:count]


def _build_bootstrap_configs(
    name: str,
    regions: list[str],
    volume_size: int = 20,
    droplet_size: str = BOOTSTRAP_DROPLET_SIZE,
) -> list[TerraformRunConfig]:
    """Build Terraform run configs for bootstrap nodes."""
    resources_dir = get_resources_dir()
    tf_source = resources_dir / "digitalocean-bootstrap"
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"

    configs = []
    for i, region in enumerate(regions):
        bootstrap_index = i + 1
        workspace_dir = workspace_base / f"do-bootstrap-{name}-{region}"
        state_key = f"{BOOTSTRAP_STATE_KEY_PREFIX}-{name}-{region}.tfstate"

        variables = {
            "name": name,
            "region": region,
            "attached_volume_size": str(volume_size),
            "droplet_size": droplet_size,
            "bootstrap_index": str(bootstrap_index),
        }

        configs.append(
            TerraformRunConfig(
                provider="digitalocean",
                region=region,
                tf_source_dir=tf_source,
                workspace_dir=workspace_dir,
                state_key=state_key,
                variables=variables,
            )
        )
    return configs


def create_bootstrap_vms(
    name: str,
    count: int = 1,
    volume_size: int = 20,
    droplet_size: str = BOOTSTRAP_DROPLET_SIZE,
    force: bool = False,
    console: Console | None = None,
) -> list[dict]:
    """Create bootstrap VMs via Terraform across geographically distributed regions.

    Returns a list of dicts with keys: ip, region, bootstrap_index.
    The first entry (index=1) is the genesis node.
    """
    console = console or Console()
    regions = _select_bootstrap_regions(count)
    configs = _build_bootstrap_configs(name, regions, volume_size, droplet_size)

    # Check for stale state
    stale_keys = [c.state_key for c in configs if check_terraform_state_exists(c.state_key)]
    if stale_keys:
        if not force:
            console.print("[bold red]Error:[/bold red] Stale bootstrap Terraform state found:")
            for key in stale_keys:
                console.print(f"  {key}")
            console.print("Use --force to delete and proceed.")
            raise RuntimeError("Stale bootstrap Terraform state")
        for key in stale_keys:
            console.print(f"[yellow]Warning: Deleting stale bootstrap state: {key}[/yellow]")
            delete_terraform_state(key)

    console.print(
        f"[bold]Creating {count} bootstrap VM(s) across "
        f"{', '.join(regions)}...[/bold]"
    )

    results = execute_terraform_runs(configs)

    failures = [r for r in results if not r.success]
    if failures:
        raise RuntimeError(
            f"{len(failures)} bootstrap region(s) failed to provision"
        )

    bootstrap_nodes = []
    for config, result in zip(configs, results):
        if result.success:
            ip = result.outputs.get("vm_ip")
            if not ip:
                raise RuntimeError(
                    f"No IP output from bootstrap Terraform in {config.region}"
                )
            bootstrap_nodes.append({
                "ip": ip,
                "region": config.region,
                "bootstrap_index": int(config.variables["bootstrap_index"]),
            })

    bootstrap_nodes.sort(key=lambda n: n["bootstrap_index"])
    return bootstrap_nodes


def destroy_bootstrap_vms(
    name: str,
    regions: list[str],
    volume_size: int = 20,
    droplet_size: str = BOOTSTRAP_DROPLET_SIZE,
    console: Console | None = None,
) -> None:
    """Destroy bootstrap VMs via Terraform."""
    console = console or Console()
    configs = _build_bootstrap_configs(name, regions, volume_size, droplet_size)

    # Only destroy configs where state exists
    active_configs = [c for c in configs if check_terraform_state_exists(c.state_key)]
    if not active_configs:
        console.print("[yellow]No bootstrap Terraform state found, nothing to destroy.[/yellow]")
        return

    console.print(
        f"[bold]Destroying {len(active_configs)} bootstrap VM(s)...[/bold]"
    )
    results = execute_terraform_runs(active_configs, action="destroy")

    failures = [r for r in results if not r.success]
    if failures:
        raise RuntimeError(
            f"{len(failures)} bootstrap region(s) failed to destroy"
        )

    # Clean up Terraform state and workspace directories
    for config in active_configs:
        try:
            delete_terraform_state(config.state_key)
        except Exception:
            pass
        if config.workspace_dir.exists():
            shutil.rmtree(config.workspace_dir)
