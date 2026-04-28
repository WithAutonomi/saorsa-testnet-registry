import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from saorsa_deploy.evm import get_evm_network_info
from saorsa_deploy.provisioning.volume import MOUNT_POINT
from saorsa_deploy.state import load_client_state, load_deployment_state

INVENTORY_SCHEMA_VERSION = 4
DEFAULT_SSH_USER = "root"
DEFAULT_SSH_KEY = "~/.ssh/id_rsa"
DEFAULT_OUTPUT_TEMPLATE = "~/.autonomi/testnets/{name}/inventory.json"
DEVNET_MANIFEST_FILENAME = "devnet-manifest.json"
DEVNET_DATA_DIR = f"{MOUNT_POINT}/data"
DEVNET_EVM_NETWORK = "arbitrum-sepolia"

FIREWALL_PLACEHOLDER = "managed by terraform"
CLIENT_SERVICES = ("ant-uploader", "ant-downloader")


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


def _build_firewalls_field(state: dict, clients: list[dict] | None = None) -> dict:
    providers_used = set()
    for key in (state.get("vm_ips") or {}):
        providers_used.add(key.split("/", 1)[0])
    if state.get("bootstrap_nodes"):
        providers_used.add("digitalocean")
    if clients:
        providers_used.add("digitalocean")
    return {provider: FIREWALL_PLACEHOLDER for provider in sorted(providers_used)}


