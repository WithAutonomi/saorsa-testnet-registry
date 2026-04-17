import sys

from rich.console import Console

from saorsa_deploy.bootstrap import (
    create_bootstrap_vms,
    list_stale_bootstrap_state_keys,
    purge_stale_bootstrap_state,
)
from saorsa_deploy.registry import record_deployment_created, record_event
from saorsa_deploy.state import (
    load_deployment_state,
    save_deployment_state,
    update_deployment_state,
)


def _try_load_deployment_state(name: str) -> dict | None:
    try:
        return load_deployment_state(name)
    except RuntimeError:
        return None


def cmd_infra_bootstraps(args):
    """Create the bootstrap VMs for a deployment.

    Owns the ``bootstrap_nodes`` slice of the deployment state. Does not
    create or touch node/main-VM infrastructure — run ``infra-nodes`` for
    that step.
    """
    console = Console()

    bootstrap_count = getattr(args, "bootstrap_count", 1)
    if bootstrap_count < 1 or bootstrap_count > 7:
        console.print(
            "[bold red]Error:[/bold red] --bootstrap-count must be between 1 and 7."
        )
        sys.exit(1)

    force = getattr(args, "force", False)

    deploy_args = {
        "name": args.name,
        "attached_volume_size": args.attached_volume_size,
        "bootstrap_count": bootstrap_count,
    }
    record_deployment_created(args.name, deploy_args)
    record_event(args.name, "infra_bootstraps_started")

    existing_state = _try_load_deployment_state(args.name)

    if existing_state and existing_state.get("bootstrap_nodes"):
        bootstrap_nodes = existing_state["bootstrap_nodes"]
        if len(bootstrap_nodes) != bootstrap_count:
            console.print(
                f"[bold red]Error:[/bold red] Deployment '{args.name}' already has "
                f"{len(bootstrap_nodes)} bootstrap node(s), but --bootstrap-count is "
                f"{bootstrap_count}. Destroy the existing bootstraps first or re-run "
                "with the matching count."
            )
            sys.exit(1)
        console.print(
            f"[bold]Reusing {len(bootstrap_nodes)} existing bootstrap VM(s) "
            f"for {args.name}.[/bold]"
        )
        for node in bootstrap_nodes:
            console.print(
                f"[green]  Bootstrap #{node['bootstrap_index']}: "
                f"{node['ip']} ({node['region']})[/green]"
            )
        record_event(args.name, "infra_bootstraps_reused")
        return

    stale_bootstrap_keys = list_stale_bootstrap_state_keys(args.name)
    if stale_bootstrap_keys:
        if not force:
            console.print(
                "[bold red]Error:[/bold red] Bootstrap Terraform state exists for "
                f"'{args.name}' but no parent deployment state was found:"
            )
            for key in stale_bootstrap_keys:
                console.print(f"  {key}")
            console.print(
                "These are orphaned. Use --force to destroy the tracked cloud "
                "resources and proceed."
            )
            sys.exit(1)
        try:
            purge_stale_bootstrap_state(
                args.name,
                volume_size=args.attached_volume_size,
                console=console,
            )
        except Exception as e:
            console.print(
                f"[bold red]Failed to purge stale bootstrap state:[/bold red] {e}"
            )
            sys.exit(1)

    console.print(
        f"[bold]Creating {bootstrap_count} bootstrap VM(s) for {args.name}...[/bold]"
    )
    try:
        bootstrap_nodes = create_bootstrap_vms(
            args.name,
            count=bootstrap_count,
            volume_size=args.attached_volume_size,
            console=console,
        )
        for node in bootstrap_nodes:
            console.print(
                f"[green]  Bootstrap #{node['bootstrap_index']}: "
                f"{node['ip']} ({node['region']})[/green]"
            )
    except Exception as e:
        console.print(f"[bold red]Failed to create bootstrap VMs:[/bold red] {e}")
        sys.exit(1)

    # Persist state. If any pre-existing state slice (e.g. leftover regions
    # from a previous infra-nodes run) is present it's preserved via
    # update_deployment_state; otherwise we write a fresh file.
    try:
        if existing_state is None:
            save_deployment_state(
                args.name,
                regions=[],
                terraform_variables={
                    "name": args.name,
                    "attached_volume_size": str(args.attached_volume_size),
                    "providers": {},
                },
                bootstrap_nodes=bootstrap_nodes,
                vm_ips={},
                vm_ids={},
            )
        else:
            update_deployment_state(
                args.name,
                {
                    "bootstrap_nodes": bootstrap_nodes,
                    "bootstrap_ip": bootstrap_nodes[0]["ip"],
                },
            )
    except Exception as e:
        console.print(
            f"[bold red]Failed to save deployment state after bootstraps:[/bold red] {e}"
        )
        console.print(
            "[bold red]Aborting — without saved state, destroy-bootstraps "
            "cannot clean these up.[/bold red]"
        )
        sys.exit(1)

    console.print("[dim]Bootstrap state saved to S3.[/dim]")
    record_event(
        args.name,
        "infra_bootstraps_completed",
        {"count": bootstrap_count},
    )
