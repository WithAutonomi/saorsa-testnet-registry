from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.provisioning.elastic import (
    DEFAULT_RETENTION_DAYS,
    ELASTIC_DATA_DIR,
    ELASTIC_LOG_DIR,
    ELASTIC_MOUNT_POINT,
    ES_USERNAME,
    ES_VERSION,
    ILM_POLICY_NAME,
    INDEX_PATTERN,
    ElasticProvisioner,
)


def _make_provisioner(**overrides):
    defaults = {
        "host_ip": "10.0.0.1",
        "ssh_key_path": "~/.ssh/id_rsa",
        "testnet_name": "DEV-01",
    }
    defaults.update(overrides)
    return ElasticProvisioner(**defaults)


class TestElasticsearchYml:
    def test_contains_cluster_name(self):
        p = _make_provisioner(testnet_name="DEV-01")
        yml = p._build_elasticsearch_yml()
        assert "cluster.name: ant-logs-dev-01" in yml

    def test_contains_data_and_log_paths(self):
        p = _make_provisioner()
        yml = p._build_elasticsearch_yml()
        assert f"path.data: {ELASTIC_DATA_DIR}" in yml
        assert f"path.logs: {ELASTIC_LOG_DIR}" in yml

    def test_single_node_discovery(self):
        p = _make_provisioner()
        yml = p._build_elasticsearch_yml()
        assert "discovery.type: single-node" in yml

    def test_security_enabled_without_tls(self):
        p = _make_provisioner()
        yml = p._build_elasticsearch_yml()
        assert "xpack.security.enabled: true" in yml
        assert "xpack.security.http.ssl.enabled: false" in yml
        assert "xpack.security.transport.ssl.enabled: false" in yml

    def test_binds_to_all_interfaces(self):
        p = _make_provisioner()
        yml = p._build_elasticsearch_yml()
        assert "network.host: 0.0.0.0" in yml

    def test_anonymous_access_for_version_detection(self):
        p = _make_provisioner()
        yml = p._build_elasticsearch_yml()
        assert "anonymous:" in yml
        assert "roles: remote_monitoring_collector" in yml
        assert "authz_exception: false" in yml


class TestHeapOptionsCommand:
    def test_reads_meminfo(self):
        p = _make_provisioner()
        cmd = p._build_heap_options_command()
        assert "/proc/meminfo" in cmd
        assert "MemTotal" in cmd

    def test_caps_at_31g(self):
        p = _make_provisioner()
        cmd = p._build_heap_options_command()
        # 31g = 31744 MiB preserves compressed-oops threshold.
        assert "31744" in cmd

    def test_floors_at_1g(self):
        p = _make_provisioner()
        cmd = p._build_heap_options_command()
        assert "1024" in cmd

    def test_writes_heap_options_file(self):
        p = _make_provisioner()
        cmd = p._build_heap_options_command()
        assert "/etc/elasticsearch/jvm.options.d/heap.options" in cmd
        assert "-Xms" in cmd
        assert "-Xmx" in cmd


def _simulate_heap_shell(mem_kb: int) -> int:
    """Python reference implementation that mirrors the shell logic."""
    heap_mb = mem_kb // 1024 // 2
    if heap_mb > 31744:
        heap_mb = 31744
    if heap_mb < 1024:
        heap_mb = 1024
    return heap_mb


class TestHeapSizingLogic:
    @pytest.mark.parametrize(
        "mem_kb,expected_mb",
        [
            (8_000_000, 3906),  # ~8 GB RAM → ~3.9g
            (16_432_000, 8023),  # ~16 GB RAM → ~8g
            (32_000_000, 15625),  # ~32 GB RAM → ~15.6g
            (66_000_000, 31744),  # ~64 GB RAM → capped at 31g
            (100_000_000, 31744),  # 100 GB RAM → still capped at 31g
            (512_000, 1024),  # ~500 MB RAM → floored at 1g
        ],
    )
    def test_expected_heap(self, mem_kb, expected_mb):
        assert _simulate_heap_shell(mem_kb) == expected_mb