def _build_node_vms_field(state: dict, ssh_key: str, node_count: int) -> list[dict]:
    name = state["name"]
    vm_ips = state.get("vm_ips") or {}
    vm_ids = state.get("vm_ids") or {}
    nat_ips = set(state.get("nat_vm_ips") or [])
    services = [f"ant-node-{i + 1}" for i in range(node_count)]

    entries = []
    for key in sorted(vm_ips.keys()):
        provider, region = key.split("/", 1)
        ips = vm_ips[key]
        ids = vm_ids.get(key, [])
        for index, ip in enumerate(ips):
            provider_id = ids[index] if index < len(ids) else ""
            is_nat = ip in nat_ips
            entries.append(
                {
                    "name": f"{name}-ant-node-{region}-{index + 1}",
                    "provider": provider,
                    "provider_id": provider_id,
                    "region": region,
                    "ip": ip,
                    "ssh_user": DEFAULT_SSH_USER,
                    "ssh_key": ssh_key,
                    "nat_simulated": is_nat,
                    "nodes": [
                        {"service_name": svc, "nat_simulated": is_nat}
                        for svc in services
                    ],
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


def _build_client_vms_field(
    state: dict, clients: list[dict] | None, ssh_key: str
) -> list[dict]:
    if not clients:
        return []
    name = state["name"]
    services = list(CLIENT_SERVICES)
    nodes = [{"service_name": svc} for svc in services]

    entries = []
    for client in sorted(clients, key=lambda c: c.get("client_index", 0)):
        index = client["client_index"]
        region = client["region"]
        entries.append(
            {
                "name": f"{name}-ant-client-{region}-{index}",
                "provider": "digitalocean",
                "provider_id": client.get("provider_id", ""),
                "region": region,
                "ip": client.get("ip", ""),
                "client_index": index,
                "file_size_mb": client.get("file_size_mb"),
                "vm_size": client.get("vm_size", ""),
                "ssh_user": DEFAULT_SSH_USER,
                "ssh_key": ssh_key,
                "nodes": list(nodes),
                "services": list(services),
            }
        )
    return entries


def build_inventory(
    state: dict,
    ssh_key: str = DEFAULT_SSH_KEY,
    node_count: int | None = None,
    created_at: str | None = None,
    clients: list[dict] | None = None,
) -> dict:
    """Build the schema-version-4 inventory dict from a loaded deployment state.

    Pure function, easy to unit test. `clients` is optional; pass the list from
    client state when present, or omit/None for deployments without client VMs.
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
        "firewalls": _build_firewalls_field(state, clients),
        "bootstrap_vms": _build_bootstrap_vms_field(state, ssh_key),
        "node_vms": _build_node_vms_field(state, ssh_key, resolved_node_count),
        "client_vms": _build_client_vms_field(state, clients, ssh_key),
    }


def _bootstrap_multiaddrs(state: dict) -> list[str]:
    bootstrap_port = state.get("bootstrap_port")
    bootstrap_nodes = state.get("bootstrap_nodes") or []
    multiaddrs = []
    for node in sorted(bootstrap_nodes, key=lambda n: n.get("bootstrap_index", 0)):
        port = node.get("port", bootstrap_port)
        multiaddrs.append(f"/ip4/{node['ip']}/udp/{port}/quic")
    return multiaddrs


def _total_node_count(state: dict, node_count_per_vm: int) -> int:
    vm_ips = state.get("vm_ips") or {}
    node_vm_count = sum(len(ips) for ips in vm_ips.values())
    return node_count_per_vm * node_vm_count


def build_devnet_manifest(
    state: dict,
    wallet_private_key: str,
    evm_network: str = DEVNET_EVM_NETWORK,
    node_count: int | None = None,
    created_at: str | None = None,
) -> dict:
    """Build the `ant_node::devnet::DevnetManifest` dict for a deployment.

    Pure function for testability. Shape matches ant-node byte-for-byte so
    that `serde_json::from_str::<DevnetManifest>` accepts the output.

    Args:
        state: Loaded deployment state (from `load_deployment_state`).
        wallet_private_key: EVM wallet private key to embed in the manifest's
            `evm` block.
        evm_network: EVM network key into `saorsa_deploy.evm` (default
            arbitrum-sepolia).
        node_count: Nodes-per-VM; defaults to `state['node_count']`. The
            manifest emits the cluster total (per-VM × node VM count).
        created_at: Override timestamp (for deterministic tests). Default is
            `datetime.now(UTC).isoformat()`.
    """
    resolved_per_vm = node_count if node_count is not None else state.get("node_count")
    if resolved_per_vm is None:
        raise ValueError(
            "node_count is missing from deployment state. "
            "Run 'provision' first (or pass --node-count to override)."
        )

    bootstrap_port = state.get("bootstrap_port")
    if not bootstrap_port:
        raise ValueError(
            "bootstrap_port is missing from deployment state. "
            "Has the nodes provision-genesis command been run?"
        )

    evm_info = get_evm_network_info(evm_network)
    created = created_at or datetime.now(timezone.utc).isoformat()

    return {
        "base_port": bootstrap_port,
        "node_count": _total_node_count(state, resolved_per_vm),
        "bootstrap": _bootstrap_multiaddrs(state),
        "data_dir": DEVNET_DATA_DIR,
        "created_at": created,
        "evm": {
            "rpc_url": evm_info["rpc_url"],
            "wallet_private_key": wallet_private_key,
            "payment_token_address": evm_info["payment_token_address"],
            "payment_vault_address": evm_info["payment_vault_address"],
        },
    }


def _resolve_wallet_secret_key(args) -> str:
    """Resolve the wallet secret key from args or env.

    Priority: --wallet-secret-key > ANT_DEPLOY_SECRET_KEY. Exits on miss.
    """
    key = getattr(args, "wallet_secret_key", None) or os.environ.get(
        "ANT_DEPLOY_SECRET_KEY"
    )
    if not key:
        Console().print(
            "[bold red]Error:[/bold red] No wallet secret key provided. "
            "Use --wallet-secret-key or set ANT_DEPLOY_SECRET_KEY."
        )
        sys.exit(1)
    return key


def cmd_generate_inventory(args):
    """Generate a schema-version-4 inventory.json for a deployment."""
    console = Console()

    wallet_secret_key = _resolve_wallet_secret_key(args)

    console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
    try:
        state = load_deployment_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    clients: list[dict] = []
    try:
        client_state = load_client_state(args.name)
    except RuntimeError:
        client_state = None
    if client_state:
        clients = client_state.get("clients") or []

    try:
        inventory = build_inventory(
            state,
            ssh_key=args.ssh_key_path,
            node_count=args.node_count,
            clients=clients,
        )
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    try:
        manifest = build_devnet_manifest(
            state,
            wallet_private_key=wallet_secret_key,
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

    manifest_path = output_path.parent / DEVNET_MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    node_vm_count = len(inventory["node_vms"])
    services_per_vm = len(inventory["node_vms"][0]["services"]) if node_vm_count else 0
    total_nodes = node_vm_count * services_per_vm
    bootstrap_vm_count = len(inventory["bootstrap_vms"])
    client_vm_count = len(inventory["client_vms"])
    nat_vm_count = sum(1 for vm in inventory["node_vms"] if vm.get("nat_simulated"))
    console.print(f"[bold green]Wrote inventory to {output_path}[/bold green]")
    console.print(f"  {bootstrap_vm_count} bootstrap VM(s)")
    nat_suffix = f" ({nat_vm_count} NAT-simulated)" if nat_vm_count else ""
    console.print(
        f"  {node_vm_count} node VMs × {services_per_vm} service(s) each = "
        f"{total_nodes} nodes{nat_suffix}"
    )
    console.print(f"  {client_vm_count} client VM(s)")
    console.print(f"[bold green]Wrote devnet manifest to {manifest_path}[/bold green]")
    console.print(
        f"  {len(manifest['bootstrap'])} bootstrap multiaddr(s), "
        f"{manifest['node_count']} nodes total, "
        f"evm={DEVNET_EVM_NETWORK}"
    )
