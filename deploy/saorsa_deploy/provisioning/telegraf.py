"""Telegraf provisioning for Elasticsearch log forwarding."""

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.provisioning.shell_ops import as_single_shell_command
from saorsa_deploy.provisioning.telegraf_install import (
    TELEGRAF_INSTALL_COMMANDS,
    TELEGRAF_INSTALL_OP_NAME,
)
from saorsa_deploy.provisioning.volume import LOG_DIR

TELEGRAF_CONFIG_DIR = "/etc/telegraf-elasticsearch"
TELEGRAF_CONFIG_PATH = f"{TELEGRAF_CONFIG_DIR}/telegraf_ant_node_elasticsearch.conf"
TELEGRAF_ENV_PATH = "/etc/default/telegraf-ant"
TELEGRAF_SERVICE_NAME = "telegraf-ant-elasticsearch"
TELEGRAF_UNIT_PATH = f"/etc/systemd/system/{TELEGRAF_SERVICE_NAME}.service"


#: Log levels dropped before lines are shipped to Elasticsearch. Only the
#: truly low-signal levels are dropped by default: ``DEBUG`` and ``TRACE``
#: are rarely useful unless you're actively debugging, and ES pays a real
#: cost for indexing them. ``INFO`` is kept because it carries the
#: breadcrumbs needed for post-hoc investigation ("accepted connection
#: from peer X", "registered peer ID Y", etc.) — dropping it makes it
#: impossible to reconstruct what a node was doing at a given moment.
#:
#: Override via ``--drop-log-levels`` on the ``deployments provision-telegraf``
#: command. Use ``--drop-log-levels ""`` to keep every line.
DEFAULT_DROP_LOG_LEVELS: tuple[str, ...] = ("DEBUG", "TRACE")

#: Upper bound on the number of metrics Telegraf buffers in its output
#: queue while waiting for Elasticsearch to accept them. When full,
#: Telegraf drops the oldest metrics — that's how DEV-01 lost all
#: node-side log forwarding from 01:30 UTC onward: with the previous
#: default of 200_000 lines, any ES slowdown longer than ~5 seconds at
#: peak load meant oldest-first eviction, and once the tail position
#: advanced past dropped lines we never caught up.
#:
#: 2_000_000 gives ~50 s of headroom at 40k lines/sec — enough to ride
#: out ES garbage-collection pauses and shard-rollover stalls. Override
#: with ``--buffer-limit`` when you know a testnet's logging profile.
DEFAULT_METRIC_BUFFER_LIMIT: int = 2_000_000

