"""Telegraf provisioning for forwarding CLIENT ant logs to Elasticsearch.

Unlike the node forwarder (``telegraf.py``), client ``ant`` processes emit
plain-text tracing on stdout/stderr — not structured JSON written to disk. So
this forwarder tails the per-role stdout/stderr log files, ships each raw line
as a ``message`` field, and tags it with the client ``role`` (static per pass)
and the ``stream`` (stdout/stderr, derived from the filename). Lines land in a
SEPARATE index (``telegraf-ant-clients-{name}``) so client and node logs never
mingle even on a shared ES instance.
"""

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

TELEGRAF_CONFIG_DIR = "/etc/telegraf-client-elasticsearch"
TELEGRAF_CONFIG_PATH = f"{TELEGRAF_CONFIG_DIR}/telegraf_ant_client_elasticsearch.conf"
TELEGRAF_ENV_PATH = "/etc/default/telegraf-ant-client"
TELEGRAF_SERVICE_NAME = "telegraf-ant-client-elasticsearch"
TELEGRAF_UNIT_PATH = f"/etc/systemd/system/{TELEGRAF_SERVICE_NAME}.service"

#: Buffering: client VMs forward far less than a node fleet (a single ant
#: process at a time per upload/download), so a 200k-line output buffer with a
#: 50k tail buffer is ample headroom while staying small.
DEFAULT_METRIC_BUFFER_LIMIT: int = 200_000
DEFAULT_MAX_UNDELIVERED_LINES: int = DEFAULT_METRIC_BUFFER_LIMIT // 4


