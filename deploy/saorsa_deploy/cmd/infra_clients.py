import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    check_terraform_state_exists,
    delete_terraform_state,
    save_client_state,
)
from saorsa_deploy.terraform import TerraformRunConfig

CLIENT_STATE_KEY_PREFIX = "saorsa-deploy/do-client"
DEFAULT_VM_SIZE = "s-2vcpu-4gb"


def parse_client_spec(spec: str) -> dict:
    """Parse a client spec string: 'region:file_size_mb[:vm_size]'.

    Returns a dict with keys: region, file_size_mb, vm_size.
    """
    parts = spec.split(":")
    if len(parts) < 2 or len(parts) > 3:
        raise ValueError(
            f"Invalid client spec '{spec}'. "
            f"Expected format: region:file_size_mb[:vm_size]"
        )
    region = parts[0]
    try:
        file_size_mb = int(parts[1])
    except ValueError:
        raise ValueError(
            f"Invalid file size '{parts[1]}' in client spec '{spec}'. "
            f"Must be an integer (MB)."
        )
    vm_size = parts[2] if len(parts) == 3 else DEFAULT_VM_SIZE
    return {
        "region": region,
        "file_size_mb": file_size_mb,
        "vm_size": vm_size,
    }


def cmd_infra_clients(args):
    """Execute the infra-clients command: create client VMs via Terraform."""
    console = Console()

    if not args.client:
        console.print(
            "[bold red]Error:[/bold red] At least one --client spec is required. "
            "Format: --client 'region:file_size_mb[:vm_size]'"
        )
        sys.exit(1)

    # Parse client specs
    client_specs = []
    for i, spec_str in enumerate(args.client, start=1):
        try:
            spec = parse_client_spec(spec_str)
            spec["client_index"] = i
            client_specs.append(spec)
        except ValueError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

    console.print(f"[bold]Creating {len(client_specs)} client VM(s) for {args.name}...[/bold]")
    for spec in client_specs:
        console.print(
            f"  Client #{spec['client_index']}: {spec['region']} "
            f"({spec['vm_size']}, {spec['file_size_mb']}MB uploads)"
        )
    console.print()

    resources_dir = get_resources_dir()
    tf_source = resources_dir / "digitalocean-client"
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"

    configs = []
    for spec in client_specs:
        workspace_dir = (
            workspace_base / f"do-client-{args.name}-{spec['region']}-{spec['client_index']}"
        )
        state_key = (
            f"{CLIENT_STATE_KEY_PREFIX}-{args.name}"
            f"-{spec['region']}-{spec['client_index']}.tfstate"
        )
        variables = {
            "name": args.name,
            "region": spec["region"],
            "client_index": str(spec["client_index"]),
            "droplet_size": spec["vm_size"],
        }
        config = TerraformRunConfig(
            provider="digitalocean",
            region=spec["region"],
            tf_source_dir=tf_source,
            workspace_dir=workspace_dir,
            state_key=state_key,
            variables=variables,
        )
        configs.append(config)

    # Check for stale Terraform state files
    stale_keys = [c.state_key for c in configs if check_terraform_state_exists(c.state_key)]
    if stale_keys:
        if not getattr(args, "force", False):
            console.print("[bold red]Error:[/bold red] Stale Terraform state found:")
            for key in stale_keys:
                console.print(f"  {key}")
            console.print("Use --force to delete and proceed.")
            sys.exit(1)
        for key in stale_keys:
            console.print(f"[yellow]Warning: Deleting stale Terraform state: {key}[/yellow]")
            delete_terraform_state(key)

    results = execute_terraform_runs(configs)

    failures = [r for r in results if not r.success]
    if failures:
        console.print(f"[bold red]{len(failures)} client VM(s) failed.[/bold red]")
        sys.exit(1)

    # Collect IPs and save state
    for i, (spec, result) in enumerate(zip(client_specs, results)):
        if result.success and result.outputs.get("vm_ip"):
            spec["ip"] = result.outputs["vm_ip"]
        else:
            spec["ip"] = ""

    try:
        save_client_state(args.name, client_specs)
        console.print("[dim]Client state saved to S3.[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to save client state: {e}[/yellow]")

    console.print()
    console.print(
        f"[bold green]All {len(results)} client VM(s) provisioned successfully.[/bold green]"
    )
    for spec in client_specs:
        console.print(
            f"  Client #{spec['client_index']}: {spec.get('ip', 'N/A')} "
            f"({spec['region']}, {spec['vm_size']})"
        )
