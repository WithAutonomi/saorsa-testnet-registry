import json
from unittest.mock import MagicMock, patch

from saorsa_deploy.provisioning import egress_dashboard
from saorsa_deploy.provisioning.monitoring import (
    GRAFANA_CLIENT_DASHBOARD_PATH,
    GRAFANA_EGRESS_DASHBOARD_PATH,
    INFLUXDB_BUCKET,
    INFLUXDB_CLIENT_BUCKET,
    MonitoringProvisioner,
)


def _make_provisioner(**overrides):
    defaults = {
        "influxdb_ip": "10.0.0.1",
        "grafana_ip": "10.0.0.2",
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


def _all_queries(dashboard: dict) -> list[str]:
    """Every Flux string a dashboard issues: panel targets + template vars."""
    queries = []
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            queries.append(target["query"])
    for var in dashboard["templating"]["list"]:
        queries.append(var.get("query", ""))
    return queries


def _buckets_referenced(dashboard: dict) -> set[str]:
    """All bucket names the dashboard's Flux queries read from."""
    import re

    buckets = set()
    for query in _all_queries(dashboard):
        buckets.update(re.findall(r'bucket:\s*"([^"]+)"', query))
    return buckets


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

    def test_queries_only_the_node_bucket(self):
        # Every panel + template-var query reads the node bucket; the client
        # bucket must never be touched here, so client data can't bleed in.
        dashboard = json.loads(
            _make_provisioner()._build_host_metrics_dashboard()
        )
        assert _buckets_referenced(dashboard) == {INFLUXDB_BUCKET}
        assert INFLUXDB_CLIENT_BUCKET not in _buckets_referenced(dashboard)


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

    def test_queries_only_the_client_bucket(self):
        # Physical isolation: the client dashboard reads exclusively from the
        # client bucket, so node data cannot appear here regardless of the
        # host_role dropdown.
        dashboard = json.loads(
            _make_provisioner()._build_client_host_metrics_dashboard()
        )
        assert _buckets_referenced(dashboard) == {INFLUXDB_CLIENT_BUCKET}
        assert INFLUXDB_BUCKET not in _buckets_referenced(dashboard)


class TestInfluxDbSetupScript:
    def test_creates_both_buckets(self):
        script = _make_provisioner()._build_influxdb_setup_py()
        # The node bucket is created by /api/v2/setup; the client bucket is
        # created explicitly afterwards via /api/v2/buckets.
        assert f'"bucket": "{INFLUXDB_BUCKET}"' in script
        assert f'"name": "{INFLUXDB_CLIENT_BUCKET}"' in script
        assert "/api/v2/buckets" in script
        # No unsubstituted placeholders remain.
        assert "{{" not in script

    def test_telegraf_token_is_org_scoped(self):
        # An org-scoped token covers both buckets (and any added later), so a
        # new bucket never requires reminting the immutable token.
        script = _make_provisioner()._build_influxdb_setup_py()
        assert '"orgID": org_id' in script
        assert '"type": "buckets", "id": bucket_id' not in script


class TestConnectionDetailsExposeClientBucket:
    def test_client_bucket_in_returned_details(self):
        # _retrieve_telegraf_token SSHes to the InfluxDB VM; patch subprocess
        # so we can assert the client bucket is surfaced for monitoring state.
        prov = _make_provisioner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="tok\n")
            token = prov._retrieve_telegraf_token()
        details = prov._connection_details(token)
        assert details["influxdb_token"] == "tok"
        assert details["influxdb_url"] == "http://10.0.0.1:8086"
        assert details["grafana_url"] == "http://10.0.0.2:3000"
        assert details["influxdb_bucket"] == INFLUXDB_BUCKET
        assert details["influxdb_client_bucket"] == INFLUXDB_CLIENT_BUCKET


class TestRetention:
    def test_default_retention_is_infinite(self):
        script = _make_provisioner()._build_influxdb_setup_py()
        assert 'RETENTION_SECONDS = int("0")' in script

    def test_retention_days_converted_to_seconds(self):
        script = _make_provisioner(
            retention_days=14
        )._build_influxdb_setup_py()
        assert f'RETENTION_SECONDS = int("{14 * 86400}")' in script

    def test_no_placeholder_left_in_script(self):
        script = _make_provisioner(
            retention_days=7
        )._build_influxdb_setup_py()
        assert "{{INFLUXDB_RETENTION_SECONDS}}" not in script


class TestGrafanaDatasource:
    def test_datasource_points_at_influxdb_vm_with_token(self):
        yaml = _make_provisioner()._build_grafana_datasource_yaml(
            influxdb_url="http://10.0.0.1:8086", influx_token="tok-123"
        )
        assert "url: http://10.0.0.1:8086" in yaml
        assert "token: tok-123" in yaml
        assert "{{INFLUXDB_URL}}" not in yaml
        assert "{{INFLUX_TOKEN}}" not in yaml


class TestProvisioningWiresClientDashboard:
    @patch("saorsa_deploy.provisioning.monitoring.add_op")
    @patch("saorsa_deploy.provisioning.monitoring.server")
    def test_configure_grafana_writes_client_dashboard(
        self, _mock_server, mock_add_op
    ):
        _make_provisioner()._add_configure_grafana_ops(
            MagicMock(),
            influxdb_url="http://10.0.0.1:8086",
            influx_token="tok",
        )

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


