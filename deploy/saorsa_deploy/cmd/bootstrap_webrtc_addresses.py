"""``bootstraps webrtc-addresses`` -- collect the tier's browser seed addresses.

Reads ``<root_dir>/webrtc-direct.multiaddr`` off every bootstrap VM and stores
the results in the deployment's S3 state as ``bootstrap_webrtc_multiaddrs``, so
WASM clients (which cannot use the bare ``ip:port`` peers the native tier
passes around) have somewhere to bootstrap from.

Runs after ``bootstraps provision``, which starts the services -- the file does
not exist until a node's listener has bound.
"""

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from rich.console import Console

from saorsa_deploy.state import load_deployment_state, update_deployment_state
from saorsa_deploy.webrtc_addresses import (
    address_host,
    multiaddr_path,
    parse_multiaddr,
    rewrite_host,
)

_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
]

#: The listener binds a moment after the service starts, so a read straight
#: after `bootstraps provision` can lose a race with a node that is still
#: coming up. Poll rather than fail: the alternative is a deploy that dies at
#: the last step with every VM already created and billing.
DEFAULT_TIMEOUT_SECS = 180
_POLL_INTERVAL_SECS = 5


def _read_once(ip: str, ssh_key_path: str, path: str) -> str | None:
    result = subprocess.run(
        ["ssh", "-i", ssh_key_path, *_SSH_OPTS, f"root@{ip}", f"cat {path}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _collect_one(node: dict, ssh_key_path: str, timeout_secs: int) -> dict:
    ip = node["ip"]
    index = node.get("bootstrap_index", 1)
    path = multiaddr_path(index)
    deadline = time.monotonic() + timeout_secs
    last_error = "no response"

    while True:
        try:
            raw = _read_once(ip, ssh_key_path, path)
        except subprocess.TimeoutExpired:
            raw = None
            last_error = "ssh timed out"
        if raw is not None:
            try:
                multiaddr = parse_multiaddr(raw)
            except ValueError as e:
                last_error = str(e)
            else:
                advertised = address_host(multiaddr)
                corrected = rewrite_host(multiaddr, ip)
                return {
                    "bootstrap_index": index,
                    "ip": ip,
                    "multiaddr": corrected,
                    # Keep what the node itself advertised. When it differs from
                    # the VM's public IP the node had not learned its external
                    # address, which is worth seeing rather than silently fixing.
                    "advertised_host": advertised,
                }
        else:
            last_error = f"{path} not readable yet"
        if time.monotonic() >= deadline:
            return {"bootstrap_index": index, "ip": ip, "error": last_error}
        time.sleep(_POLL_INTERVAL_SECS)


def cmd_bootstrap_webrtc_addresses(args):
    """Collect and persist every bootstrap's WebRTC-Direct multiaddr."""
    console = Console()

    state = load_deployment_state(args.name)
    bootstrap_nodes = state.get("bootstrap_nodes") or []
    if not bootstrap_nodes:
        bootstrap_ip = state.get("bootstrap_ip")
        if not bootstrap_ip:
            console.print(
                f"[bold red]Error:[/bold red] no bootstrap nodes in state for '{args.name}'"
            )
            sys.exit(1)
        bootstrap_nodes = [{"ip": bootstrap_ip, "bootstrap_index": 1}]

    console.print(
        f"[bold]Collecting WebRTC-Direct addresses from {len(bootstrap_nodes)} "
        f"bootstrap node(s)...[/bold]"
    )

    with ThreadPoolExecutor(max_workers=min(16, len(bootstrap_nodes))) as pool:
        results = list(
            pool.map(
                lambda n: _collect_one(n, args.ssh_key_path, args.timeout),
                sorted(bootstrap_nodes, key=lambda n: n.get("bootstrap_index", 0)),
            )
        )

    collected = [r for r in results if "multiaddr" in r]
    failed = [r for r in results if "multiaddr" not in r]

    for r in collected:
        note = ""
        if r["advertised_host"] != r["ip"]:
            note = f" [yellow](node advertised {r['advertised_host']})[/yellow]"
        console.print(f"  [green]#{r['bootstrap_index']}[/green] {r['multiaddr']}{note}")
    for r in failed:
        console.print(f"  [red]#{r['bootstrap_index']} ({r['ip']}): {r['error']}[/red]")

    if not collected:
        console.print(
            "[bold red]Error:[/bold red] no bootstrap advertised a WebRTC-Direct "
            "address. Is this deployment running an ant-node build with WebRTC "
            "support?"
        )
        sys.exit(1)

    update_deployment_state(
        args.name,
        {"bootstrap_webrtc_multiaddrs": [r["multiaddr"] for r in collected]},
    )
    console.print(
        f"[green]Saved {len(collected)} WebRTC-Direct address(es) to deployment state.[/green]"
    )

    if failed:
        # A partial collection is still usable -- a client seeds from one address
        # and discovers the rest over the network -- so warn rather than fail and
        # strand an otherwise healthy deployment.
        console.print(
            f"[yellow]Warning: {len(failed)} bootstrap(s) did not report an "
            f"address. Clients will seed from the {len(collected)} that did.[/yellow]"
        )
