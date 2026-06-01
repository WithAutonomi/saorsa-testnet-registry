"""Pyinfra operation that grows the data volume on a node VM without stopping
any ant-node service.

The shell command autodetects the on-disk layout backing
``/mnt/ant-node-storage`` and grows it accordingly. The prod-01 fleet is not
uniform and the layout does NOT track the provider — most nodes (all OVH plus
most Hetzner) are raw ext4, while all Vultr and a couple of Hetzner nodes are
LVM:

  * **LVM** (``/dev/mapper/...``): rescan the underlying PV(s), ``pvresize``,
    ``lvextend -l +100%FREE``, then ``resize2fs`` the LV.
  * **Raw ext4 on a whole device** (ext4 directly on ``/dev/sdb``, no LVM):
    rescan the device, then ``resize2fs`` it directly.

The layout is discovered from the live mount (``findmnt`` + ``lsblk``) rather
than assumed, so the same op covers every node regardless of provider and
survives future re-provisioning. Every step is idempotent — re-running once the filesystem
already matches the device size is a no-op. A partition-backed filesystem
(which would need ``growpart`` first) is deliberately refused with an
``ANT_RESIZE:UNSUPPORTED`` marker rather than mis-resized.

After the resize, the same script verifies that every ant-node service named
by the caller is still ``active`` — an unexpected restart counts as a
failure even though the resize itself completed.

Execution shape: the script is base64-encoded into a one-liner that pipes
the decoded payload through ``sudo -n bash -s``. This guarantees the script
runs as root (AWS production VMs SSH in as ``ubuntu`` — passwordless sudo
is enabled) and under bash (Ubuntu's ``/bin/sh`` is dash, which doesn't
support ``set -o pipefail``). On root SSH users, ``sudo -n`` is a harmless
pass-through.
"""

import base64
import shlex

from pyinfra.api import State
from pyinfra.api.operation import add_op
from pyinfra.operations import server

MOUNT_POINT = "/mnt/ant-node-storage"
# How long to wait for the kernel to reflect a cloud-side volume grow before
# giving up (the SCSI rescan is asynchronous — see ``rescan_and_wait`` below).
RESCAN_SETTLE_TIMEOUT_SECONDS = 180


