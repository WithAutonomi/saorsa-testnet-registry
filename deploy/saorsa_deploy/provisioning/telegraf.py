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

#: ``target`` tag patterns whose log lines the ES forwarder ALSO writes to
#: InfluxDB (V2-1225). These are the periodic cumulative traffic-accounting
#: summaries (V2-623): the counters themselves are right, but they sat in
#: Elasticsearch where nothing could divide them by the host's ``net``
#: byte counters, which live in InfluxDB -- so an 18-hour bandwidth episode
#: on a prod node was invisible in every dashboard. Shipping the same points
#: to Influx lets one Flux query reconcile accounted egress against the NIC.
#:
#: Telegraf compiles ``tagpass`` values as globs with no separator, so ``*``
#: spans ``::``. ``*::traffic`` therefore admits every summary target that
#: follows the ``<crate>::…::traffic`` convention -- ``saorsa_transport::traffic``,
#: ``saorsa_core::traffic``, ``ant_node::replication::traffic``,
#: ``saorsa_transport::relay::traffic`` today, and the
#: ``ant_node::{payment,storage,upgrade}::traffic`` targets V2-834 adds --
#: without a deploy change per new counter. The MASQUE relay-server summary
#: lives under a component target rather than a ``::traffic`` one, so it is
#: listed explicitly.
INFLUX_TRAFFIC_TARGET_PATTERNS: tuple[str, ...] = (
    "*::traffic",
    "saorsa_transport::masque::relay_server",
)


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
        influxdb_url: str | None = None,
        influxdb_token: str | None = None,
        influxdb_org: str | None = None,
        influxdb_bucket: str | None = None,
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
        # The InfluxDB output is all-or-nothing: with every credential set the
        # traffic-summary lines are ALSO written to Influx; with none set the
        # rendered env file and config are byte-identical to before the option
        # existed. A partial set is a caller bug, not a silent "no Influx".
        influx_args = (influxdb_url, influxdb_token, influxdb_org, influxdb_bucket)
        if any(influx_args) and not all(influx_args):
            raise ValueError(
                "influxdb_url, influxdb_token, influxdb_org and influxdb_bucket "
                "must be given together (or all omitted)"
            )
        self.influxdb_url = influxdb_url
        self.influxdb_token = influxdb_token
        self.influxdb_org = influxdb_org
        self.influxdb_bucket = influxdb_bucket
        self.console = console or Console()

    @property
    def influx_output_enabled(self) -> bool:
        """True when the traffic-summary lines are also shipped to InfluxDB."""
        return self.influxdb_url is not None

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
        content = (
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
        if self.influx_output_enabled:
            # Same variable names the metrics forwarder reads from its own env
            # file (/etc/default/telegraf-ant-metrics); this unit only reads
            # /etc/default/telegraf-ant, so the values are duplicated here.
            content += (
                f"INFLUXDB_URL={self.influxdb_url}\n"
                f"INFLUXDB_TOKEN={self.influxdb_token}\n"
                f"INFLUXDB_ORG={self.influxdb_org}\n"
                f"INFLUXDB_BUCKET={self.influxdb_bucket}\n"
            )
        return content

    def _build_influx_output_block(self) -> str:
        """The optional ``[[outputs.influxdb_v2]]`` block for the traffic summaries."""
        if not self.influx_output_enabled:
            return ""
        targets = ", ".join(f'"{p}"' for p in INFLUX_TRAFFIC_TARGET_PATTERNS)
        return f"""
# Traffic-accounting summaries ALSO go to InfluxDB (V2-1225), where the host
# `net` byte counters live, so one Flux query can reconcile accounted egress
# (udp_tx_bytes + bytes_relayed) against what the NIC actually sent.
#
# The `tagpass` filter is REQUIRED: without it every log line the tail input
# parses would be written to Influx. `*::traffic` is a separator-less glob and
# admits every `<crate>::…::traffic` summary target, current and future; the
# MASQUE relay-server summary is under a component target, hence explicit.
#
# NB the `host` tag reaches Influx UPPERCASED (the processor above runs before
# every output), while the metrics forwarder writes it lowercase -- dashboard
# queries lower-case the tail side (`strings.toLower`) before joining on host.
[[outputs.influxdb_v2]]
  urls         = ["${{INFLUXDB_URL}}"]
  token        = "${{INFLUXDB_TOKEN}}"
  organization = "${{INFLUXDB_ORG}}"
  bucket       = "${{INFLUXDB_BUCKET}}"

  namepass = ["tail"]

  [outputs.influxdb_v2.tagpass]
    target = [{targets}]
"""

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
  # Fallback only. ant-node stamps `node_version` / `node_commit` onto every
  # JSON log line itself (see `tag_keys` below), and an input-supplied tag
  # wins over a global tag of the same name, so this value is only seen on
  # lines written by a binary that predates the stamp. It is a static
  # provisioning-time string and can never be right across an auto-upgrade,
  # which is exactly why the node reports its own version (ant-node V2-1153).
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

  # Capture EVERY non-numeric field, rather than a curated allow-list.
  #
  # Telegraf's json parser passes numbers through automatically but silently
  # drops any other value not matched here -- so a curated list means every
  # ant-node PR that logs a new string field loses it, with no error anywhere:
  # the line still arrives, just missing fields. That failure recurred
  # repeatedly (most recently ant-node#179's `peer_1_id`..`peer_10_id`), and
  # the list also drifted per-host -- 49 of 66 ant-prod-01 VMs were still on a
  # 6-entry version while 17 had the full list, so the same field was captured
  # on some hosts and dropped on others.
  #
  # The pattern is a glob, so "*" matches everything. Verified against telegraf
  # 1.33 that this captures strings, booleans, nested objects (flattened to
  # `parent_child`) and arrays (`field_0`, `field_1`), while `json_time_key`
  # below still consumes `timestamp` as the metric time and `tag_keys` still
  # moves level/target to tags rather than duplicating them as fields.
  #
  # Cost is bounded: a 95k-line sample of live prod logs held 121 distinct keys,
  # only 24 of them non-numeric, against Elasticsearch's 1000-field default --
  # and `index.mapping.ignore_malformed` is set on the index template, so a
  # field whose type conflicts with an earlier mapping is dropped rather than
  # 400-ing (and wedging) the whole batch.
  #
  # NOT fixed by this: telegraf parses JSON numbers as float64, so integers
  # above 2^53 (u64 ids) lose precision no matter how fields are selected.
  json_string_fields = ["*"]

  json_time_key = "timestamp"
  json_time_format = "2006-01-02T15:04:05.999999999Z"

  # `node_version` and `node_commit` are written by the node onto every JSON
  # line; promoting them to tags gives `tag.node_version.keyword`, so a staged
  # rollout can be split by the build each line was actually written by.
  tag_keys = ["level", "target", "node_version", "node_commit"]
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
#   /mnt/ant-node-storage/log/ant-bootstrap-3/ant-node.log -> "3"
#
# Anything unmatched keeps the raw path -- which is what every testnet
# bootstrap carried until the `ant-bootstrap-{index}` form was added: the
# bootstrap provisioner names its service and log dir by index, and only the
# bare prod form was handled.
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
        if part.startswith("ant-bootstrap-"):
            num = part[len("ant-bootstrap-"):]
            if num.isdigit():
                metric.tags["service_number"] = num
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
__INFLUX_OUTPUT_BLOCK__""".replace("__FROM_BEGINNING__", from_beginning).replace(
            "__TAGDROP_BLOCK__", tagdrop_block
        ).replace(
            "__METRIC_BUFFER_LIMIT__", str(self.metric_buffer_limit)
        ).replace(
            "__MAX_UNDELIVERED_LINES__", str(self.max_undelivered_lines)
        ).replace(
            "__INFLUX_OUTPUT_BLOCK__", self._build_influx_output_block()
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
