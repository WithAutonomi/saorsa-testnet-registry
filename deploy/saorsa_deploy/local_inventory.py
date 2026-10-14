"""The local inventory snapshot's lifecycle: where it lives, and retiring it.

`deployments generate-inventory` writes `~/.autonomi/testnets/<name>/inventory.json`
(and, for a self-hosted network, `devnet-manifest.json` beside it) as the last
step of `deployments up`. It is a snapshot, and for a long time it was a
snapshot with no lifecycle: nothing removed it on teardown, so a file naming
the VMs of a destroyed deployment outlived it indefinitely. Deployment names
are reused, provider IPs are recycled, and every testnet VM accepts the same
deploy key — so a stale snapshot for name X can name a live VM of deployment Y,
and a command run "against X" succeeds on Y.

So the destroy commands retire the snapshot when they delete a deployment's S3
state, and the `analysis` readers refuse one that no longer matches a live
deployment (`saorsa_deploy.analysis.hosts`).

Retiring is a RENAME, not a delete: the bytes are evidence, and the run issues
cite them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

#: The one definition of where a deployment's local snapshot lives. Both the
#: writer (`cmd/generate_inventory.py`) and the readers (`analysis/hosts.py`)
#: resolve through here, so a retirement can never target a different path
#: from the one a reader would load.
DEFAULT_INVENTORY_TEMPLATE = "~/.autonomi/testnets/{name}/inventory.json"

INVENTORY_FILENAME = "inventory.json"
DEVNET_MANIFEST_FILENAME = "devnet-manifest.json"

#: The two files `up` writes, and the only two a retirement ever touches.
#: `bootstrap-inventory.json` (hand-maintained for the production bootstraps)
#: and any already-retired `inventory.json.*` sibling are deliberately absent.
RETIRABLE_FILENAMES = (INVENTORY_FILENAME, DEVNET_MANIFEST_FILENAME)


def default_inventory_path(name: str) -> Path:
    return Path(DEFAULT_INVENTORY_TEMPLATE.format(name=name)).expanduser()


def inventory_testnet_id(path: Path) -> str | None:
    """The `testnet_id` a snapshot claims, or None if it cannot be read.

    Unreadable and malformed files return None rather than raising: a retirement
    then leaves them alone, which is the safe side of a file we cannot identify.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    testnet_id = raw.get("testnet_id")
    return testnet_id if isinstance(testnet_id, str) else None


def _retired_path(path: Path, reason: str, stamp: str) -> Path:
    """`inventory.json` → `inventory.json.<reason>-<stamp>`, never clobbering.

    Two teardowns of one name inside the same second would otherwise have the
    second overwrite the first's evidence.
    """
    candidate = path.with_name(f"{path.name}.{reason}-{stamp}")
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.{reason}-{stamp}-{index}")
        index += 1
    return candidate


def retire_local_inventory(
    name: str,
    console=None,
    reason: str = "destroyed",
    now: datetime | None = None,
) -> list[tuple[Path, Path]]:
    """Rename `<name>`'s default-path snapshot aside, and say so.

    Only the default path, and only when the file's own `testnet_id` is
    `<name>`: a hand-edited or foreign file is left alone. Never raises — a
    teardown must not fail over a local file — so a rename failure is a yellow
    warning and a missing file is silent.

    Returns the (before, after) pairs actually renamed.
    """
    path = default_inventory_path(name)
    if not path.exists():
        return []

    testnet_id = inventory_testnet_id(path)
    if testnet_id != name:
        if console is not None:
            console.print(
                f"[dim]Left {path} alone: its testnet_id is {testnet_id!r}, not {name!r}.[/dim]"
            )
        return []

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    renamed: list[tuple[Path, Path]] = []
    for filename in RETIRABLE_FILENAMES:
        source = path.with_name(filename)
        if not source.exists():
            continue
        target = _retired_path(source, reason, stamp)
        try:
            source.rename(target)
        except OSError as e:
            if console is not None:
                console.print(f"[yellow]Warning: Failed to retire {source}: {e}[/yellow]")
            continue
        renamed.append((source, target))
        if console is not None:
            console.print(f"[dim]Retired {source} -> {target.name}[/dim]")
    return renamed
