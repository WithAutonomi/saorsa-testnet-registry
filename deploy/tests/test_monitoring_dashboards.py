import json
from unittest.mock import MagicMock, patch

from saorsa_deploy.provisioning.monitoring import (
    GRAFANA_CLIENT_DASHBOARD_PATH,
    MonitoringProvisioner,
)


def _make_provisioner(**overrides):
    defaults = {
        "host_ip": "10.0.0.1",
        "ssh_key_path": "~/.ssh/id_rsa",
        "grafana_password": "secret",
    }
    defaults.update(overrides)
    return MonitoringProvisioner(**defaults)


def _panel_titles(dashboard: dict) -> list[str]:
    return [p["title"] for p in dashboard["panels"]]


def _host_role_var(dashboard: dict) -> dict:
    return next(
        v
        for v in dashboard["templating"]["list"]
        if v["name"] == "host_role"
    )


class TestNodeHostDashboard:
    def test_title_renamed_to_node_host_statistics(self):
        dashboard = json.loads(
            _make_provisioner()._build_host_metrics_dashboard()
        )
        assert dashboard["title"] == "Node Host Statistics"

    def test_uid_and_tag_stable(self):
        # The on-disk uid stays put so re-provisioning an existing monitoring
        # VM updates the dashboard in place rather than orphaning the old one.
        dashboard = json.loads(
            _make_provisioner()._build_host_metrics_dashboard()
        )
        assert dashboard["uid"] == "ant-host-metrics"
        assert dashboard["tags"] == ["ant", "testnet", "host-metrics"]

    def test_keeps_per_process_panels(self):
        titles = _panel_titles(
            json.loads(_make_provisioner()._build_host_metrics_dashboard())
        )
        assert "Node RSS (max / mean / min across nodes)" in titles
        assert "Node CPU (max / mean / min across nodes)" in titles
        assert "Attached Volume Usage" in titles

    def test_role_defaults_to_all(self):
        dashboard = json.loads(
            _make_provisioner()._build_host_metrics_dashboard()
        )
        assert _host_role_var(dashboard)["current"]["value"] == "$__all"


class TestClientHostDashboard:
    def test_title_and_identity(self):
        dashboard = json.loads(
            _make_provisioner()._build_client_host_metrics_dashboard()
        )
        assert dashboard["title"] == "Client Host Statistics"
        assert dashboard["uid"] == "ant-client-host-metrics"
        assert dashboard["tags"] == ["ant", "testnet", "client-host-metrics"]

    def test_exact_panel_set(self):
        titles = _panel_titles(
            json.loads(
                _make_provisioner()._build_client_host_metrics_dashboard()
            )
        )
        assert titles == [
            "CPU Usage",
            "Memory Usage",
            "System Disk Usage",
            "Network Inbound",
            "Network Outbound",
        ]

    def test_excludes_node_and_volume_panels(self):
        titles = _panel_titles(
            json.loads(
                _make_provisioner()._build_client_host_metrics_dashboard()
            )
        )
        assert not any("Node RSS" in t for t in titles)
        assert not any("Node CPU" in t for t in titles)
        assert "Attached Volume Usage" not in titles

    def test_no_procstat_queries(self):
        # Clients emit no procstat; assert the rendered queries never touch it.
        raw = _make_provisioner()._build_client_host_metrics_dashboard()
        assert "procstat" not in raw
        assert "/mnt/ant-node-storage" not in raw

    def test_role_defaults_to_client(self):
        dashboard = json.loads(
            _make_provisioner()._build_client_host_metrics_dashboard()
        )
        role_var = _host_role_var(dashboard)
        assert role_var["current"]["value"] == "CLIENT"
        assert role_var["current"]["text"] == "CLIENT"
        # Still selectable to other roles if the user wants.
        assert role_var["includeAll"] is True


class TestProvisioningWiresClientDashboard:
    @patch("saorsa_deploy.provisioning.monitoring.add_op")
    @patch("saorsa_deploy.provisioning.monitoring.server")
    def test_configure_grafana_writes_client_dashboard(
        self, _mock_server, mock_add_op
    ):
        _make_provisioner()._add_configure_grafana_ops(MagicMock())

        op_names = [call.kwargs["name"] for call in mock_add_op.call_args_list]
        assert "Write client host metrics dashboard" in op_names

        # The op writes to the dedicated client dashboard path.
        client_call = next(
            call
            for call in mock_add_op.call_args_list
            if call.kwargs["name"] == "Write client host metrics dashboard"
        )
        commands = "\n".join(client_call.kwargs["commands"])
        assert GRAFANA_CLIENT_DASHBOARD_PATH in commands
