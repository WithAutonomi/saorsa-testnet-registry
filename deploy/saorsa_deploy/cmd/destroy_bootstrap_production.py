"""Destroy bootstrap production network command.

Tears down all infrastructure created by prod bootstrap deploy across all providers.
Does NOT release the static IPs (those are managed outside Terraform).

Reads the node list from production-ips.json and runs terraform destroy for each.
Terraform's own state (in S3) tracks what was actually created.
"""

import sys

from rich.console import Console

from saorsa_deploy.cmd.bootstrap_production import (
    _build_terraform_configs,
    _load_production_config,
)
from saorsa_deploy.executor import execute_terraform_runs


def cmd_destroy_bootstrap_production(args):
    """Execute the prod bootstrap destroy command."""
    console = Console()
    name = args.name

    console.print(f"[bold]Destroy Production Network: {name}[/bold]")
    console.print()

    # Load node list and SSH key IDs from production-ips.json
    nodes, ssh_key_ids = _load_production_config(console)

    # Confirm
    if not args.force:
        console.print("[bold yellow]WARNING:[/bold yellow] This will destroy ALL production nodes:")
        for node in nodes:
            console.print(
                f"  - {node['provider']}/{node['region']} ({node['ip']}) "
                f"[{node.get('role', 'node')}]"
            )
        console.print()
        console.print("[dim]Static IPs will NOT be released.[/dim]")
        response = input("Are you sure you want to proceed? [y/N] ")
        if response.strip().lower() not in ("y", "yes"):
            console.print("[bold red]Aborted.[/bold red]")
            sys.exit(1)

    # Build Terraform destroy configs (same configs as bootstrap uses)
    configs = _build_terraform_configs(nodes, name, ssh_key_ids, console)

    console.print(
        f"[bold]Destroying infrastructure across {len(configs)} provider/regions...[/bold]"
    )
    console.print()

    results = execute_terraform_runs(configs, action="destroy")

    failures = [r for r in results if not r.success]
    if failures:
        for f in failures:
            console.print(f"[bold red]FAILED: {f.provider}/{f.region}[/bold red]")
            console.print(f"  stderr: {f.stderr[:500]}")
        console.print(
            f"[bold red]{len(failures)} region(s) failed to destroy.[/bold red]"
        )
        sys.exit(1)

    console.print(
        f"[bold green]All {len(results)} regions destroyed successfully.[/bold green]"
    )

    console.print()
    console.print("[bold green]Production network destroyed.[/bold green]")
    console.print("[dim]Static IPs have been preserved for reuse.[/dim]")
    console.print("[dim]Terraform state files preserved in S3 (safe to re-apply).[/dim]")
