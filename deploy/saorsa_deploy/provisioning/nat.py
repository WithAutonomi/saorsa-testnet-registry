"""Port-restricted NAT simulation for node VMs.

Ports the Category E setup from
``/home/chris/dev/testnets/1000_node-low_nat_sim-versioned/.claude/skills/deploy-prod-testnet/SKILL.md``
(lines 740-806) verbatim — a known-working configuration on DO/Vultr VMs.

The shell command below MUST match that reference byte-for-byte where it
matters; NAT traversal is easy to break with seemingly harmless changes.
"""

from __future__ import annotations

import math

from saorsa_deploy.provisioning.firewall import build_disable_firewall_command

NAT_NAMESPACE = "ant_nat"
NAT_HOST_IP = "10.200.0.1"
NAT_NS_IP = "10.200.0.2"
NAT_SUBNET = "10.200.0.0/24"
NAT_IP_EXEC_PREFIX = f"/usr/sbin/ip netns exec {NAT_NAMESPACE} "


def select_nat_ips(vm_ips_by_region: dict[str, list[str]], percent: int) -> list[str]:
    """Select which VM IPs should be NAT-simulated.

    Strategy: "one per region first, then round-robin". Regions are sorted by
    key; IPs within each region are sorted; we interleave lanes slot-by-slot
    so the first ``ceil(total * percent / 100)`` IPs span as many regions as
    possible.

    Deterministic for a given input — same vm_ips and percent always yield
    the same selection, so re-running provisioning doesn't flip categories.
    """
    if percent <= 0 or not vm_ips_by_region:
        return []
    total = sum(len(ips) for ips in vm_ips_by_region.values())
    if total == 0:
        return []
    target = min(total, math.ceil(total * percent / 100))

    sorted_regions = sorted(vm_ips_by_region.keys())
    lanes = [sorted(vm_ips_by_region[r]) for r in sorted_regions]
    max_len = max(len(lane) for lane in lanes)

    ordered: list[str] = []
    for slot in range(max_len):
        for lane in lanes:
            if slot < len(lane):
                ordered.append(lane[slot])

    return ordered[:target]


def build_nat_setup_command() -> str:
    """Return the shell command that sets up port-restricted NAT on a host.

    Idempotent: if the ``ant_nat`` namespace already exists the script
    short-circuits with ``ANT_NAT:ALREADY``. Otherwise it prints
    ``ANT_NAT:CONFIGURED`` on success.

    The body is a verbatim port of Category E (DO/Vultr, user=root) from
    the reference SKILL.md lines 761-781. Do not rewrite, reorder, or
    "simplify" the iptables/ip commands — they work as a unit.
    """
    # NOTE: pyinfra's server.shell runs under /bin/sh (dash on Debian/Ubuntu),
    # which doesn't support `set -o pipefail`. We use plain `set -eu` — errors
    # inside pipelines are caught by the subsequent commands failing, not by
    # pipefail itself.
    return f"""\
set -eu
if ip netns list 2>/dev/null | awk '{{print $1}}' | grep -qx "{NAT_NAMESPACE}"; then
    echo 'ANT_NAT:ALREADY'
    exit 0
fi
{build_disable_firewall_command()}

NS="{NAT_NAMESPACE}"
HOST_IP="{NAT_HOST_IP}"
NS_IP="{NAT_NS_IP}"
SUBNET="{NAT_SUBNET}"
OUT_IFACE=$(ip route get 1.1.1.1 | awk '{{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1); exit}}')
ip netns add "$NS"
ip link add veth-nat type veth peer name veth-node
ip link set veth-node netns "$NS"
ip addr add "${{HOST_IP}}/24" dev veth-nat && ip link set veth-nat up
ip netns exec "$NS" ip addr add "${{NS_IP}}/24" dev veth-node
ip netns exec "$NS" ip link set veth-node up && ip netns exec "$NS" ip link set lo up
ip netns exec "$NS" ip route add default via "$HOST_IP"
sysctl -w net.ipv4.ip_forward=1 >/dev/null
modprobe ip_conntrack 2>/dev/null || true
iptables -t nat -A PREROUTING -i "$OUT_IFACE" -p tcp --dport 22 -j ACCEPT
iptables -t nat -A PREROUTING -i "$OUT_IFACE" -p tcp -j DNAT --to-destination "$NS_IP"
iptables -t nat -A PREROUTING -i "$OUT_IFACE" -p udp -j DNAT --to-destination "$NS_IP"
iptables -t nat -A POSTROUTING -s "$SUBNET" -o "$OUT_IFACE" -j MASQUERADE
iptables -A FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A FORWARD -s "$NS_IP" -j ACCEPT
iptables -A FORWARD -p tcp -d "$NS_IP" ! --dport 22 -j DROP
iptables -A FORWARD -p udp -d "$NS_IP" -j DROP
mkdir -p /etc/netns/"$NS" && echo "nameserver 1.1.1.1" > /etc/netns/"$NS"/resolv.conf
echo 'ANT_NAT:CONFIGURED'
"""
