"""Collecting the bootstrap tier's browser-facing WebRTC-Direct addresses.

A WASM/browser client cannot bootstrap from the ``ip:port`` pairs the rest of
this tool passes around. It needs a full multiaddr:

    /ip4/<ip>/udp/<port>/webrtc-direct/certhash/u<multihash>/p2p/<peer-id>

The certhash is the SHA-256 of the node's DTLS certificate, which is generated
on the node's first start and persisted at ``<root_dir>/webrtc-direct.pem``. It
therefore cannot be computed from anything the deployment already knows -- it
only exists once the node has run. ant-node writes the assembled address to
``<root_dir>/webrtc-direct.multiaddr`` after its listener binds, so collecting
it means reading that file back off each bootstrap VM.

Pure functions here; the SSH and S3 I/O live in
``cmd/bootstrap_webrtc_addresses.py``.
"""

import re

from saorsa_deploy.provisioning.volume import MOUNT_POINT

#: ant-node's ``WEBRTC_DIRECT_MULTIADDR_FILENAME``.
MULTIADDR_FILENAME = "webrtc-direct.multiaddr"

#: Shape of a WebRTC-Direct multiaddr, per saorsa-webrtc's ``BrowserEndpoint``.
#: The peer id is hex (NOT base58, unlike libp2p's usual rendering) and the
#: certhash is multibase base64url-no-pad over a 0x12,0x20-prefixed multihash.
_MULTIADDR_RE = re.compile(
    r"^/ip[46]/[^/]+/udp/(?P<port>\d+)/webrtc-direct"
    r"/certhash/u(?P<certhash>[A-Za-z0-9_-]+)"
    r"/p2p/(?P<peer_id>[0-9a-fA-F]{64})$"
)


def bootstrap_root_dir(bootstrap_index: int) -> str:
    """Root dir of a bootstrap service, matching ``provisioning/bootstrap.py``."""
    return f"{MOUNT_POINT}/data/ant-bootstrap-{bootstrap_index}"


def multiaddr_path(bootstrap_index: int) -> str:
    """Absolute path of a bootstrap's persisted browser address."""
    return f"{bootstrap_root_dir(bootstrap_index)}/{MULTIADDR_FILENAME}"


#: Glob matching every node-tier service's persisted browser address on a VM.
#: Covers both tiers: bootstraps live in ``ant-bootstrap-<n>`` and regular nodes
#: in ``ant-node-<n>``, side by side under the same data directory.
MULTIADDR_GLOB = f"{MOUNT_POINT}/data/*/{MULTIADDR_FILENAME}"


def peer_id(multiaddr: str) -> str:
    """The 64-hex peer id at the end of a WebRTC-Direct multiaddr."""
    return multiaddr.rsplit("/p2p/", 1)[-1]


def spread_sample(multiaddrs: list[str], count: int) -> list[str]:
    """Pick ``count`` addresses spread evenly across the peer-id keyspace.

    Seeding matters far more than it looks. A node publishes its browser
    endpoint to the peers CLOSEST TO ITSELF, so a node knows endpoints for its
    own neighbourhood and nobody else's. A client seeded only with the bootstrap
    tier can therefore reach the bootstraps and nothing else: asked for the
    peers closest to some chunk, a bootstrap returns 20 nodes of which ~1 has a
    reachable endpoint, and the iterative walk toward that chunk stalls on its
    first hop.

    Measured on DEV-01-wasm (100 nodes), downloading the same 10 MB address:
    3 bootstrap seeds failed with "no closest WebRtcDirect node returned chunk";
    30 seeds spread across the fleet succeeded in 31s. Upstream's "one address
    is enough" holds on a 5-node devnet, where every node is in every other
    node's close group, and stops holding well before 100.

    Sorting by peer id and taking evenly-spaced entries gives coverage of the
    XOR keyspace, so whatever a client looks up, some seed is near it.
    """
    if count <= 0 or len(multiaddrs) <= count:
        return list(multiaddrs)
    ordered = sorted(multiaddrs, key=peer_id)
    step = len(ordered) / count
    return [ordered[int(i * step)] for i in range(count)]


def parse_multiaddr(raw: str) -> str:
    """Validate one address read off a VM and return it stripped.

    Raises ``ValueError`` rather than passing a malformed address through.  A
    bad address is not a cosmetic problem: it is written verbatim into the
    deployment state and then handed to clients as their only way into the
    network, where it would surface as an unexplained connection failure long
    after the deploy that produced it.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty multiaddr")
    # ant-node writes exactly one line, but tolerate a trailing blank line.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError(f"expected a single address, got {len(lines)} lines")
    if not _MULTIADDR_RE.match(lines[0]):
        raise ValueError(f"not a webrtc-direct multiaddr: {lines[0]!r}")
    return lines[0]


def rewrite_host(multiaddr: str, ip: str) -> str:
    """Replace the host in ``multiaddr`` with ``ip``.

    ant-node advertises the external address its native transport observed,
    falling back to a routed local IP and finally to loopback. On a cloud VM the
    observed address is normally the public one, but the fallbacks are not: a
    node whose transport has not yet learned its external address writes a
    private or loopback host, and an address like ``/ip4/10.x.x.x/...`` is
    useless to a client on another machine while looking perfectly well-formed.
    The deployment already knows each bootstrap's real public IP, so prefer it
    and keep the port, certhash and peer id -- the parts only the node can know.
    """
    parts = multiaddr.split("/")
    # ["", "ip4", host, "udp", port, ...]
    if len(parts) < 3:
        raise ValueError(f"malformed multiaddr: {multiaddr!r}")
    parts[2] = ip
    return "/".join(parts)


def address_host(multiaddr: str) -> str:
    """The host portion of a multiaddr."""
    parts = multiaddr.split("/")
    if len(parts) < 3:
        raise ValueError(f"malformed multiaddr: {multiaddr!r}")
    return parts[2]