class TestSetupScript:
    def test_contains_username(self):
        p = _make_provisioner()
        script = p._build_setup_script()
        assert f"USERNAME = '{ES_USERNAME}'" in script

    def test_contains_password(self):
        p = _make_provisioner()
        script = p._build_setup_script()
        assert f"PASSWORD = '{p._es_password}'" in script

    def test_creates_index_template(self):
        p = _make_provisioner()
        script = p._build_setup_script()
        assert "/_index_template/ant-node-logs" in script
        assert INDEX_PATTERN in script

    def test_creates_role(self):
        p = _make_provisioner()
        script = p._build_setup_script()
        assert "/_security/role/ant_log_writer" in script
        assert "'write'" in script
        assert "'read'" in script

    def test_creates_user(self):
        p = _make_provisioner()
        script = p._build_setup_script()
        assert "/_security/user/{USERNAME}" in script
        assert "'ant_log_writer'" in script

    def test_creates_api_key(self):
        p = _make_provisioner()
        script = p._build_setup_script()
        assert "/_security/api_key" in script
        assert "ant_log_reader" in script

    def test_writes_credentials_to_files(self):
        p = _make_provisioner()
        script = p._build_setup_script()
        assert "/etc/elasticsearch/ant-credentials" in script
        assert "SETUP_COMPLETE" in script

    def test_creates_ilm_policy_with_default_retention(self):
        p = _make_provisioner()
        script = p._build_setup_script()
        assert f"ILM_POLICY_NAME = '{ILM_POLICY_NAME}'" in script
        assert f"RETENTION_DAYS = {DEFAULT_RETENTION_DAYS}" in script
        assert "/_ilm/policy/" in script
        assert "'delete'" in script
        assert "f'{RETENTION_DAYS}d'" in script

    def test_creates_ilm_policy_with_custom_retention(self):
        p = _make_provisioner(retention_days=3)
        script = p._build_setup_script()
        assert "RETENTION_DAYS = 3" in script

    def test_rejects_zero_retention(self):
        with pytest.raises(ValueError, match="retention_days"):
            _make_provisioner(retention_days=0)

    def test_default_shards_matches_s_4vcpu_droplet(self):
        p = _make_provisioner()
        script = p._build_setup_script()
        assert "NUMBER_OF_SHARDS = 4" in script
        assert "'number_of_shards': NUMBER_OF_SHARDS" in script

    def test_custom_shards(self):
        p = _make_provisioner(shards=8)
        script = p._build_setup_script()
        assert "NUMBER_OF_SHARDS = 8" in script

    def test_rejects_zero_shards(self):
        with pytest.raises(ValueError, match="shards"):
            _make_provisioner(shards=0)

    def test_rejects_negative_retention(self):
        with pytest.raises(ValueError, match="retention_days"):
            _make_provisioner(retention_days=-1)

    def test_index_template_attaches_ilm_policy(self):
        p = _make_provisioner()
        script = p._build_setup_script()
        assert "index.lifecycle.name" in script
        assert "ILM_POLICY_NAME" in script

    def test_index_template_has_expected_mappings(self):
        p = _make_provisioner()
        script = p._build_setup_script()
        for field in [
            "timestamp",
            "message",
            "level",
            "target",
            "peer_id",
            "version",
            "commit",
            "host",
            "service_number",
            "testnet_name",
            "host_role",
        ]:
            assert f"'{field}'" in script

    def test_password_is_random(self):
        p1 = _make_provisioner()
        p2 = _make_provisioner()
        assert p1._es_password != p2._es_password


class TestMountVolumeOp:
    @patch("saorsa_deploy.provisioning.elastic.add_op")
    def test_mount_command_uses_elastic_mount_point(self, mock_add_op):
        p = _make_provisioner()
        mock_state = MagicMock()
        p._add_mount_volume_op(mock_state)

        mock_add_op.assert_called_once()
        call_kwargs = mock_add_op.call_args
        commands = call_kwargs.kwargs.get("commands", call_kwargs[1].get("commands", []))
        mount_cmd = commands[0]
        assert ELASTIC_MOUNT_POINT in mount_cmd
        # Shared multi-provider mount command: must try every provider's
        # device glob (the old DO-only glob broke OVH) and format raw volumes.
        assert "ANT_VOLUME:" in mount_cmd
        assert "scsi-0DO_Volume_" in mount_cmd
        assert "scsi-SQEMU_QEMU_HARDDISK_" in mount_cmd
        assert "mkfs.ext4" in mount_cmd


