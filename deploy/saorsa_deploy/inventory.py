"""Loader for production-style inventory JSON files.

Production deployments are not provisioned via ``saorsa-deploy`` and therefore
have no entry in S3 deployment state. They ship instead as JSON inventories
(schema_version 3) under e.g. ``~/.autonomi/testnets/<name>/inventory.json``.

Operational commands that target an existing prod fleet (such as
``prod extend-unmanaged-volumes``) consume these inventories directly through
``load_node_inventory``.
"""

import json
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_PROVIDERS = {"aws", "digitalocean", "hetzner", "ovh", "vultr"}
SUPPORTED_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class NodeVm:
    """A single node VM extracted from a production inventory."""

    name: str
    provider: str
    provider_id: str
    region: str
    ip: str
    ssh_user: str
    ssh_key: str
    services: tuple[str, ...]


def load_node_inventory(
    inventory_path: str | Path,
    *,
    provider_filter: str | None = None,
) -> list[NodeVm]:
    """Read a production inventory JSON file and return its node VMs.

    ``provider_filter`` restricts the result to a single provider when set.
    The inventory file is read-only — never mutated.

    Raises ``ValueError`` for an unrecognised schema, missing fields, or an
    unknown provider.
    """
    path = Path(inventory_path).expanduser()
    raw = json.loads(path.read_text())

    schema_version = raw.get("schema_version")
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported inventory schema_version: {schema_version!r} "
            f"(expected {SUPPORTED_SCHEMA_VERSION})"
        )

    node_vms_raw = raw.get("node_vms")
    if not isinstance(node_vms_raw, list) or not node_vms_raw:
        raise ValueError("Inventory has no node_vms entries")

    if provider_filter is not None and provider_filter not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unknown provider {provider_filter!r}. "
            f"Supported: {sorted(SUPPORTED_PROVIDERS)}"
        )

    vms: list[NodeVm] = []
    for entry in node_vms_raw:
        provider = entry.get("provider")
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"Inventory entry {entry.get('name')!r} has unknown provider "
                f"{provider!r}"
            )
        if provider_filter is not None and provider != provider_filter:
            continue

        provider_id = entry.get("provider_id")
        if not provider_id:
            raise ValueError(
                f"Inventory entry {entry.get('name')!r} ({provider}) is missing "
                "'provider_id' — required to look up its attached volume"
            )

        services = tuple(entry.get("services") or [])

        vms.append(
            NodeVm(
                name=entry["name"],
                provider=provider,
                provider_id=str(provider_id),
                region=entry["region"],
                ip=entry["ip"],
                ssh_user=entry["ssh_user"],
                ssh_key=entry["ssh_key"],
                services=services,
            )
        )

    if provider_filter is not None and not vms:
        raise ValueError(
            f"No node VMs match provider {provider_filter!r} in {path}"
        )

    return vms
