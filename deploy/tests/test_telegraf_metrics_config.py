"""Content tests for the generated Telegraf host-metrics config.

These assert that CLIENT-role hosts get the client `ant` memory inputs appended
while node-tier configs stay byte-identical (no regression / no node-fleet noise).
"""

from saorsa_deploy.provisioning.telegraf_metrics import (
    TelegrafMetricsProvisioner,
    build_telegraf_metrics_config,
)


class TestBaseConfig:
    def test_default_excludes_client_services(self):
        config = build_telegraf_metrics_config()
        assert "metric_source = \"ant-client\"" not in config
        assert "inputs.cgroup" not in config
        assert "memory.peak" not in config

    def test_default_keeps_node_procstat_stanzas(self):
        config = build_telegraf_metrics_config()
        assert 'cgroup = "system.slice/ant-node-*.service"' in config
        assert 'cgroup = "system.slice/ant-genesis-node.service"' in config


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
