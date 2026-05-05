"""Fetch the canonical production bootstrap peers for the Autonomi network.

The authoritative list is maintained at
``https://raw.githubusercontent.com/WithAutonomi/ant-client/refs/heads/main/resources/bootstrap_peers.toml``
and consists of a single top-level ``peers`` array of ``ip:port`` strings.
``saorsa-deploy`` fetches it at runtime so we never drift from upstream.
"""

import tomllib
from pathlib import Path

import requests

PROD_BOOTSTRAPS_URL = (
    "https://raw.githubusercontent.com/WithAutonomi/ant-client/"
    "refs/heads/main/resources/bootstrap_peers.toml"
)

DEFAULT_PROD_BOOTSTRAPS_PATH = "~/.config/ant/bootstrap_peers.toml"

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

    peers = data.get("peers")
    if not isinstance(peers, list) or not peers:
        raise ProdBootstrapsError(
            f"Prod bootstraps TOML at {url} is missing a non-empty 'peers' array."
        )
    if not all(isinstance(p, str) and p for p in peers):
        raise ProdBootstrapsError(
            f"Prod bootstraps TOML at {url} contains non-string entries in 'peers'."
        )
    return list(peers)


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
