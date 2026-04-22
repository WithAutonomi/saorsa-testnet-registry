"""InfluxDB 2.x + Grafana provisioning for monitoring VMs."""

import json
import secrets

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.provisioning.apt import (
    DISABLE_UNATTENDED_UPGRADES_OP_NAME,
    build_disable_unattended_upgrades_command,
)
from saorsa_deploy.provisioning.volume import VOLUME_DEVICE_GLOB
from saorsa_deploy.resources import get_resources_dir

MONITORING_MOUNT_POINT = "/mnt/monitoring-storage"
INFLUXDB_DATA_DIR = f"{MONITORING_MOUNT_POINT}/influxdb"
INFLUXDB_ORG = "ant"
INFLUXDB_BUCKET = "testnet-metrics"
INFLUXDB_ADMIN_USER = "admin"

GRAFANA_DATASOURCE_PATH = "/etc/grafana/provisioning/datasources/influxdb.yml"
GRAFANA_DASHBOARD_PROVIDER_PATH = "/etc/grafana/provisioning/dashboards/ant.yml"
GRAFANA_DASHBOARD_DIR = "/var/lib/grafana/dashboards"
GRAFANA_DASHBOARD_PATH = f"{GRAFANA_DASHBOARD_DIR}/host-metrics.json"
GRAFANA_NODE_TABLE_PATH = f"{GRAFANA_DASHBOARD_DIR}/node-overview.json"


