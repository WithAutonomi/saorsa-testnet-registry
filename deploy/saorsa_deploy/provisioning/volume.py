from pyinfra.api import State
from pyinfra.api.operation import add_op
from pyinfra.operations import server

MOUNT_POINT = "/mnt/ant-node-storage"
LOG_DIR = f"{MOUNT_POINT}/log"
VOLUME_DEVICE_GLOB = "/dev/disk/by-id/scsi-0DO_Volume_*"


def build_mount_volume_command(mount_point: str = MOUNT_POINT) -> str:
    """Build a shell command that finds a DigitalOcean volume, mounts it, and adds an fstab entry.

    The command is idempotent: it skips if already mounted and only adds the fstab entry once.
    Reports status via stdout markers:
      - ANT_VOLUME:ALREADY_MOUNTED — mount point already active
      - ANT_VOLUME:MOUNTED — volume was mounted and fstab updated
      - ANT_VOLUME:NO_DEVICE — no attached volume device found
    """
    return (
        f"if mountpoint -q {mount_point}; then "
        f"echo 'ANT_VOLUME:ALREADY_MOUNTED'; "
        f"else "
        f"DEVICE=$(ls {VOLUME_DEVICE_GLOB} 2>/dev/null | head -n1); "
        f'if [ -z "$DEVICE" ]; then '
        f"echo 'ANT_VOLUME:NO_DEVICE'; exit 1; "
        f"fi; "
        f"mkdir -p {mount_point} && "
        f"mount -o defaults,nofail,discard $DEVICE {mount_point} && "
        f"grep -q '{mount_point}' /etc/fstab || "
        f'echo "$DEVICE {mount_point} ext4 defaults,nofail,discard 0 2" >> /etc/fstab && '
        f"echo 'ANT_VOLUME:MOUNTED'; "
        f"fi"
    )


def add_mount_volume_op(state: State, mount_point: str = MOUNT_POINT):
    """Add a Pyinfra operation that mounts the attached DigitalOcean volume."""
    mount_cmd = build_mount_volume_command(mount_point)
    return add_op(
        state,
        server.shell,
        name="Mount attached storage volume",
        commands=[mount_cmd],
    )
