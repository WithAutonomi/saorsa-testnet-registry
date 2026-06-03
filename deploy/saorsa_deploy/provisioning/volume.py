from pyinfra.api import State
from pyinfra.api.operation import add_op
from pyinfra.operations import server

MOUNT_POINT = "/mnt/ant-node-storage"
LOG_DIR = f"{MOUNT_POINT}/log"
VOLUME_DEVICE_GLOBS = [
    "/dev/disk/by-id/scsi-0DO_Volume_*",          # Digital Ocean
    "/dev/disk/by-id/scsi-0HC_Volume_*",          # Hetzner
    "/dev/disk/by-id/virtio-*",                   # Vultr
    # OVH (OpenStack/QEMU): attached Cinder volumes get a UUID-suffixed
    # SCSI VPD-83 by-id symlink with an `S` prefix; the boot disk has only
    # the `0`-prefixed alias for `drive-scsi0-0-0-0`, so this glob is
    # unambiguous for data volumes.
    "/dev/disk/by-id/scsi-SQEMU_QEMU_HARDDISK_*",
]
STRIPE_VG_NAME = "ant_storage_vg"
STRIPE_LV_NAME = "ant_storage_lv"


def build_mount_volume_command(mount_point: str = MOUNT_POINT) -> str:
    """Build a shell command that finds an attached volume, mounts it, and adds an fstab entry.

    Supports Digital Ocean, Hetzner, and Vultr volume device paths.
    The command is idempotent: it skips if already mounted and only adds the fstab entry once.
    Reports status via stdout markers:
      - ANT_VOLUME:ALREADY_MOUNTED — mount point already active
      - ANT_VOLUME:MOUNTED — volume was mounted and fstab updated
      - ANT_VOLUME:NO_DEVICE — no attached volume device found
    """
    # Try each provider's glob pattern until one matches
    discover_parts = " ".join(
        f'[ -z "$DEVICE" ] && DEVICE=$(ls {glob} 2>/dev/null | head -n1);'
        for glob in VOLUME_DEVICE_GLOBS
    )
    return (
        f"if mountpoint -q {mount_point}; then "
        f"echo 'ANT_VOLUME:ALREADY_MOUNTED'; "
        f"else "
        f"DEVICE=''; {discover_parts} "
        f'if [ -z "$DEVICE" ]; then '
        f"echo 'ANT_VOLUME:NO_DEVICE'; exit 1; "
        f"fi; "
        f"if ! blkid -o value -s TYPE $DEVICE > /dev/null 2>&1; then "
        f"mkfs.ext4 -q $DEVICE || {{ echo 'ANT_VOLUME:FORMAT_FAILED'; exit 1; }}; "
        f"fi; "
        f"mkdir -p {mount_point} && "
        f"mount -o defaults,nofail,discard $DEVICE {mount_point} || "
        f"{{ echo 'ANT_VOLUME:MOUNT_FAILED'; exit 1; }}; "
        f"grep -q '{mount_point}' /etc/fstab || "
        f'echo "$DEVICE {mount_point} ext4 defaults,nofail,discard 0 2" >> /etc/fstab; '
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


# --- Provider-specific device discovery for striped volumes ---

DEVICE_DISCOVERY = {
    "digitalocean": (
        "ls /dev/disk/by-id/scsi-0DO_Volume_* 2>/dev/null | sort"
    ),
    "hetzner": (
        "ls /dev/disk/by-id/scsi-0HC_Volume_* 2>/dev/null | sort"
    ),
    "vultr": (
        "ls /dev/vd[b-z] 2>/dev/null | sort"
    ),
    "aws": (
        "ls /dev/xvd[f-z] /dev/nvme[1-9]n1 2>/dev/null | sort"
    ),
}


def build_striped_volume_command(
    provider: str,
    mount_point: str = MOUNT_POINT,
    vg_name: str = STRIPE_VG_NAME,
    lv_name: str = STRIPE_LV_NAME,
) -> str:
    """Build a shell command that creates an LVM striped volume from two block devices.

    The command is idempotent:
      - Skips if the mount point is already active
      - Uses provider-specific device discovery
      - Creates PVs, a VG, a striped LV (2 stripes), formats ext4, mounts, adds fstab

    Reports status via stdout markers:
      - ANT_STRIPE:ALREADY_MOUNTED — mount point already active
      - ANT_STRIPE:CREATED — striped volume created and mounted
      - ANT_STRIPE:NOT_ENOUGH_DEVICES — fewer than 2 block devices found
    """
    discover_cmd = DEVICE_DISCOVERY.get(provider)
    if not discover_cmd:
        raise ValueError(f"Unknown provider for volume discovery: {provider}")

    lv_path = f"/dev/{vg_name}/{lv_name}"

    return (
        f"if mountpoint -q {mount_point}; then "
        f"echo 'ANT_STRIPE:ALREADY_MOUNTED'; "
        f"else "
        f"apt-get install -y lvm2 > /dev/null 2>&1; "
        f"DEV1=$({discover_cmd} | sed -n '1p'); "
        f"DEV2=$({discover_cmd} | sed -n '2p'); "
        f'if [ -z "$DEV1" ] || [ -z "$DEV2" ]; then '
        f"echo 'ANT_STRIPE:NOT_ENOUGH_DEVICES'; exit 1; "
        f"fi; "
        f"pvcreate -f $DEV1 $DEV2 && "
        f"vgcreate {vg_name} $DEV1 $DEV2 && "
        f"lvcreate -l 100%FREE --stripes 2 --stripesize 64 -n {lv_name} {vg_name} && "
        f"mkfs.ext4 {lv_path} && "
        f"mkdir -p {mount_point} && "
        f"mount -o defaults,noatime {lv_path} {mount_point} && "
        f"grep -q '{lv_path}' /etc/fstab || "
        f'echo "{lv_path} {mount_point} ext4 defaults,noatime 0 2" >> /etc/fstab && '
        f"echo 'ANT_STRIPE:CREATED'; "
        f"fi"
    )


def add_striped_volume_op(
    state: State,
    provider: str,
    mount_point: str = MOUNT_POINT,
):
    """Add a Pyinfra operation that creates a striped LVM volume from two attached disks."""
    cmd = build_striped_volume_command(provider, mount_point)
    return add_op(
        state,
        server.shell,
        name=f"Create striped LVM volume ({provider})",
        commands=[cmd],
    )
