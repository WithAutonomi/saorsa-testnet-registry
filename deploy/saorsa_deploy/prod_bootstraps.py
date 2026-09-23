"""Fetch the canonical production bootstrap peers for the Autonomi network.

The authoritative list is maintained at
``https://raw.githubusercontent.com/WithAutonomi/ant-client/refs/heads/main/ant-core/resources/bootstrap_peers.toml``
and holds a ``quic`` array of QUIC multiaddrs (``/ip4/<ip>/udp/<port>/quic``,
optionally ``/p2p/<peer-id>``) plus a ``webrtc`` array for browsers.
``saorsa-deploy`` fetches it at runtime so we never drift from upstream.

ant-client #186 moved the file out of ``resources/`` and replaced the legacy
``peers`` array of ``ip:port`` strings with ``quic``. The client still accepts
``peers`` (as an alias) and bare socket addresses, so callers keep receiving
``ip:port`` strings and the file rendered onto VMs is unchanged.
"""

import tomllib
from pathlib import Path

import requests

PROD_BOOTSTRAPS_URL = (
    "https://raw.githubusercontent.com/WithAutonomi/ant-client/"
    "refs/heads/main/ant-core/resources/bootstrap_peers.toml"
)

DEFAULT_PROD_BOOTSTRAPS_PATH = "~/.config/ant/bootstrap_peers.toml"

# Client VMs run every ant service as root, so this is where the ant client
# resolves its config dir when no --bootstrap argument is supplied.
REMOTE_BOOTSTRAP_PEERS_TOML_PATH = "/root/.config/ant/bootstrap_peers.toml"

_FETCH_TIMEOUT_SECONDS = 15


class ProdBootstrapsError(RuntimeError):
    """Raised when the prod bootstraps list cannot be fetched or parsed."""


def fetch_prod_bootstraps(url: str = PROD_BOOTSTRAPS_URL) -> list[str]:
    """Return the production bootstrap peers list (bare ``ip:port`` strings).

    Raises ``ProdBootstrapsError`` on any HTTP / parse / validation failure.
    """
    try:
        response = requests.get(url, timeout=_FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as e:
        raise ProdBootstrapsError(
            f"Failed to fetch prod bootstraps from {url}: {e}"
        ) from e

    try:
        data = tomllib.loads(response.text)
    except tomllib.TOMLDecodeError as e:
        raise ProdBootstrapsError(
            f"Prod bootstraps TOML at {url} is malformed: {e}"
        ) from e

    key = "quic" if "quic" in data else "peers"
    peers = data.get(key)
    if not isinstance(peers, list) or not peers:
        raise ProdBootstrapsError(
            f"Prod bootstraps TOML at {url} is missing a non-empty 'quic' "
            "(or legacy 'peers') array."
        )
    if not all(isinstance(p, str) and p for p in peers):
        raise ProdBootstrapsError(
            f"Prod bootstraps TOML at {url} contains non-string entries in '{key}'."
        )
    try:
        return [_to_socket_addr(p) for p in peers]
    except ValueError as e:
        raise ProdBootstrapsError(
            f"Prod bootstraps TOML at {url} has an unusable entry: {e}"
        ) from e


def _to_socket_addr(peer: str) -> str:
    """Reduce a QUIC multiaddr to ``ip:port``; pass a bare socket address through.

    Any ``/p2p/<peer-id>`` suffix is dropped: every consumer downstream takes
    bare socket addresses (``--bootstrap``, the rendered ``peers`` file, node
    bootstrap state).
    """
    if not peer.startswith("/"):
        return peer
    parts = peer.strip("/").split("/")
    if len(parts) < 5 or parts[0] not in ("ip4", "ip6") or parts[2] != "udp" or parts[4] != "quic":
        raise ValueError(f"expected /ip4|ip6/<ip>/udp/<port>/quic, got {peer!r}")
    ip, port = parts[1], parts[3]
    return f"[{ip}]:{port}" if parts[0] == "ip6" else f"{ip}:{port}"


def render_bootstrap_peers_toml(peers: list[str]) -> str:
    """Render a ``bootstrap_peers.toml`` document from bare ``ip:port`` strings.

    This is the file the ant client loads from its config dir when invoked
    without ``--bootstrap`` (which is also what enables its peer cache).
    """
    lines = ["peers = ["]
    lines.extend(f'    "{peer}",' for peer in peers)
    lines.append("]")
    return "\n".join(lines) + "\n"


def ensure_local_bootstrap_peers_file(
    path: str | Path | None = None,
) -> tuple[Path, bool]:
    """Ensure the operator's ``bootstrap_peers.toml`` exists, returning
    ``(path, created)``.

    Used by the local-uploads/downloads commands when the bootstrap cache is
    enabled: ant (invoked without ``--bootstrap``) reads this file from its
    config dir. An existing file is NEVER overwritten — it is the operator's
    own machine config; only a missing file is created, from the fetched
    production peers list.

    Raises ``ProdBootstrapsError`` if the file is missing and the fetch fails.
    """
    raw_path = path if path is not None else DEFAULT_PROD_BOOTSTRAPS_PATH
    peers_path = Path(raw_path).expanduser()
    if peers_path.exists():
        return peers_path, False
    peers = fetch_prod_bootstraps()
    peers_path.parent.mkdir(parents=True, exist_ok=True)
    peers_path.write_text(render_bootstrap_peers_toml(peers))
    return peers_path, True


def load_prod_bootstraps_from_file(
    path: str | Path | None = None,
) -> list[str]:
    """Read the prod bootstrap peers list from a local TOML file.

    Default path is ``~/.config/ant/bootstrap_peers.toml`` — the same
    location the ``ant`` CLI itself reads. Returns the peers as bare
    ``ip:port`` strings (the format ``AntNodeProvisioner`` consumes).
    """
    raw_path = path if path is not None else DEFAULT_PROD_BOOTSTRAPS_PATH
    peers_path = Path(raw_path).expanduser()
    if not peers_path.exists():
        raise ProdBootstrapsError(
            f"Bootstrap peers file not found: {peers_path}. "
            f"Pass --bootstrap-peers-file or create the default at "
            f"{DEFAULT_PROD_BOOTSTRAPS_PATH}."
        )

    try:
        with peers_path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise ProdBootstrapsError(
            f"Bootstrap peers file {peers_path} is malformed: {e}"
        ) from e

    peers = data.get("peers")
    if not isinstance(peers, list) or not peers:
        raise ProdBootstrapsError(
            f"Bootstrap peers file {peers_path} is missing a non-empty "
            "'peers' array."
        )
    if not all(isinstance(p, str) and p for p in peers):
        raise ProdBootstrapsError(
            f"Bootstrap peers file {peers_path} contains non-string entries "
            "in 'peers'."
        )
    return list(peers)
