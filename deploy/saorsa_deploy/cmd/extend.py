import copy
import sys

from rich.console import Console

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


def cmd_extend(args):
    """Seed an extension testnet's deployment state from a source testnet.

    The extension shares the source's bootstrap VMs. It gets its own node
    VMs (provisioned later via the normal ``nodes infra`` / ``nodes provision``
    pipeline) and records ``shared_bootstrap_from`` in state so destroys can
    refuse to yank the bootstraps out from under it.
    """
    console = Console()

    name = args.name
    source = args.source
    force = getattr(args, "force", False)

    if name == source:
        console.print(
            "[bold red]Error:[/bold red] --name and --source must be different."
        )
        sys.exit(1)

    source_state = _try_load_deployment_state(source)
    if source_state is None:
        console.print(
            f"[bold red]Error:[/bold red] No deployment state found for source "
            f"'{source}'. Deploy the source testnet first, or check the name."
        )
        sys.exit(1)

    source_bootstraps = source_state.get("bootstrap_nodes") or []
    if not source_bootstraps:
        console.print(
            f"[bold red]Error:[/bold red] Source '{source}' has no bootstrap "
            "nodes recorded. The source must be fully deployed before it can "
            "be extended."
        )
        sys.exit(1)

    source_port = source_state.get("bootstrap_port")
    copied_bootstraps = copy.deepcopy(source_bootstraps)
    copied_ip = source_state.get("bootstrap_ip") or copied_bootstraps[0]["ip"]

    existing_state = _try_load_deployment_state(name)

    if existing_state is not None:
        existing_source = existing_state.get("shared_bootstrap_from")
        if existing_source is None:
            console.print(
                f"[bold red]Error:[/bold red] Deployment '{name}' already "
                "exists as a regular (non-extension) deployment. Refusing to "
                "overwrite — destroy it first or pick a different --name."
            )
            sys.exit(1)
        if existing_source != source and not force:
            console.print(
                f"[bold red]Error:[/bold red] Deployment '{name}' is already "
                f"extending '{existing_source}', not '{source}'. Re-run with "
                "--force to repoint it."
            )
            sys.exit(1)

        updates = {
            "bootstrap_nodes": copied_bootstraps,
            "bootstrap_ip": copied_ip,
            "shared_bootstrap_from": source,
        }
        if source_port is not None:
            updates["bootstrap_port"] = source_port
        try:
            update_deployment_state(name, updates)
        except Exception as e:
            console.print(
                f"[bold red]Failed to update deployment state:[/bold red] {e}"
            )
            sys.exit(1)
        record_event(
            name,
            "extend_refreshed",
            {
                "source": source,
                "bootstrap_count": len(copied_bootstraps),
                "previous_source": existing_source,
            },
        )
        console.print(
            f"[bold green]Extension '{name}' refreshed from '{source}' "
            f"({len(copied_bootstraps)} bootstrap node(s)).[/bold green]"
        )
        return

    deploy_args = {
        "name": name,
        "shared_bootstrap_from": source,
        "bootstrap_count": len(copied_bootstraps),
    }
    record_deployment_created(name, deploy_args)
    record_event(
        name,
        "extend_started",
        {"source": source, "bootstrap_count": len(copied_bootstraps)},
    )

    try:
        save_deployment_state(
            name,
            regions=[],
            terraform_variables={
                "name": name,
                "attached_volume_size": "20",
                "providers": {},
            },
            bootstrap_nodes=copied_bootstraps,
            vm_ips={},
            vm_ids={},
        )
        followup = {"shared_bootstrap_from": source}
        if source_port is not None:
            followup["bootstrap_port"] = source_port
        update_deployment_state(name, followup)
    except Exception as e:
        console.print(
            f"[bold red]Failed to save deployment state:[/bold red] {e}"
        )
        sys.exit(1)

    console.print(
        f"[bold]Extended '{name}' from '{source}'[/bold] "
        f"({len(copied_bootstraps)} bootstrap node(s)):"
    )
    for node in copied_bootstraps:
        console.print(
            f"[green]  Bootstrap #{node['bootstrap_index']}: "
            f"{node['ip']} ({node['region']})[/green]"
        )
    if source_port is not None:
        console.print(f"[dim]  Bootstrap port: {source_port}[/dim]")
    console.print()
    console.print(
        f"[dim]Next: `saorsa-deploy nodes infra --name {name} ...` "
        f"(do NOT run bootstraps infra / nodes provision-genesis / "
        "bootstraps provision for an extension).[/dim]"
    )

    record_event(
        name,
        "extend_completed",
        {"source": source, "bootstrap_count": len(copied_bootstraps)},
    )