class ClientTelegrafProvisioner:
    """Provision Telegraf to forward client ant stdout/stderr to Elasticsearch.

    One pass handles one ``role`` (uploader / downloader / canary-upload /
    canary-probe): pass the host IPs for that role and the file globs its
    service writes its stdout/stderr to.
    """

    def __init__(
        self,
        host_ips: list[str],
        ssh_key_path: str,
        testnet_name: str,
        elasticsearch_url: str,
        elasticsearch_username: str,
        elasticsearch_password: str,
        role: str,
        tail_globs: list[str],
        host_role: str = "CLIENT",
        ssh_user: str = "root",
        from_beginning: bool = True,
        metric_buffer_limit: int = DEFAULT_METRIC_BUFFER_LIMIT,
        max_undelivered_lines: int | None = None,
        console: Console | None = None,
    ):
        if not tail_globs:
            raise ValueError("tail_globs must be non-empty")
        if metric_buffer_limit < 1:
            raise ValueError(f"metric_buffer_limit must be >= 1 (got {metric_buffer_limit})")
        self.host_ips = host_ips
        self.ssh_key_path = ssh_key_path
        self.testnet_name = testnet_name
        self.elasticsearch_url = elasticsearch_url
        self.elasticsearch_username = elasticsearch_username
        self.elasticsearch_password = elasticsearch_password
        self.role = role
        self.tail_globs = list(tail_globs)
        self.host_role = host_role
        self.ssh_user = ssh_user
        self.from_beginning = from_beginning
        self.metric_buffer_limit = metric_buffer_limit
        self.max_undelivered_lines = (
            max_undelivered_lines
            if max_undelivered_lines is not None
            else max(1, metric_buffer_limit // 4)
        )
        self.console = console or Console()

    def execute(self) -> None:
        """Install and configure the client Telegraf forwarder on all hosts."""
        hosts_data = [
            (ip, {"ssh_user": self.ssh_user, "ssh_key": self.ssh_key_path}) for ip in self.host_ips
        ]
        inventory = Inventory((hosts_data, {}))
        config = Config()
        if self.ssh_user != "root":
            config.SUDO = True
            config.USE_SUDO_PASSWORD = False
        state = State(inventory=inventory, config=config)

        self.console.print(f"Connecting to {len(self.host_ips)} {self.role} host(s)...")
        connect_all(state)

        try:
            add_op(
                state,
                server.shell,
                name="Install Telegraf",
                commands=[
                    "dpkg -s telegraf >/dev/null 2>&1 && echo 'TELEGRAF:SKIP' || "
                    "(rm -f /tmp/telegraf.deb && "
                    "wget -q https://dl.influxdata.com/telegraf/releases/"
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
                name="Write client Telegraf environment file",
                commands=[
                    f"cat > {TELEGRAF_ENV_PATH} << 'ENV_EOF'\n{env_content}ENV_EOF",
                    f'echo "ANT_HOST_CPU_ARCH=$(uname -m)" >> {TELEGRAF_ENV_PATH}',
                ],
            )

            config_content = self._build_telegraf_config()
            add_op(
                state,
                server.shell,
                name="Write client Telegraf configuration",
                commands=[
                    f"mkdir -p {TELEGRAF_CONFIG_DIR}",
                    f"cat > {TELEGRAF_CONFIG_PATH} << 'CONF_EOF'\n{config_content}CONF_EOF",
                ],
            )

            unit_content = self._build_unit_file()
            add_op(
                state,
                server.shell,
                name="Write client Telegraf systemd unit",
                commands=[f"cat > {TELEGRAF_UNIT_PATH} << 'UNIT_EOF'\n{unit_content}UNIT_EOF"],
            )

            add_op(
                state,
                server.shell,
                name="Enable and start client Telegraf service",
                commands=[
                    "systemctl daemon-reload",
                    f"systemctl enable {TELEGRAF_SERVICE_NAME}",
                    f"systemctl restart {TELEGRAF_SERVICE_NAME}",
                ],
            )

            add_op(
                state,
                server.shell,
                name="Verify client Telegraf service is active",
                commands=[
                    f"systemctl is-active --quiet {TELEGRAF_SERVICE_NAME} "
                    f"|| (echo 'TELEGRAF_VERIFY:FAILED - {TELEGRAF_SERVICE_NAME} "
                    "is not running'; "
                    f"journalctl -u {TELEGRAF_SERVICE_NAME} --no-pager -n 20; exit 1)",
                ],
            )

            self.console.print("Running client Telegraf provisioning operations...")
            run_ops(state)
        finally:
            disconnect_all(state)

        failed = state.failed_hosts
        total = len(self.host_ips)
        succeeded = total - len(failed)
        self.console.print(
            f"Client Telegraf provisioning complete: {succeeded}/{total} "
            f"{self.role} host(s) succeeded"
        )
        if failed:
            for host in failed:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError(f"{len(failed)} host(s) failed client Telegraf provisioning")

    def _build_env_file(self) -> str:
        testnet_name_lower = self.testnet_name.lower()
        return (
            f"TELEGRAF_ELASTICSEARCH_URL={self.elasticsearch_url}\n"
            f"TELEGRAF_ELASTICSEARCH_USERNAME={self.elasticsearch_username}\n"
            f"TELEGRAF_ELASTICSEARCH_PASSWORD={self.elasticsearch_password}\n"
            f"ANT_TESTNET_NAME={self.testnet_name}\n"
            f"ANT_TESTNET_NAME_LOWER={testnet_name_lower}\n"
            f"ANT_HOST_ROLE={self.host_role}\n"
            f"ANT_CLIENT_ROLE={self.role}\n"
        )

    def _build_telegraf_config(self) -> str:
        from_beginning = "true" if self.from_beginning else "false"
        files_list = ", ".join(f'"{g}"' for g in self.tail_globs)
        return (
            """\
###############################################################################
#                            GLOBAL TAGS                                      #
###############################################################################

[global_tags]
  testnet_name = "${ANT_TESTNET_NAME}"
  host_role = "${ANT_HOST_ROLE}"
  role = "${ANT_CLIENT_ROLE}"
  host_cpu_architecture = "${ANT_HOST_CPU_ARCH}"

###############################################################################
#                            AGENT                                            #
###############################################################################

[agent]
  interval = "60s"
  round_interval = true
  metric_batch_size = 1000
  metric_buffer_limit = __METRIC_BUFFER_LIMIT__
  collection_jitter = "0s"
  flush_interval = "10s"
  flush_jitter = "0s"
  precision = "0s"
  hostname = ""
  omit_hostname = false

###############################################################################
#                            INPUT                                            #
###############################################################################

# Client ant logs are plain text (not JSON). Capture each raw line into a
# single string field named `message`; the `path` tag carries the source file.
[[inputs.tail]]
  files = [__FILES__]

  from_beginning = __FROM_BEGINNING__
  watch_method = "inotify"
  max_undelivered_lines = __MAX_UNDELIVERED_LINES__

  data_format = "value"
  data_type = "string"

###############################################################################
#                            PROCESSORS                                       #
###############################################################################

# --- Order 1: raw line field `value` -> `message` ---
[[processors.rename]]
  order = 1
  [[processors.rename.replace]]
    field = "value"
    dest = "message"

# --- Order 2: derive `stream` (stdout/stderr) from the file name, drop path ---
[[processors.starlark]]
  order = 2
  source = '''
def apply(metric):
    p = metric.tags.get("path", "")
    if p.endswith(".stderr") or p.endswith(".err"):
        metric.tags["stream"] = "stderr"
    else:
        metric.tags["stream"] = "stdout"
    metric.tags.pop("path", None)
    return metric
'''

# --- Order 3: uppercase hostname ---
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

  index_name = "telegraf-ant-clients-${ANT_TESTNET_NAME_LOWER}-%Y.%m.%d"

  force_document_id = true

  enable_sniffer = false
  health_check_interval = "0s"
  health_check_timeout = "1s"

  manage_template = false
  overwrite_template = false
""".replace("__FROM_BEGINNING__", from_beginning)
            .replace("__FILES__", files_list)
            .replace("__METRIC_BUFFER_LIMIT__", str(self.metric_buffer_limit))
            .replace("__MAX_UNDELIVERED_LINES__", str(self.max_undelivered_lines))
        )

    def _build_unit_file(self) -> str:
        return f"""\
[Unit]
Description=Telegraf - Ant Client Elasticsearch Log Forwarder
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
