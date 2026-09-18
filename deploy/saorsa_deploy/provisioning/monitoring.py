"""InfluxDB 2.x + Grafana provisioning for monitoring VMs."""

import json
import secrets

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.provisioning import egress_dashboard
from saorsa_deploy.provisioning.apt import (
    DISABLE_UNATTENDED_UPGRADES_OP_NAME,
    build_disable_unattended_upgrades_command,
)
from saorsa_deploy.provisioning.volume import (
    build_mount_ordering_dropin_command,
    build_mount_volume_command,
    build_root_disk_dir_command,
)
from saorsa_deploy.resources import get_resources_dir

MONITORING_MOUNT_POINT = "/mnt/monitoring-storage"
INFLUXDB_DATA_DIR = f"{MONITORING_MOUNT_POINT}/influxdb"
INFLUXDB_ORG = "ant"
# Node-tier host metrics (cpu/mem/disk/net/procstat) plus the prometheus and
# application measurements (download/prod_upload/canary/...) all live here.
INFLUXDB_BUCKET = "testnet-metrics"
# Client-tier host metrics live in their own bucket so the Client Host
# Statistics dashboard is physically isolated from node data (and vice versa)
# rather than relying on a mutable host_role dropdown to keep them apart.
INFLUXDB_CLIENT_BUCKET = "client-metrics"
# WASM (browser-path) client file operations live in a bucket of their own.
#
# They are the SAME `upload`/`download` measurements the native clients emit, so
# in one bucket every dashboard panel and ad-hoc Flux query would silently
# average two different transports — measured at ~2.2x (upload) and ~4.2x
# (download) apart. Physically separating them is the same reasoning that put
# client host metrics in their own bucket rather than behind a mutable tag: a
# query cannot mix what it cannot reach. Note this is the *file operations*
# bucket; a WASM client VM's host metrics still go to INFLUXDB_CLIENT_BUCKET
# alongside every other client's, because a host metric is a host metric.
INFLUXDB_WASM_BUCKET = "wasm-client-metrics"
INFLUXDB_ADMIN_USER = "admin"

GRAFANA_DATASOURCE_PATH = "/etc/grafana/provisioning/datasources/influxdb.yml"
GRAFANA_DASHBOARD_PROVIDER_PATH = "/etc/grafana/provisioning/dashboards/ant.yml"
GRAFANA_DASHBOARD_DIR = "/var/lib/grafana/dashboards"
GRAFANA_DASHBOARD_PATH = f"{GRAFANA_DASHBOARD_DIR}/host-metrics.json"
GRAFANA_CLIENT_DASHBOARD_PATH = f"{GRAFANA_DASHBOARD_DIR}/client-host-metrics.json"
GRAFANA_NODE_TABLE_PATH = f"{GRAFANA_DASHBOARD_DIR}/node-overview.json"
GRAFANA_EGRESS_DASHBOARD_PATH = f"{GRAFANA_DASHBOARD_DIR}/egress-accounting.json"


