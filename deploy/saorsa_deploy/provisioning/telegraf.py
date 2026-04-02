"""Telegraf provisioning for Elasticsearch log forwarding."""

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.provisioning.volume import LOG_DIR

TELEGRAF_CONFIG_DIR = "/etc/telegraf-elasticsearch"
TELEGRAF_CONFIG_PATH = f"{TELEGRAF_CONFIG_DIR}/telegraf_ant_node_elasticsearch.conf"
TELEGRAF_ENV_PATH = "/etc/default/telegraf-ant"
TELEGRAF_SERVICE_NAME = "telegraf-ant-elasticsearch"
TELEGRAF_UNIT_PATH = f"/etc/systemd/system/{TELEGRAF_SERVICE_NAME}.service"


class TelegrafProvisioner:
    """Provisions Telegraf for Elasticsearch log forwarding on testnet VMs."""

    def __init__(
        self,
        host_ips: list[str],
        ssh_key_path: str,
        testnet_name: str,
        elasticsearch_url: str,
        elasticsearch_username: str,
        elasticsearch_password: str,
        host_role: str = "regular",
        branch_commit: str = "unknown",
        node_version: str = "unknown",
        ssh_user: str = "root",
        console: Console | None = None,
    ):
        self.host_ips = host_ips
        self.ssh_key_path = ssh_key_path
        self.testnet_name = testnet_name
        self.elasticsearch_url = elasticsearch_url
        self.elasticsearch_username = elasticsearch_username
        self.elasticsearch_password = elasticsearch_password
        self.host_role = host_role
        self.branch_commit = branch_commit
        self.node_version = node_version
        self.ssh_user = ssh_user
        self.console = console or Console()

    def execute(self) -> None:
        """Install and configure Telegraf on all hosts."""
        hosts_data = [
            (ip, {"ssh_user": self.ssh_user, "ssh_key": self.ssh_key_path}) for ip in self.host_ips
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

            add_op(
                state,
                server.shell,
                name="Grant telegraf access to log directory",
                commands=["usermod -aG root telegraf"],
            )

            env_content = self._build_env_file()
            add_op(
                state,
                server.shell,
                name="Write Telegraf environment file",
                commands=[
                    f"cat > {TELEGRAF_ENV_PATH} << 'ENV_EOF'\n{env_content}ENV_EOF",
                    f'echo "ANT_HOST_CPU_ARCH=$(uname -m)" >> {TELEGRAF_ENV_PATH}',
                ],
            )

            config_content = self._build_telegraf_config()
            add_op(
                state,
                server.shell,
                name="Write Telegraf configuration",
                commands=[
                    f"mkdir -p {TELEGRAF_CONFIG_DIR}",
                    f"cat > {TELEGRAF_CONFIG_PATH} << 'CONF_EOF'\n{config_content}CONF_EOF",
                ],
            )

            unit_content = self._build_unit_file()
            add_op(
                state,
                server.shell,
                name="Write Telegraf systemd unit",
                commands=[f"cat > {TELEGRAF_UNIT_PATH} << 'UNIT_EOF'\n{unit_content}UNIT_EOF"],
            )

            add_op(
                state,
                server.shell,
                name="Enable and start Telegraf service",
                commands=[
                    "systemctl daemon-reload",
                    f"systemctl enable {TELEGRAF_SERVICE_NAME}",
                    f"systemctl restart {TELEGRAF_SERVICE_NAME}",
                ],
            )

            add_op(
                state,
                server.shell,
                name="Verify Telegraf service is active",
                commands=[
                    f"systemctl is-active --quiet {TELEGRAF_SERVICE_NAME} "
                    f"|| (echo 'TELEGRAF_VERIFY:FAILED - {TELEGRAF_SERVICE_NAME} is not running'; "
                    f"journalctl -u {TELEGRAF_SERVICE_NAME} --no-pager -n 20; exit 1)",
                    f"test -f {TELEGRAF_CONFIG_PATH} "
                    "|| (echo 'TELEGRAF_VERIFY:FAILED - config file missing'; exit 1)",
                    f"test -f {TELEGRAF_ENV_PATH} "
                    "|| (echo 'TELEGRAF_VERIFY:FAILED - env file missing'; exit 1)",
                ],
            )

            self.console.print("Running Telegraf provisioning operations...")
            run_ops(state)
        finally:
            disconnect_all(state)

        failed = state.failed_hosts
        total = len(self.host_ips)
        succeeded = total - len(failed)
        self.console.print(f"Telegraf provisioning complete: {succeeded}/{total} hosts succeeded")
        if failed:
            for host in failed:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError(f"{len(failed)} host(s) failed Telegraf provisioning")

    def _build_env_file(self) -> str:
        """Generate the Telegraf environment file content."""
        testnet_name_lower = self.testnet_name.lower()
        return (
            f"TELEGRAF_ELASTICSEARCH_URL={self.elasticsearch_url}\n"
            f"TELEGRAF_ELASTICSEARCH_USERNAME={self.elasticsearch_username}\n"
            f"TELEGRAF_ELASTICSEARCH_PASSWORD={self.elasticsearch_password}\n"
            f"ANT_LOG_DIR={LOG_DIR}\n"
            f"ANT_TESTNET_NAME={self.testnet_name}\n"
            f"ANT_TESTNET_NAME_LOWER={testnet_name_lower}\n"
            f"ANT_HOST_ROLE={self.host_role}\n"
            f"ANT_BRANCH_COMMIT={self.branch_commit}\n"
            f"ANT_NODE_VERSION={self.node_version}\n"
        )

    def _build_telegraf_config(self) -> str:
        """Generate the Telegraf TOML configuration."""
        return """\
###############################################################################
#                            GLOBAL TAGS                                      #
###############################################################################

[global_tags]
  testnet_name = "${ANT_TESTNET_NAME}"
  host_role = "${ANT_HOST_ROLE}"
  host_cpu_architecture = "${ANT_HOST_CPU_ARCH}"
  branch_commit = "${ANT_BRANCH_COMMIT}"
  node_version = "${ANT_NODE_VERSION}"

###############################################################################
#                            AGENT                                            #
###############################################################################

[agent]
  interval = "60s"
  round_interval = true
  metric_batch_size = 1000
  metric_buffer_limit = 200000
  collection_jitter = "0s"
  flush_interval = "10s"
  flush_jitter = "0s"
  precision = "0s"
  hostname = ""
  omit_hostname = false

###############################################################################
#                            INPUT                                            #
###############################################################################

[[inputs.tail]]
  files = ["${ANT_LOG_DIR}/ant-*/ant-node*.log*"]

  from_beginning = true
  watch_method = "inotify"
  max_undelivered_lines = 50000

  data_format = "json"

  json_string_fields = [
    "message",
    "level",
    "target",
    "peer_id",
    "version",
    "commit"
  ]

  json_time_key = "timestamp"
  json_time_format = "2006-01-02T15:04:05.999999999Z"

  tag_keys = ["level", "target"]

###############################################################################
#                            PROCESSORS                                       #
###############################################################################

# --- Order 1: Rename file path tag to service_number ---
[[processors.rename]]
  order = 1
  [[processors.rename.replace]]
    tag = "path"
    dest = "service_number"

# --- Order 2: Extract numeric service number from log directory path ---
#
# Parses paths like:
#   /mnt/ant-node-storage/log/ant-node-17/ant-node.log -> "17"
#   /mnt/ant-node-storage/log/ant-genesis-node/ant-node.log -> "genesis"
[[processors.starlark]]
  order = 2
  source = '''
def apply(metric):
    svc = metric.tags.get("service_number", "")
    for part in svc.split("/"):
        if part.startswith("ant-node-"):
            num = part[len("ant-node-"):]
            if num.isdigit():
                metric.tags["service_number"] = num
                return metric
        if part.startswith("ant-genesis-"):
            metric.tags["service_number"] = "genesis"
            return metric
    return metric
'''

# --- Order 3: Uppercase hostname ---
[[processors.strings]]
  order = 3
  [[processors.strings.uppercase]]
    tag = "host"

###############################################################################
#                            OUTPUT                                           #
###############################################################################

[[outputs.elasticsearch]]
  urls = ["${TELEGRAF_ELASTICSEARCH_URL}"]
  timeout = "30s"
  username = "${TELEGRAF_ELASTICSEARCH_USERNAME}"
  password = "${TELEGRAF_ELASTICSEARCH_PASSWORD}"

  index_name = "telegraf-ant-nodes-${ANT_TESTNET_NAME_LOWER}-%Y.%m.%d"

  force_document_id = true

  enable_sniffer = false
  health_check_interval = "0s"
  health_check_timeout = "1s"

  manage_template = false
  overwrite_template = false
"""

    def _build_unit_file(self) -> str:
        """Generate the systemd unit file content for Telegraf."""
        return f"""\
[Unit]
Description=Telegraf - Ant Node Elasticsearch Log Forwarder
Documentation=https://github.com/influxdata/telegraf
After=network.target

[Service]
Type=notify
EnvironmentFile=-{TELEGRAF_ENV_PATH}
User=telegraf
ExecStart=/usr/bin/telegraf -config {TELEGRAF_CONFIG_PATH}
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartForceExitStatus=SIGPIPE
KillMode=control-group

[Install]
WantedBy=multi-user.target
"""
