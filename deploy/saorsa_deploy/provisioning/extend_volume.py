"""Pyinfra operation that grows the LVM-backed data volume on a node VM
without stopping any ant-node service.

The shell command rescans the underlying block device, runs ``pvresize``,
extends the linear LV to fill the VG, and runs ``resize2fs`` against the
mounted ext4 filesystem. Every step is idempotent — re-running after the
filesystem already matches the device size is a no-op.

The hard-coded VG name (``ant-data``) and LV path (``/dev/ant-data/storage``)
match every node VM in ant-prod-01. The script aborts with a
``ANT_RESIZE:NO_PV`` marker if the VG is missing, so we never accidentally
operate on a host that doesn't match the production template.

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

VG_NAME = "ant-data"
LV_PATH = "/dev/ant-data/storage"
MOUNT_POINT = "/mnt/ant-node-storage"


def build_extend_and_verify_command(services: list[str]) -> str:
    """Return a self-contained shell that resizes the LV+FS then asserts every
    listed service is still ``active``.

    On success, prints two markers to stdout:
      ANT_RESIZE:DONE size_bytes=<bytes-after-resize>
      ANT_VERIFY:OK active=<n>
    """
    if not services:
        raise ValueError("services list must not be empty — cannot verify nothing")
    quoted = " ".join(shlex.quote(s) for s in services)
    # `pvs <name>` treats <name> as a device path, not a VG name — must use
    # --select to filter by vg_name. Getting this wrong returns "Failed to
    # find device 'ant-data'" with exit 5.
    return f"""set -euo pipefail
PV=$(pvs -S 'vg_name={VG_NAME}' --noheadings -o pv_name 2>/dev/null | tr -d ' ')
if [ -z "$PV" ]; then
    echo 'ANT_RESIZE:NO_PV'
    exit 1
fi

DEV_BASE=$(basename "$PV")
RESCAN="/sys/class/block/$DEV_BASE/device/rescan"
if [ -w "$RESCAN" ]; then
    echo 1 > "$RESCAN" || true
fi

pvresize "$PV" >/dev/null

VG_FREE_BYTES=$(vgs --noheadings -o vg_free --units b {VG_NAME} | tr -dc '0-9')
if [ "${{VG_FREE_BYTES:-0}}" -gt 0 ]; then
    lvextend -l +100%FREE {LV_PATH} >/dev/null
fi

resize2fs {LV_PATH} >/dev/null

SIZE_BYTES=$(df -B1 --output=size {MOUNT_POINT} | tail -1 | tr -dc '0-9')
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


def add_extend_volume_op(state: State, services: list[str]):
    """Add the resize+verify op to a Pyinfra State.

    The script is base64-encoded and piped through ``sudo -n bash -s`` so it
    runs as root under bash regardless of which user the SSH connection used
    or what ``/bin/sh`` resolves to on the target.
    """
    script = build_extend_and_verify_command(services)
    encoded = base64.b64encode(script.encode()).decode()
    one_liner = f"echo {encoded} | base64 -d | sudo -n bash -s"
    return add_op(
        state,
        server.shell,
        name="Extend ant-data LV and verify ant-node services",
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