class MonitoringProvisioner:
    """Provisions InfluxDB 2.x and Grafana on separate VMs.

    InfluxDB (and its data volume) live on ``influxdb_ip``; Grafana runs on its
    own ``grafana_ip`` and reaches InfluxDB over the public ``:8086`` endpoint.
    Splitting the two means Grafana's panel rendering no longer competes with
    InfluxDB ingest/query for CPU and RAM — the cause of the RSS panels failing
    to render when both shared one small VM. Pass the same IP for both to keep
    the legacy co-located layout.

    ``grafana_ip`` may be ``None`` to provision InfluxDB only (no Grafana VM,
    no dashboards) — the default since Grafana became opt-in.
    """

    def __init__(
        self,
        influxdb_ip: str,
        grafana_ip: str | None,
        ssh_key_path: str,
        grafana_password: str,
        retention_days: int = 0,
        console: Console | None = None,
        root_disk: bool = False,
    ):
        self.influxdb_ip = influxdb_ip
        self.grafana_ip = grafana_ip
        self.ssh_key_path = ssh_key_path
        self.grafana_password = grafana_password
        # 0 == infinite retention (the historical behaviour, kept for dev).
        self.retention_days = retention_days
        self.console = console or Console()
        # root_disk == True for providers with no attachable block storage
        # (contabo): InfluxDB data lives on the instance root disk, so we make
        # the storage directory instead of mounting a device.
        self.root_disk = root_disk
        self._admin_password = secrets.token_urlsafe(24)

    def execute(self) -> dict:
        """Install and configure InfluxDB + Grafana across the two VMs.

        Returns a dict with connection details:
            influxdb_url, influxdb_token, influxdb_org, influxdb_bucket,
            influxdb_client_bucket, grafana_url, grafana_password
        """
        # Phase 1: InfluxDB on its own VM. Provision, then read back the
        # Telegraf token so Grafana's datasource can be wired to it.
        self._run_session(
            self.influxdb_ip,
            "InfluxDB",
            [
                self._add_preamble_ops,
                self._add_mount_volume_op,
                self._add_install_influxdb_ops,
                # Must precede the first start below: the drop-in has to be in
                # place before influxd ever runs, not just before the restart.
                self._add_mount_ordering_op,
                self._add_setup_influxdb_ops,
                self._add_influxdb_start_ops,
            ],
        )
        token = self._retrieve_telegraf_token()

        # Phase 2: Grafana on its own VM, pointed at the InfluxDB VM. Skipped
        # entirely when Grafana is disabled (grafana_ip is None) — InfluxDB-only.
        if self.grafana_ip:
            influxdb_url = f"http://{self.influxdb_ip}:8086"
            self._run_session(
                self.grafana_ip,
                "Grafana",
                [
                    self._add_preamble_ops,
                    self._add_install_grafana_ops,
                    lambda state: self._add_configure_grafana_ops(
                        state, influxdb_url=influxdb_url, influx_token=token
                    ),
                    self._add_grafana_start_ops,
                ],
            )
        else:
            self.console.print(
                "[dim]Grafana disabled — provisioning InfluxDB only.[/dim]"
            )

        return self._connection_details(token)

    def _run_session(self, host_ip: str, label: str, op_adders: list) -> None:
        """Connect to a single host, add + run the given ops, then disconnect.

        ``op_adders`` is a list of callables each taking the pyinfra ``state``
        and registering operations via ``add_op``; they run in order.
        """
        hosts_data = [
            (host_ip, {"ssh_user": "root", "ssh_key": self.ssh_key_path})
        ]
        inventory = Inventory((hosts_data, {}))
        state = State(inventory=inventory, config=Config())

        self.console.print(f"Connecting to {label} VM {host_ip}...")
        connect_all(state)

        try:
            for adder in op_adders:
                adder(state)
            self.console.print(
                f"Running {label} provisioning operations..."
            )
            run_ops(state)
        finally:
            disconnect_all(state)

        if state.failed_hosts:
            for host in state.failed_hosts:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError(f"{label} provisioning failed")

    def _add_preamble_ops(self, state: State):
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

    def _add_mount_volume_op(self, state: State):
        # Providers with no attachable block storage (contabo) keep data on the
        # root disk: just make the storage directory. Everything else uses the
        # shared multi-provider mount: tries every provider's device glob
        # (DO / Hetzner / Vultr / OVH) and formats the volume if it has no
        # filesystem yet — OVH Cinder volumes arrive raw, unlike DO volumes
        # which Terraform pre-formats ext4.
        if self.root_disk:
            add_op(
                state,
                server.shell,
                name="Set up monitoring storage directory (root disk, no volume)",
                commands=[build_root_disk_dir_command(MONITORING_MOUNT_POINT)],
            )
            return
        add_op(
            state,
            server.shell,
            name="Mount monitoring storage volume",
            commands=[build_mount_volume_command(MONITORING_MOUNT_POINT)],
        )

    def _add_mount_ordering_op(self, state: State):
        # Root-disk providers (contabo) have no volume to wait for.
        if self.root_disk:
            return
        add_op(
            state,
            server.shell,
            name="Order InfluxDB after its data volume",
            commands=[
                build_mount_ordering_dropin_command("influxdb", MONITORING_MOUNT_POINT)
            ],
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

        # NB: the guard must check for OUR data dir, not just "bolt-path" —
        # the influxdb2 deb ships a default config.toml that already contains
        # `bolt-path = "/var/lib/influxdb/..."`, so a bare bolt-path grep
        # always matches and the volume-pointing config never gets written
        # (data then lands on the root disk and the volume sits empty). If a
        # previous run already started influxd against /var/lib/influxdb, the
        # existing bolt + engine are migrated onto the volume so the org,
        # buckets, and minted tokens survive.
        configure_cmd = (
            f'if grep -q "{INFLUXDB_DATA_DIR}" /etc/influxdb/config.toml '
            f"2>/dev/null; then "
            f"echo 'INFLUXDB_CONFIG:SKIP'; "
            f"else "
            f"systemctl stop influxdb 2>/dev/null || true; "
            f"mkdir -p {INFLUXDB_DATA_DIR}; "
            f"if [ -f /var/lib/influxdb/influxd.bolt ] && "
            f"[ ! -f {INFLUXDB_DATA_DIR}/influxd.bolt ]; then "
            f"mv /var/lib/influxdb/influxd.bolt {INFLUXDB_DATA_DIR}/ && "
            f"echo 'INFLUXDB_CONFIG:MIGRATED_BOLT'; "
            f"fi; "
            f"if [ -d /var/lib/influxdb/engine ] && "
            f"[ ! -d {INFLUXDB_DATA_DIR}/engine ]; then "
            f"mv /var/lib/influxdb/engine {INFLUXDB_DATA_DIR}/ && "
            f"echo 'INFLUXDB_CONFIG:MIGRATED_ENGINE'; "
            f"fi; "
            f"cat > /etc/influxdb/config.toml << 'CONF_EOF'\n"
            f'bolt-path = "{INFLUXDB_DATA_DIR}/influxd.bolt"\n'
            f'engine-path = "{INFLUXDB_DATA_DIR}/engine"\n'
            f"CONF_EOF\n"
            f"chown -R influxdb:influxdb {INFLUXDB_DATA_DIR}; "
            f"echo 'INFLUXDB_CONFIG:WRITTEN'; "
            f"fi"
        )
        add_op(
            state,
            server.shell,
            name="Configure InfluxDB storage on volume",
            commands=[
                "mkdir -p /etc/influxdb2",
                configure_cmd,
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

    def _add_configure_grafana_ops(
        self, state: State, *, influxdb_url: str, influx_token: str
    ):
        # Grafana runs on its own VM and cannot read the InfluxDB VM's local
        # token file, so the URL + token are substituted here and written with
        # a quoted heredoc (no shell expansion of the already-resolved values).
        datasource_yaml = self._build_grafana_datasource_yaml(
            influxdb_url=influxdb_url, influx_token=influx_token
        )
        add_op(
            state,
            server.shell,
            name="Write Grafana datasource provisioning",
            commands=[
                "mkdir -p /etc/grafana/provisioning/datasources",
                f"cat > {GRAFANA_DATASOURCE_PATH} << 'DS_EOF'\n"
                f"{datasource_yaml}DS_EOF",
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

        client_dashboard_json = self._build_client_host_metrics_dashboard()
        add_op(
            state,
            server.shell,
            name="Write client host metrics dashboard",
            commands=[
                f"cat > {GRAFANA_CLIENT_DASHBOARD_PATH}"
                f" << 'CLIENT_DASH_EOF'\n{client_dashboard_json}\nCLIENT_DASH_EOF",
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

        egress_json = self._build_egress_accounting_dashboard()
        add_op(
            state,
            server.shell,
            name="Write egress accounting dashboard",
            commands=[
                f"cat > {GRAFANA_EGRESS_DASHBOARD_PATH}"
                f" << 'EGRESS_EOF'\n{egress_json}\nEGRESS_EOF",
            ],
        )

    def _add_influxdb_start_ops(self, state: State):
        add_op(
            state,
            server.shell,
            name="Enable and start InfluxDB",
            commands=[
                "systemctl daemon-reload",
                "systemctl enable influxdb",
                "systemctl restart influxdb",
            ],
        )
        add_op(
            state,
            server.shell,
            name="Verify InfluxDB is running",
            commands=[
                "systemctl is-active --quiet influxdb "
                "|| (echo 'VERIFY:INFLUXDB_FAILED'; "
                "journalctl -u influxdb --no-pager -n 20; exit 1)",
            ],
        )

    def _add_grafana_start_ops(self, state: State):
        add_op(
            state,
            server.shell,
            name="Enable and start Grafana",
            commands=[
                "systemctl daemon-reload",
                "systemctl enable grafana-server",
                "systemctl restart grafana-server",
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
            name="Verify Grafana is running",
            commands=[
                "systemctl is-active --quiet grafana-server "
                "|| (echo 'VERIFY:GRAFANA_FAILED'; "
                "journalctl -u grafana-server --no-pager -n 20; exit 1)",
            ],
        )

    def _retrieve_telegraf_token(self) -> str:
        """SSH into the InfluxDB VM to read the generated Telegraf token."""
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
                f"root@{self.influxdb_ip}",
                "cat /etc/influxdb2/telegraf-token",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to retrieve Telegraf token: {result.stderr}"
            )
        return result.stdout.strip()

    def _connection_details(self, token: str) -> dict:
        # Keep grafana_* keys present (empty when disabled) so downstream
        # consumers can use plain dict access without KeyErrors.
        return {
            "influxdb_url": f"http://{self.influxdb_ip}:8086",
            "influxdb_token": token,
            "influxdb_org": INFLUXDB_ORG,
            "influxdb_bucket": INFLUXDB_BUCKET,
            "influxdb_client_bucket": INFLUXDB_CLIENT_BUCKET,
            "influxdb_wasm_bucket": INFLUXDB_WASM_BUCKET,
            "grafana_url": f"http://{self.grafana_ip}:3000" if self.grafana_ip else "",
            "grafana_password": self.grafana_password if self.grafana_ip else "",
        }

    def _read_resource(self, filename: str) -> str:
        """Read a file from the monitoring resources directory."""
        path = get_resources_dir() / "monitoring" / filename
        return path.read_text()

    def _build_influxdb_setup_py(self) -> str:
        """Read the InfluxDB setup script and substitute placeholders."""
        template = self._read_resource("influxdb_setup.py")
        retention_seconds = max(0, self.retention_days) * 86400
        return (
            template.replace("{{INFLUXDB_ADMIN_USER}}", INFLUXDB_ADMIN_USER)
            .replace("{{INFLUXDB_ADMIN_PASSWORD}}", self._admin_password)
            .replace("{{INFLUXDB_ORG}}", INFLUXDB_ORG)
            .replace("{{INFLUXDB_BUCKET}}", INFLUXDB_BUCKET)
            .replace("{{INFLUXDB_CLIENT_BUCKET}}", INFLUXDB_CLIENT_BUCKET)
            .replace("{{INFLUXDB_WASM_BUCKET}}", INFLUXDB_WASM_BUCKET)
            .replace("{{INFLUXDB_RETENTION_SECONDS}}", str(retention_seconds))
        )

    def _build_grafana_datasource_yaml(
        self, *, influxdb_url: str, influx_token: str
    ) -> str:
        template = self._read_resource("grafana-datasource.yml")
        return template.replace(
            "{{INFLUXDB_URL}}", influxdb_url
        ).replace("{{INFLUX_TOKEN}}", influx_token)

    def _build_grafana_dashboard_provider_yaml(self) -> str:
        template = self._read_resource("grafana-dashboard-provider.yml")
        return template.replace("{{GRAFANA_DASHBOARD_DIR}}", GRAFANA_DASHBOARD_DIR)

    def _build_host_metrics_dashboard(self) -> str:
        """Node-side host dashboard: all host panels plus the per-process
        (procstat) Node RSS / Node CPU panels, which only the ant-node VMs
        emit. Defaults to All roles so every host is visible."""
        return self._build_host_dashboard(
            title="Node Host Statistics",
            uid="ant-host-metrics",
            tag="host-metrics",
            bucket=INFLUXDB_BUCKET,
            panel_specs=[
                (self._panel_cpu_usage, {}),
                (self._panel_memory_usage, {}),
                (self._panel_node_rss, {}),
                (self._panel_node_cpu, {}),
                (self._panel_disk_usage,
                 {"title": "System Disk Usage", "mount_path": "/"}),
                (self._panel_disk_usage,
                 {"title": "Attached Volume Usage",
                  "mount_path": "/mnt/ant-node-storage"}),
                (self._panel_network_inbound, {}),
                (self._panel_network_outbound, {}),
            ],
        )

    def _build_client_host_metrics_dashboard(self) -> str:
        """Client-side host dashboard: the host-level CPU / Memory / Disk /
        Network panels only, scoped to host_role=CLIENT by default.

        The per-process Node RSS / Node CPU panels are omitted because client
        VMs run no ant-node services and therefore emit no procstat metrics;
        the node-only attached-volume mount (/mnt/ant-node-storage) is omitted
        for the same reason. This is the view for watching client resource
        usage during prod upload/download runs."""
        return self._build_host_dashboard(
            title="Client Host Statistics",
            uid="ant-client-host-metrics",
            tag="client-host-metrics",
            bucket=INFLUXDB_CLIENT_BUCKET,
            panel_specs=[
                (self._panel_cpu_usage, {}),
                (self._panel_memory_usage, {}),
                (self._panel_disk_usage,
                 {"title": "System Disk Usage", "mount_path": "/"}),
                (self._panel_network_inbound, {}),
                (self._panel_network_outbound, {}),
            ],
            default_host_role="CLIENT",
        )

    def _build_egress_accounting_dashboard(self) -> str:
        """Accounted vs actual node egress (V2-1225): the traffic-accounting
        summaries the ES forwarder also ships to Influx, reconciled against the
        host ``net`` counters. Panels live in ``egress_dashboard``; the Role /
        Host template variables are the host-statistics ones."""
        return self._build_host_dashboard(
            title=egress_dashboard.DASHBOARD_TITLE,
            uid=egress_dashboard.DASHBOARD_UID,
            tag=egress_dashboard.DASHBOARD_TAG,
            bucket=INFLUXDB_BUCKET,
            panel_specs=list(egress_dashboard.EGRESS_ACCOUNTING_PANEL_SPECS),
        )

    def _build_host_dashboard(
        self,
        *,
        title: str,
        uid: str,
        tag: str,
        bucket: str,
        panel_specs: list[tuple],
        default_host_role: str | None = None,
    ) -> str:
        """Build a host-statistics dashboard from a list of panel specs.

        Each panel spec is a (panel_fn, kwargs) tuple; panels are laid out
        full-width, top to bottom. Every panel and template-variable query is
        scoped to ``bucket`` so the node and client host dashboards read from
        physically separate InfluxDB buckets. When ``default_host_role`` is
        given, the Role template variable opens scoped to that single role
        instead of "All" (the panels themselves filter on the $host /
        $host_role template variables regardless)."""
        panels = []
        y = 0
        x = 0

        def add(panel_fn, w=24, h=8, **kwargs):
            nonlocal y, x
            if x + w > 24:
                x = 0
                y += h
            panels.append(panel_fn(
                grid_pos={"h": h, "w": w, "x": x, "y": y},
                bucket=bucket, **kwargs,
            ))
            x += w
            if x >= 24:
                x = 0
                y += h

        for panel_fn, kwargs in panel_specs:
            add(panel_fn, **kwargs)

        if default_host_role is not None:
            host_role_current = {
                "selected": True,
                "text": default_host_role,
                "value": default_host_role,
            }
        else:
            host_role_current = {
                "selected": True,
                "text": "All",
                "value": "$__all",
            }

        dashboard = {
            "annotations": {"list": []},
            "editable": True,
            "fiscalYearStartMonth": 0,
            "graphTooltip": 1,
            "links": [],
            "panels": panels,
            "schemaVersion": 39,
            "tags": ["ant", "testnet", tag],
            "templating": {
                "list": [
                    {
                        "current": host_role_current,
                        "datasource": {"type": "influxdb", "uid": "influxdb"},
                        "definition": (
                            'import "influxdata/influxdb/schema"\n'
                            'schema.tagValues(\n'
                            f'  bucket: "{bucket}",\n'
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
                            f'  bucket: "{bucket}",\n'
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
                        "datasource": {"type": "influxdb", "uid": "influxdb"},
                        # Pinned to `cpu` (every telegraf-metrics host emits it)
                        # so the traffic-accounting `tail` points -- whose host
                        # tag is UPPER-cased -- do not add a duplicate of every
                        # host to the dropdown.
                        "definition": (
                            f'from(bucket: "{bucket}")\n'
                            '  |> range(start: -1h)\n'
                            '  |> filter(fn: (r) => r["_measurement"] == "cpu")\n'
                            '  |> filter(fn: (r) => '
                            'r["host_role"] =~ /^${host_role}$/)\n'
                            '  |> keep(columns: ["host"])\n'
                            '  |> group()\n'
                            '  |> distinct(column: "host")'
                        ),
                        "allValue": ".*",
                        "includeAll": True,
                        "multi": True,
                        "name": "host",
                        "label": "Host",
                        "query": (
                            f'from(bucket: "{bucket}")\n'
                            '  |> range(start: -1h)\n'
                            '  |> filter(fn: (r) => r["_measurement"] == "cpu")\n'
                            '  |> filter(fn: (r) => '
                            'r["host_role"] =~ /^${host_role}$/)\n'
                            '  |> keep(columns: ["host"])\n'
                            '  |> group()\n'
                            '  |> distinct(column: "host")'
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
            "title": title,
            "uid": uid,
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
        *,
        bucket: str,
    ) -> str:
        field_filter = " or ".join(f'r["_field"] == "{f}"' for f in fields)
        query = (
            f'from(bucket: "{bucket}")\n'
            f"  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
            f'  |> filter(fn: (r) => r["_measurement"] == "{measurement}")\n'
            f"  |> filter(fn: (r) => {field_filter})\n"
            f'  |> filter(fn: (r) => r["host"] =~ /^${{host}}$/)\n'
            f'  |> filter(fn: (r) => r["host_role"] =~ /^${{host_role}}$/)\n'
        )
        if extra_filter:
            query += f"  |> filter(fn: (r) => {extra_filter})\n"
        query += (
            f"  |> aggregateWindow(every: v.windowPeriod, fn: {fn}, createEmpty: false)\n"
            '  |> keep(columns: ["_time", "_value", "host"])\n'
            f'  |> yield(name: "{fn}")'
        )
        return query

    def _panel_cpu_usage(self, grid_pos: dict, *, bucket: str) -> dict:
        cpu_query = (
            f'from(bucket: "{bucket}")\n'
            "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
            '  |> filter(fn: (r) => r["_measurement"] == "cpu")\n'
            '  |> filter(fn: (r) => r["_field"] == "usage_idle")\n'
            '  |> filter(fn: (r) => r["cpu"] == "cpu-total")\n'
            '  |> filter(fn: (r) => r["host"] =~ /^$host$/)\n'
            '  |> filter(fn: (r) => r["host_role"] =~ /^$host_role$/)\n'
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

    def _panel_memory_usage(self, grid_pos: dict, *, bucket: str) -> dict:
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
                    "query": self._flux_query("mem", ["used"], bucket=bucket),
                    "refId": "A",
                }
            ],
        }

    def _procstat_agg_query(self, field: str, agg: str, *, bucket: str) -> str:
        """Cross-node aggregate Flux query against the procstat measurement.

        Collapses every matching node into a single line per aggregate
        (max / mean / min), so the panel stays readable whether the testnet
        has 50 nodes or 900. The Host / Role template variables still scope
        which nodes are included.

        All matching series are merged with ``group()`` and reduced in a
        single ``aggregateWindow`` pass — the aggregate is taken over every
        raw point in each window across all nodes. The previous shape
        (per-series window, then ``group(columns: ["_time"])`` + reduce)
        created one table group per timestamp, which is the classic slow
        path in Flux and degrades linearly with the number of windows; on a
        ~1000-process production fleet it made the RSS panels crawl.
        """
        return (
            f'from(bucket: "{bucket}")\n'
            "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
            '  |> filter(fn: (r) => r["_measurement"] == "procstat")\n'
            f'  |> filter(fn: (r) => r["_field"] == "{field}")\n'
            '  |> filter(fn: (r) => r["host"] =~ /^$host$/)\n'
            '  |> filter(fn: (r) => r["host_role"] =~ /^$host_role$/)\n'
            "  |> group()\n"
            f"  |> aggregateWindow(every: v.windowPeriod, fn: {agg}, "
            "createEmpty: false)\n"
            '  |> keep(columns: ["_time", "_value"])\n'
            # Rename the value column so Grafana labels the series "max" /
            # "mean" / "min" instead of three identical "Value" entries (the
            # Flux datasource names a series after its value column).
            f'  |> rename(columns: {{_value: "{agg}"}})\n'
            f'  |> yield(name: "{agg}")'
        )

    def _procstat_agg_targets(self, field: str, *, bucket: str) -> list[dict]:
        """max / mean / min across nodes as three Grafana targets."""
        return [
            {"query": self._procstat_agg_query(field, "max", bucket=bucket),
             "refId": "A"},
            {"query": self._procstat_agg_query(field, "mean", bucket=bucket),
             "refId": "B"},
            {"query": self._procstat_agg_query(field, "min", bucket=bucket),
             "refId": "C"},
        ]

    def _panel_node_rss(self, grid_pos: dict, *, bucket: str) -> dict:
        """Resident memory (RSS) aggregated across nodes: max / mean / min.

        The leak-watch view: `mean` rising is fleet-wide growth (warm-up or a
        systemic leak); `max` pulling away from `mean` is one node leaking;
        `min` is the floor. Three lines at any node count. Narrow the Host
        variable to a single VM to scope it down.
        """
        return {
            "title": "Node RSS (max / mean / min across nodes)",
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
            "targets": self._procstat_agg_targets("memory_rss", bucket=bucket),
        }

    def _panel_node_cpu(self, grid_pos: dict, *, bucket: str) -> dict:
        """Instantaneous per-process CPU aggregated across nodes.

        procstat reports a true point-in-time CPU% (unlike `ps`, whose value
        is the cumulative average since process start). max / mean / min so
        it scales like the RSS panel.
        """
        return {
            "title": "Node CPU (max / mean / min across nodes)",
            "type": "timeseries",
            "datasource": {"type": "influxdb", "uid": "influxdb"},
            "fieldConfig": {
                "defaults": {"unit": "percent"},
                "overrides": [],
            },
            "gridPos": grid_pos,
            "options": {
                "legend": {"displayMode": "list", "placement": "right"},
            },
            "targets": self._procstat_agg_targets("cpu_usage", bucket=bucket),
        }

    def _panel_disk_usage(
        self, grid_pos: dict, *, bucket: str, title: str, mount_path: str,
    ) -> dict:
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
                        bucket=bucket,
                    ),
                    "refId": "A",
                }
            ],
        }

    def _panel_network_inbound(self, grid_pos: dict, *, bucket: str) -> dict:
        return self._panel_network(
            "Network Inbound", "bytes_recv", grid_pos, bucket=bucket)

    def _panel_network_outbound(self, grid_pos: dict, *, bucket: str) -> dict:
        return self._panel_network(
            "Network Outbound", "bytes_sent", grid_pos, bucket=bucket)

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

    def _panel_network(
        self, title: str, field: str, grid_pos: dict, *, bucket: str,
    ) -> dict:
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
                        f'from(bucket: "{bucket}")\n'
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
