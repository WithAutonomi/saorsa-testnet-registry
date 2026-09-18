import json
from unittest.mock import MagicMock, patch

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

    def _query(self, title: str) -> str:
        panel = next(p for p in self._dash()["panels"] if p["title"] == title)
        return panel["targets"][0]["query"]

    def test_identity_stable(self):
        dash = self._dash()
        assert dash["title"] == "Egress accounting"
        assert dash["uid"] == "ant-egress-accounting"
        assert "egress-accounting" in dash["tags"]

    def test_exact_panel_set_in_order(self):
        assert _panel_titles(self._dash()) == [
            "Egress accounting coverage (%)",
            "Unaccounted egress (mean over range, top 15)",
            "Socket ground truth — egress (%)",
            "Socket ground truth — ingress (%)",
            "Egress decomposition (selected hosts, stacked)",
            "Relay throughput (relay servers)",
            "Active relay reservations / sessions",
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

    def test_numerator_is_udp_plus_relay_carrier_plus_relayed(self):
        """wire_tx_bytes / fetch_response_tx_bytes would double-count the same
        bytes at a higher layer; virtual_udp_tx_bytes rides on the carrier."""
        for title in ("Egress accounting coverage (%)",
                      "Unaccounted egress (mean over range, top 15)"):
            q = self._query(title)
            for target, field in (
                ("saorsa_transport::traffic", "udp_tx_bytes"),
                ("saorsa_transport::traffic", "relay_carrier_udp_tx_bytes"),
                ("saorsa_transport::relay::traffic", "bytes_relayed"),
                ("saorsa_transport::masque::relay_server", "bytes_relayed"),
            ):
                assert f'(r["target"] == "{target}" and r["_field"] == "{field}")' in q
            assert "wire_tx_bytes" not in q
            assert "fetch_response_tx_bytes" not in q
            assert "virtual_udp_tx_bytes" not in q
            assert "sock_tx_bytes" not in q

    def test_burst_collapse_precedes_differencing(self):
        """A series can carry several lines per tick (two endpoint summaries,
        per-session relay lines): udp_tx_bytes sums them, the relay totals take
        the max, and only then is the 10m window differenced."""
        q = self._query("Egress accounting coverage (%)")
        assert "aggregateWindow(every: 1m, fn: sum, createEmpty: false)" in q
        assert "aggregateWindow(every: 1m, fn: max, createEmpty: false)" in q
        for stream in ("accounted_sum", "accounted_max"):
            body = q.split(f"{stream} = ")[1]
            burst = body.index("aggregateWindow(every: 1m")
            window = body.index("aggregateWindow(every: 10m, fn: last")
            diff = body.index("difference(nonNegative: true)")
            assert burst < window < diff
        assert "union(tables: [accounted_max, accounted_sum])" in q

    def test_cross_service_total_is_a_sum_not_a_second_window(self):
        """Summing across services with another aggregateWindow re-stamps the
        increase one window late against the NIC side of the join."""
        for title in ("Egress accounting coverage (%)", "Relay throughput (relay servers)",
                      "Replication serving"):
            q = self._query(title)
            assert 'group(columns: ["host", "_time"])\n  |> sum()' in q or \
                'group(columns: ["host", "_field", "_time"])\n  |> sum()' in q
            # per stream: burst + 10m window; the nic stream: 10m window only.
            streams = q.count(" = from(bucket")
            nic = q.count("nic = from(bucket")
            assert q.count("aggregateWindow(") == 2 * (streams - nic) + nic

    def test_both_sides_lowercase_host_and_match_case_insensitively(self):
        """tail carries the UPPER-cased host; net carries the OS hostname, which on
        a testnet keeps the deployment name's case (DEV-01-ant-node-lon1-1). The
        Host variable lists the raw net tags, so the match ignores case."""
        q = self._query("Egress accounting coverage (%)")
        assert 'import "strings"' in q
        assert q.count("strings.toLower(v: r.host)") == 3  # 2 accounted streams + nic
        assert 'r["host"] =~ /(?i)^$host$/' in q
        assert 'r["host"] =~ /^$host$/' not in q
        assert 'join(tables: {a: accounted, n: nic}, on: ["_time", "host"])' in q

    def test_nic_denominator_excludes_lo_all_and_nat_veth(self):
        """On a NAT-simulated VM the root end of the veth pair reports the node's
        traffic in the opposite direction and is the busiest interface."""
        q = self._query("Egress accounting coverage (%)")
        assert 'r["_measurement"] == "net" and r["_field"] == "bytes_sent"' in q
        assert 'r["interface"] !~ /^(lo|all|veth.*)$/' in q
        assert 'group(columns: ["host", "_time"])\n  |> max()' in q

    def test_ground_truth_panels_use_socket_counters(self):
        def clause(target, field):
            return f'(r["target"] == "{target}" and r["_field"] == "{field}")'

        tx = self._query("Socket ground truth — egress (%)")
        assert clause("saorsa_transport::traffic", "sock_tx_bytes") in tx
        assert 'r["_field"] == "bytes_sent"' in tx
        rx = self._query("Socket ground truth — ingress (%)")
        assert clause("saorsa_transport::traffic", "sock_rx_bytes") in rx
        assert clause("saorsa_transport::relay::traffic", "raw_rx_bytes") in rx
        assert 'r["_field"] == "bytes_recv"' in rx

    def test_decomposition_is_stacked_per_field_over_selected_hosts(self):
        panel = next(p for p in self._dash()["panels"]
                     if p["title"].startswith("Egress decomposition"))
        assert panel["fieldConfig"]["defaults"]["custom"]["stacking"]["mode"] == "normal"
        q = panel["targets"][0]["query"]
        assert 'group(columns: ["_field", "_time"])\n  |> sum()' in q
        for field in ("wire_tx_bytes", "get_success_tx_bytes", "fetch_response_tx_bytes",
                      "bytes_relayed", "lost_bytes", "stateless_tx_bytes", "failed_dial_tx_bytes"):
            assert field in q
        labelled = {o["matcher"]["options"] for o in panel["fieldConfig"]["overrides"]}
        assert "wire_tx_bytes" in labelled

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
        for phrase in ("udp_tx_bytes", "relay_carrier_udp_tx_bytes", "bytes_relayed",
                       "wire_tx_bytes", "fetch_response_tx_bytes", "upper bound", "V2-834",
                       "sock_tx_bytes"):
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


class TestHostVariableIgnoresTailPoints:
    """The traffic-accounting `tail` points share the node bucket and carry the
    UPPER-cased host; the Host dropdown must not list every host twice."""

    def test_host_variable_pinned_to_cpu_measurement(self):
        for build in ("_build_host_metrics_dashboard", "_build_client_host_metrics_dashboard",
                      "_build_egress_accounting_dashboard"):
            dash = json.loads(getattr(_make_provisioner(), build)())
            var = next(v for v in dash["templating"]["list"] if v["name"] == "host")
            assert 'r["_measurement"] == "cpu"' in var["query"]
            assert 'r["_measurement"] == "cpu"' in var["definition"]
