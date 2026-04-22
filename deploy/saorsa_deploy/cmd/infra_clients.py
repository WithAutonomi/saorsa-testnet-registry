import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    check_terraform_state_exists,
    delete_terraform_state,
    save_client_state,
    update_client_state,
)
from saorsa_deploy.terraform import TerraformRunConfig

CLIENT_STATE_KEY_PREFIX = "saorsa-deploy/do-client"
DEFAULT_VM_SIZE = "s-2vcpu-4gb"
DEFAULT_STORE_CONCURRENCY = 8
ROLE_UPLOAD = "upload"
ROLE_DOWNLOAD = "download"


def parse_uploader_spec(spec: str) -> dict:
    """Parse an uploader spec: 'region:file_size_mb[:vm_size[:store_concurrency]]'.

    Returns a dict with keys: role=upload, region, file_size_mb, vm_size,
    store_concurrency.
    """
    parts = spec.split(":")
    if len(parts) < 2 or len(parts) > 4:
        raise ValueError(
            f"Invalid uploader spec '{spec}'. "
            f"Expected format: region:file_size_mb[:vm_size[:store_concurrency]]"
        )
    region = parts[0]
    try:
        file_size_mb = int(parts[1])
    except ValueError:
        raise ValueError(
            f"Invalid file size '{parts[1]}' in uploader spec '{spec}'. "
            f"Must be an integer (MB)."
        )
    vm_size = parts[2] if len(parts) >= 3 else DEFAULT_VM_SIZE
    if len(parts) == 4:
        try:
            store_concurrency = int(parts[3])
        except ValueError:
            raise ValueError(
                f"Invalid store concurrency '{parts[3]}' in uploader spec '{spec}'. "
                f"Must be an integer."
            )
    else:
        store_concurrency = DEFAULT_STORE_CONCURRENCY
    return {
        "role": ROLE_UPLOAD,
        "region": region,
        "file_size_mb": file_size_mb,
        "vm_size": vm_size,
        "store_concurrency": store_concurrency,
    }


def parse_downloader_spec(spec: str) -> dict:
    """Parse a downloader spec: 'region[:vm_size]'.

    Returns a dict with keys: role=download, region, vm_size.
    """
    parts = spec.split(":")
    if len(parts) < 1 or len(parts) > 2:
        raise ValueError(
            f"Invalid downloader spec '{spec}'. "
            f"Expected format: region[:vm_size]"
        )
    region = parts[0]
    if not region:
        raise ValueError(f"Invalid downloader spec '{spec}': region cannot be empty.")
    vm_size = parts[1] if len(parts) == 2 else DEFAULT_VM_SIZE
    return {
        "role": ROLE_DOWNLOAD,
        "region": region,
        "vm_size": vm_size,
    }


def cmd_infra_clients(args):
    """Execute the infra-clients command: create client VMs via Terraform."""
    console = Console()

    uploader_specs_raw = args.uploader or []
    downloader_specs_raw = args.downloader or []

    if not uploader_specs_raw and not downloader_specs_raw:
        console.print(
            "[bold red]Error:[/bold red] At least one --uploader or --downloader "
            "spec is required."
        )
        sys.exit(1)

    client_specs: list[dict] = []
    try:
        for i, spec_str in enumerate(uploader_specs_raw, start=1):
            spec = parse_uploader_spec(spec_str)
            spec["client_index"] = i
            client_specs.append(spec)
        for i, spec_str in enumerate(downloader_specs_raw, start=1):
            spec = parse_downloader_spec(spec_str)
            spec["client_index"] = i
            client_specs.append(spec)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    console.print(f"[bold]Creating {len(client_specs)} client VM(s) for {args.name}...[/bold]")
    for spec in client_specs:
        if spec["role"] == ROLE_UPLOAD:
            console.print(
                f"  Uploader #{spec['client_index']}: {spec['region']} "
                f"({spec['vm_size']}, {spec['file_size_mb']}MB uploads, "
                f"sc={spec['store_concurrency']})"
            )
        else:
            console.print(
                f"  Downloader #{spec['client_index']}: {spec['region']} "
                f"({spec['vm_size']})"
            )
    console.print()

    resources_dir = get_resources_dir()
    tf_source = resources_dir / "digitalocean-client"
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"

    configs = []
    for spec in client_specs:
        workspace_dir = (
            workspace_base
            / f"do-client-{args.name}-{spec['role']}-{spec['region']}-{spec['client_index']}"
        )
        state_key = (
            f"{CLIENT_STATE_KEY_PREFIX}-{args.name}-{spec['role']}"
            f"-{spec['region']}-{spec['client_index']}.tfstate"
        )
        variables = {
            "name": args.name,
            "role": spec["role"],
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

    # Save client state UPFRONT (with empty IPs) so that destroy-clients can
    # always tear down whatever terraform may have started, even on partial
    # or total failure. IPs get merged in after the apply completes.
    upfront_specs = [{**spec, "ip": ""} for spec in client_specs]
    try:
        save_client_state(args.name, upfront_specs)
    except Exception as e:
        console.print(
            f"[bold red]Failed to save initial client state:[/bold red] {e}"
        )
        console.print(
            "[bold red]Aborting — without saved state, destroy-clients "
            "couldn't clean up a partial apply.[/bold red]"
        )
        sys.exit(1)

    results = execute_terraform_runs(configs)

    # Merge whichever IPs came back (partial success included).
    for spec, result in zip(client_specs, results):
        if result.success and result.outputs.get("vm_ip"):
            spec["ip"] = result.outputs["vm_ip"]
        else:
            spec["ip"] = ""

    try:
        update_client_state(args.name, {"clients": client_specs})
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to update client state: {e}[/yellow]")

    failures = [r for r in results if not r.success]
    if failures:
        console.print(f"[bold red]{len(failures)} client VM(s) failed.[/bold red]")
        sys.exit(1)

    console.print()
    console.print(
        f"[bold green]All {len(results)} client VM(s) provisioned successfully.[/bold green]"
    )
    for spec in client_specs:
        label = "Uploader" if spec["role"] == ROLE_UPLOAD else "Downloader"
        console.print(
            f"  {label} #{spec['client_index']}: {spec.get('ip', 'N/A')} "
            f"({spec['region']}, {spec['vm_size']})"
        )
