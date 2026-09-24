"""Loader (and single-service updater) for production-style inventory JSON files.

Production deployments are not provisioned via ``saorsa-deploy`` and therefore
have no entry in S3 deployment state. They ship instead as JSON inventories
(schema_version 3, 4 or 5) under e.g. ``~/.autonomi/testnets/<name>/inventory.json``.

Operational commands that target an existing prod fleet (such as
``prod extend-unmanaged-volumes``) consume these inventories directly through
``load_node_inventory``, which is read-only. ``prod services rm``/``add``
instead use ``update_service_in_inventory`` to keep this file's per-host
service lists in sync with the systemd units they add/remove live over SSH.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_PROVIDERS = {"aws", "digitalocean", "hetzner", "ovh", "vultr", "contabo"}
# generate-inventory currently emits v6; v3 files predate the client/observability
# split, v5 added client_config, v6 added elasticsearch_config. The node_vms shape
# this loader reads is identical across all of them, so accept any rather than
# rejecting freshly-generated inventories. A client-only or Elasticsearch-only
# inventory has no node_vms and is still correctly rejected below.
SUPPORTED_SCHEMA_VERSIONS = frozenset({3, 4, 5, 6})


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
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(
            f"Unsupported inventory schema_version: {schema_version!r} "
            f"(expected one of {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
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


def update_service_in_inventory(
    inventory_path: str | Path,
    host_name: str,
    service_name: str,
    *,
    added: bool,
) -> None:
    """Add or remove one service in a host's ``nodes``/``services`` lists and
    atomically rewrite the inventory file in place.

    Mirrors the read-whole-file / mutate-one-entry / atomic-write pattern
    ``migration complete`` uses for splicing whole VMs, but at the single
    service level — for ``prod services rm``/``add``, which change a live
    host's systemd units over SSH without otherwise touching this file.
    Keeping ``nodes[]`` and ``services[]`` in lockstep matters because
    different consumers read different arrays: ``load_node_inventory`` only
    reads ``services``, while the provisioners size a host's fleet from
    ``nodes``.

    Raises ``ValueError`` if ``host_name`` isn't found in the inventory.
    """
    path = Path(inventory_path).expanduser()
    raw = json.loads(path.read_text())
    node_vms = raw.get("node_vms") or []

    for entry in node_vms:
        if entry.get("name") != host_name:
            continue
        services = list(entry.get("services") or [])
        nodes = list(entry.get("nodes") or [])
        if added:
            if service_name not in services:
                services.append(service_name)
            if not any(n.get("service_name") == service_name for n in nodes):
                new_node = {"service_name": service_name}
                if nodes:
                    for key, value in nodes[0].items():
                        if key != "service_name":
                            new_node[key] = False if isinstance(value, bool) else value
                nodes.append(new_node)
        else:
            services = [s for s in services if s != service_name]
            nodes = [n for n in nodes if n.get("service_name") != service_name]
        entry["services"] = services
        entry["nodes"] = nodes
        break
    else:
        raise ValueError(f"No host named {host_name!r} in {path}")

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(raw, indent=2) + "\n")
    os.replace(tmp_path, path)
