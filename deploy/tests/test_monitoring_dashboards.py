import json
from unittest.mock import MagicMock, patch

from saorsa_deploy.provisioning.monitoring import (
    GRAFANA_BY_SERVICE_DASHBOARD_PATH,
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

    def test_window_is_pushed_down_and_scales_with_range(self):
        """range -> filters -> aggregateWindow, nothing in between: that is the
        shape InfluxDB pushes into storage (one row per series per window
        instead of every sample -- 18s -> 2s on a 24h view). The window widens
        with the range so a 48h prod view stays tractable."""
        q = self._query("Egress accounting coverage (%)")
        assert "range_ns = int(v: v.timeRangeStop) - int(v: v.timeRangeStart)" in q
        assert ("w = if range_ns > int(v: 24h) then 1h "
                "else if range_ns > int(v: 6h) then 30m else 10m") in q
        for stream in ("accounted", "nic"):
            body = q.split(f"{stream} = from(bucket")[1].split("\n\n")[0]
            lines = [ln.strip() for ln in body.splitlines()[1:]]
            first_non_filter = next(
                i for i, ln in enumerate(lines)
                if not ln.startswith("|> range") and not ln.startswith("|> filter")
            )
            assert lines[first_non_filter].startswith("|> aggregateWindow(every: w, fn:")
            assert "|> difference(nonNegative: true)" in lines
            # The cross-series total is a per-(host, _time) sum/max, not a second
            # aggregateWindow, which would re-stamp each increase one window late.
            assert body.count("aggregateWindow(") == 1

    def test_counters_windowed_with_max_not_last(self):
        """One series can carry several lines per tick (two endpoint summaries,
        one at udp_tx_bytes=0; per-session relay lines): `last` would flap and
        `nonNegative` keep the full total every tick (25x the NIC). The
        summary total is >= every other line in the window, so `max`."""
        for title in ("Egress accounting coverage (%)", "Relay throughput (relay servers)",
                      "Replication serving", "Egress decomposition (selected hosts, stacked)"):
            q = self._query(title)
            assert "aggregateWindow(every: w, fn: max, createEmpty: false)" in q
            assert "fn: last" not in q.split("nic = from")[0]

    def test_partial_last_window_dropped_on_both_sides(self):
        """The window cut off by the range end pairs 5 minutes of counter with
        however many seconds of NIC -- measured at 5x, dragging the legend mean."""
        q = self._query("Egress accounting coverage (%)")
        assert q.count("|> filter(fn: (r) => r._time < v.timeRangeStop)") == 2

    def test_sides_combined_with_pivot_not_join(self):
        q = self._query("Egress accounting coverage (%)")
        assert "union(tables: [accounted, nic])" in q
        assert 'pivot(rowKey: ["_time"], columnKey: ["k"], valueColumn: "_value")' in q
        assert "filter(fn: (r) => exists r.a and exists r.n)" in q
        assert "join(" not in q
        assert "100.0 * r.a / r.n" in q

    def test_both_sides_lowercase_host_and_match_case_insensitively(self):
        """tail carries the UPPER-cased host; net carries the OS hostname, which on
        a testnet keeps the deployment name's case (DEV-01-ant-node-lon1-1). The
        Host variable lists the raw net tags, so the match ignores case."""
        q = self._query("Egress accounting coverage (%)")
        assert 'import "strings"' in q
        assert q.count("strings.toLower(v: r.host)") == 2  # accounted + nic
        assert 'r["host"] =~ /(?i)^$host$/' in q
        assert 'r["host"] =~ /^$host$/' not in q

    def test_nic_denominator_excludes_lo_all_and_nat_veth(self):
        """On a NAT-simulated VM the root end of the veth pair reports the node's
        traffic in the opposite direction and is the busiest interface."""
        q = self._query("Egress accounting coverage (%)")
        assert 'r["_measurement"] == "net" and r["_field"] == "bytes_sent"' in q
        assert 'r["interface"] !~ /^(lo|all|veth.*)$/' in q
        assert 'group(columns: ["host", "_time", "k"])\n  |> max()' in q

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
        assert "r._value / w_secs" in q
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
        assert "(r.n - r.a) / w_secs" in q

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
                      "_build_egress_accounting_dashboard",
                      "_build_node_traffic_by_service_dashboard"):
            dash = json.loads(getattr(_make_provisioner(), build)())
            var = next(v for v in dash["templating"]["list"] if v["name"] == "host")
            assert 'r["_measurement"] == "cpu"' in var["query"]
            assert 'r["_measurement"] == "cpu"' in var["definition"]


