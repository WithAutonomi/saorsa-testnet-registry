import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.bootstrap import create_bootstrap_vm
from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.providers import PROVIDERS, resolve_regions
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import save_deployment_state
from saorsa_deploy.terraform import TerraformRunConfig


def cmd_infra(args):
    """Execute the infra command: provision VMs using Terraform."""
    console = Console()

    # Create the bootstrap VM first (idempotent)
    console.print(f"[bold]Bootstrap VM ({args.name}-saorsa-bootstrap)...[/bold]")
    try:
        bootstrap = create_bootstrap_vm(args.name)
        if bootstrap["created"]:
            console.print(
                f"[green]Bootstrap VM created: {bootstrap['droplet_name']} "
                f"({bootstrap['ip_address']})[/green]"
            )
        else:
            console.print(
                f"[green]Bootstrap VM already exists: {bootstrap['droplet_name']} "
                f"({bootstrap['ip_address']})[/green]"
            )
    except Exception as e:
        console.print(f"[bold red]Failed to create bootstrap VM:[/bold red] {e}")
        sys.exit(1)

    console.print()

    # Resolve regions for the main deployment
    if args.testnet and args.region_counts:
        console.print(
            "[yellow]Warning: --region-counts is ignored when --testnet is used "
            "(testnet uses a single region: lon1)[/yellow]"
        )
    try:
        region_pairs = resolve_regions(
            region_counts=args.region_counts,
            testnet=args.testnet,
        )
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    resources_dir = get_resources_dir()
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"

    configs = []
    for provider_name, region in region_pairs:
        provider = PROVIDERS[provider_name]
        tf_source = resources_dir / provider.tf_dir
        workspace_dir = workspace_base / f"{provider_name}-{region}"
        state_key = f"{provider.state_key_prefix}-{region}.tfstate"

        config = TerraformRunConfig(
            provider=provider_name,
            region=region,
            tf_source_dir=tf_source,
            workspace_dir=workspace_dir,
            state_key=state_key,
            variables={
                "name": args.name,
                "region": region,
                "vm_count": str(args.vm_count),
                "attached_volume_size": str(args.attached_volume_size),
            },
        )
        configs.append(config)

    console.print(f"[bold]Provisioning infrastructure across {len(configs)} region(s)...[/bold]")
    console.print()

    results = execute_terraform_runs(configs)

    failures = [r for r in results if not r.success]
    if failures:
        console.print(f"[bold red]{len(failures)} region(s) failed.[/bold red]")
        sys.exit(1)
    else:
        console.print(
            f"[bold green]All {len(results)} region(s) provisioned successfully.[/bold green]"
        )

    vm_ips = {}
    for result in results:
        if result.success and result.outputs.get("droplet_ips"):
            key = f"{result.provider}/{result.region}"
            vm_ips[key] = result.outputs["droplet_ips"]

    terraform_variables = {
        "name": args.name,
        "vm_count": str(args.vm_count),
        "attached_volume_size": str(args.attached_volume_size),
    }
    try:
        save_deployment_state(
            args.name,
            region_pairs,
            terraform_variables,
            bootstrap["ip_address"],
            vm_ips=vm_ips,
        )
        console.print("[dim]Deployment state saved to S3.[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to save deployment state: {e}[/yellow]")
