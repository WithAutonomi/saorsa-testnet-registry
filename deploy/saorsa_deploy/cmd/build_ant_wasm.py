"""``build ant-wasm`` -- build the WASM client bundle and upload it to S3."""

import os
import sys

from rich.console import Console

from saorsa_deploy.binary_source import validate_branch_exists
from saorsa_deploy.build_droplet import create_build_vm, destroy_build_vm, wait_for_ssh
from saorsa_deploy.provisioning.build_ant_wasm import AntWasmBuilder
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.wasm_build import DEFAULT_FEATURES


def cmd_build_ant_wasm(args):
    """Build ant-core to WASM plus the TypeScript client, and upload to S3."""
    console = Console()

    for var in ("ANT_BUILD_AWS_ACCESS_KEY_ID", "ANT_BUILD_AWS_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            console.print(f"[bold red]Error:[/bold red] {var} environment variable is not set")
            sys.exit(1)

    try:
        validate_branch_exists(args.repo_owner, "ant-client", args.branch_name)
    except (ValueError, RuntimeError) as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    features = getattr(args, "features", None) or DEFAULT_FEATURES
    console.print(
        f"[bold]Building the WASM client from {args.repo_owner}/ant-client "
        f"(branch: {args.branch_name}, features: {features})...[/bold]"
    )
    console.print()

    droplet_id = None
    try:
        console.print("[bold]Creating build droplet...[/bold]")
        vm = create_build_vm(
            "ant-wasm", args.name, args.repo_owner, args.branch_name, args.build_vm_size
        )
        droplet_id = vm["droplet_id"]
        if vm.get("reused"):
            console.print(
                f"[yellow]Reusing existing build droplet: "
                f"{vm['droplet_name']} ({vm['ip_address']})[/yellow]"
            )
        else:
            console.print(
                f"[green]Build droplet created: {vm['droplet_name']} ({vm['ip_address']})[/green]"
            )

        console.print("Waiting for SSH...")
        wait_for_ssh(vm["ip_address"])
        console.print("[green]SSH ready.[/green]")
        console.print()

        clear_known_hosts([vm["ip_address"]], console)

        builder = AntWasmBuilder(
            name=args.name,
            ip=vm["ip_address"],
            ssh_key_path=args.ssh_key_path,
            repo_owner=args.repo_owner,
            branch_name=args.branch_name,
            console=console,
            features=features,
        )
        s3_url = builder.execute()

        console.print()
        console.print("[bold green]Build complete.[/bold green]")
        console.print(f"  Bundle URL: {s3_url}")
    except Exception as e:
        console.print(f"[bold red]Build failed:[/bold red] {e}")
        sys.exit(1)
    finally:
        if droplet_id:
            console.print()
            console.print("Destroying build droplet...")
            try:
                destroy_build_vm(droplet_id)
                console.print("[green]Build droplet destroyed.[/green]")
            except Exception as e:
                console.print(f"[yellow]Warning: Failed to destroy build droplet: {e}[/yellow]")
