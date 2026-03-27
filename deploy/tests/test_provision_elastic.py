from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.provisioning.elastic import (
    ELASTIC_DATA_DIR,
    ELASTIC_LOG_DIR,
    ELASTIC_MOUNT_POINT,
    ES_USERNAME,
    ES_VERSION,
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


class TestJvmOptions:
    def test_sets_heap_size(self):
        p = _make_provisioner()
        jvm = p._build_jvm_options()
        assert "-Xms4g" in jvm
        assert "-Xmx4g" in jvm


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

    def test_index_template_has_expected_mappings(self):
        p = _make_provisioner()
        script = p._build_setup_script()
        for field in ["timestamp", "message", "level", "target", "peer_id",
                       "version", "commit", "host", "service_number",
                       "testnet_name", "host_role"]:
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
        assert "ELASTIC_VOLUME:" in mount_cmd


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
