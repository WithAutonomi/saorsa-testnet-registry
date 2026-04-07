"""Bootstrap production network command.

Deploys 7 hardened bootstrap nodes across 4 cloud providers with static IPs,
striped LVM volumes, and systemd-managed ant-node services.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from saorsa_deploy.binary_source import (
    check_release_exists,
    get_release_url,
)
from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.provisioning.hardening import (
    build_systemd_unit,
)
from saorsa_deploy.provisioning.volume import (
    MOUNT_POINT,
    build_striped_volume_command,
)
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import (
    check_terraform_state_exists,
    delete_terraform_state,
)
from saorsa_deploy.terraform import TerraformRunConfig

PRODUCTION_IPS_FILE = "production-ips.json"

# Hetzner server type fallbacks per location.
# Each entry is a list of (type_name, type_id) tuples tried in order.
HETZNER_TYPE_FALLBACKS = {
    "fsn1": [("cpx22", 109), ("cx23", 114)],
    "nbg1": [("cpx22", 109), ("cx23", 114)],
    "hel1": [("cpx22", 109), ("cx23", 114)],
    "ash": [("cpx21", 23), ("cpx31", 24)],
    "hil": [("cpx21", 23), ("cpx31", 24)],
}
NODE_PORT = 10000
BINARY_INSTALL_PATH = "/usr/local/bin/ant-node"
SERVICE_NAME = "ant-bootstrap"
DATA_DIR = f"{MOUNT_POINT}/data/{SERVICE_NAME}"
UNIT_FILE_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"
SSH_KEY_PATH = "~/.ssh/ant-production"
SERVICE_USER = "ant"

# Provider-specific SSH users (most use root, AWS uses ubuntu)
PROVIDER_SSH_USERS = {
    "aws": "ubuntu",
}

# Map of provider -> Terraform resource directory name
PROVIDER_TF_DIRS = {
    "hetzner": "hetzner-production",
    "vultr": "vultr-production",
    "aws": "aws-production",
}

# S3 state key prefix for production Terraform state
STATE_KEY_PREFIX = "saorsa-deploy/production"


def _load_production_config(console: Console) -> tuple[list[dict], dict[str, str]]:
    """Load production node and SSH key configuration from the repo.

    Returns (nodes, ssh_key_ids).
    """
    ips_file = Path(__file__).parent.parent.parent / PRODUCTION_IPS_FILE
    if not ips_file.exists():
        console.print(
            f"[bold red]Error:[/bold red] {PRODUCTION_IPS_FILE} not found at {ips_file}"
        )
        sys.exit(1)

    with open(ips_file) as f:
        data = json.load(f)

    nodes = data.get("nodes", [])
    if not nodes:
        console.print(f"[bold red]Error:[/bold red] No nodes defined in {PRODUCTION_IPS_FILE}")
        sys.exit(1)

    ssh_keys = data.get("ssh_keys", {})
    if not ssh_keys:
        console.print(
            f"[bold red]Error:[/bold red] No ssh_keys defined in {PRODUCTION_IPS_FILE}. "
            "Register keys with each provider and add their IDs to the file."
        )
        sys.exit(1)

    # Verify all providers in nodes have a corresponding SSH key
    providers = {node["provider"] for node in nodes}
    missing_keys = providers - set(ssh_keys.keys())
    if missing_keys:
        console.print(
            f"[bold red]Error:[/bold red] Missing SSH key IDs for providers: "
            f"{', '.join(sorted(missing_keys))}"
        )
        sys.exit(1)

    return nodes, ssh_keys


def _build_terraform_configs(
    nodes: list[dict],
    name: str,
    ssh_key_ids: dict[str, str],
    console: Console,
) -> list[TerraformRunConfig]:
    """Build Terraform configs for all production nodes."""
    resources_dir = get_resources_dir()
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"
    configs = []

    for node in nodes:
        provider = node["provider"]
        region = node["region"]
        resource_id = node.get("resource_id")

        tf_dir = PROVIDER_TF_DIRS.get(provider)
        if not tf_dir:
            console.print(
                f"[bold red]Error:[/bold red] No Terraform manifests for provider '{provider}'"
            )
            sys.exit(1)

        tf_source = resources_dir / tf_dir
        workspace_dir = workspace_base / f"production-{provider}-{region}"
        state_key = f"{STATE_KEY_PREFIX}/{name}-{provider}-{region}.tfstate"

        variables = {
            "name": name,
        }

        if provider == "hetzner":
            variables["location"] = region
            variables["floating_ip_id"] = resource_id
            variables["ssh_key_id"] = ssh_key_ids[provider]
            if node.get("server_type"):
                variables["server_type"] = node["server_type"]
        elif provider == "vultr":
            variables["region"] = region
            variables["reserved_ip_id"] = resource_id
            variables["ssh_key_id"] = ssh_key_ids[provider]
        elif provider == "aws":
            variables["region"] = region
            variables["eip_allocation_id"] = resource_id
            variables["key_pair_name"] = ssh_key_ids[provider]

        configs.append(
            TerraformRunConfig(
                provider=provider,
                region=region,
                tf_source_dir=tf_source,
                workspace_dir=workspace_dir,
                state_key=state_key,
                variables=variables,
            )
        )

    return configs


def _provision_node(
    ip: str,
    provider: str,
    ssh_key_path: str,
    binary_url: str,
    binary_is_archive: bool,
    bootstrap_peers: str | None,
    rewards_address: str,
    evm_network: str,
    console: Console,
    ssh_ip: str | None = None,
) -> None:
    """Provision a single production node: striped volume, binary, service."""
    from io import StringIO

    from pyinfra.api import Config, Inventory, State
    from pyinfra.api.connect import connect_all, disconnect_all
    from pyinfra.api.operation import add_op
    from pyinfra.api.operations import run_ops
    from pyinfra.operations import files, server, systemd

    from saorsa_deploy.binary_source import RELEASE_ASSET_NAME

    connect_addr = ssh_ip or ip
    ssh_user = PROVIDER_SSH_USERS.get(provider, "root")
    connect_opts = {"ssh_user": ssh_user, "ssh_key": ssh_key_path}
    inventory = Inventory(
        ([(connect_addr, connect_opts)], {})
    )
    config = Config()
    if ssh_user != "root":
        config.SUDO = True
        config.USE_SUDO_PASSWORD = False
    state = State(inventory=inventory, config=config)
    connect_all(state)

    try:
        # Striped volume
        add_op(
            state,
            server.shell,
            name="Create striped LVM volume",
            commands=[build_striped_volume_command(provider)],
        )

        # Configure Hetzner floating IP on the network interface
        if provider == "hetzner":
            netplan_cfg = (
                "network:\\n"
                "  version: 2\\n"
                "  ethernets:\\n"
                "    eth0:\\n"
                "      addresses:\\n"
                f"        - {ip}/32"
            )
            route_cmd = (
                f"GATEWAY=$(ip route show default | awk '{{print $3}}') && "
                f"ip route replace default via $GATEWAY dev eth0 src {ip}"
            )
            add_op(
                state,
                server.shell,
                name="Configure Hetzner floating IP",
                commands=[
                    f"ip addr add {ip}/32 dev eth0 2>/dev/null || true",
                    "mkdir -p /etc/netplan",
                    f"printf '{netplan_cfg}\\n' > /etc/netplan/60-floating-ip.yaml",
                    "netplan apply 2>/dev/null || true",
                    route_cmd,
                ],
            )

        # Create directories and set ownership
        add_op(
            state,
            server.shell,
            name="Create data directory",
            commands=[
                f"mkdir -p {DATA_DIR}",
                f"chown -R {SERVICE_USER}:{SERVICE_USER} {MOUNT_POINT}",
            ],
        )

        # Install binary
        if binary_is_archive:
            install_cmd = (
                f"test -f {BINARY_INSTALL_PATH} "
                f"&& echo 'ANT_BINARY:SKIP' || "
                f"(wget -q {binary_url} -O /tmp/{RELEASE_ASSET_NAME} && "
                f"tar -xzf /tmp/{RELEASE_ASSET_NAME} -C /tmp/ && "
                f"mv /tmp/ant-node {BINARY_INSTALL_PATH} && "
                f"chmod +x {BINARY_INSTALL_PATH} && "
                f"rm -f /tmp/{RELEASE_ASSET_NAME} && "
                f"echo 'ANT_BINARY:INSTALLED')"
            )
        else:
            install_cmd = (
                f"test -f {BINARY_INSTALL_PATH} "
                f"&& echo 'ANT_BINARY:SKIP' || "
                f"(wget -q {binary_url} -O {BINARY_INSTALL_PATH} && "
                f"chmod +x {BINARY_INSTALL_PATH} && "
                f"echo 'ANT_BINARY:INSTALLED')"
            )

        add_op(
            state,
            server.shell,
            name="Download and install ant-node binary",
            commands=[install_cmd],
        )

        # Systemd unit
        unit_content = build_systemd_unit(
            binary_path=BINARY_INSTALL_PATH,
            data_dir=DATA_DIR,
            port=NODE_PORT,
            service_user=SERVICE_USER,
            bootstrap_peers=bootstrap_peers,
            rewards_address=rewards_address,
            evm_network=evm_network,
        )
        add_op(
            state,
            files.put,
            name="Write systemd unit file",
            src=StringIO(unit_content),
            dest=UNIT_FILE_PATH,
            mode="644",
            add_deploy_dir=False,
        )

        add_op(state, systemd.daemon_reload, name="Reload systemd daemon")

        add_op(
            state,
            systemd.service,
            name="Enable and start production node service",
            service=SERVICE_NAME,
            running=True,
            enabled=True,
        )

        run_ops(state)
    finally:
        disconnect_all(state)


def _confirm_hetzner_token(console: Console) -> None:
    """Warn the user to verify their Hetzner token is for the ant-production project."""
    console.print()
    console.print(
        "[bold yellow]WARNING:[/bold yellow] This command will create resources in Hetzner Cloud."
    )
    console.print(
        "Please confirm that your [bold]HCLOUD_TOKEN[/bold] environment variable "
        "is set to the token for the [bold]ant-production[/bold] project."
    )
    console.print()
    response = input("Is your HCLOUD_TOKEN for the ant-production project? [y/N] ")
    if response.strip().lower() not in ("y", "yes"):
        console.print("[bold red]Aborted.[/bold red] Please set the correct HCLOUD_TOKEN.")
        sys.exit(1)


def _resolve_hetzner_availability(nodes: list[dict], console: Console) -> None:
    """Check Hetzner server type availability and fall back if needed.

    For each Hetzner node, queries the Hetzner datacenters API to verify the
    configured server_type is available in the configured location. If not,
    tries fallback server types in the same location (since primary IPs are
    location-bound, we cannot fall back to a different location).

    Updates node dicts in-place if a fallback type is found.
    Exits with an error if no type is available in the node's location.
    """
    token = os.environ.get("HCLOUD_TOKEN")
    if not token:
        # Already checked in the env var block, but be safe
        console.print(
            "[bold red]Error:[/bold red] HCLOUD_TOKEN not set"
        )
        sys.exit(1)

    hetzner_nodes = [n for n in nodes if n["provider"] == "hetzner"]
    if not hetzner_nodes:
        return

    result = subprocess.run(
        [
            "curl", "-sf",
            "-H", f"Authorization: Bearer {token}",
            "https://api.hetzner.cloud/v1/datacenters",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(
            "[bold red]Error:[/bold red] Failed to query Hetzner datacenters API"
        )
        sys.exit(1)

    data = json.loads(result.stdout)

    # Build availability map: {location: {type_id: bool}}
    avail: dict[str, dict[int, bool]] = {}
    for dc in data["datacenters"]:
        loc = dc["location"]["name"]
        avail[loc] = {
            tid: (tid in dc["server_types"]["available"])
            for tid in dc["server_types"]["supported"]
        }

    for node in hetzner_nodes:
        location = node["region"]
        current_type = node.get("server_type")

        if location not in avail:
            console.print(
                f"[bold red]Error:[/bold red] Hetzner location '{location}' not found "
                f"in datacenters API response"
            )
            sys.exit(1)

        # Check if current type is available
        fallbacks = HETZNER_TYPE_FALLBACKS.get(location, [])
        current_type_id = None
        for type_name, type_id in fallbacks:
            if type_name == current_type:
                current_type_id = type_id
                break

        if current_type_id is not None and avail[location].get(current_type_id, False):
            # Current type is available, nothing to do
            continue

        # Current type is unavailable, try fallbacks in same location
        resolved = None
        for type_name, type_id in fallbacks:
            if avail[location].get(type_id, False):
                resolved = type_name
                break

        if resolved:
            console.print(
                f"[bold yellow]Warning:[/bold yellow] Hetzner {location}: "
                f"'{current_type}' unavailable, falling back to '{resolved}'"
            )
            node["server_type"] = resolved
        else:
            tried = ", ".join(t for t, _ in fallbacks) if fallbacks else current_type or "none"
            console.print(
                f"[bold red]Error:[/bold red] Hetzner {location}: no server type available "
                f"(tried: {tried}). You may need to switch to a different Hetzner location, "
                f"which requires reallocating the primary IP in the new location."
            )
            sys.exit(1)


def cmd_bootstrap_production(args):
    """Execute the bootstrap-production command."""
    console = Console()
    name = args.name

    console.print(f"[bold]Bootstrap Production Network: {name}[/bold]")
    console.print()

    # Check required environment variables
    required_env = {
        "HCLOUD_TOKEN": "Hetzner",
        "VULTR_API_KEY": "Vultr",
    }
    missing = [f"{desc} ({var})" for var, desc in required_env.items() if not os.environ.get(var)]
    if missing:
        console.print("[bold red]Error:[/bold red] Missing required environment variables:")
        for m in missing:
            console.print(f"  - {m}")
        sys.exit(1)

    # AWS uses default credential chain (env vars or ~/.aws/credentials)
    try:
        subprocess.run(
            ["aws", "sts", "get-caller-identity"],
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        console.print(
            "[bold red]Error:[/bold red] AWS credentials not configured. "
            "Set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or configure ~/.aws/credentials"
        )
        sys.exit(1)

    # Hetzner token confirmation
    if not args.force:
        _confirm_hetzner_token(console)

    # Load IPs and SSH key IDs
    nodes, ssh_key_ids = _load_production_config(console)

    # Check Hetzner server type availability and fall back if needed
    _resolve_hetzner_availability(nodes, console)

    # Display planned deployment
    table = Table(title="Production Bootstrap Nodes")
    table.add_column("#", style="dim")
    table.add_column("Provider")
    table.add_column("Region")
    table.add_column("IP")
    table.add_column("Role")

    for i, node in enumerate(nodes, 1):
        role = node.get("role", "node")
        table.add_row(str(i), node["provider"], node["region"], node["ip"], role)
    console.print(table)
    console.print()

    # Resolve binary
    console.print("Resolving ant-node binary...")
    if args.node_version:
        if not check_release_exists(args.node_version):
            console.print(
                f"[bold red]Error:[/bold red] Release v{args.node_version} not found"
            )
            sys.exit(1)
        binary_url = get_release_url(args.node_version)
        binary_is_archive = True
        console.print(f"  Using release: v{args.node_version}")
    else:
        binary_url = get_release_url()
        binary_is_archive = True
        console.print("  Using latest release")
    console.print()

    # Verify SSH private key exists
    ssh_key_path = args.ssh_key_path
    if not Path(ssh_key_path).expanduser().exists():
        console.print(
            f"[bold red]Error:[/bold red] SSH key not found at {ssh_key_path}. "
            f"Generate one with: ssh-keygen -t ed25519 -f {ssh_key_path}"
        )
        sys.exit(1)

    # Build Terraform configs
    configs = _build_terraform_configs(nodes, name, ssh_key_ids, console)

    # Check for stale state
    stale_keys = [c.state_key for c in configs if check_terraform_state_exists(c.state_key)]
    if stale_keys:
        if not args.force:
            console.print("[bold red]Error:[/bold red] Stale Terraform state found:")
            for key in stale_keys:
                console.print(f"  {key}")
            console.print("Use --force to delete and proceed.")
            sys.exit(1)
        for key in stale_keys:
            console.print(f"[yellow]Deleting stale state: {key}[/yellow]")
            delete_terraform_state(key)

    # Phase 1: Terraform — create all VMs in parallel
    console.print(f"[bold]Creating infrastructure across {len(configs)} provider/regions...[/bold]")
    console.print()

    results = execute_terraform_runs(configs)

    failures = [r for r in results if not r.success]
    if failures:
        for f in failures:
            console.print(f"[bold red]FAILED: {f.provider}/{f.region}[/bold red]")
            console.print(f"  stderr: {f.stderr[:500]}")
        console.print(f"[bold red]{len(failures)} region(s) failed. Aborting.[/bold red]")
        console.print(
            "[dim]Run 'destroy-bootstrap-production' to clean up created resources.[/dim]"
        )
        sys.exit(1)

    console.print(
        f"[bold green]All {len(results)} regions provisioned successfully.[/bold green]"
    )
    console.print()

    # Resolve SSH IPs — for Hetzner floating IPs, we need to SSH via the
    # server's auto-assigned primary IP, then configure the floating IP
    ssh_ips = {}
    for node in nodes:
        if node["provider"] == "hetzner":
            server_name = f"ant-bootstrap-{node['region']}"
            result = subprocess.run(
                ["hcloud", "server", "ip", server_name],
                capture_output=True, text=True,
            )
            primary_ip = result.stdout.strip()
            if primary_ip:
                ssh_ips[node["ip"]] = primary_ip
                console.print(
                    f"[dim]Hetzner {node['region']}: SSH via {primary_ip} "
                    f"(floating IP: {node['ip']})[/dim]"
                )

    # Collect all IPs for SSH known_hosts cleanup
    all_ips = list({node["ip"] for node in nodes} | set(ssh_ips.values()))
    clear_known_hosts(all_ips, console)

    # Phase 2: Harden all nodes in parallel (via subprocesses — Pyinfra is not thread-safe)
    console.print("[bold]Applying security hardening to all nodes...[/bold]")

    hardening_procs = {}
    for node in nodes:
        ip = node["ip"]
        connect_ip = ssh_ips.get(ip, ip)
        ssh_user = PROVIDER_SSH_USERS.get(node["provider"], "root")
        label = f"{node['provider']}/{node['region']}"
        proc = subprocess.Popen(
            [
                sys.executable, "-c",
                "from saorsa_deploy.provisioning.hardening import "
                "ProductionHardeningProvisioner; "
                f"ProductionHardeningProvisioner("
                f"ip='{connect_ip}', "
                f"ssh_key_path='{ssh_key_path}', "
                f"node_port={NODE_PORT}, "
                f"ssh_user='{ssh_user}'"
                f").execute()",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        hardening_procs[label] = proc

    hardening_errors = []
    for label, proc in hardening_procs.items():
        _, stderr = proc.communicate()
        if proc.returncode == 0:
            console.print(f"  [green]{label}[/green] hardened")
        else:
            err_msg = stderr.decode().strip().split("\n")[-1] if stderr else "unknown"
            console.print(f"  [bold red]{label} FAILED:[/bold red] {err_msg}")
            hardening_errors.append(label)

    if hardening_errors:
        console.print(
            f"[bold red]{len(hardening_errors)} node(s) failed hardening. Aborting.[/bold red]"
        )
        sys.exit(1)
    console.print()

    # Phase 3: Provision genesis node (must be first — others bootstrap from it)
    genesis_node = next((n for n in nodes if n.get("role") == "genesis"), None)
    if not genesis_node:
        console.print("[bold red]Error:[/bold red] No genesis node defined in production IPs")
        sys.exit(1)

    console.print(
        f"[bold]Provisioning genesis node at {genesis_node['ip']} "
        f"({genesis_node['provider']}/{genesis_node['region']})...[/bold]"
    )
    _provision_node(
        ip=genesis_node["ip"],
        provider=genesis_node["provider"],
        ssh_key_path=ssh_key_path,
        binary_url=binary_url,
        binary_is_archive=binary_is_archive,
        bootstrap_peers=None,
        rewards_address=args.rewards_address,
        evm_network=args.evm_network,
        console=console,
        ssh_ip=ssh_ips.get(genesis_node["ip"]),
    )
    console.print("[green]Genesis node provisioned.[/green]")
    console.print()

    # Phase 4: Provision remaining nodes in parallel
    genesis_peer = f"{genesis_node['ip']}:{NODE_PORT}"
    other_nodes = [n for n in nodes if n.get("role") != "genesis"]

    if not other_nodes:
        console.print("[dim]No remaining nodes to provision.[/dim]")
    else:
        console.print(
            f"[bold]Provisioning {len(other_nodes)} remaining nodes in parallel...[/bold]"
        )

    provision_procs = {}
    for node in other_nodes:
        ip = node["ip"]
        node_ssh_ip = ssh_ips.get(ip)
        ssh_user = PROVIDER_SSH_USERS.get(node["provider"], "root")
        label = f"{node['provider']}/{node['region']}"
        # Build the provision call as a subprocess script
        script = (
            "from saorsa_deploy.cmd.bootstrap_production import _provision_node; "
            "from rich.console import Console; "
            f"_provision_node("
            f"ip='{ip}', "
            f"provider='{node['provider']}', "
            f"ssh_key_path='{ssh_key_path}', "
            f"binary_url='{binary_url}', "
            f"binary_is_archive={binary_is_archive}, "
            f"bootstrap_peers='{genesis_peer}', "
            f"rewards_address='{args.rewards_address}', "
            f"evm_network='{args.evm_network}', "
            f"console=Console(), "
            f"ssh_ip={repr(node_ssh_ip)}"
            f")"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        provision_procs[label] = proc

    provision_errors = []
    for label, proc in provision_procs.items():
        _, stderr = proc.communicate()
        if proc.returncode == 0:
            console.print(f"  [green]{label}[/green] provisioned")
        else:
            err_msg = stderr.decode().strip().split("\n")[-1] if stderr else "unknown"
            console.print(f"  [bold red]{label} FAILED:[/bold red] {err_msg}")
            provision_errors.append(label)

    if provision_errors:
        console.print(
            f"[bold red]{len(provision_errors)} node(s) failed provisioning.[/bold red]"
        )
        sys.exit(1)
    console.print()

    console.print("[bold green]Production bootstrap network deployed successfully![/bold green]")
    console.print()
    console.print(f"  Genesis: {genesis_node['ip']}:{NODE_PORT}")
    console.print(f"  Peer address: {genesis_peer}")
    console.print(f"  Total nodes: {len(nodes)}")