class TestNodeTrafficByServiceDashboard:
    """The per-service companion: rank fleet-wide, plot only for one host."""

    def _dash(self):
        return json.loads(_make_provisioner()._build_node_traffic_by_service_dashboard())

    def test_identity_stable(self):
        dash = self._dash()
        assert dash["title"] == "Node traffic by service"
        assert dash["uid"] == "ant-node-traffic-by-service"
        assert "node-traffic-by-service" in dash["tags"]
        assert _buckets_referenced(dash) == {INFLUXDB_BUCKET}

    def test_exact_panel_set_in_order(self):
        assert _panel_titles(self._dash()) == [
            "Top 20 services by relay bytes forwarded",
            "Top 20 services by socket egress",
            "Top 20 services by socket ingress",
            "Top 20 services by chunk GET serving",
            "Per-service detail",
            "Socket egress per service",
            "Socket ingress per service",
            "Relay bytes forwarded per service",
            "Chunk GET serving per service",
        ]

    def test_host_variable_is_single_select(self):
        """Per-service lines only make sense for one VM; the prod fleet has 700+
        services and must never be plotted line-per-service fleet-wide."""
        var = next(v for v in self._dash()["templating"]["list"] if v["name"] == "host")
        assert var["multi"] is False
        assert var["includeAll"] is False
        assert var["current"] == {}

    def test_top_tables_rank_fleet_wide_without_host_filter(self):
        dash = self._dash()
        tables = [p for p in dash["panels"] if p["type"] == "table"]
        assert len(tables) == 4
        for panel in tables:
            q = panel["targets"][0]["query"]
            assert "$host" not in q
            assert 'group(columns: ["host", "service_number", "_time"])' in q
            assert 'sort(columns: ["mean"], desc: true)' in q
            assert "limit(n: 20)" in q
            assert "peak: if r._value > accumulator.peak" in q
            # unit on the rate columns only -- a table default formats the
            # service index as "4 B/s"
            assert "unit" not in panel["fieldConfig"]["defaults"]
            units = {
                o["matcher"]["options"]: [p["value"] for p in o["properties"] if p["id"] == "unit"]
                for o in panel["fieldConfig"]["overrides"]
            }
            assert units == {"host": [], "service_number": [], "mean": ["Bps"], "peak": ["Bps"]}
            assert 'regexp.replaceAllString(r: /^.*ant-(node|bootstrap)-([0-9]+).*$/' in q
            # pushdown shape, same as the egress dashboard
            assert "aggregateWindow(every: w, fn: max, createEmpty: false)" in q
            assert "|> filter(fn: (r) => r._time < v.timeRangeStop)" in q

    def test_per_service_panels_are_scoped_to_the_selected_host(self):
        dash = self._dash()
        series = [p for p in dash["panels"] if p["type"] == "timeseries"]
        assert len(series) == 4
        for panel in series:
            q = panel["targets"][0]["query"]
            assert 'r["host"] =~ /(?i)^$host$/' in q
            assert 'group(columns: ["service_number", "_time"])' in q
            assert 'regexp.replaceAllString(r: /^.*ant-(node|bootstrap)-([0-9]+).*$/' in q
            assert panel["fieldConfig"]["defaults"]["displayName"] == (
                "service ${__field.labels.service_number}"
            )

    def test_panels_are_stacked_full_width(self):
        for panel in self._dash()["panels"]:
            assert panel["gridPos"]["w"] == 24
            assert panel["gridPos"]["x"] == 0

    def test_relay_tables_cover_both_relay_servers(self):
        panel = next(p for p in self._dash()["panels"]
                     if p["title"].endswith("relay bytes forwarded"))
        q = panel["targets"][0]["query"]
        for target in ("saorsa_transport::relay::traffic",
                       "saorsa_transport::masque::relay_server"):
            assert f'(r["target"] == "{target}" and r["_field"] == "bytes_relayed")' in q

    @patch("saorsa_deploy.provisioning.monitoring.add_op")
    @patch("saorsa_deploy.provisioning.monitoring.server")
    def test_configure_grafana_writes_by_service_dashboard(self, _mock_server, mock_add_op):
        _make_provisioner()._add_configure_grafana_ops(
            MagicMock(), influxdb_url="http://10.0.0.1:8086", influx_token="tok",
        )
        call = next(
            c for c in mock_add_op.call_args_list
            if c.kwargs["name"] == "Write node traffic by service dashboard"
        )
        commands = "\n".join(call.kwargs["commands"])
        assert GRAFANA_BY_SERVICE_DASHBOARD_PATH in commands
        assert '"uid": "ant-node-traffic-by-service"' in commands
