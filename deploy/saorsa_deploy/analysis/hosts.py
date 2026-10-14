"""Resolve a deployment's VMs, tiers and SSH settings from its inventory.

The inventory (`~/.autonomi/testnets/<name>/inventory.json`, written by
`deployments generate-inventory`) is the one place that knows which VMs are
NAT-simulated. That flag matters to every command here: NAT'd VMs run their
node services inside the `ant_nat` network namespace, so captures, counters and
iptables rules must be taken inside it, and telegraf's host-namespace metrics
exclude them entirely.

It is also a snapshot of a deployment that may be long gone, and three of the
commands reading it MUTATE the hosts it names (`restart-nodes`, `isolate`,
`rot-chunks`). So a default-path load is checked against the deployment's S3
state before any VM is returned: a snapshot whose deployment no longer exists,
or whose IPs the state does not know, is refused rather than acted on. An
explicit ``inventory=`` override skips the check and makes no S3 call — that is
the operator saying "I know".
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from saorsa_deploy.local_inventory import default_inventory_path

# Matches the options the proven scripts used. BatchMode so a missing key fails
# fast instead of prompting; no known-hosts persistence because testnet IPs are
# recycled across deployments.
SSH_BASE_OPTS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "ConnectTimeout=20",
    "-o",
    "LogLevel=ERROR",
)


@dataclass(frozen=True)
class Vm:
    """One VM from the inventory, with the fields the analysis commands need."""

    name: str
    ip: str
    role: str  # "node" | "bootstrap" | "client"
    nat: bool
    ssh_user: str
    ssh_key: str
    services: tuple[str, ...]
    provider: str = ""
    region: str = ""

    @property
    def target(self) -> str:
        return f"{self.ssh_user}@{self.ip}"

    def ssh_args(self) -> list[str]:
        return ["ssh", "-i", os.path.expanduser(self.ssh_key), *SSH_BASE_OPTS]

    def scp_args(self) -> list[str]:
        return ["scp", "-i", os.path.expanduser(self.ssh_key), *SSH_BASE_OPTS]


def inventory_path(name: str, override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    return default_inventory_path(name)


class StaleInventoryError(RuntimeError):
    """A default-path snapshot that no longer describes a live deployment.

    Raised rather than returning its VMs: the addresses in a stale snapshot
    have been released back to the provider and reassigned, so a command that
    reaches one of them is acting on a machine we do not own.
    """


def _load_deployment_state(name: str) -> dict:
    """Indirection so tests can stay offline; the import is lazy for the same
    reason — nothing in this module needs boto3 until a freshness check runs."""
    from saorsa_deploy.state import load_deployment_state

    return load_deployment_state(name)


def _load_client_state(name: str) -> dict:
    from saorsa_deploy.state import load_client_state

    return load_client_state(name)


def _state_node_ips(state: dict) -> set[str]:
    """Every node and bootstrap IP the S3 deployment state knows about."""
    ips = {ip for ips in (state.get("vm_ips") or {}).values() for ip in ips}
    ips.update(n["ip"] for n in (state.get("bootstrap_nodes") or []) if n.get("ip"))
    return ips


def _client_state_ips(state: dict) -> set[str]:
    return {c["ip"] for c in (state.get("clients") or []) if c.get("ip")}


def _regenerate_hint(name: str) -> str:
    return (
        f"run `saorsa-deploy deployments generate-inventory --name {name}` "
        "after `up` to refresh it, or pass `--inventory <path>` to use this "
        "file anyway."
    )


def _generated_at(raw: dict) -> str:
    return raw.get("created_at") or "at an unrecorded time"


def _check_freshness(name: str, path: Path, raw: dict) -> None:
    """Refuse a default-path snapshot that no longer matches a live deployment.

    The comparison is on IP sets, not timestamps: the S3 state carries no
    `created_at`, and the hazard is precisely an address that is no longer ours.
    """
    tiers = [
        *(("node", vm) for vm in raw.get("node_vms") or []),
        *(("bootstrap", vm) for vm in raw.get("bootstrap_vms") or []),
    ]
    if tiers:
        try:
            state = _load_deployment_state(name)
        except RuntimeError:
            raise StaleInventoryError(
                f"{path} (generated {_generated_at(raw)}) describes a "
                f"deployment that no longer exists: there is no S3 state for "
                f"'{name}'. Deploy first, or {_regenerate_hint(name)}"
            )
        known = _state_node_ips(state)
        for role, vm in tiers:
            if vm.get("ip") not in known:
                raise StaleInventoryError(
                    f"{path} (generated {_generated_at(raw)}) is stale: its "
                    f"{role} VM {vm.get('name')} at {vm.get('ip')} is not in "
                    f"the S3 state for '{name}'. To fix, {_regenerate_hint(name)}"
                )
        return

    clients = raw.get("client_vms") or []
    if not clients:
        return
    try:
        state = _load_client_state(name)
    except RuntimeError:
        raise StaleInventoryError(
            f"{path} (generated {_generated_at(raw)}) describes a client "
            f"deployment that no longer exists: there is no S3 client state "
            f"for '{name}'. Deploy first, or {_regenerate_hint(name)}"
        )
    known = _client_state_ips(state)
    for vm in clients:
        if vm.get("ip") not in known:
            raise StaleInventoryError(
                f"{path} (generated {_generated_at(raw)}) is stale: its "
                f"client VM {vm.get('name')} at {vm.get('ip')} is not in the "
                f"S3 client state for '{name}'. To fix, {_regenerate_hint(name)}"
            )


def load_vms(name: str, inventory: str | None = None) -> list[Vm]:
    """All VMs of a deployment. Raises FileNotFoundError with a hint if absent.

    A default-path snapshot is checked against the deployment's S3 state and
    refused when stale (:class:`StaleInventoryError`); an explicit ``inventory``
    override bypasses the check and makes no S3 call.
    """
    path = inventory_path(name, inventory)
    if not path.exists():
        raise FileNotFoundError(
            f"No inventory at {path}. Run "
            f"`saorsa-deploy deployments generate-inventory --name {name}` first."
        )
    raw = json.loads(path.read_text())
    if inventory:
        # stderr, because several of these commands write a JSON document to
        # stdout for `--json-out -` and an unattended runner pipes that to jq.
        Console(stderr=True).print(f"[dim]Using {path} as given; freshness check skipped.[/dim]")
    else:
        _check_freshness(name, path, raw)
    vms: list[Vm] = []
    roles = (("node", "node_vms"), ("bootstrap", "bootstrap_vms"), ("client", "client_vms"))
    for role, key in roles:
        for entry in raw.get(key) or []:
            vms.append(
                Vm(
                    name=entry["name"],
                    ip=entry["ip"],
                    role=role,
                    nat=bool(entry.get("nat_simulated", False)),
                    ssh_user=entry.get("ssh_user", "root"),
                    ssh_key=entry.get("ssh_key", "~/.ssh/ant_deploy"),
                    services=tuple(entry.get("services") or []),
                    provider=entry.get("provider", ""),
                    region=entry.get("region", ""),
                )
            )
    return vms


def node_vms(name: str, inventory: str | None = None, tier: str = "all") -> list[Vm]:
    """Node VMs of a deployment, optionally one tier: ``all``, ``nat`` or ``plain``."""
    vms = [v for v in load_vms(name, inventory) if v.role == "node"]
    if tier == "nat":
        return [v for v in vms if v.nat]
    if tier == "plain":
        return [v for v in vms if not v.nat]
    if tier != "all":
        raise ValueError(f"unknown tier {tier!r}; expected all, nat or plain")
    return vms


def select_vms(
    name: str,
    ips: list[str] | None,
    inventory: str | None = None,
    tier: str = "all",
    roles: tuple[str, ...] = ("node",),
) -> list[Vm]:
    """Resolve ``--ips`` (or, when empty, a whole tier) against the inventory.

    An IP that is not in the inventory is an error rather than an ad-hoc host:
    the NAT flag and key come from the inventory, and guessing them on a VM
    that is not part of the deployment is how a probe lands on the wrong fleet.
    """
    vms = [v for v in load_vms(name, inventory) if v.role in roles]
    if not ips:
        if tier == "nat":
            return [v for v in vms if v.nat]
        if tier == "plain":
            return [v for v in vms if not v.nat]
        return vms
    by_ip = {v.ip: v for v in vms}
    missing = [ip for ip in ips if ip not in by_ip]
    if missing:
        raise KeyError(
            f"IP(s) not in the {name} inventory (roles {', '.join(roles)}): {', '.join(missing)}"
        )
    return [by_ip[ip] for ip in ips]
