"""Pyinfra operation that lowers the ext4 reserved-block percentage on a node
VM's data volume, without stopping any ant-node service.

ext4 reserves 5% of blocks for root by default. On a large node data volume
(hundreds of GB) that is many GB the node can never use — the volume reports
100% full while ``df`` still shows headroom. These volumes hold only node data
(never the OS root), so the reserve can safely drop to ~1%.

The device backing ``/mnt/ant-node-storage`` is discovered from the live mount
(``findmnt``) rather than assumed, so the same op covers raw-ext4 nodes
(``/dev/sdb``) and LVM-striped nodes (``/dev/mapper/...``) regardless of
provider, and survives future re-provisioning. Root-disk providers (Contabo,
OVH 3-AZ) keep the node data on the OS root filesystem — there is no separate
data volume to tune — so the script detects that case and SKIPS, never touching
the root filesystem's reserve.

Every step is idempotent: re-running once the reserve already matches is a
no-op. ``tune2fs -m`` works on a mounted ext4 filesystem (the reserved-block
count is a live-writable superblock field), so no unmount/service stop is
needed.

Execution shape mirrors ``extend_volume``: the script is base64-encoded into a
one-liner piped through ``sudo -n bash -s`` so it runs as root under bash
regardless of the SSH user (AWS prod VMs SSH in as ``ubuntu`` with passwordless
sudo; on root SSH users ``sudo -n`` is a harmless pass-through).
"""

import base64

from pyinfra.api import State
from pyinfra.api.operation import add_op
from pyinfra.operations import server

MOUNT_POINT = "/mnt/ant-node-storage"


def build_set_reserved_blocks_command(reserve_percent: int) -> str:
    """Return a self-contained shell that sets the ext4 reserved-block
    percentage at ``/mnt/ant-node-storage`` to ``reserve_percent``.

    Prints exactly one status marker to stdout:
      ANT_RESERVE:DONE src=<dev> pct=<n> reserved_before=<blocks> reserved_after=<blocks>
      ANT_RESERVE:NO_MOUNT           — nothing mounted at the data path (root-disk host)
      ANT_RESERVE:ROOT_DISK_SKIP     — data path is on the OS root filesystem — skipped
      ANT_RESERVE:NOT_EXT4 fstype=<t> — not an ext4 filesystem — skipped
    """
    if not isinstance(reserve_percent, int) or not (0 <= reserve_percent <= 50):
        raise ValueError(f"reserve_percent must be an int in 0..50, got {reserve_percent!r}")

    mount = MOUNT_POINT
    return f"""set -euo pipefail
MOUNT="{mount}"
PCT="{reserve_percent}"
SRC=$(findmnt -nro SOURCE "$MOUNT" 2>/dev/null || true)
ROOT=$(findmnt -nro SOURCE / 2>/dev/null || true)
if [ -z "$SRC" ]; then
  echo "ANT_RESERVE:NO_MOUNT"
  exit 0
fi
if [ "$SRC" = "$ROOT" ]; then
  echo "ANT_RESERVE:ROOT_DISK_SKIP src=$SRC"
  exit 0
fi
FSTYPE=$(findmnt -nro FSTYPE "$MOUNT" 2>/dev/null || true)
if [ "$FSTYPE" != "ext4" ]; then
  echo "ANT_RESERVE:NOT_EXT4 fstype=$FSTYPE src=$SRC"
  exit 0
fi
reserved() {{ tune2fs -l "$SRC" | awk -F: '/Reserved block count/ {{gsub(/ /,"",$2); print $2}}'; }}
BEFORE=$(reserved)
tune2fs -m "$PCT" "$SRC" >/dev/null
AFTER=$(reserved)
echo "ANT_RESERVE:DONE src=$SRC pct=$PCT reserved_before=$BEFORE reserved_after=$AFTER"
"""


def add_set_reserved_blocks_op(state: State, reserve_percent: int):
    """Add the set-reserve op to a Pyinfra State.

    The script is base64-encoded and piped through ``sudo -n bash -s`` so it
    runs as root under bash regardless of which user the SSH connection used.
    """
    script = build_set_reserved_blocks_command(reserve_percent)
    encoded = base64.b64encode(script.encode()).decode()
    one_liner = f"echo {encoded} | base64 -d | sudo -n bash -s"
    return add_op(
        state,
        server.shell,
        name=f"Set ext4 reserved blocks to {reserve_percent}% on data volume",
        commands=[one_liner],
    )
