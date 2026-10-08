"""Resolve a deployment's VMs, tiers and SSH settings from its inventory.

The inventory (`~/.autonomi/testnets/<name>/inventory.json`, written by
`deployments generate-inventory`) is the one place that knows which VMs are
NAT-simulated. That flag matters to every command here: NAT'd VMs run their
node services inside the `ant_nat` network namespace, so captures, counters and
iptables rules must be taken inside it, and telegraf's host-namespace metrics
exclude them entirely.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_INVENTORY_TEMPLATE = "~/.autonomi/testnets/{name}/inventory.json"

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
    return Path(DEFAULT_INVENTORY_TEMPLATE.format(name=name)).expanduser()


def load_vms(name: str, inventory: str | None = None) -> list[Vm]:
    """All VMs of a deployment. Raises FileNotFoundError with a hint if absent."""
    path = inventory_path(name, inventory)
    if not path.exists():
        raise FileNotFoundError(
            f"No inventory at {path}. Run "
            f"`saorsa-deploy deployments generate-inventory --name {name}` first."
        )
    raw = json.loads(path.read_text())
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
