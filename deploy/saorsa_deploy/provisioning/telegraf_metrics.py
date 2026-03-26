"""Telegraf provisioning for InfluxDB metrics forwarding."""

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.provisioning.node import METRICS_PORT_BASE
from saorsa_deploy.resources import get_resources_dir

TELEGRAF_CONFIG_DIR = "/etc/telegraf-metrics"
TELEGRAF_CONFIG_PATH = f"{TELEGRAF_CONFIG_DIR}/telegraf_ant_metrics.conf"
TELEGRAF_PROMETHEUS_CONFIG_PATH = f"{TELEGRAF_CONFIG_DIR}/telegraf_prometheus.conf"
TELEGRAF_ENV_PATH = "/etc/default/telegraf-ant-metrics"
TELEGRAF_SERVICE_NAME = "telegraf-ant-metrics"
TELEGRAF_UNIT_PATH = f"/etc/systemd/system/{TELEGRAF_SERVICE_NAME}.service"


class TelegrafMetricsProvisioner:
    """Provisions Telegraf for InfluxDB metrics forwarding on testnet VMs."""

    def __init__(
        self,
        host_ips: list[str],
        ssh_key_path: str,
        influxdb_url: str,
        influxdb_token: str,
        influxdb_org: str,
        influxdb_bucket: str,
        testnet_name: str = "",
        node_count: int = 0,
        is_genesis: bool = False,
        console: Console | None = None,
    ):
        self.host_ips = host_ips
        self.ssh_key_path = ssh_key_path
        self.influxdb_url = influxdb_url
        self.influxdb_token = influxdb_token
        self.influxdb_org = influxdb_org
        self.influxdb_bucket = influxdb_bucket
        self.testnet_name = testnet_name
        self.node_count = node_count
        self.is_genesis = is_genesis
        self.console = console or Console()

    def execute(self) -> None:
        """Install and configure Telegraf for metrics on all hosts."""
        hosts_data = [
            (ip, {"ssh_user": "root", "ssh_key": self.ssh_key_path})
            for ip in self.host_ips
        ]
        inventory = Inventory((hosts_data, {}))
        state = State(inventory=inventory, config=Config())

        self.console.print(f"Connecting to {len(self.host_ips)} host(s)...")
        connect_all(state)

        try:
            add_op(
                state,
                server.shell,
                name="Install Telegraf",
                commands=[
                    "dpkg -s telegraf >/dev/null 2>&1 && echo 'TELEGRAF:SKIP' || "
                    "(wget -q https://dl.influxdata.com/telegraf/releases/"
                    "telegraf_1.33.0-1_amd64.deb -O /tmp/telegraf.deb && "
                    "dpkg -i /tmp/telegraf.deb && "
                    "rm -f /tmp/telegraf.deb && "
                    "echo 'TELEGRAF:INSTALLED')",
                ],
            )

            env_content = self._build_env_file()
            add_op(
                state,
                server.shell,
                name="Write Telegraf metrics environment file",
                commands=[
                    f"cat > {TELEGRAF_ENV_PATH} << 'ENV_EOF'\n{env_content}ENV_EOF",
                ],
            )

            config_content = self._build_telegraf_config()
            add_op(
                state,
                server.shell,
                name="Write Telegraf metrics configuration",
                commands=[
                    f"mkdir -p {TELEGRAF_CONFIG_DIR}",
                    f"cat > {TELEGRAF_CONFIG_PATH}"
                    f" << 'CONF_EOF'\n{config_content}CONF_EOF",
                ],
            )

            if self.node_count > 0:
                prometheus_config = self._build_prometheus_config()
                add_op(
                    state,
                    server.shell,
                    name="Write Telegraf prometheus scrape configuration",
                    commands=[
                        f"cat > {TELEGRAF_PROMETHEUS_CONFIG_PATH}"
                        f" << 'PROM_EOF'\n{prometheus_config}PROM_EOF",
                    ],
                )

            unit_content = self._build_unit_file()
            add_op(
                state,
                server.shell,
                name="Write Telegraf metrics systemd unit",
                commands=[
                    f"cat > {TELEGRAF_UNIT_PATH} << 'UNIT_EOF'\n{unit_content}UNIT_EOF",
                ],
            )

            add_op(
                state,
                server.shell,
                name="Enable and start Telegraf metrics service",
                commands=[
                    "systemctl daemon-reload",
                    f"systemctl enable {TELEGRAF_SERVICE_NAME}",
                    f"systemctl restart {TELEGRAF_SERVICE_NAME}",
                ],
            )

            add_op(
                state,
                server.shell,
                name="Verify Telegraf metrics service is active",
                commands=[
                    f"systemctl is-active --quiet {TELEGRAF_SERVICE_NAME} "
                    f"|| (echo 'TELEGRAF_METRICS_VERIFY:FAILED - "
                    f"{TELEGRAF_SERVICE_NAME} is not running'; "
                    f"journalctl -u {TELEGRAF_SERVICE_NAME} --no-pager -n 20; exit 1)",
                    f"test -f {TELEGRAF_CONFIG_PATH} "
                    "|| (echo 'TELEGRAF_METRICS_VERIFY:FAILED - config file missing'; "
                    "exit 1)",
                ],
            )

            self.console.print("Running Telegraf metrics provisioning operations...")
            run_ops(state)
        finally:
            disconnect_all(state)

        failed = state.failed_hosts
        total = len(self.host_ips)
        succeeded = total - len(failed)
        self.console.print(
            f"Telegraf metrics provisioning complete: {succeeded}/{total} hosts succeeded"
        )
        if failed:
            for host in failed:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError(
                f"{len(failed)} host(s) failed Telegraf metrics provisioning"
            )

    def _build_env_file(self) -> str:
        return (
            f"INFLUXDB_URL={self.influxdb_url}\n"
            f"INFLUXDB_TOKEN={self.influxdb_token}\n"
            f"INFLUXDB_ORG={self.influxdb_org}\n"
            f"INFLUXDB_BUCKET={self.influxdb_bucket}\n"
            f"ANT_TESTNET_NAME={self.testnet_name}\n"
        )

    def _build_telegraf_config(self) -> str:
        return build_telegraf_metrics_config()

    def _build_prometheus_config(self) -> str:
        """Build the prometheus scraping config with per-node tagged blocks.

        Each node gets its own [[inputs.prometheus]] block with a node_service
        tag so metrics can be distinguished per-node in dashboards.
        """
        header = _read_resource("telegraf_prometheus.conf")
        blocks = []

        if self.is_genesis:
            blocks.append(
                f'[[inputs.prometheus]]\n'
                f'  urls = ["http://127.0.0.1:{METRICS_PORT_BASE}/metrics"]\n'
                f'  metric_version = 2\n'
                f'  [inputs.prometheus.tags]\n'
                f'    node_service = "ant-genesis-node"\n'
                f'    host_role = "GENESIS"\n'
            )
        else:
            for i in range(self.node_count):
                port = METRICS_PORT_BASE + i
                service_name = f"ant-node-{i + 1}"
                blocks.append(
                    f'[[inputs.prometheus]]\n'
                    f'  urls = ["http://127.0.0.1:{port}/metrics"]\n'
                    f'  metric_version = 2\n'
                    f'  [inputs.prometheus.tags]\n'
                    f'    node_service = "{service_name}"\n'
                    f'    host_role = "WAN_NODE"\n'
                )

        return header + "\n".join(blocks)

    def _build_unit_file(self) -> str:
        template = _read_resource("telegraf-ant-metrics.service")
        return template.replace(
            "{{TELEGRAF_ENV_PATH}}", TELEGRAF_ENV_PATH
        ).replace(
            "{{TELEGRAF_CONFIG_DIR}}", TELEGRAF_CONFIG_DIR
        )


def _read_resource(filename: str) -> str:
    """Read a file from the monitoring resources directory."""
    path = get_resources_dir() / "monitoring" / filename
    return path.read_text()


def build_telegraf_metrics_config() -> str:
    """Read the Telegraf metrics config from the resources directory.

    Uses environment variables for connection details, making the config
    portable across hosts. Can also be used standalone with monitoring-info --telegraf-config.
    """
    return _read_resource("telegraf_metrics.conf")
