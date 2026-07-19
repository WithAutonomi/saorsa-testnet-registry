"""Tests for the `prod set-volume-reserve` in-VM tune2fs command builder."""

import pytest

from saorsa_deploy.provisioning.set_reserved_blocks import (
    MOUNT_POINT,
    build_set_reserved_blocks_command,
)


class TestBuildSetReservedBlocksCommand:
    def test_runs_tune2fs_with_requested_percent(self):
        cmd = build_set_reserved_blocks_command(1)
        assert 'tune2fs -m "$PCT" "$SRC"' in cmd
        assert 'PCT="1"' in cmd

    def test_honours_a_non_default_percent(self):
        cmd = build_set_reserved_blocks_command(3)
        assert 'PCT="3"' in cmd

    def test_detects_device_via_findmnt_not_hardcoded(self):
        cmd = build_set_reserved_blocks_command(1)
        assert 'findmnt -nro SOURCE "$MOUNT"' in cmd
        assert f'MOUNT="{MOUNT_POINT}"' in cmd
        # No hardcoded data-volume device.
        assert "/dev/sdb" not in cmd

    def test_skips_root_disk_hosts(self):
        # Contabo / OVH 3-AZ keep node data on the OS root fs — never touch it.
        cmd = build_set_reserved_blocks_command(1)
        assert 'if [ "$SRC" = "$ROOT" ]; then' in cmd
        assert "ANT_RESERVE:ROOT_DISK_SKIP" in cmd
        assert "ANT_RESERVE:NO_MOUNT" in cmd

    def test_skips_non_ext4_filesystems(self):
        cmd = build_set_reserved_blocks_command(1)
        assert 'if [ "$FSTYPE" != "ext4" ]; then' in cmd
        assert "ANT_RESERVE:NOT_EXT4" in cmd

    def test_emits_done_marker_with_before_and_after(self):
        cmd = build_set_reserved_blocks_command(1)
        assert "ANT_RESERVE:DONE" in cmd
        assert "reserved_before=" in cmd
        assert "reserved_after=" in cmd

    @pytest.mark.parametrize("bad", [-1, 51, 1.5, "1"])
    def test_rejects_out_of_range_or_non_int_percent(self, bad):
        with pytest.raises(ValueError):
            build_set_reserved_blocks_command(bad)