#: Upper bound on how many lines ``inputs.tail`` holds between reading a
#: file and handing off to the output. Should be >= output flush batch
#: size with some headroom; defaults to 1/4 of ``metric_buffer_limit`` so
#: the two scale together under a single knob.
DEFAULT_MAX_UNDELIVERED_LINES: int = DEFAULT_METRIC_BUFFER_LIMIT // 4


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
        from_beginning: bool = True,
        ant_log_dir: str | None = None,
        drop_log_levels: tuple[str, ...] | list[str] | None = None,
        metric_buffer_limit: int = DEFAULT_METRIC_BUFFER_LIMIT,
        max_undelivered_lines: int | None = None,
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
        self.from_beginning = from_beginning
        self.ant_log_dir = ant_log_dir or LOG_DIR
        # `None` -> default; explicit empty list -> no filtering
        self.drop_log_levels = (
            tuple(DEFAULT_DROP_LOG_LEVELS)
            if drop_log_levels is None
            else tuple(drop_log_levels)
        )
        if metric_buffer_limit < 1:
            raise ValueError(
                f"metric_buffer_limit must be >= 1 (got {metric_buffer_limit})"
            )
        self.metric_buffer_limit = metric_buffer_limit
        # Default the tail buffer to 1/4 of the output buffer so the two
        # scale together when the caller only tunes one knob.
        self.max_undelivered_lines = (
            max_undelivered_lines
            if max_undelivered_lines is not None
            else max(1, metric_buffer_limit // 4)
        )
        self.console = console or Console()

    def execute(self) -> None:
        """Install and configure Telegraf on all hosts."""
        hosts_data = [
            (ip, {"ssh_user": self.ssh_user, "ssh_key": self.ssh_key_path}) for ip in self.host_ips
        ]
        inventory = Inventory((hosts_data, {}))
        config = Config()
        if self.ssh_user != "root":
            config.SUDO = True
            config.USE_SUDO_PASSWORD = False
        state = State(inventory=inventory, config=config)

        self.console.print(f"Connecting to {len(self.host_ips)} host(s)...")
        connect_all(state)

        try:
            self.add_ops(state, install=True)
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

    def add_ops(self, state: State, *, install: bool = True) -> None:
        """Append the ES-log-forwarder operations to a connected pyinfra ``state``.

        Split out from :meth:`execute` so a single shared ``state`` can carry the
        operations for both this provisioner and the InfluxDB metrics provisioner
        in one pass over the hosts. Pass ``install=False`` when another provisioner
        already added the (identical) Telegraf install op to the same state, so the
        package is fetched and installed only once per host.
        """
        if install:
            add_op(
                state,
                server.shell,
                name=TELEGRAF_INSTALL_OP_NAME,
                commands=TELEGRAF_INSTALL_COMMANDS,
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
                as_single_shell_command([
                    f"cat > {TELEGRAF_ENV_PATH} << 'ENV_EOF'\n{env_content}ENV_EOF",
                    f'echo "ANT_HOST_CPU_ARCH=$(uname -m)" >> {TELEGRAF_ENV_PATH}',
                ])
            ],
        )

        config_content = self._build_telegraf_config()
        add_op(
            state,
            server.shell,
            name="Write Telegraf configuration",
            commands=[
                as_single_shell_command([
                    f"mkdir -p {TELEGRAF_CONFIG_DIR}",
                    f"cat > {TELEGRAF_CONFIG_PATH} << 'CONF_EOF'\n{config_content}CONF_EOF",
                ])
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
                as_single_shell_command([
                    "systemctl daemon-reload",
                    f"systemctl enable {TELEGRAF_SERVICE_NAME}",
                    f"systemctl restart {TELEGRAF_SERVICE_NAME}",
                ])
            ],
        )

        add_op(
            state,
            server.shell,
            name="Verify Telegraf service is active",
            commands=[
                as_single_shell_command([
                    f"systemctl is-active --quiet {TELEGRAF_SERVICE_NAME} "
                    f"|| (echo 'TELEGRAF_VERIFY:FAILED - {TELEGRAF_SERVICE_NAME} is not running'; "
                    f"journalctl -u {TELEGRAF_SERVICE_NAME} --no-pager -n 20; exit 1)",
                    f"test -f {TELEGRAF_CONFIG_PATH} "
                    "|| (echo 'TELEGRAF_VERIFY:FAILED - config file missing'; exit 1)",
                    f"test -f {TELEGRAF_ENV_PATH} "
                    "|| (echo 'TELEGRAF_VERIFY:FAILED - env file missing'; exit 1)",
                ])
            ],
        )

    def _build_env_file(self) -> str:
        """Generate the Telegraf environment file content."""
        testnet_name_lower = self.testnet_name.lower()
        return (
            f"TELEGRAF_ELASTICSEARCH_URL={self.elasticsearch_url}\n"
            f"TELEGRAF_ELASTICSEARCH_USERNAME={self.elasticsearch_username}\n"
            f"TELEGRAF_ELASTICSEARCH_PASSWORD={self.elasticsearch_password}\n"
            f"ANT_LOG_DIR={self.ant_log_dir}\n"
            f"ANT_TESTNET_NAME={self.testnet_name}\n"
            f"ANT_TESTNET_NAME_LOWER={testnet_name_lower}\n"
            f"ANT_HOST_ROLE={self.host_role}\n"
            f"ANT_BRANCH_COMMIT={self.branch_commit}\n"
            f"ANT_NODE_VERSION={self.node_version}\n"
        )

    def _build_telegraf_config(self) -> str:
        """Generate the Telegraf TOML configuration."""
        from_beginning = "true" if self.from_beginning else "false"
        if self.drop_log_levels:
            levels_list = ", ".join(f'"{lvl}"' for lvl in self.drop_log_levels)
            tagdrop_block = (
                "\n  # Drop noisy log levels before flushing to Elasticsearch.\n"
                "  [inputs.tail.tagdrop]\n"
                f"    level = [{levels_list}]\n"
            )
        else:
            tagdrop_block = ""
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

[[inputs.tail]]
  files = ["${ANT_LOG_DIR}/ant-*/ant-node*.log*"]

  from_beginning = __FROM_BEGINNING__
  watch_method = "inotify"
  max_undelivered_lines = __MAX_UNDELIVERED_LINES__

  data_format = "json"

  json_string_fields = [
    "message",
    "level",
    "target",
    "peer_id",
    "version",
    "commit",
    "relayer",
    "allocated",
    "peer",
    "client",
    "public_addr",
    "address",
    "kind",
    "event",
    "component",
    # Relay-affinity reacquire-decision line (saorsa_core::reachability::
    # acquisition, "relay acquisition: reacquire decision"). These are string
    # fields; without whitelisting them the telegraf json parser silently drops
    # them and only `message` reaches Elasticsearch. `present_in_candidates` is
    # a JSON boolean (not capturable via json_string_fields) but is redundant
    # with `previous_index` (== "None" iff the previous relayer was absent).
    "previous_relayer",
    "previous_index",
    "chosen"
  ]

  json_time_key = "timestamp"
  json_time_format = "2006-01-02T15:04:05.999999999Z"

  tag_keys = ["level", "target"]
__TAGDROP_BLOCK__
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
#   /mnt/ant-node-storage/logs/ant-bootstrap/ant-node.log -> "1"
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
        if part == "ant-bootstrap":
            metric.tags["service_number"] = "1"
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
""".replace("__FROM_BEGINNING__", from_beginning).replace(
            "__TAGDROP_BLOCK__", tagdrop_block
        ).replace(
            "__METRIC_BUFFER_LIMIT__", str(self.metric_buffer_limit)
        ).replace(
            "__MAX_UNDELIVERED_LINES__", str(self.max_undelivered_lines)
        )

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
