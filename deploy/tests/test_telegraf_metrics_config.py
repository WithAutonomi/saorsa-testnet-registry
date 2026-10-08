"""Content tests for the generated Telegraf host-metrics config.

These assert that CLIENT-role hosts get the client `ant` memory inputs appended
while node-tier configs stay byte-identical (no regression / no node-fleet noise).
"""

from saorsa_deploy.provisioning.telegraf_metrics import (
    NO_DATA_DEVICE,
    TelegrafMetricsProvisioner,
    build_data_location_env_command,
    build_telegraf_metrics_config,
)


class TestBaseConfig:
    def test_default_excludes_client_services(self):
        config = build_telegraf_metrics_config()
        assert 'metric_source = "ant-client"' not in config
        assert "inputs.cgroup" not in config
        assert "memory.peak" not in config

    def test_default_keeps_node_procstat_stanzas(self):
        config = build_telegraf_metrics_config()
        assert 'cgroup = "system.slice/ant-node-*.service"' in config
        assert 'cgroup = "system.slice/ant-genesis-node.service"' in config


class TestDiskIoInput:
    """Per-device disk I/O collection, present on BOTH tiers.

    The client config is an append to the node base, so a single diskio block in
    the base file covers node-tier and client-tier hosts alike.
    """

    def test_base_config_includes_diskio(self):
        config = build_telegraf_metrics_config()
        assert "[[inputs.diskio]]" in config

    def test_client_config_includes_diskio(self):
        config = build_telegraf_metrics_config(include_client_services=True)
        assert "[[inputs.diskio]]" in config

    def test_diskio_declared_exactly_once_on_client(self):
        # The client block appends to the base; a second diskio stanza would
        # double-collect every device.
        config = build_telegraf_metrics_config(include_client_services=True)
        assert config.count("[[inputs.diskio]]") == 1

    def test_diskio_device_glob_covers_every_provider(self):
        # DigitalOcean vda/sda, OVH + Hetzner sda/sdb, Vultr vda/vdb, NVMe, and
        # LVM dm-* for the volumes that are not raw ext4.
        config = build_telegraf_metrics_config()
        assert 'devices = ["sd*", "vd*", "nvme*", "xvd*", "dm-*"]' in config

    def test_diskio_device_glob_excludes_snapd_loop_devices(self):
        # `devices` is include-only (the plugin has no exclude option), so the
        # ~10 permanently-idle snapd loop devices on Ubuntu cloud images are
        # kept out purely by not matching the glob.
        config = build_telegraf_metrics_config()
        diskio_block = config.split("[[inputs.diskio]]", 1)[1].split("[[inputs.", 1)[0]
        assert "loop" not in diskio_block

    def test_diskio_skips_serial_numbers(self):
        # Disk serials as tags would inflate cardinality for no analytical gain.
        config = build_telegraf_metrics_config()
        assert "skip_serial_number = true" in config


class TestDiskCapacityInput:
    """`inputs.disk` is the OCCUPANCY series, the numerator of "disk consumed
    per GB stored". It is not interchangeable with diskio, which counts I/O."""

    def test_base_config_includes_disk(self):
        config = build_telegraf_metrics_config()
        assert "[[inputs.disk]]" in config

    def test_disk_declared_exactly_once_on_client(self):
        config = build_telegraf_metrics_config(include_client_services=True)
        assert config.count("[[inputs.disk]]") == 1


class TestNodeStorageVolumeTag:
    """The node storage volume is picked out by a `volume_role` tag on BOTH disk
    measurements — but by different keys, because diskio knows only kernel
    device names while disk knows only mountpoints."""

    def test_config_tags_the_data_device_with_volume_role(self):
        config = build_telegraf_metrics_config()
        assert 'namepass = ["diskio"]' in config
        assert 'pattern = "^${ANT_DATA_DEVICE}$"' in config
        assert 'replacement = "node-storage"' in config
        assert 'result_key = "volume_role"' in config

    def test_config_tags_the_data_mountpoint_with_volume_role(self):
        # `disk` carries a `device` tag too, but its value is the fstab-style
        # source ("mapper/vg-lv"), never the kernel name ANT_DATA_DEVICE holds,
        # so matching on the mountpoint is the only thing that works on LVM.
        config = build_telegraf_metrics_config()
        assert 'namepass = ["disk"]' in config
        assert 'pattern = "^${ANT_DATA_MOUNT}$"' in config
        assert 'key = "path"' in config

    def test_both_volume_role_processors_present_on_client_hosts_too(self):
        config = build_telegraf_metrics_config(include_client_services=True)
        assert config.count('result_key = "volume_role"') == 2

    def test_disk_processor_does_not_capture_diskio(self):
        # namepass globs are matched against the measurement name; "disk" must
        # not swallow "diskio", or the device-keyed tag would be overwritten by
        # a path-keyed miss.
        config = build_telegraf_metrics_config()
        assert 'namepass = ["disk"]' in config
        assert 'namepass = ["diskio"]' in config

    def test_node_service_processor_is_untouched(self):
        # The pre-existing procstat regex processor must keep its own namepass;
        # a diskio-scoped processor must not capture procstat metrics.
        config = build_telegraf_metrics_config()
        assert 'namepass = ["procstat"]' in config
        assert 'result_key = "node_service"' in config


