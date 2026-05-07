import shutil
import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    delete_client_addresses_prefix,
    delete_client_state,
    delete_terraform_state,
    list_terraform_state_keys,
    load_client_state,
    load_terraform_state,
    terraform_state_attributes,
)
from saorsa_deploy.terraform import TerraformRunConfig, run_terraform_destroy

CLIENT_STATE_KEY_PREFIX = "saorsa-deploy/do-client"
ROLE_UPLOAD = "upload"
ROLE_DOWNLOAD = "download"
VALID_ROLES = (ROLE_UPLOAD, ROLE_DOWNLOAD)


def cmd_destroy_clients(args):
    """Execute the clients destroy command: tear down client VMs."""
    console = Console()

    console.print(f"[bold]Loading client state for '{args.name}'...[/bold]")
    clients: list[dict] = []
    try:
        client_state = load_client_state(args.name)
        clients = client_state.get("clients", []) or []
    except RuntimeError:
        console.print(
            "[yellow]No client state in S3 — scanning for orphan client tfstate "
            "files...[/yellow]"
        )
        clients = _recover_clients_from_tfstate(args.name, console)

    if not clients:
        console.print(
            f"[yellow]No client infrastructure found for '{args.name}'. "
            "Nothing to destroy.[/yellow]"
        )
        return

    console.print(f"[dim]Found {len(clients)} client VM(s) to destroy.[/dim]")
    for c in clients:
        role = c.get("role", ROLE_UPLOAD)
        console.print(
            f"  [dim]{role} #{c['client_index']}: {c.get('ip', 'N/A')} "
            f"({c['region']}, {c['vm_size']})[/dim]"
        )
    console.print()

    if not args.force:
        confirm = input(
            f"This will destroy all client VMs for '{args.name}'. Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            console.print("[yellow]Aborted.[/yellow]")
            sys.exit(0)
        console.print()

    resources_dir = get_resources_dir()
    tf_source = resources_dir / "digitalocean-client"
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"

    configs = []
    for spec in clients:
        role = spec.get("role", ROLE_UPLOAD)
        workspace_dir = (
            workspace_base
            / f"do-client-{args.name}-{role}-{spec['region']}-{spec['client_index']}"
        )
        state_key = (
            f"{CLIENT_STATE_KEY_PREFIX}-{args.name}-{role}"
            f"-{spec['region']}-{spec['client_index']}.tfstate"
        )
        variables = {
            "name": args.name,
            "role": role,
            "region": spec["region"],
            "client_index": str(spec["client_index"]),
            "droplet_size": spec.get("vm_size", "s-2vcpu-4gb"),
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

    console.print(f"[bold]Destroying {len(configs)} client VM(s)...[/bold]")

    failures = []
    for config in configs:
        result = run_terraform_destroy(config)
        if not result.success:
            console.print(
                f"[red]Failed to destroy {config.region} "
                f"(client #{config.variables.get('client_index', '?')}):[/red]"
            )
            console.print(f"  {result.stderr[:500]}")
            failures.append(config)
        else:
            console.print(
                f"[green]  Destroyed client in {config.region} "
                f"(#{config.variables.get('client_index', '?')})[/green]"
            )
            # Clean up Terraform state
            try:
                delete_terraform_state(config.state_key)
            except Exception:
                pass

    # Clean up workspace directories
    for config in configs:
        if config.workspace_dir.exists():
            shutil.rmtree(config.workspace_dir, ignore_errors=True)

    if failures:
        console.print(
            f"[bold red]{len(failures)} client VM(s) failed to destroy.[/bold red]"
        )
        sys.exit(1)

    # Clean up client state
    try:
        delete_client_state(args.name)
        console.print("[dim]Client state deleted from S3.[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to delete client state: {e}[/yellow]")

    # Wipe the per-testnet addresses S3 folder so a future testnet with the
    # same name doesn't see stale uploader addresses.
    try:
        deleted = delete_client_addresses_prefix(args.name)
        if deleted:
            console.print(
                f"[dim]Deleted {deleted} address file(s) from "
                f"s3://.../saorsa-deploy/addresses/{args.name}/[/dim]"
            )
    except Exception as e:
        console.print(
            f"[yellow]Warning: Failed to delete addresses prefix: {e}[/yellow]"
        )

    console.print()
    console.print(
        f"[bold green]All {len(clients)} client VM(s) destroyed successfully.[/bold green]"
    )


def _recover_clients_from_tfstate(name: str, console: Console) -> list[dict]:
    """Reconstruct client specs from S3-backed tfstate when client state is missing.

    Used when an earlier clients infra run crashed before saving client state,
    leaving orphan droplets that clients destroy otherwise couldn't see.
    """
    prefix = f"{CLIENT_STATE_KEY_PREFIX}-{name}-"
    keys = list_terraform_state_keys(prefix)
    if not keys:
        return []

    recovered: list[dict] = []
    for key in keys:
        # Key shape: saorsa-deploy/do-client-{name}-{role}-{region}-{client_index}.tfstate
        suffix = key[len(prefix) : -len(".tfstate")]
        parts = suffix.split("-")
        if len(parts) < 3 or not parts[-1].isdigit() or parts[0] not in VALID_ROLES:
            console.print(
                f"[yellow]Skipping unparseable tfstate key: {key}[/yellow]"
            )
            continue
        role = parts[0]
        client_index = int(parts[-1])
        region = "-".join(parts[1:-1])

        vm_size = "s-2vcpu-4gb"
        try:
            tfstate = load_terraform_state(key)
            droplet = terraform_state_attributes(
                tfstate, "digitalocean_droplet", "client_vm"
            )
            if droplet and droplet.get("size"):
                vm_size = droplet["size"]
        except Exception:
            # Empty/corrupt tfstate (e.g. the 181-byte init-only files from a
            # failed create) — nothing to destroy; skip.
            continue

        entry = {
            "role": role,
            "region": region,
            "client_index": client_index,
            "vm_size": vm_size,
            "ip": "",
        }
        if role == ROLE_UPLOAD:
            entry["file_size_mb"] = 0
        recovered.append(entry)

    recovered.sort(key=lambda c: (c["role"], c["region"], c["client_index"]))
    console.print(
        f"[dim]Recovered {len(recovered)} client(s) from tfstate.[/dim]"
    )
    return recovered
