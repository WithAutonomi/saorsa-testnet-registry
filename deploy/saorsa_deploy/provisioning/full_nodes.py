"""Storage-full node simulation for node VMs.

Mirrors the structure of :mod:`saorsa_deploy.provisioning.nat` and
:mod:`saorsa_deploy.provisioning.clock_skew`: a deterministic region-spread
selection helper plus an idempotent shell command. A "full" VM has its mounted
data volume pre-filled so every ant-node sharing it sees (near-)zero free space
(ENOSPC) and behaves as storage-full.

The fill is applied as an orthogonal per-host op (like clock skew) — it does NOT
change the systemd unit files, because consuming disk doesn't alter how the
process runs.
"""

from __future__ import annotations

import math
import random

from saorsa_deploy.provisioning.volume import MOUNT_POINT

FULL_FILLER_PATH = f"{MOUNT_POINT}/.ant_full_filler"
DEFAULT_FULL_HEADROOM_MB = 100


def select_full_ips(
    vm_ips_by_region: dict[str, list[str]],
    percent: int,
    exclude: set[str] | None = None,
    seed: object | None = None,
) -> list[str]:
    """Select which VM IPs should be storage-full.

    Selection is **uniform random** over the candidate pool — deliberately NOT
    region-distributed (unlike :func:`saorsa_deploy.provisioning.nat.select_nat_ips`).
    A chunk PUT only exercises a full node when that node is among the closest
    nodes to the chunk's keyspace address, so full nodes must be scattered, not
    clustered by region. ``seed`` (e.g. the deployment name) makes the draw
    reproducible across runs/machines; pass ``None`` for a system-random draw.

    Any IPs in ``exclude`` (e.g. the NAT-simulated VMs) are removed from the
    candidate pool first, so the selection is guaranteed disjoint from NAT and
    ``percent`` is taken as a percentage of the NON-excluded VMs.

    ``ceil(total * percent / 100)`` VMs are returned (clamped to the pool size).
    The candidate list is sorted before shuffling so a given ``seed`` yields the
    same selection regardless of the input dict's ordering.
    """
    if percent <= 0 or not vm_ips_by_region:
        return []
    exclude = exclude or set()
    candidates = sorted(
        ip for ips in vm_ips_by_region.values() for ip in ips if ip not in exclude
    )
    total = len(candidates)
    if total == 0:
        return []
    target = min(total, math.ceil(total * percent / 100))

    random.Random(seed).shuffle(candidates)
    return candidates[:target]


def build_fill_storage_command(headroom_mb: int = DEFAULT_FULL_HEADROOM_MB) -> str:
    """Return the shell command that pre-fills the data volume on a host.

    Creates a single filler file at ``FULL_FILLER_PATH`` (on the mounted data
    volume) sized to consume all free space minus ``headroom_mb`` MiB, so every
    ant-node sharing the volume sees (near-)zero free space and behaves as
    storage-full. ``fallocate`` reserves real blocks instantly (statvfs free
    space drops); a ``dd`` fallback covers filesystems without fallocate
    support.

    Idempotent via the marker file: if the filler already exists the script
    short-circuits with ``ANT_FULL:ALREADY``. Prints ``ANT_FULL:CONFIGURED`` on
    success, or ``ANT_FULL:NO_SPACE`` if the volume already has less than the
    requested headroom free.

    Runs under /bin/sh (dash on Ubuntu) via pyinfra's server.shell — no
    bashisms / pipefail. ``df -Pk`` reports the Available column in 1024-byte
    (KiB) blocks, matching the headroom maths below.
    """
    return f"""\
set -eu
MARKER="{FULL_FILLER_PATH}"
MOUNT="{MOUNT_POINT}"
if [ -f "$MARKER" ]; then
    echo "ANT_FULL:ALREADY"
    exit 0
fi
AVAIL_KB=$(df -Pk "$MOUNT" | awk 'NR==2 {{print $4}}')
HEADROOM_KB=$(( {int(headroom_mb)} * 1024 ))
FILL_KB=$(( AVAIL_KB - HEADROOM_KB ))
if [ "$FILL_KB" -le 0 ]; then
    echo "ANT_FULL:NO_SPACE"
    exit 0
fi
if command -v fallocate >/dev/null 2>&1; then
    fallocate -l "${{FILL_KB}}KiB" "$MARKER" 2>/dev/null \
        || dd if=/dev/zero of="$MARKER" bs=1024 count="$FILL_KB" status=none
else
    dd if=/dev/zero of="$MARKER" bs=1024 count="$FILL_KB" status=none
fi
sync
echo "ANT_FULL:CONFIGURED"
"""
