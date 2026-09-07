"""``deployments webrtc-addresses`` -- collect the browser seed addresses.

Reads every node-tier service's ``webrtc-direct.multiaddr`` off the deployment's
VMs and stores the results in S3 deployment state, so WASM/browser clients
(which cannot use the bare ``ip:port`` peers the native tier passes around) have
somewhere to bootstrap from.

Covers the NODE tier as well as the bootstraps, and that is the whole point --
see :func:`~saorsa_deploy.webrtc_addresses.spread_sample`. Runs after the nodes
are started, because the address embeds a certificate hash that exists only once
a node has run.
"""

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from rich.console import Console

from saorsa_deploy.state import load_deployment_state, update_deployment_state
from saorsa_deploy.webrtc_addresses import (
    MULTIADDR_GLOB,
    parse_multiaddr,
    rewrite_host,
    spread_sample,
)

_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
]

#: A listener binds a moment after its service starts, so a read taken straight
#: after `nodes start` can lose a race with a node still coming up. Poll rather
#: than fail: the alternative is a deploy dying at the last step with every VM
#: already created and billing.
DEFAULT_TIMEOUT_SECS = 180
_POLL_INTERVAL_SECS = 5

#: Seeds handed to each WASM client. Enough to cover the keyspace without
#: bloating the service environment file (~180 bytes per address).
DEFAULT_SAMPLE = 32


def _read_addresses(ip: str, ssh_key_path: str) -> list[str]:
    """Every browser address published on one VM, corrected to its public IP."""
    result = subprocess.run(
        ["ssh", "-i", ssh_key_path, *_SSH_OPTS, f"root@{ip}", f"cat {MULTIADDR_GLOB}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return []
    out = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            # ant-node advertises the external address its transport observed,
            # falling back to a routed local address and then loopback. Those
            # fallbacks are well-formed and useless to anyone else, and the
            # deployment already knows the VM's real public IP.
            out.append(rewrite_host(parse_multiaddr(line), ip))
        except ValueError:
            continue
    return out


def _collect(ip: str, ssh_key_path: str, timeout_secs: int, require: bool) -> tuple[str, list[str]]:
    deadline = time.monotonic() + timeout_secs
    while True:
        try:
            found = _read_addresses(ip, ssh_key_path)
        except subprocess.TimeoutExpired:
            found = []
        if found or not require or time.monotonic() >= deadline:
            return ip, found
        time.sleep(_POLL_INTERVAL_SECS)


def _vm_ips(state: dict) -> tuple[list[str], list[str]]:
    """(bootstrap VM IPs, node VM IPs) from deployment state."""
    boots = [n["ip"] for n in (state.get("bootstrap_nodes") or []) if n.get("ip")]
    if not boots and state.get("bootstrap_ip"):
        boots = [state["bootstrap_ip"]]
    nodes: list[str] = []
    for ips in (state.get("vm_ips") or {}).values():
        nodes.extend(ips)
    return boots, nodes


def cmd_webrtc_addresses(args):
    """Collect and persist the deployment's WebRTC-Direct seed addresses."""
    console = Console()

    state = load_deployment_state(args.name)
    boot_ips, node_ips = _vm_ips(state)
    if not boot_ips and not node_ips:
        console.print(f"[bold red]Error:[/bold red] no VMs in state for '{args.name}'")
        sys.exit(1)

    console.print(
        f"[bold]Collecting WebRTC-Direct addresses from {len(boot_ips)} bootstrap "
        f"and {len(node_ips)} node VM(s)...[/bold]"
    )

    targets = [(ip, True) for ip in boot_ips] + [(ip, False) for ip in node_ips]
    with ThreadPoolExecutor(max_workers=min(32, len(targets))) as pool:
        results = dict(
            pool.map(
                lambda t: _collect(t[0], args.ssh_key_path, args.timeout, t[1]),
                targets,
            )
        )

    bootstrap_addrs = [a for ip in boot_ips for a in results.get(ip, [])]
    node_addrs = [a for ip in node_ips for a in results.get(ip, [])]
    all_addrs = bootstrap_addrs + node_addrs

    silent = [ip for ip, found in results.items() if not found]
    console.print(
        f"  [green]{len(bootstrap_addrs)}[/green] bootstrap and "
        f"[green]{len(node_addrs)}[/green] node address(es) collected"
    )
    if silent:
        console.print(
            f"  [yellow]{len(silent)} VM(s) reported no address[/yellow]"
        )

    if not bootstrap_addrs:
        console.print(
            "[bold red]Error:[/bold red] no bootstrap advertised a WebRTC-Direct "
            "address. Is this deployment running an ant-node build with WebRTC "
            "support?"
        )
        sys.exit(1)

    # Seeds are sampled across the peer-id keyspace, not simply truncated: a
    # client can only walk toward a chunk if some seed is already near it.
    seeds = spread_sample(all_addrs, args.sample_nodes)
    update_deployment_state(
        args.name,
        {
            "bootstrap_webrtc_multiaddrs": bootstrap_addrs,
            "webrtc_seed_multiaddrs": seeds,
        },
    )
    console.print(
        f"[green]Saved {len(bootstrap_addrs)} bootstrap address(es) and "
        f"{len(seeds)} client seed(s) to deployment state.[/green]"
    )
    if len(all_addrs) > len(bootstrap_addrs) and len(seeds) <= len(bootstrap_addrs):
        console.print(
            "[yellow]Warning: seeds cover only the bootstrap tier. A client "
            "seeded this narrowly cannot reach chunks stored elsewhere in the "
            "keyspace — raise --sample-nodes.[/yellow]"
        )
