"""Helpers for apt/unattended-upgrades on testnet VMs.

A previous testnet run (DEV-01, 2026-04-22) saw every VM restart its
services within a ~50 minute window because Ubuntu's
``apt-daily-upgrade.timer`` fired on its randomised per-VM schedule and
pulled in a security-update batch containing ``systemd``/``libc6``/``libpam*``.
Those packages force ``systemctl daemon-reexec``, which in turn restarts
every service with a systemd dependency — including ``ant-node`` and
``ant-bootstrap-*``. The staggered restart wiped DHT routing state across
the cluster and uploads/downloads fell from ~1% to ~96% failure.

Testnets only run for a few days, so the trade-off is easy: take no
automatic updates and restart nothing. This module provides the command
used by every VM provisioner to fully disable the machinery.

Production nodes (``hardening.py``) intentionally keep unattended-upgrades
on and do not call this helper.
"""

DISABLE_UNATTENDED_UPGRADES_OP_NAME = "Disable Ubuntu unattended-upgrades"

SET_HOSTNAME_OP_NAME = "Set system hostname"


def build_disable_unattended_upgrades_command() -> str:
    """Return a shell command that fully disables Ubuntu's automatic upgrade
    machinery. Idempotent; requires root.
    """
    return (
        "systemctl disable --now apt-daily.timer apt-daily-upgrade.timer "
        "2>/dev/null || true; "
        "systemctl mask apt-daily.service apt-daily-upgrade.service "
        "apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true; "
        "DEBIAN_FRONTEND=noninteractive apt-get -y purge unattended-upgrades "
        "2>/dev/null || true; "
        "echo 'UU:DISABLED'"
    )


def build_set_hostname_command(hostname: str) -> str:
    """Return a shell command that sets the VM's OS hostname.

    Relying on the cloud provider to set ``hostname`` from the droplet name
    is not reliable: DEV-01 (2026-04-22) showed 5 of 7 bootstrap VMs with
    scrambled OS hostnames vs. their inventory names, even though the
    underlying services ran correctly. Setting the hostname explicitly at
    provision time removes the ambiguity and keeps telemetry labels aligned
    with the inventory.

    Idempotent; requires root. The ``shlex``-style quoting is deliberately
    avoided — hostnames come from inventory names we control
    (``<testnet>-ant-<role>-<index>``), which are safe shell tokens.
    """
    # Quoting the hostname defends against any future caller that passes
    # something unexpected.
    quoted = hostname.replace("'", "'\\''")
    return (
        f"current=$(hostname); "
        f"if [ \"$current\" = '{quoted}' ]; then "
        f"echo 'HOSTNAME:SKIP'; "
        f"else "
        f"hostnamectl set-hostname '{quoted}' && echo 'HOSTNAME:SET'; "
        f"fi"
    )
