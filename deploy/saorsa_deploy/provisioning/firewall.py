"""Host firewall handling for provisioned VMs.

Vultr's Ubuntu 24.04 image ships with ``ufw`` **enabled**, default-deny on
incoming, and exactly one allow rule — ``22/tcp``. Every other inbound packet
is dropped by netfilter after reaching the NIC. That is invisible until
something has to dial *in*: SSH works, provisioning works, node-to-node
traffic works (nodes hole-punch outbound), and only direct inbound connections
fail.

It cost testnet run DEV-03 (V2-1410, registry id 632) its upload rate: clients
made 6,821 direct QUIC connection attempts to node ports on the seven Vultr
node VMs and **none** connected, every one timing out, while the DigitalOcean
VMs on the same fleet accepted tens of thousands. None of the 105 Vultr nodes
was ever promoted to a ``Direct`` address by saorsa-core's classifier, which
needs two inbound connections from previously unseen IPs.

Confirmed on a throwaway Vultr instance (2026-10-03, V2-1417): with ``ufw``
active, ``tcpdump`` saw the probe datagrams arrive on the NIC and the listener
received none of them; after the command below, all three arrived.

Node services listen on OS-assigned high UDP ports (and MASQUE relay servers
on further OS-assigned ports), so an allow-list cannot name them without
changing how ports are assigned — hence disable rather than allow. Testnet VMs
are disposable and already exposed by design, and the NAT path
(:mod:`saorsa_deploy.provisioning.nat`) has disabled ``ufw`` since June without
incident. :func:`build_disable_firewall_command` is the single definition of
that command; the NAT setup script interpolates the same string, so the two
cannot drift.
"""

DISABLE_FIREWALL_OP_NAME = "Disable host firewall (ufw)"


def build_disable_firewall_command() -> str:
    """Return a shell command that disables ``ufw`` if it is installed.

    Idempotent and persistent: ``ufw disable`` writes ``ENABLED=no`` to
    ``/etc/ufw/ufw.conf``, so the host stays open across reboots, and a second
    run on an already-inactive host exits 0. Hosts with no ``ufw`` binary
    (every non-Vultr image we use) are a no-op, which is why this is added
    unconditionally rather than per provider.
    """
    return "command -v ufw >/dev/null 2>&1 && ufw disable >/dev/null 2>&1 || true"
