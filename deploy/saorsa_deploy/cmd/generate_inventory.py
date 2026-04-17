import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from saorsa_deploy.state import load_deployment_state

INVENTORY_SCHEMA_VERSION = 3
DEFAULT_SSH_USER = "root"
DEFAULT_SSH_KEY = "~/.ssh/id_rsa"
DEFAULT_OUTPUT_TEMPLATE = "~/.autonomi/testnets/{name}/inventory.json"

FIREWALL_PLACEHOLDER = "managed by terraform"


def _default_output_path(name: str) -> Path:
    return Path(os.path.expanduser(DEFAULT_OUTPUT_TEMPLATE.format(name=name)))


def _build_binary_field(state: dict) -> dict:
    binary = state.get("binary") or {}
    version = binary.get("version")
    branch = binary.get("branch")
    owner = binary.get("repo_owner")
    url = binary.get("url", "")

    if version:
        label = version
    elif branch and owner:
        label = f"branch:{owner}/{branch}"
    else:
        label = ""

    return {
        "ant_node_version": label,
        "ant_node_release_url": url,
    }


def _build_firewalls_field(state: dict) -> dict:
    providers_used = set()
    for key in (state.get("vm_ips") or {}):
        providers_used.add(key.split("/", 1)[0])
    if state.get("bootstrap_nodes"):
        providers_used.add("digitalocean")
    return {provider: FIREWALL_PLACEHOLDER for provider in sorted(providers_used)}


def _build_node_vms_field(state: dict, ssh_key: str, node_count: int) -> list[dict]:
    name = state["name"]
    vm_ips = state.get("vm_ips") or {}
    vm_ids = state.get("vm_ids") or {}
    services = [f"ant-node-{i + 1}" for i in range(node_count)]
    nodes = [{"service_name": svc} for svc in services]

    entries = []
    for key in sorted(vm_ips.keys()):
        provider, region = key.split("/", 1)
        ips = vm_ips[key]
        ids = vm_ids.get(key, [])
        for index, ip in enumerate(ips):
            provider_id = ids[index] if index < len(ids) else ""
            entries.append(
                {
                    "name": f"{name}-ant-node-{region}-{index + 1}",
                    "provider": provider,
                    "provider_id": provider_id,
                    "region": region,
                    "ip": ip,
                    "ssh_user": DEFAULT_SSH_USER,
                    "ssh_key": ssh_key,
                    "nodes": nodes,
                    "services": list(services),
                }
            )
    return entries


def _build_bootstrap_vms_field(state: dict, ssh_key: str) -> list[dict]:
    name = state["name"]
    bootstrap_nodes = state.get("bootstrap_nodes") or []

    entries = []
    for node in sorted(bootstrap_nodes, key=lambda n: n.get("bootstrap_index", 0)):
        index = node["bootstrap_index"]
        service = f"ant-bootstrap-{index}"
        entries.append(
            {
                "name": f"{name}-ant-bootstrap-{index}",
                "provider": "digitalocean",
                "provider_id": node.get("provider_id", ""),
                "region": node["region"],
                "ip": node["ip"],
                "bootstrap_index": index,
                "ssh_user": DEFAULT_SSH_USER,
                "ssh_key": ssh_key,
                "nodes": [{"service_name": service}],
                "services": [service],
            }
        )
    return entries


def build_inventory(
    state: dict,
    ssh_key: str = DEFAULT_SSH_KEY,
    node_count: int | None = None,
    created_at: str | None = None,
) -> dict:
    """Build the schema-version-3 inventory dict from a loaded deployment state.

    Pure function, easy to unit test.
    """
    resolved_node_count = node_count if node_count is not None else state.get("node_count")
    if resolved_node_count is None:
        raise ValueError(
            "node_count is missing from deployment state. "
            "Run 'provision' first (or pass --node-count to override)."
        )

    created = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "testnet_id": state["name"],
        "created_at": created,
        "binary": _build_binary_field(state),
        "firewalls": _build_firewalls_field(state),
        "bootstrap_vms": _build_bootstrap_vms_field(state, ssh_key),
        "node_vms": _build_node_vms_field(state, ssh_key, resolved_node_count),
    }


def cmd_generate_inventory(args):
    """Generate a schema-version-3 inventory.json for a deployment."""
    console = Console()

    console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
    try:
        state = load_deployment_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    try:
        inventory = build_inventory(
            state,
            ssh_key=args.ssh_key_path,
            node_count=args.node_count,
        )
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    if args.output:
        output_path = Path(os.path.expanduser(args.output))
    else:
        output_path = _default_output_path(args.name)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(inventory, indent=2) + "\n")

    node_vm_count = len(inventory["node_vms"])
    services_per_vm = len(inventory["node_vms"][0]["services"]) if node_vm_count else 0
    total_nodes = node_vm_count * services_per_vm
    bootstrap_vm_count = len(inventory["bootstrap_vms"])
    console.print(f"[bold green]Wrote inventory to {output_path}[/bold green]")
    console.print(f"  {bootstrap_vm_count} bootstrap VM(s)")
    console.print(
        f"  {node_vm_count} node VMs × {services_per_vm} service(s) each = {total_nodes} nodes"
    )
