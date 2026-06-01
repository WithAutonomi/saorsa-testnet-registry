"""Tests for the layout-autodetecting resize script (LVM vs raw ext4)."""

import pytest

from saorsa_deploy.provisioning.extend_volume import (
    MOUNT_POINT,
    build_extend_and_verify_command,
    parse_size_marker,
)


def test_script_autodetects_both_layouts():
    script = build_extend_and_verify_command(["ant-node-1", "ant-node-2"], 100)
    # Discovers the live mount rather than assuming a device/VG.
    assert 'findmnt -nro SOURCE "$MOUNT"' in script
    assert 'DEVTYPE=$(lsblk -ndo TYPE "$SRC")' in script
    # LVM branch: rescan PVs, pvresize, fill LV, resize2fs.
    assert 'if [ "$DEVTYPE" = "lvm" ]' in script
    assert "pvresize" in script and "lvextend -l +100%FREE" in script
    # Raw-device branch: resize2fs directly, no LVM.
    assert 'elif [ "$DEVTYPE" = "disk" ]' in script
    # Partition / anything else is refused, not mis-resized.
    assert "ANT_RESIZE:UNSUPPORTED" in script
    assert "ANT_RESIZE:NO_MOUNT" in script
    assert MOUNT_POINT in script


def test_script_waits_for_device_to_reflect_grow():
    # The async-rescan race fix: both layouts must wait for the backing device
    # to report the new size (via rescan_and_wait) before resizing.
    script = build_extend_and_verify_command(["ant-node-1"], 100)
    assert "rescan_and_wait" in script
    # 100 GB lower bound in 512-byte sectors (10^9-per-GB).
    assert f"MIN_SECTORS={100 * 1_000_000_000 // 512}" in script
    assert "ANT_RESIZE:DEVICE_TOO_SMALL" in script
    # The wait must precede the resize in the raw branch.
    raw = script.split('elif [ "$DEVTYPE" = "disk" ]', 1)[1]
    assert raw.index("rescan_and_wait") < raw.index("resize2fs")


def test_script_verifies_named_services():
    script = build_extend_and_verify_command(["ant-node-1", "ant-node-7"], 100)
    assert "systemctl is-active --quiet" in script
    assert "ant-node-1" in script and "ant-node-7" in script
    assert "ANT_VERIFY:OK" in script


def test_invalid_args_rejected():
    with pytest.raises(ValueError, match="services list must not be empty"):
        build_extend_and_verify_command([], 100)
    with pytest.raises(ValueError, match="target_gb must be a positive integer"):
        build_extend_and_verify_command(["ant-node-1"], 0)


def test_parse_size_marker_roundtrip():
    stdout = "noise\nANT_RESIZE:DONE size_bytes=107317510144\nANT_VERIFY:OK active=15"
    assert parse_size_marker(stdout) == 107317510144
    assert parse_size_marker("no marker here") is None