class TestDataLocationResolution:
    """`ANT_DATA_DEVICE` and `ANT_DATA_MOUNT` are resolved ON THE HOST: the
    backing device differs by provider and by raw-ext4 vs LVM, and the
    mountpoint differs between volume-backed and root-disk-only providers, so
    neither can be baked into the config."""

    def test_resolves_from_the_node_storage_mount(self):
        cmd = build_data_location_env_command(host_role="WAN_NODE")
        assert 'findmnt -nro SOURCE "/mnt/ant-node-storage"' in cmd
        # lsblk KNAME maps /dev/sdb -> sdb and /dev/mapper/vg-lv -> dm-0, which
        # is what diskio actually reports as the device name.
        assert "lsblk -nro KNAME" in cmd

    def test_emits_both_the_device_and_the_mountpoint(self):
        cmd = build_data_location_env_command(host_role="WAN_NODE")
        assert "ANT_DATA_DEVICE=" in cmd
        assert "ANT_DATA_MOUNT=" in cmd

    def test_mount_is_the_node_storage_path_when_it_is_mounted(self):
        cmd = build_data_location_env_command(host_role="WAN_NODE")
        assert 'MNT="/mnt/ant-node-storage"' in cmd

    def test_node_hosts_fall_back_to_the_root_device_and_root_mount(self):
        # Contabo and OVH 3-AZ are root-disk only: node data lives on the OS
        # root filesystem, so that device and `/` ARE the node storage.
        cmd = build_data_location_env_command(host_role="WAN_NODE")
        assert "findmnt -nro SOURCE /" in cmd
        assert "MNT=/;" in cmd

    def test_client_hosts_never_fall_back_to_root(self):
        # A client VM runs no node storage at all; tagging its root disk or its
        # root filesystem "node-storage" would be simply untrue.
        cmd = build_data_location_env_command(host_role="CLIENT")
        assert "findmnt -nro SOURCE / 2>/dev/null" not in cmd
        assert "MNT=/;" not in cmd
        assert NO_DATA_DEVICE in cmd

    def test_falls_back_to_a_sentinel_that_matches_no_device_or_path(self):
        cmd = build_data_location_env_command(host_role="WAN_NODE")
        assert f"ANT_DATA_DEVICE={NO_DATA_DEVICE}" in cmd
        assert f"ANT_DATA_MOUNT={NO_DATA_DEVICE}" in cmd
        # Must not be a valid kernel device name, or it would tag a real disk.
        assert not NO_DATA_DEVICE.isalnum()
        # Must not be a valid mountpoint either: every real path starts with "/".
        assert not NO_DATA_DEVICE.startswith("/")

    def test_appends_rather_than_truncating_the_env_file(self):
        # The static env content (InfluxDB creds, testnet name) is written
        # first; truncating here would destroy it.
        cmd = build_data_location_env_command(host_role="WAN_NODE")
        assert ">> /etc/default/telegraf-ant-metrics" in cmd
        assert "> /etc/default/telegraf-ant-metrics" not in cmd.replace(">>", "")

    def test_never_fails_the_provisioning_pass(self):
        # A missing disk tag must not break metrics provisioning.
        cmd = build_data_location_env_command(host_role="WAN_NODE")
        assert "|| true" in cmd
        # The fallback branch must set BOTH sentinels, not just the device one.
        fallback = cmd.rsplit("} || ", 1)[1]
        assert f"ANT_DATA_DEVICE={NO_DATA_DEVICE}" in fallback
        assert f"ANT_DATA_MOUNT={NO_DATA_DEVICE}" in fallback


class TestClientConfig:
    def test_client_appends_procstat_with_source_tag(self):
        config = build_telegraf_metrics_config(include_client_services=True)
        assert 'cgroup = "system.slice/ant-*.service"' in config
        assert 'metric_source = "ant-client"' in config

    def test_client_appends_cgroup_memory_inputs(self):
        config = build_telegraf_metrics_config(include_client_services=True)
        assert "[[inputs.cgroup]]" in config
        assert 'paths = ["/sys/fs/cgroup/system.slice/ant-*.service"]' in config
        assert 'files = ["memory.current", "memory.peak"]' in config

    def test_client_uses_finer_2s_interval(self):
        config = build_telegraf_metrics_config(include_client_services=True)
        assert 'interval = "2s"' in config

    def test_client_is_superset_of_base(self):
        base = build_telegraf_metrics_config()
        client = build_telegraf_metrics_config(include_client_services=True)
        # Node config is appended-to verbatim, never mutated.
        assert client.startswith(base)
        # Node procstat stanzas still present on a client host (they just match
        # nothing there).
        assert 'cgroup = "system.slice/ant-node-*.service"' in client


class TestUnitFile:
    def _provisioner(self):
        return TelegrafMetricsProvisioner(
            host_ips=["198.51.100.1"],
            ssh_key_path="/dev/null",
            influxdb_url="http://localhost:8086",
            influxdb_token="token",
            influxdb_org="org",
            influxdb_bucket="testnet-metrics",
        )

    def test_unit_grants_dac_read_search_for_num_fds(self):
        # telegraf runs as User=telegraf but ant services run as root; without
        # CAP_DAC_READ_SEARCH it cannot read /proc/<pid>/fd, so procstat omits
        # num_fds.
        unit = self._provisioner()._build_unit_file()
        assert "AmbientCapabilities=CAP_DAC_READ_SEARCH" in unit
