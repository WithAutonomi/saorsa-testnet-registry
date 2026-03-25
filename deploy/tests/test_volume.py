from saorsa_deploy.provisioning.volume import (
    MOUNT_POINT,
    VOLUME_DEVICE_GLOB,
    build_mount_volume_command,
)


class TestBuildMountVolumeCommand:
    def test_default_mount_point(self):
        cmd = build_mount_volume_command()
        assert MOUNT_POINT in cmd

    def test_custom_mount_point(self):
        cmd = build_mount_volume_command("/mnt/custom")
        assert "/mnt/custom" in cmd
        assert MOUNT_POINT not in cmd

    def test_checks_if_already_mounted(self):
        cmd = build_mount_volume_command()
        assert f"mountpoint -q {MOUNT_POINT}" in cmd

    def test_reports_already_mounted(self):
        cmd = build_mount_volume_command()
        assert "ANT_VOLUME:ALREADY_MOUNTED" in cmd

    def test_reports_mounted(self):
        cmd = build_mount_volume_command()
        assert "ANT_VOLUME:MOUNTED" in cmd

    def test_reports_no_device(self):
        cmd = build_mount_volume_command()
        assert "ANT_VOLUME:NO_DEVICE" in cmd

    def test_uses_do_volume_glob(self):
        cmd = build_mount_volume_command()
        assert VOLUME_DEVICE_GLOB in cmd

    def test_creates_mount_point_directory(self):
        cmd = build_mount_volume_command()
        assert f"mkdir -p {MOUNT_POINT}" in cmd

    def test_adds_fstab_entry(self):
        cmd = build_mount_volume_command()
        assert "/etc/fstab" in cmd

    def test_uses_nofail_mount_option(self):
        cmd = build_mount_volume_command()
        assert "nofail" in cmd

    def test_uses_discard_mount_option(self):
        cmd = build_mount_volume_command()
        assert "discard" in cmd

    def test_exits_with_error_when_no_device(self):
        cmd = build_mount_volume_command()
        assert "exit 1" in cmd
