"""``deployments webrtc-addresses`` -- collect the bootstrap tier's WebRTC addresses.

Reads each bootstrap service's ``webrtc-direct.multiaddr`` off the deployment's
bootstrap VMs and stores them in S3 deployment state as
``bootstrap_webrtc_multiaddrs``, so WASM/browser clients have somewhere to
bootstrap from.

**A WASM client gets the same bootstraps as a native client**, and nothing more.
The only difference between the two is the address format: the native tier is
handed bare ``ip:port`` peers, while a browser client needs the full multiaddr
because a WebRTC-Direct address also carries the UDP port, the DTLS certificate
hash and the peer id. Collecting them requires a command like this one only
because the certhash does not exist until the node has started and written
``<root_dir>/webrtc-direct.pem`` -- so, unlike ``ip:port``, the list cannot be
written ahead of time.

This deliberately does NOT sample the node tier. A client given the bootstraps
must be able to reach the rest of the network by walking the DHT, exactly as the
native client does from its seven bootstrap peers; pre-loading it with a slice of
the node tier would hide any failure to do so behind a list that production
could never supply. If uploads or downloads do not work from the bootstraps
alone, that is a node-side defect to fix, not a reason to hand out more
addresses.

Runs after the nodes are started, because of the certhash above.
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


def _bootstrap_vm_ips(state: dict) -> list[str]:
    """Bootstrap VM IPs from deployment state."""
    boots = [n["ip"] for n in (state.get("bootstrap_nodes") or []) if n.get("ip")]
    if not boots and state.get("bootstrap_ip"):
        boots = [state["bootstrap_ip"]]
    return boots


def cmd_webrtc_addresses(args):
    """Collect and persist the bootstrap tier's WebRTC-Direct addresses."""
    console = Console()

    state = load_deployment_state(args.name)
    boot_ips = _bootstrap_vm_ips(state)
    if not boot_ips:
        console.print(
            f"[bold red]Error:[/bold red] no bootstrap VMs in state for "
            f"'{args.name}'"
        )
        sys.exit(1)

    console.print(
        f"[bold]Collecting WebRTC-Direct addresses from {len(boot_ips)} "
        f"bootstrap VM(s)...[/bold]"
    )

    with ThreadPoolExecutor(max_workers=min(32, len(boot_ips))) as pool:
        results = dict(
            pool.map(
                lambda ip: _collect(ip, args.ssh_key_path, args.timeout, True),
                boot_ips,
            )
        )

    bootstrap_addrs = [a for ip in boot_ips for a in results.get(ip, [])]
    silent = [ip for ip, found in results.items() if not found]

    console.print(
        f"  [green]{len(bootstrap_addrs)}[/green] bootstrap address(es) collected"
    )
    if silent:
        # Every bootstrap is polled to the deadline, so a silent one here is a
        # real failure to advertise, not a race.
        console.print(
            f"  [yellow]{len(silent)} bootstrap VM(s) reported no address: "
            f"{', '.join(silent)}[/yellow]"
        )

    if not bootstrap_addrs:
        console.print(
            "[bold red]Error:[/bold red] no bootstrap advertised a WebRTC-Direct "
            "address. Is this deployment running an ant-node build with WebRTC "
            "support?"
        )
        sys.exit(1)

    update_deployment_state(
        args.name,
        {"bootstrap_webrtc_multiaddrs": bootstrap_addrs},
    )
    console.print(
        f"[green]Saved {len(bootstrap_addrs)} bootstrap address(es) to "
        f"deployment state.[/green]"
    )