class MonitoringProvisioner:
    """Provisions InfluxDB 2.x and Grafana on a monitoring VM."""

    def __init__(
        self,
        host_ip: str,
        ssh_key_path: str,
        grafana_password: str,
        console: Console | None = None,
    ):
        self.host_ip = host_ip
        self.ssh_key_path = ssh_key_path
        self.grafana_password = grafana_password
        self.console = console or Console()
        self._admin_password = secrets.token_urlsafe(24)

    def execute(self) -> dict:
        """Install and configure InfluxDB + Grafana.

        Returns a dict with connection details:
            influxdb_url, influxdb_token, influxdb_org, influxdb_bucket, grafana_url
        """
        hosts_data = [
            (self.host_ip, {"ssh_user": "root", "ssh_key": self.ssh_key_path})
        ]
        inventory = Inventory((hosts_data, {}))
        state = State(inventory=inventory, config=Config())

        self.console.print(f"Connecting to {self.host_ip}...")
        connect_all(state)

        try:
            add_op(
                state,
                server.shell,
                name="Wait for cloud-init to finish",
                commands=["cloud-init status --wait || true"],
            )

            add_op(
                state,
                server.shell,
                name=DISABLE_UNATTENDED_UPGRADES_OP_NAME,
                commands=[build_disable_unattended_upgrades_command()],
            )

            self._add_mount_volume_op(state)
            self._add_install_influxdb_ops(state)
            self._add_setup_influxdb_ops(state)
            self._add_install_grafana_ops(state)
            self._add_configure_grafana_ops(state)
            self._add_start_services_ops(state)

            self.console.print("Running monitoring provisioning operations...")
            run_ops(state)
        finally:
            disconnect_all(state)

        failed = state.failed_hosts
        if failed:
            for host in failed:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError("Monitoring provisioning failed")

        # Retrieve the API token from the remote host
        connection_details = self._retrieve_connection_details()
        return connection_details

    def _add_mount_volume_op(self, state: State):
        mount_cmd = (
            f"if mountpoint -q {MONITORING_MOUNT_POINT}; then "
            f"echo 'MONITORING_VOLUME:ALREADY_MOUNTED'; "
            f"else "
            f"DEVICE=$(ls {VOLUME_DEVICE_GLOB} 2>/dev/null | head -n1); "
            f'if [ -z "$DEVICE" ]; then '
            f"echo 'MONITORING_VOLUME:NO_DEVICE'; exit 1; "
            f"fi; "
            f"mkdir -p {MONITORING_MOUNT_POINT} && "
            f"mount -o defaults,nofail,discard $DEVICE {MONITORING_MOUNT_POINT} && "
            f"grep -q '{MONITORING_MOUNT_POINT}' /etc/fstab || "
            f'echo "$DEVICE {MONITORING_MOUNT_POINT} ext4 defaults,nofail,discard 0 2"'
            f" >> /etc/fstab && "
            f"echo 'MONITORING_VOLUME:MOUNTED'; "
            f"fi"
        )
        add_op(
            state,
            server.shell,
            name="Mount monitoring storage volume",
            commands=[mount_cmd],
        )

    def _add_install_influxdb_ops(self, state: State):
        add_op(
            state,
            server.shell,
            name="Install InfluxDB 2.x",
            commands=[
                "dpkg -s influxdb2 >/dev/null 2>&1 && echo 'INFLUXDB:SKIP' || "
                "(wget -q https://dl.influxdata.com/influxdb/releases/"
                "influxdb2_2.7.10-1_amd64.deb -O /tmp/influxdb2.deb && "
                "dpkg -i /tmp/influxdb2.deb && "
                "rm -f /tmp/influxdb2.deb && "
                "echo 'INFLUXDB:INSTALLED')",
            ],
        )

        add_op(
            state,
            server.shell,
            name="Configure InfluxDB storage on volume",
            commands=[
                f"mkdir -p {INFLUXDB_DATA_DIR}",
                f"chown influxdb:influxdb {INFLUXDB_DATA_DIR}",
                "mkdir -p /etc/influxdb2",
                f'grep -q "bolt-path" /etc/influxdb/config.toml 2>/dev/null && '
                f"echo 'INFLUXDB_CONFIG:SKIP' || "
                f'cat > /etc/influxdb/config.toml << \'CONF_EOF\'\n'
                f'bolt-path = "{INFLUXDB_DATA_DIR}/influxd.bolt"\n'
                f'engine-path = "{INFLUXDB_DATA_DIR}/engine"\n'
                f"CONF_EOF",
            ],
        )

    def _add_setup_influxdb_ops(self, state: State):
        add_op(
            state,
            server.shell,
            name="Start InfluxDB for initial setup",
            commands=[
                "systemctl enable influxdb",
                "systemctl start influxdb",
                "sleep 3",
            ],
        )

        # Deploy a Python setup script to the remote host and execute it.
        # This avoids shell quoting issues with the InfluxDB HTTP API calls.
        setup_script = self._build_influxdb_setup_py()
        add_op(
            state,
            server.shell,
            name="Write InfluxDB setup script",
            commands=[
                f"cat > /tmp/influxdb_setup.py << 'SETUP_EOF'\n{setup_script}SETUP_EOF",
            ],
        )

        add_op(
            state,
            server.shell,
            name="Run InfluxDB initial setup via API",
            commands=[
                "python3 /tmp/influxdb_setup.py && rm -f /tmp/influxdb_setup.py",
            ],
        )

    def _add_install_grafana_ops(self, state: State):
        add_op(
            state,
            server.shell,
            name="Install Grafana OSS",
            commands=[
                "dpkg -s grafana >/dev/null 2>&1 && echo 'GRAFANA:SKIP' || "
                "(apt-get install -y adduser libfontconfig1 musl && "
                "wget -q https://dl.grafana.com/oss/release/"
                "grafana_11.4.0_amd64.deb -O /tmp/grafana.deb && "
                "dpkg -i /tmp/grafana.deb && "
                "rm -f /tmp/grafana.deb && "
                "echo 'GRAFANA:INSTALLED')",
            ],
        )

    def _add_configure_grafana_ops(self, state: State):
        # Datasource config is written via shell script that reads the token
        # at runtime, since it's generated in a prior step
        add_op(
            state,
            server.shell,
            name="Write Grafana datasource provisioning",
            commands=[
                "INFLUX_TOKEN=$(cat /etc/influxdb2/telegraf-token) && "
                f"cat > {GRAFANA_DATASOURCE_PATH} << DS_EOF\n"
                f"{self._build_grafana_datasource_yaml()}DS_EOF",
            ],
        )

        dashboard_provider_yaml = self._build_grafana_dashboard_provider_yaml()
        add_op(
            state,
            server.shell,
            name="Write Grafana dashboard provider",
            commands=[
                f"cat > {GRAFANA_DASHBOARD_PROVIDER_PATH}"
                f" << 'DP_EOF'\n{dashboard_provider_yaml}DP_EOF",
            ],
        )

        dashboard_json = self._build_host_metrics_dashboard()
        add_op(
            state,
            server.shell,
            name="Write host metrics dashboard",
            commands=[
                f"mkdir -p {GRAFANA_DASHBOARD_DIR}",
                f"cat > {GRAFANA_DASHBOARD_PATH} << 'DASH_EOF'\n{dashboard_json}\nDASH_EOF",
            ],
        )

        node_table_json = self._build_node_overview_dashboard()
        add_op(
            state,
            server.shell,
            name="Write node overview dashboard",
            commands=[
                f"cat > {GRAFANA_NODE_TABLE_PATH}"
                f" << 'TABLE_EOF'\n{node_table_json}\nTABLE_EOF",
            ],
        )

    def _add_start_services_ops(self, state: State):
        add_op(
            state,
            server.shell,
            name="Enable and start services",
            commands=[
                "systemctl daemon-reload",
                "systemctl enable grafana-server",
                "systemctl restart grafana-server",
                "systemctl restart influxdb",
            ],
        )

        add_op(
            state,
            server.shell,
            name="Set Grafana admin password",
            commands=[
                "for i in 1 2 3 4 5; do "
                "curl -sf http://admin:admin@localhost:3000/api/org >/dev/null 2>&1 && break; "
                "sleep 3; done && "
                "curl -s -X PUT http://admin:admin@localhost:3000/api/user/password "
                "-H 'Content-Type: application/json' "
                f"-d '{{\"oldPassword\":\"admin\",\"newPassword\":\"{self.grafana_password}\"}}'"
                " || echo 'GRAFANA_PASSWORD:ALREADY_CHANGED'",
            ],
        )

        add_op(
            state,
            server.shell,
            name="Verify services are running",
            commands=[
                "systemctl is-active --quiet influxdb "
                "|| (echo 'VERIFY:INFLUXDB_FAILED'; "
                "journalctl -u influxdb --no-pager -n 20; exit 1)",
                "systemctl is-active --quiet grafana-server "
                "|| (echo 'VERIFY:GRAFANA_FAILED'; "
                "journalctl -u grafana-server --no-pager -n 20; exit 1)",
            ],
        )

    def _retrieve_connection_details(self) -> dict:
        """SSH back in to read the generated Telegraf token."""
        import subprocess

        result = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-i",
                self.ssh_key_path,
                f"root@{self.host_ip}",
                "cat /etc/influxdb2/telegraf-token",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to retrieve Telegraf token: {result.stderr}"
            )

        token = result.stdout.strip()
        return {
            "influxdb_url": f"http://{self.host_ip}:8086",
            "influxdb_token": token,
            "influxdb_org": INFLUXDB_ORG,
            "influxdb_bucket": INFLUXDB_BUCKET,
            "grafana_url": f"http://{self.host_ip}:3000",
            "grafana_password": self.grafana_password,
        }

    def _read_resource(self, filename: str) -> str:
        """Read a file from the monitoring resources directory."""
        path = get_resources_dir() / "monitoring" / filename
        return path.read_text()

    def _build_influxdb_setup_py(self) -> str:
        """Read the InfluxDB setup script and substitute placeholders."""
        template = self._read_resource("influxdb_setup.py")
        return (
            template.replace("{{INFLUXDB_ADMIN_USER}}", INFLUXDB_ADMIN_USER)
            .replace("{{INFLUXDB_ADMIN_PASSWORD}}", self._admin_password)
            .replace("{{INFLUXDB_ORG}}", INFLUXDB_ORG)
            .replace("{{INFLUXDB_BUCKET}}", INFLUXDB_BUCKET)
        )

    def _build_grafana_datasource_yaml(self) -> str:
        return self._read_resource("grafana-datasource.yml")

    def _build_grafana_dashboard_provider_yaml(self) -> str:
        template = self._read_resource("grafana-dashboard-provider.yml")
        return template.replace("{{GRAFANA_DASHBOARD_DIR}}", GRAFANA_DASHBOARD_DIR)

    def _build_host_metrics_dashboard(self) -> str:
        panels = []
        y = 0

        def row(title):
            nonlocal y, x
            if x > 0:
                y += 8
                x = 0
            panels.append({
                "title": title, "type": "row", "collapsed": False,
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": [],
            })
            y += 1

        def add(panel_fn, w=24, h=8, **kwargs):
            nonlocal y, x
            if x + w > 24:
                x = 0
                y += h
            panels.append(panel_fn(
                grid_pos={"h": h, "w": w, "x": x, "y": y}, **kwargs,
            ))
            x += w
            if x >= 24:
                x = 0
                y += h

        x = 0

        add(self._panel_cpu_usage)
        add(self._panel_memory_usage)
        add(self._panel_disk_usage, title="System Disk Usage", mount_path="/")
        add(self._panel_disk_usage, title="Attached Volume Usage",
            mount_path="/mnt/ant-node-storage")
        add(self._panel_network_inbound)
        add(self._panel_network_outbound)

        dashboard = {
            "annotations": {"list": []},
            "editable": True,
            "fiscalYearStartMonth": 0,
            "graphTooltip": 1,
            "links": [],
            "panels": panels,
            "schemaVersion": 39,
            "tags": ["ant", "testnet", "host-metrics"],
            "templating": {
                "list": [
                    {
                        "current": {
                            "selected": True,
                            "text": "All",
                            "value": "$__all",
                        },
                        "datasource": {"type": "influxdb", "uid": "influxdb"},
                        "definition": (
                            'import "influxdata/influxdb/schema"\n'
                            'schema.tagValues(\n'
                            '  bucket: "testnet-metrics",\n'
                            '  tag: "host"\n'
                            ")"
                        ),
                        "allValue": ".*",
                        "includeAll": True,
                        "multi": True,
                        "name": "host",
                        "label": "Host",
                        "query": (
                            'import "influxdata/influxdb/schema"\n'
                            'schema.tagValues(\n'
                            '  bucket: "testnet-metrics",\n'
                            '  tag: "host"\n'
                            ")"
                        ),
                        "refresh": 2,
                        "regex": "",
                        "type": "query",
                    },
                    {
                        "current": {
                            "selected": True,
                            "text": "All",
                            "value": "$__all",
                        },
                        "datasource": {"type": "influxdb", "uid": "influxdb"},
                        "definition": (
                            'import "influxdata/influxdb/schema"\n'
                            'schema.tagValues(\n'
                            '  bucket: "testnet-metrics",\n'
                            '  tag: "host_role"\n'
                            ")"
                        ),
                        "allValue": ".*",
                        "includeAll": True,
                        "multi": True,
                        "name": "host_role",
                        "label": "Role",
                        "query": (
                            'import "influxdata/influxdb/schema"\n'
                            'schema.tagValues(\n'
                            '  bucket: "testnet-metrics",\n'
                            '  tag: "host_role"\n'
                            ")"
                        ),
                        "refresh": 2,
                        "regex": "",
                        "type": "query",
                    },
                ]
            },
            "time": {"from": "now-1h", "to": "now"},
            "timepicker": {},
            "timezone": "utc",
            "title": "Host Metrics",
            "uid": "ant-host-metrics",
        }
        return json.dumps(dashboard, indent=2)

    def _build_node_overview_dashboard(self) -> str:
        """Build a table dashboard showing per-node detail."""
        # Common filters for template variables
        filters = (
            '  |> filter(fn: (r) => r["host"] =~ /^$host$/)\n'
            '  |> filter(fn: (r) => r["host_role"]'
            ' =~ /^$host_role$/)\n'
            '  |> filter(fn: (r) => r["node_service"]'
            ' =~ /^$node_service$/)\n'
        )

        # Table fields: always-present ones first, then optional ones.
        # pivot() silently drops columns for fields with no data, so
        # the table gracefully degrades when event-driven metrics
        # haven't fired yet.
        table_fields = [
            "p2p_uptime_seconds",
            "p2p_runtime_threads",
            "p2p_connected_peers",
            "p2p_transport_active_connections",
            "p2p_routing_table_size",
            "p2p_routing_buckets_filled",
        ]
        field_filter = " or\n       ".join(
            f'r["_field"] == "{f}"' for f in table_fields
        )
        # Only rename columns guaranteed to exist. Optional columns
        # (connected_peers, routing, etc.) keep their p2p_ names and
        # get human-readable names via Grafana field overrides.
        rename_map = {
            "p2p_uptime_seconds": "Uptime (s)",
            "p2p_runtime_threads": "Threads",
        }
        rename_lines = ",\n       ".join(
            f'{k}: "{v}"' for k, v in rename_map.items()
        )

        table_query = (
            'import "join"\n\n'
            'metrics = from(bucket: "testnet-metrics")\n'
            "  |> range(start: v.timeRangeStart,"
            " stop: v.timeRangeStop)\n"
            '  |> filter(fn: (r) => r["_measurement"]'
            ' == "prometheus")\n'
            f"  |> filter(fn: (r) =>\n       {field_filter})\n"
            + filters
            + "  |> last()\n"
            "  |> group()\n"
            '  |> pivot(rowKey: ["host", "node_service"],'
            ' columnKey: ["_field"], valueColumn: "_value")\n\n'
            'versions = from(bucket: "testnet-metrics")\n'
            "  |> range(start: v.timeRangeStart,"
            " stop: v.timeRangeStop)\n"
            '  |> filter(fn: (r) => r["_measurement"]'
            ' == "prometheus"'
            ' and r["_field"] == "p2p_node_info")\n'
            + filters
            + "  |> last()\n"
            "  |> group()\n"
            '  |> keep(columns: ["host", "node_service",'
            ' "version"])\n\n'
            "join.left(\n"
            "  left: metrics,\n"
            "  right: versions,\n"
            "  on: (l, r) => l.host == r.host"
            " and l.node_service == r.node_service,\n"
            '  as: (l, r) => ({l with version: r.version}),\n'
            ")\n"
            "  |> rename(columns: {\n"
            '       host: "Host",\n'
            '       node_service: "Service",\n'
            '       version: "Version",\n'
            f"       {rename_lines}\n"
            "  })\n"
            '  |> sort(columns: ["Host", "Service"])\n'
            '  |> yield(name: "table")'
        )

        dashboard = {
            "annotations": {"list": []},
            "editable": True,
            "graphTooltip": 0,
            "links": [],
            "panels": [
                {
                    "title": "Node Overview",
                    "type": "table",
                    "datasource": {
                        "type": "influxdb",
                        "uid": "influxdb",
                    },
                    "fieldConfig": {
                        "defaults": {},
                        "overrides": [
                            self._table_override(
                                "Uptime (s)", unit="s"),
                            self._table_override(
                                "p2p_connected_peers",
                                display="Connected Peers"),
                            self._table_override(
                                "p2p_transport_active_connections",
                                display="Active Connections"),
                            self._table_override(
                                "p2p_routing_table_size",
                                display="Routing Table Size"),
                            self._table_override(
                                "p2p_routing_buckets_filled",
                                display="Routing Buckets"),
                        ],
                    },
                    "gridPos": {
                        "h": 24, "w": 24, "x": 0, "y": 0,
                    },
                    "options": {
                        "showHeader": True,
                        "sortBy": [
                            {"displayName": "Host", "desc": False},
                        ],
                    },
                    "targets": [
                        {"query": table_query, "refId": "A"},
                    ],
                },
            ],
            "schemaVersion": 39,
            "tags": ["ant", "testnet", "node-overview"],
            "templating": {
                "list": [
                    {
                        "current": {
                            "selected": True,
                            "text": "All",
                            "value": "$__all",
                        },
                        "datasource": {
                            "type": "influxdb",
                            "uid": "influxdb",
                        },
                        "definition": (
                            'import "influxdata/influxdb/schema"\n'
                            'schema.tagValues(\n'
                            '  bucket: "testnet-metrics",\n'
                            '  tag: "host"\n'
                            ")"
                        ),
                        "allValue": ".*",
                        "includeAll": True,
                        "multi": True,
                        "name": "host",
                        "label": "Host",
                        "query": (
                            'import "influxdata/influxdb/schema"\n'
                            'schema.tagValues(\n'
                            '  bucket: "testnet-metrics",\n'
                            '  tag: "host"\n'
                            ")"
                        ),
                        "refresh": 2,
                        "regex": "",
                        "type": "query",
                    },
                    {
                        "current": {
                            "selected": True,
                            "text": "All",
                            "value": "$__all",
                        },
                        "datasource": {
                            "type": "influxdb",
                            "uid": "influxdb",
                        },
                        "definition": (
                            'import "influxdata/influxdb/schema"\n'
                            'schema.tagValues(\n'
                            '  bucket: "testnet-metrics",\n'
                            '  tag: "host_role"\n'
                            ")"
                        ),
                        "allValue": ".*",
                        "includeAll": True,
                        "multi": True,
                        "name": "host_role",
                        "label": "Role",
                        "query": (
                            'import "influxdata/influxdb/schema"\n'
                            'schema.tagValues(\n'
                            '  bucket: "testnet-metrics",\n'
                            '  tag: "host_role"\n'
                            ")"
                        ),
                        "refresh": 2,
                        "regex": "",
                        "type": "query",
                    },
                    {
                        "current": {
                            "selected": True,
                            "text": "All",
                            "value": "$__all",
                        },
                        "datasource": {
                            "type": "influxdb",
                            "uid": "influxdb",
                        },
                        "definition": (
                            'import "influxdata/influxdb/schema"\n'
                            'schema.tagValues(\n'
                            '  bucket: "testnet-metrics",\n'
                            '  tag: "node_service"\n'
                            ")"
                        ),
                        "allValue": ".*",
                        "includeAll": True,
                        "multi": True,
                        "name": "node_service",
                        "label": "Service",
                        "query": (
                            'import "influxdata/influxdb/schema"\n'
                            'schema.tagValues(\n'
                            '  bucket: "testnet-metrics",\n'
                            '  tag: "node_service"\n'
                            ")"
                        ),
                        "refresh": 2,
                        "regex": "",
                        "type": "query",
                    },
                ],
            },
            "time": {"from": "now-5m", "to": "now"},
            "timepicker": {},
            "timezone": "utc",
            "title": "Node Overview",
            "uid": "ant-node-overview",
        }
        return json.dumps(dashboard, indent=2)

    def _flux_query(
        self,
        measurement: str,
        fields: list[str],
        fn: str = "mean",
        extra_filter: str | None = None,
    ) -> str:
        field_filter = " or ".join(f'r["_field"] == "{f}"' for f in fields)
        query = (
            f'from(bucket: "testnet-metrics")\n'
            f"  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
            f'  |> filter(fn: (r) => r["_measurement"] == "{measurement}")\n'
            f"  |> filter(fn: (r) => {field_filter})\n"
            f'  |> filter(fn: (r) => r["host"] =~ /^${{host}}$/)\n'
        )
        if extra_filter:
            query += f"  |> filter(fn: (r) => {extra_filter})\n"
        query += (
            f"  |> aggregateWindow(every: v.windowPeriod, fn: {fn}, createEmpty: false)\n"
            '  |> keep(columns: ["_time", "_value", "host"])\n'
            f'  |> yield(name: "{fn}")'
        )
        return query

    def _panel_cpu_usage(self, grid_pos: dict) -> dict:
        cpu_query = (
            'from(bucket: "testnet-metrics")\n'
            "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
            '  |> filter(fn: (r) => r["_measurement"] == "cpu")\n'
            '  |> filter(fn: (r) => r["_field"] == "usage_idle")\n'
            '  |> filter(fn: (r) => r["cpu"] == "cpu-total")\n'
            '  |> filter(fn: (r) => r["host"] =~ /^$host$/)\n'
            "  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)\n"
            "  |> map(fn: (r) => ({r with _value: 100.0 - r._value, "
            '_field: "usage"}))\n'
            '  |> keep(columns: ["_time", "_value", "host"])\n'
            '  |> yield(name: "mean")'
        )
        return {
            "title": "CPU Usage",
            "type": "timeseries",
            "datasource": {"type": "influxdb", "uid": "influxdb"},
            "fieldConfig": {
                "defaults": {
                    "unit": "percent",
                    "min": 0,
                    "max": 100,
                },
                "overrides": [],
            },
            "gridPos": grid_pos,
            "options": {
                "legend": {"displayMode": "list", "placement": "right"},
            },
            "targets": [
                {
                    "query": cpu_query,
                    "refId": "A",
                }
            ],
        }

    def _panel_memory_usage(self, grid_pos: dict) -> dict:
        return {
            "title": "Memory Usage",
            "type": "timeseries",
            "datasource": {"type": "influxdb", "uid": "influxdb"},
            "fieldConfig": {
                "defaults": {"unit": "bytes"},
                "overrides": [],
            },
            "gridPos": grid_pos,
            "options": {
                "legend": {"displayMode": "list", "placement": "right"},
            },
            "targets": [
                {
                    "query": self._flux_query("mem", ["used"]),
                    "refId": "A",
                }
            ],
        }

    def _panel_disk_usage(self, title: str, mount_path: str, grid_pos: dict) -> dict:
        return {
            "title": title,
            "type": "timeseries",
            "datasource": {"type": "influxdb", "uid": "influxdb"},
            "fieldConfig": {
                "defaults": {"unit": "bytes"},
                "overrides": [],
            },
            "gridPos": grid_pos,
            "options": {
                "legend": {"displayMode": "list", "placement": "right"},
            },
            "targets": [
                {
                    "query": self._flux_query(
                        "disk",
                        ["used"],
                        extra_filter=f'r["path"] == "{mount_path}"',
                    ),
                    "refId": "A",
                }
            ],
        }

    def _panel_network_inbound(self, grid_pos: dict) -> dict:
        return self._panel_network("Network Inbound", "bytes_recv", grid_pos)

    def _panel_network_outbound(self, grid_pos: dict) -> dict:
        return self._panel_network("Network Outbound", "bytes_sent", grid_pos)

    def _p2p_query(self, fields: list[str]) -> str:
        """Base Flux query for p2p metrics from the prometheus measurement.

        Filters by host, host_role, and node_service template variables,
        then regroups by host + _field so values are averaged across nodes.
        """
        field_filter = " or ".join(f'r["_field"] == "{f}"' for f in fields)
        return (
            'from(bucket: "testnet-metrics")\n'
            "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
            '  |> filter(fn: (r) => r["_measurement"] == "prometheus")\n'
            f"  |> filter(fn: (r) => {field_filter})\n"
            '  |> filter(fn: (r) => r["host"] =~ /^$host$/)\n'
            '  |> filter(fn: (r) => r["host_role"] =~ /^$host_role$/)\n'
            '  |> filter(fn: (r) => r["node_service"] =~ /^$node_service$/)\n'
            '  |> group(columns: ["host", "_field"])\n'
        )

    def _p2p_panel(
        self, title: str, fields: list[str], unit: str, grid_pos: dict,
    ) -> dict:
        """Timeseries panel — for metrics where trends over time matter
        (latency, duration, scores that evolve)."""
        query = (
            self._p2p_query(fields)
            + "  |> aggregateWindow(every: v.windowPeriod, fn: mean, "
            "createEmpty: false)\n"
            '  |> keep(columns: ["_time", "_value", "host"])\n'
            '  |> yield(name: "mean")'
        )
        return {
            "title": title,
            "type": "timeseries",
            "datasource": {"type": "influxdb", "uid": "influxdb"},
            "fieldConfig": {
                "defaults": {"unit": unit},
                "overrides": [],
            },
            "gridPos": grid_pos,
            "options": {
                "legend": {"displayMode": "list", "placement": "right"},
            },
            "targets": [{"query": query, "refId": "A"}],
        }

    def _p2p_stat(
        self, title: str, fields: list[str], unit: str, grid_pos: dict,
    ) -> dict:
        """Stat panel — shows current value per host as big numbers.
        For point-in-time gauges (peers, table size, threads, uptime)."""
        query = (
            self._p2p_query(fields)
            + "  |> mean()\n"
            '  |> keep(columns: ["_value", "host"])\n'
            '  |> yield(name: "mean")'
        )
        return {
            "title": title,
            "type": "stat",
            "datasource": {"type": "influxdb", "uid": "influxdb"},
            "fieldConfig": {
                "defaults": {
                    "unit": unit,
                    "color": {"mode": "palette-classic"},
                },
                "overrides": [],
            },
            "gridPos": grid_pos,
            "options": {
                "reduceOptions": {
                    "values": False,
                    "calcs": ["lastNotNull"],
                    "fields": "",
                },
                "textMode": "auto",
                "colorMode": "value",
                "graphMode": "area",
                "justifyMode": "auto",
            },
            "targets": [{"query": query, "refId": "A"}],
        }

    def _p2p_gauge(
        self, title: str, fields: list[str], unit: str, grid_pos: dict,
        min_val: float = 0, max_val: float = 1,
    ) -> dict:
        """Gauge panel — semicircle showing current value against a range.
        For scores and ratios (success rate, health, security scores)."""
        query = (
            self._p2p_query(fields)
            + "  |> mean()\n"
            '  |> keep(columns: ["_value", "host"])\n'
            '  |> yield(name: "mean")'
        )
        return {
            "title": title,
            "type": "gauge",
            "datasource": {"type": "influxdb", "uid": "influxdb"},
            "fieldConfig": {
                "defaults": {
                    "unit": unit,
                    "min": min_val,
                    "max": max_val,
                    "thresholds": {
                        "mode": "absolute",
                        "steps": [
                            {"color": "green", "value": None},
                            {"color": "yellow", "value": max_val * 0.7},
                            {"color": "red", "value": max_val * 0.9},
                        ],
                    },
                },
                "overrides": [],
            },
            "gridPos": grid_pos,
            "options": {
                "reduceOptions": {
                    "values": False,
                    "calcs": ["lastNotNull"],
                    "fields": "",
                },
            },
            "targets": [{"query": query, "refId": "A"}],
        }

    def _p2p_rate(
        self, title: str, fields: list[str], unit: str, grid_pos: dict,
    ) -> dict:
        """Timeseries panel showing rate of change — for counters
        (operations, events, errors). Uses derivative to show ops/sec."""
        query = (
            self._p2p_query(fields)
            + "  |> aggregateWindow(every: v.windowPeriod, fn: mean, "
            "createEmpty: false)\n"
            "  |> derivative(unit: 1s, nonNegative: true)\n"
            '  |> keep(columns: ["_time", "_value", "host"])\n'
            '  |> yield(name: "rate")'
        )
        return {
            "title": title,
            "type": "timeseries",
            "datasource": {"type": "influxdb", "uid": "influxdb"},
            "fieldConfig": {
                "defaults": {"unit": unit},
                "overrides": [],
            },
            "gridPos": grid_pos,
            "options": {
                "legend": {"displayMode": "list", "placement": "right"},
            },
            "targets": [{"query": query, "refId": "A"}],
        }

    def _p2p_testnet_query(self, fields: list[str]) -> str:
        """Base Flux query that aggregates across the entire testnet.

        Drops host grouping so all nodes are averaged together.
        Filters still apply (host_role, node_service).
        """
        field_filter = " or ".join(
            f'r["_field"] == "{f}"' for f in fields
        )
        return (
            'from(bucket: "testnet-metrics")\n'
            "  |> range(start: v.timeRangeStart,"
            " stop: v.timeRangeStop)\n"
            '  |> filter(fn: (r) => r["_measurement"]'
            ' == "prometheus")\n'
            f"  |> filter(fn: (r) => {field_filter})\n"
            '  |> filter(fn: (r) => r["host"] =~ /^$host$/)\n'
            '  |> filter(fn: (r) => r["host_role"]'
            ' =~ /^$host_role$/)\n'
            '  |> filter(fn: (r) => r["node_service"]'
            ' =~ /^$node_service$/)\n'
            '  |> group(columns: ["_field"])\n'
        )

    def _p2p_testnet_stat(
        self, title: str, fields: list[str], unit: str,
        grid_pos: dict, agg: str = "mean",
    ) -> dict:
        """Stat panel showing a single testnet-wide value."""
        query = (
            self._p2p_testnet_query(fields)
            + f"  |> {agg}()\n"
            '  |> keep(columns: ["_value", "_field"])\n'
            f'  |> yield(name: "{agg}")'
        )
        return {
            "title": title,
            "type": "stat",
            "datasource": {"type": "influxdb", "uid": "influxdb"},
            "fieldConfig": {
                "defaults": {
                    "unit": unit,
                    "color": {"mode": "palette-classic"},
                },
                "overrides": [],
            },
            "gridPos": grid_pos,
            "options": {
                "reduceOptions": {
                    "values": False,
                    "calcs": ["lastNotNull"],
                    "fields": "",
                },
                "textMode": "auto",
                "colorMode": "value",
                "graphMode": "none",
                "justifyMode": "auto",
            },
            "targets": [{"query": query, "refId": "A"}],
        }

    def _p2p_testnet_gauge(
        self, title: str, fields: list[str], unit: str,
        grid_pos: dict, min_val: float = 0, max_val: float = 1,
    ) -> dict:
        """Gauge panel showing a testnet-wide average."""
        query = (
            self._p2p_testnet_query(fields)
            + "  |> mean()\n"
            '  |> keep(columns: ["_value", "_field"])\n'
            '  |> yield(name: "mean")'
        )
        return {
            "title": title,
            "type": "gauge",
            "datasource": {"type": "influxdb", "uid": "influxdb"},
            "fieldConfig": {
                "defaults": {
                    "unit": unit,
                    "min": min_val,
                    "max": max_val,
                    "thresholds": {
                        "mode": "absolute",
                        "steps": [
                            {"color": "green", "value": None},
                            {"color": "yellow", "value": max_val * 0.7},
                            {"color": "red", "value": max_val * 0.9},
                        ],
                    },
                },
                "overrides": [],
            },
            "gridPos": grid_pos,
            "options": {
                "reduceOptions": {
                    "values": False,
                    "calcs": ["lastNotNull"],
                    "fields": "",
                },
            },
            "targets": [{"query": query, "refId": "A"}],
        }

    @staticmethod
    def _table_override(
        column: str, display: str | None = None, unit: str | None = None,
    ) -> dict:
        """Build a Grafana field override for the table panel."""
        props = []
        if display:
            props.append({"id": "displayName", "value": display})
        if unit:
            props.append({"id": "unit", "value": unit})
        return {
            "matcher": {"id": "byName", "options": column},
            "properties": props,
        }

    def _panel_network(self, title: str, field: str, grid_pos: dict) -> dict:
        return {
            "title": title,
            "type": "timeseries",
            "datasource": {"type": "influxdb", "uid": "influxdb"},
            "fieldConfig": {
                "defaults": {"unit": "Bps"},
                "overrides": [],
            },
            "gridPos": grid_pos,
            "options": {
                "legend": {"displayMode": "list", "placement": "right"},
            },
            "targets": [
                {
                    "query": (
                        'from(bucket: "testnet-metrics")\n'
                        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
                        '  |> filter(fn: (r) => r["_measurement"] == "net")\n'
                        f'  |> filter(fn: (r) => r["_field"] == "{field}")\n'
                        '  |> filter(fn: (r) => r["host"] =~ /^$host$/)\n'
                        "  |> derivative(unit: 1s, nonNegative: true)\n"
                        '  |> keep(columns: ["_time", "_value", "host"])\n'
                        '  |> yield(name: "rate")'
                    ),
                    "refId": "A",
                }
            ],
        }
