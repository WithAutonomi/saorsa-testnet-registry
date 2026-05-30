"""Intentional clock-skew injection for client and node VMs.

When enabled on a VM, provisioning disables the OS time-sync service and jumps
the system clock by a fixed magnitude (``SKEW_HOURS``) in a per-host "random"
direction. This is a *test* affordance: the network previously had bugs in its
quotation / connection paths when a client's clock diverged from UTC, and we
deliberately skew clocks to prove that dependence is gone.

Design notes:

- **Fixed magnitude, random direction.** Every skewed host shifts by the same
  number of hours; whether it runs fast or slow is decided by
  :func:`skew_direction_for_ip`, a deterministic hash of the host IP. This
  spreads both directions across a deployment while staying reproducible — a
  re-provision picks the same direction for the same host.
- **Idempotent.** :func:`build_clock_skew_command` guards on a marker file so a
  re-run never *compounds* another offset onto an already-skewed clock.
- **Persistent.** Time sync is disabled before the clock is moved, so the skew
  survives until the VM is destroyed.

The node-side selection (:func:`select_skew_ips`) mirrors ``nat.py``'s
region-distributed, deterministic strategy so re-runs don't flip which hosts
are skewed.
"""

from __future__ import annotations

import hashlib

# Magnitude of the injected skew. "A few hours" — large enough to break any
# naive UTC assumption, small enough to stay sane. Tune here if needed.
SKEW_HOURS = 3

# Marker file written once the skew has been applied; its presence makes the
# setup command a no-op on subsequent provision runs (prevents compounding).
SKEW_MARKER = "/etc/ant-clock-skew-applied"


def skew_direction_for_ip(ip: str) -> int:
    """Return +1 (clock runs fast) or -1 (clock runs slow) for ``ip``.

    Deterministic: a hash of the IP decides the direction, so the choice is
    effectively random across hosts yet stable across re-provisions.
    """
    digest = hashlib.blake2b(ip.encode("utf-8"), digest_size=8).digest()
    return 1 if digest[0] % 2 == 0 else -1


def select_skew_ips(
    vm_ips_by_region: dict[str, list[str]], count: int
) -> list[str]:
    """Select up to ``count`` VM IPs to skew, spread across regions.

    Strategy mirrors :func:`saorsa_deploy.provisioning.nat.select_nat_ips`:
    "one per region first, then round-robin". Regions are sorted by key; IPs
    within each region are sorted; lanes are interleaved slot-by-slot so the
    first ``count`` IPs span as many regions as possible.

    Deterministic for a given input — re-running provisioning yields the same
    selection, so a host doesn't flip between skewed and un-skewed.
    """
    if count <= 0 or not vm_ips_by_region:
        return []
    total = sum(len(ips) for ips in vm_ips_by_region.values())
    if total == 0:
        return []
    target = min(total, count)

    sorted_regions = sorted(vm_ips_by_region.keys())
    lanes = [sorted(vm_ips_by_region[r]) for r in sorted_regions]
    max_len = max(len(lane) for lane in lanes)

    ordered: list[str] = []
    for slot in range(max_len):
        for lane in lanes:
            if slot < len(lane):
                ordered.append(lane[slot])

    return ordered[:target]


def build_clock_skew_command(direction: int, hours: int = SKEW_HOURS) -> str:
    """Return the shell command that skews a host's clock.

    ``direction`` is +1 (fast) or -1 (slow). The command:

    1. Short-circuits with ``ANT_SKEW:ALREADY`` if the marker file exists.
    2. Disables NTP and stops/disables every common time-sync service.
    3. Jumps the system clock by ``±hours`` using ``date -s``.
    4. Records the applied offset in the marker file and prints
       ``ANT_SKEW:APPLIED:<offset>``.

    Idempotent via the marker file — re-running never compounds the offset.
    """
    sign = "+" if direction >= 0 else "-"
    offset = f"{sign}{hours} hours"
    # Runs under /bin/sh (dash on Ubuntu) via pyinfra's server.shell, so no
    # bashisms / pipefail. `date -d 'now <offset>'` is GNU coreutils (present
    # on the Ubuntu images we provision).
    return f"""\
set -eu
MARKER="{SKEW_MARKER}"
if [ -f "$MARKER" ]; then
    echo "ANT_SKEW:ALREADY"
    exit 0
fi
timedatectl set-ntp false 2>/dev/null || true
for svc in systemd-timesyncd chrony chronyd ntp ntpsec; do
    systemctl stop "$svc" 2>/dev/null || true
    systemctl disable "$svc" 2>/dev/null || true
done
NEW_TIME=$(date -d 'now {offset}' '+%Y-%m-%d %H:%M:%S')
date -s "$NEW_TIME" >/dev/null
echo "{offset}" > "$MARKER"
echo "ANT_SKEW:APPLIED:{offset}"
"""