def build_extend_and_verify_command(services: list[str], target_gb: int) -> str:
    """Return a self-contained shell that resizes the data volume then asserts
    every listed service is still ``active``.

    The layout (LVM vs raw ext4) is autodetected from the live mount.
    ``target_gb`` is the size the cloud volume was grown to — the script waits
    for the backing block device to actually reflect it before resizing.

    On success, prints two markers to stdout:
      ANT_RESIZE:DONE size_bytes=<bytes-after-resize>
      ANT_VERIFY:OK active=<n>
    """
    if not services:
        raise ValueError("services list must not be empty — cannot verify nothing")
    if target_gb <= 0:
        raise ValueError("target_gb must be a positive integer")
    quoted = " ".join(shlex.quote(s) for s in services)
    # Minimum sectors the backing device must report before we resize. Computed
    # with 1 GB = 10^9 bytes so a GiB-provisioned device (which is larger)
    # always clears the bar — we only need a lower bound that proves the grow
    # landed, not an exact match.
    min_sectors = target_gb * 1_000_000_000 // 512
    return f"""set -euo pipefail
MOUNT={MOUNT_POINT}
MIN_SECTORS={min_sectors}
SETTLE_TIMEOUT={RESCAN_SETTLE_TIMEOUT_SECONDS}

# Trigger a device rescan so a cloud-side grow becomes visible without a reboot,
# then BLOCK until the kernel actually reports the new size. The SCSI
# revalidation kicked off by the rescan is asynchronous, so resizing
# immediately can otherwise run against the stale pre-grow size and silently
# no-op (observed on OVH). The rescan node only exists for SCSI disks (sdX); on
# virtio-blk (vdX) it's absent and the kernel picks the resize up itself, so
# the `-w` guard makes the write a harmless no-op there.
rescan_and_wait() {{
    base=$(basename "$1")
    rescan="/sys/class/block/$base/device/rescan"
    if [ -w "$rescan" ]; then
        echo 1 > "$rescan" || true
    fi
    sizefile="/sys/block/$base/size"
    deadline=$(( $(date +%s) + SETTLE_TIMEOUT ))
    while :; do
        cur=$(cat "$sizefile" 2>/dev/null || echo 0)
        if [ "${{cur:-0}}" -ge "$MIN_SECTORS" ]; then
            return 0
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "ANT_RESIZE:DEVICE_TOO_SMALL dev=$1 sectors=${{cur:-0}} want=$MIN_SECTORS"
            return 1
        fi
        sleep 2
    done
}}

SRC=$(findmnt -nro SOURCE "$MOUNT" 2>/dev/null || true)
if [ -z "$SRC" ]; then
    echo 'ANT_RESIZE:NO_MOUNT'
    exit 1
fi

DEVTYPE=$(lsblk -ndo TYPE "$SRC")
if [ "$DEVTYPE" = "lvm" ]; then
    # LVM-backed: grow every PV in the VG, then fill the LV.
    VG=$(lvs --noheadings -o vg_name "$SRC" | tr -d ' ')
    if [ -z "$VG" ]; then
        echo 'ANT_RESIZE:NO_VG'
        exit 1
    fi
    for PV in $(pvs -S "vg_name=$VG" --noheadings -o pv_name | tr -d ' '); do
        rescan_and_wait "$PV"
        pvresize "$PV" >/dev/null
    done
    VG_FREE_BYTES=$(vgs --noheadings -o vg_free --units b "$VG" | tr -dc '0-9')
    if [ "${{VG_FREE_BYTES:-0}}" -gt 0 ]; then
        lvextend -l +100%FREE "$SRC" >/dev/null
    fi
    resize2fs "$SRC" >/dev/null
elif [ "$DEVTYPE" = "disk" ]; then
    # Raw ext4 on a whole device: no LVM, grow the filesystem directly.
    rescan_and_wait "$SRC"
    resize2fs "$SRC" >/dev/null
else
    echo "ANT_RESIZE:UNSUPPORTED devtype=$DEVTYPE src=$SRC"
    exit 1
fi

SIZE_BYTES=$(df -B1 --output=size "$MOUNT" | tail -1 | tr -dc '0-9')
echo "ANT_RESIZE:DONE size_bytes=${{SIZE_BYTES}}"

INACTIVE=0
ACTIVE=0
for svc in {quoted}; do
    if systemctl is-active --quiet "$svc"; then
        ACTIVE=$((ACTIVE+1))
    else
        INACTIVE=$((INACTIVE+1))
        echo "ANT_VERIFY:INACTIVE svc=$svc"
    fi
done
if [ $INACTIVE -gt 0 ]; then
    echo "ANT_VERIFY:FAIL inactive=$INACTIVE"
    exit 1
fi
echo "ANT_VERIFY:OK active=$ACTIVE"
"""


def add_extend_volume_op(state: State, services: list[str], target_gb: int):
    """Add the resize+verify op to a Pyinfra State.

    The script is base64-encoded and piped through ``sudo -n bash -s`` so it
    runs as root under bash regardless of which user the SSH connection used
    or what ``/bin/sh`` resolves to on the target.
    """
    script = build_extend_and_verify_command(services, target_gb)
    encoded = base64.b64encode(script.encode()).decode()
    one_liner = f"echo {encoded} | base64 -d | sudo -n bash -s"
    return add_op(
        state,
        server.shell,
        name="Extend node data volume and verify ant-node services",
        commands=[one_liner],
    )


def parse_size_marker(stdout: str) -> int | None:
    """Pull ``size_bytes`` out of the ``ANT_RESIZE:DONE`` line, if present."""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("ANT_RESIZE:DONE"):
            for token in line.split():
                if token.startswith("size_bytes="):
                    try:
                        return int(token.split("=", 1)[1])
                    except ValueError:
                        return None
    return None