class TestMountOrderingOp:
    @patch("saorsa_deploy.provisioning.elastic.add_op")
    def test_installs_dropin_requiring_the_data_volume(self, mock_add_op):
        p = _make_provisioner()
        p._add_mount_ordering_op(MagicMock())

        mock_add_op.assert_called_once()
        commands = mock_add_op.call_args.kwargs["commands"]
        assert f"RequiresMountsFor={ELASTIC_MOUNT_POINT}" in commands[0]
        assert "/etc/systemd/system/elasticsearch.service.d" in commands[0]

    @patch("saorsa_deploy.provisioning.elastic.add_op")
    def test_skipped_on_root_disk_providers(self, mock_add_op):
        # Contabo has no attachable volume: the data dir is on / by design.
        p = _make_provisioner(root_disk=True)
        p._add_mount_ordering_op(MagicMock())
        mock_add_op.assert_not_called()


class TestInstallElasticsearchOps:
    @patch("saorsa_deploy.provisioning.elastic.add_op")
    def test_install_downloads_correct_version(self, mock_add_op):
        p = _make_provisioner()
        mock_state = MagicMock()
        p._add_install_elasticsearch_ops(mock_state)

        mock_add_op.assert_called_once()
        call_kwargs = mock_add_op.call_args
        commands = call_kwargs.kwargs.get("commands", call_kwargs[1].get("commands", []))
        install_cmd = commands[0]
        assert ES_VERSION in install_cmd
        assert "dpkg -i" in install_cmd


class TestConfigureElasticsearchOps:
    @patch("saorsa_deploy.provisioning.elastic.add_op")
    def test_creates_data_and_log_dirs(self, mock_add_op):
        p = _make_provisioner()
        mock_state = MagicMock()
        p._add_configure_elasticsearch_ops(mock_state)

        # Should have 3 calls: dirs, config, jvm
        assert mock_add_op.call_count == 3

        # First call creates directories
        first_call = mock_add_op.call_args_list[0]
        commands = first_call.kwargs.get("commands", first_call[1].get("commands", []))
        dir_cmds = " ".join(commands)
        assert ELASTIC_DATA_DIR in dir_cmds
        assert ELASTIC_LOG_DIR in dir_cmds
        assert "chown" in dir_cmds


class TestRetrieveConnectionDetails:
    @patch("subprocess.run")
    def test_returns_expected_keys(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="test-value\n")
        p = _make_provisioner(host_ip="1.2.3.4", testnet_name="DEV-01")
        details = p._retrieve_connection_details()

        assert details["elasticsearch_url"] == "http://1.2.3.4:9200"
        assert details["elasticsearch_username"] == "test-value"
        assert details["elasticsearch_password"] == "test-value"
        assert details["elasticsearch_api_key"] == "test-value"
        assert "dev-01" in details["elasticsearch_index_pattern"]

    @patch("subprocess.run")
    def test_reads_three_credential_files(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="val\n")
        p = _make_provisioner()
        p._retrieve_connection_details()

        assert mock_run.call_count == 3
        paths_read = [call.args[0][-1] for call in mock_run.call_args_list]
        assert "cat /etc/elasticsearch/ant-credentials/username" in paths_read
        assert "cat /etc/elasticsearch/ant-credentials/password" in paths_read
        assert "cat /etc/elasticsearch/ant-credentials/api-key" in paths_read

    @patch("subprocess.run")
    def test_raises_on_ssh_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="Connection refused")
        p = _make_provisioner()

        with pytest.raises(RuntimeError, match="Failed to read"):
            p._retrieve_connection_details()
