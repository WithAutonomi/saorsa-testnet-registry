from saorsa_deploy.provisioning.volume import (
    DATA_VOLUME_RESERVED_PERCENT,
    MOUNT_POINT,
    VOLUME_DEVICE_GLOBS,
    build_mount_ordering_dropin_command,
    build_mount_volume_command,
    build_root_disk_dir_command,
    build_striped_volume_command,
)


class TestBuildRootDiskDirCommand:
    def test_makes_mount_point_and_reports_root_disk(self):
        cmd = build_root_disk_dir_command()
        assert f"mkdir -p {MOUNT_POINT}" in cmd
        assert "ANT_VOLUME:ROOT_DISK" in cmd

    def test_custom_mount_point(self):
        cmd = build_root_disk_dir_command("/mnt/custom")
        assert "mkdir -p /mnt/custom" in cmd

    def test_never_touches_a_block_device(self):
        # Root-disk hosts (OVH 3-AZ) have no volume — the command must not try to
        # discover/format/mount a device.
        cmd = build_root_disk_dir_command()
        for token in ("blkid", "mkfs", "mount ", "NO_DEVICE", "/dev/"):
            assert token not in cmd


class TestBuildMountOrderingDropinCommand:
    def test_writes_dropin_for_the_named_service(self):
        cmd = build_mount_ordering_dropin_command("influxdb", "/mnt/monitoring-storage")
        assert "/etc/systemd/system/influxdb.service.d" in cmd
        assert "10-mount-ordering.conf" in cmd

    def test_requires_mounts_for_the_data_volume(self):
        # The whole point: systemd must both order the unit after the mount and
        # fail it if the mount never happens.
        cmd = build_mount_ordering_dropin_command("influxdb", "/mnt/monitoring-storage")
        assert "[Unit]" in cmd
        assert "RequiresMountsFor=/mnt/monitoring-storage" in cmd

    def test_reloads_systemd_so_the_dropin_takes_effect(self):
        cmd = build_mount_ordering_dropin_command("influxdb", "/mnt/monitoring-storage")
        assert "systemctl daemon-reload" in cmd
        assert "ANT_MOUNT_ORDER:APPLIED" in cmd

    def test_service_and_mount_point_are_independent(self):
        cmd = build_mount_ordering_dropin_command("elasticsearch", "/mnt/elastic-storage")
        assert "/etc/systemd/system/elasticsearch.service.d" in cmd
        assert "RequiresMountsFor=/mnt/elastic-storage" in cmd
        assert "influxdb" not in cmd
        assert "monitoring-storage" not in cmd

    def test_does_not_start_or_restart_the_service(self):
        # Installing the ordering must never bounce a running service — the op
        # runs mid-provision, before the deliberate start step.
        cmd = build_mount_ordering_dropin_command("influxdb", "/mnt/monitoring-storage")
        for token in ("systemctl start", "systemctl restart", "systemctl stop"):
            assert token not in cmd

    def test_heredoc_is_quoted_so_the_body_is_not_expanded(self):
        # An unquoted heredoc would let the shell interpolate the unit body.
        cmd = build_mount_ordering_dropin_command("influxdb", "/mnt/monitoring-storage")
        assert "<< 'DROPIN_EOF'" in cmd


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

    def test_uses_all_provider_volume_globs(self):
        cmd = build_mount_volume_command()
        for glob in VOLUME_DEVICE_GLOBS:
            assert glob in cmd

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

    def test_lowers_ext4_reserved_blocks_to_1_percent(self):
        # Future data volumes should be formatted with a 1% reserve, not the
        # ext4 default of 5% (which wastes ~16 GB on a ~394 GB volume).
        cmd = build_mount_volume_command()
        assert f"tune2fs -m {DATA_VOLUME_RESERVED_PERCENT} $DEVICE" in cmd
        assert DATA_VOLUME_RESERVED_PERCENT == 1

    def test_tune2fs_runs_outside_the_mkfs_guard(self):
        # DO/Hetzner volumes arrive pre-formatted ext4 (Terraform), so they
        # never hit the mkfs branch — the tune2fs must run after that branch
        # closes, not inside it, or those providers keep the 5% default.
        cmd = build_mount_volume_command()
        mkfs_fi = cmd.index("fi; ")  # closes the `if ! blkid ...` guard
        tune_at = cmd.index("tune2fs")
        assert tune_at > mkfs_fi


class TestBuildStripedVolumeCommand:
    def test_lowers_ext4_reserved_blocks_after_mkfs(self):
        cmd = build_striped_volume_command("digitalocean")
        lv_path = "/dev/ant_storage_vg/ant_storage_lv"
        assert f"tune2fs -m {DATA_VOLUME_RESERVED_PERCENT} {lv_path}" in cmd
        # Must come after the filesystem is created on the LV.
        assert cmd.index("mkfs.ext4") < cmd.index("tune2fs")