class TestEgressAccountingDashboard:
    """The accounted-vs-actual egress dashboard (V2-1225)."""

    def _dash(self):
        return json.loads(_make_provisioner()._build_egress_accounting_dashboard())

    def test_identity_stable(self):
        dash = self._dash()
        assert dash["title"] == "Egress accounting"
        assert dash["uid"] == "ant-egress-accounting"
        assert "egress-accounting" in dash["tags"]

    def test_exact_panel_set_in_order(self):
        assert _panel_titles(self._dash()) == [
            "Egress accounting coverage (%)",
            "Unaccounted egress (mean over range, top 15)",
            "Relay throughput (MASQUE relay server)",
            "Active relay reservations",
            "Replication serving",
            "Numerator definition",
        ]

    def test_reads_only_the_node_bucket(self):
        """No cross-datasource join: every query is Flux against testnet-metrics."""
        dash = self._dash()
        assert _buckets_referenced(dash) == {INFLUXDB_BUCKET}
        for panel in dash["panels"]:
            if panel["type"] != "text":
                assert panel["datasource"] == {"type": "influxdb", "uid": "influxdb"}

    def test_role_defaults_to_all_and_host_variable_present(self):
        dash = self._dash()
        assert _host_role_var(dash)["current"]["value"] == "$__all"
        assert {v["name"] for v in dash["templating"]["list"]} == {"host_role", "host"}

    def _query(self, title: str) -> str:
        panel = next(p for p in self._dash()["panels"] if p["title"] == title)
        return panel["targets"][0]["query"]

    def test_numerator_is_exactly_udp_tx_plus_masque_relayed(self):
        """wire_tx_bytes / fetch_response_tx_bytes would double-count the same
        bytes at a higher layer, and bytes_relayed under the relay-CLIENT target
        (saorsa_transport::relay::traffic) is the other, small leg."""
        for title in ("Egress accounting coverage (%)",
                      "Unaccounted egress (mean over range, top 15)"):
            q = self._query(title)
            assert (
                '(r["target"] == "saorsa_transport::traffic" and r["_field"] == "udp_tx_bytes")'
                in q
            )
            assert (
                '(r["target"] == "saorsa_transport::masque::relay_server" '
                'and r["_field"] == "bytes_relayed")' in q
            )
            assert "wire_tx_bytes" not in q
            assert "fetch_response_tx_bytes" not in q
            assert "saorsa_transport::relay::traffic" not in q

    def test_tail_host_is_lowercased_before_joining_on_net(self):
        """The ES forwarder upper-cases `host` before every output; `net` is
        lowercase. Joining without folding case would match nothing."""
        q = self._query("Egress accounting coverage (%)")
        assert 'import "strings"' in q
        assert "strings.toLower(v: r.host)" in q
        assert 'join(tables: {a: accounted, n: nic}, on: ["_time", "host"])' in q

    def test_counters_are_differenced_not_plotted_raw(self):
        """The summaries are cumulative per process; a restart must not go negative."""
        for title in ("Egress accounting coverage (%)", "Relay throughput (MASQUE relay server)",
                      "Replication serving"):
            q = self._query(title)
            assert "difference(nonNegative: true)" in q
            assert f"aggregateWindow(every: {egress_dashboard.WINDOW}, fn: last" in q
            # Cross-service totals are a per-(host, _time) sum, NOT a second
            # aggregateWindow -- that would re-stamp each increase one window
            # late against the NIC side of the join.
            assert q.count("aggregateWindow(") == (2 if "nic =" in q else 1)

    def test_nic_denominator_is_busiest_non_loopback_interface(self):
        q = self._query("Egress accounting coverage (%)")
        assert 'r["_measurement"] == "net" and r["_field"] == "bytes_sent"' in q
        assert 'r["interface"] != "lo" and r["interface"] != "all"' in q
        assert 'group(columns: ["host", "_time"])\n  |> max()' in q

    def test_coverage_panel_thresholds_red_below_80(self):
        panel = next(p for p in self._dash()["panels"]
                     if p["title"] == "Egress accounting coverage (%)")
        steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
        assert steps == [
            {"color": "red", "value": None},
            {"color": "green", "value": 80},
        ]
        assert panel["fieldConfig"]["defaults"]["unit"] == "percent"
        assert "mean" in panel["options"]["legend"]["calcs"]

    def test_unaccounted_panel_is_a_top_15_table_of_rates(self):
        panel = next(p for p in self._dash()["panels"]
                     if p["title"].startswith("Unaccounted egress"))
        assert panel["type"] == "table"
        assert panel["fieldConfig"]["defaults"]["unit"] == "Bps"
        q = panel["targets"][0]["query"]
        assert "limit(n: 15)" in q
        assert 'sort(columns: ["_value"], desc: true)' in q
        assert "/ 600.0" in q

    def test_numerator_note_names_the_exclusions(self):
        panel = next(p for p in self._dash()["panels"] if p["type"] == "text")
        content = panel["options"]["content"]
        for phrase in ("udp_tx_bytes", "bytes_relayed", "wire_tx_bytes",
                       "fetch_response_tx_bytes", "upper bound", "V2-834"):
            assert phrase in content

    @patch("saorsa_deploy.provisioning.monitoring.add_op")
    @patch("saorsa_deploy.provisioning.monitoring.server")
    def test_configure_grafana_writes_egress_dashboard(self, _mock_server, mock_add_op):
        _make_provisioner()._add_configure_grafana_ops(
            MagicMock(), influxdb_url="http://10.0.0.1:8086", influx_token="tok",
        )
        call = next(
            c for c in mock_add_op.call_args_list
            if c.kwargs["name"] == "Write egress accounting dashboard"
        )
        commands = "\n".join(call.kwargs["commands"])
        assert GRAFANA_EGRESS_DASHBOARD_PATH in commands
        assert '"uid": "ant-egress-accounting"' in commands
