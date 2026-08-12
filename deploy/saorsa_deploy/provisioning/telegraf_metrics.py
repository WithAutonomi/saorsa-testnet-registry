"""Telegraf provisioning for InfluxDB metrics forwarding."""

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
from saorsa_deploy.resources import get_resources_dir

TELEGRAF_CONFIG_DIR = "/etc/telegraf-metrics"
TELEGRAF_CONFIG_PATH = f"{TELEGRAF_CONFIG_DIR}/telegraf_ant_metrics.conf"
TELEGRAF_ENV_PATH = "/etc/default/telegraf-ant-metrics"
TELEGRAF_SERVICE_NAME = "telegraf-ant-metrics"
TELEGRAF_UNIT_PATH = f"/etc/systemd/system/{TELEGRAF_SERVICE_NAME}.service"

# The per-process memory-composition collector, installed on the host and run
# by the two `inputs.exec` blocks in telegraf_metrics_node_memory.conf.
NODE_MEMORY_COLLECTOR_PATH = "/usr/local/bin/ant-node-memory-collector"

# Roles that run ant node-tier services, and so are the only ones where the
# memory-composition collector has anything to read. CLIENT hosts run `ant`
# client services, which the collector's unit globs deliberately do not match.
NODE_MEMORY_ROLES = ("GENESIS", "BOOTSTRAP", "WAN_NODE")

# Telegraf runs as an unprivileged `telegraf` user, so the unit grants it the
# capabilities its inputs need. Ambient capabilities are inherited across
# execve, which is what makes them reach the `inputs.exec` collector too.
BASE_AMBIENT_CAPABILITIES = ("CAP_DAC_READ_SEARCH",)

# `/proc/<pid>/smaps_rollup` is NOT merely permission-protected: the kernel
# gates it on ptrace_may_access(PTRACE_MODE_READ_FSCREDS) against the target.
# CAP_DAC_READ_SEARCH bypasses DAC checks and does NOT satisfy that, so an
# unprivileged telegraf reading a root-owned ant-node gets EACCES on every
# process — while `/proc/<pid>/status` (world-readable, no ptrace gate) keeps
# working. The failure is therefore *partial and silent*: the cheap source
# reports normally while the costly one returns nothing at all.
#
# Granted only on hosts that opt into the collector, so every other host's unit
# stays byte-identical and telegraf keeps the narrower capability set.
NODE_MEMORY_CAPABILITIES = ("CAP_SYS_PTRACE",)

NODE_STORAGE_MOUNT = "/mnt/ant-node-storage"
# Matches no real kernel device name -- and no real mountpoint either, since
# every path starts with "/" -- so the config's two volume_role regexes tag
# nothing on hosts that have no node storage.
NO_DATA_DEVICE = "__none__"


def build_data_location_env_command(
    *,
    host_role: str,
    mount_point: str = NODE_STORAGE_MOUNT,
    env_path: str = TELEGRAF_ENV_PATH,
) -> str:
    """Shell that appends ``ANT_DATA_DEVICE`` and ``ANT_DATA_MOUNT`` to the env file.

    Two variables because the two disk measurements identify the node storage
    volume differently, and neither can be baked into the shared config:

    * ``ANT_DATA_DEVICE`` — the kernel device name (``sdb``, ``dm-0``) that
      ``diskio`` reports. The backing device varies by provider and by whether
      the volume is raw ext4 (``/dev/sdb``) or LVM-striped
      (``/dev/mapper/...`` -> ``dm-0``). Resolution mirrors
      ``set_reserved_blocks.py``: ``findmnt`` for the source, then
      ``lsblk KNAME`` to reach the kernel name.
    * ``ANT_DATA_MOUNT`` — the mountpoint that the ``disk`` (capacity)
      measurement reports as its ``path`` tag. ``disk`` also carries a
      ``device`` tag, but its value is the fstab-style source rather than the
      kernel name (``mapper/vg-lv``, not ``dm-0``), so it cannot be matched
      against ``ANT_DATA_DEVICE``. The mountpoint is the only key that lines up.

    On node-tier hosts with no separate data volume (Contabo and OVH 3-AZ are
    root-disk only) the node data lives on the OS root filesystem, so the root
    device and ``/`` are tagged instead — those series then necessarily include
    OS I/O and OS disk usage.

    CLIENT hosts run no node storage at all, so they never fall back to root;
    both variables get the ``__none__`` sentinel and nothing is tagged.

    Never fails the pass: every branch falls through to the sentinel, and the
    whole thing is ``|| true``-guarded, because a missing disk tag must not
    break metrics provisioning.
    """
    # Only node-tier hosts treat the root disk as node storage.
    root_fallback = (
        "" if host_role == "CLIENT"
        else '  [ -n "$SRC" ] || { SRC=$(findmnt -nro SOURCE / 2>/dev/null'
             ' || true); MNT=/; }\n'
    )
    return (
        "{\n"
        f'  SRC=$(findmnt -nro SOURCE "{mount_point}" 2>/dev/null || true)\n'
        f'  MNT=""\n'
        f'  [ -n "$SRC" ] && MNT="{mount_point}"\n'
        f"{root_fallback}"
        '  DEV=""\n'
        '  if [ -n "$SRC" ]; then\n'
        '    DEV=$(lsblk -nro KNAME "$SRC" 2>/dev/null | head -1)\n'
        "  fi\n"
        f'  echo "ANT_DATA_DEVICE=${{DEV:-{NO_DATA_DEVICE}}}" >> {env_path}\n'
        f'  echo "ANT_DATA_MOUNT=${{MNT:-{NO_DATA_DEVICE}}}" >> {env_path}\n'
        "} || "
        f'{{ echo "ANT_DATA_DEVICE={NO_DATA_DEVICE}" >> {env_path};'
        f' echo "ANT_DATA_MOUNT={NO_DATA_DEVICE}" >> {env_path}; }}'
    )


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
        host_role: str = "WAN_NODE",
        ssh_user: str = "root",
        collect_node_memory: bool = False,
        console: Console | None = None,
    ):
        self.host_ips = host_ips
        self.ssh_key_path = ssh_key_path
        self.influxdb_url = influxdb_url
        self.influxdb_token = influxdb_token
        self.influxdb_org = influxdb_org
        self.influxdb_bucket = influxdb_bucket
        self.testnet_name = testnet_name
        self.host_role = host_role
        self.ssh_user = ssh_user
        self.collect_node_memory = collect_node_memory
        self.console = console or Console()

    def execute(self) -> None:
        """Install and configure Telegraf for metrics on all hosts."""
        hosts_data = [
            (ip, {"ssh_user": self.ssh_user, "ssh_key": self.ssh_key_path})
            for ip in self.host_ips
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

    def add_ops(self, state: State, *, install: bool = True) -> None:
        """Append the InfluxDB host-metrics operations to a connected ``state``.

        Split out from :meth:`execute` so a single shared ``state`` can carry both
        these operations and the ES log-forwarder's in one pass over the hosts.
        Pass ``install=False`` when the (identical) Telegraf install op has already
        been added to the same state by another provisioner.
        """
        if install:
            add_op(
                state,
                server.shell,
                name=TELEGRAF_INSTALL_OP_NAME,
                commands=TELEGRAF_INSTALL_COMMANDS,
            )

        env_content = self._build_env_file()
        add_op(
            state,
            server.shell,
            name="Write Telegraf metrics environment file",
            commands=[
                f"cat > {TELEGRAF_ENV_PATH} << 'ENV_EOF'\n{env_content}ENV_EOF",
                # The data device/mountpoint cannot be baked into the static env
                # content: they differ per host and are only knowable on the
                # host itself.
                build_data_location_env_command(host_role=self.host_role),
            ],
        )

        if self._wants_node_memory():
            # Written before the config that references it, so Telegraf never
            # restarts against an `inputs.exec` pointing at a missing file.
            collector = _read_script("node_memory_collector.py")
            add_op(
                state,
                server.shell,
                name="Write ant-node memory composition collector",
                commands=[
                    as_single_shell_command([
                        f"cat > {NODE_MEMORY_COLLECTOR_PATH}"
                        f" << 'COLLECTOR_EOF'\n{collector}COLLECTOR_EOF",
                        f"chmod 0755 {NODE_MEMORY_COLLECTOR_PATH}",
                    ])
                ],
            )

        config_content = self._build_telegraf_config()
        add_op(
            state,
            server.shell,
            name="Write Telegraf metrics configuration",
            commands=[
                as_single_shell_command([
                    f"mkdir -p {TELEGRAF_CONFIG_DIR}",
                    f"cat > {TELEGRAF_CONFIG_PATH}"
                    f" << 'CONF_EOF'\n{config_content}CONF_EOF",
                ])
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
            name="Verify Telegraf metrics service is active",
            commands=[
                as_single_shell_command([
                    f"systemctl is-active --quiet {TELEGRAF_SERVICE_NAME} "
                    f"|| (echo 'TELEGRAF_METRICS_VERIFY:FAILED - "
                    f"{TELEGRAF_SERVICE_NAME} is not running'; "
                    f"journalctl -u {TELEGRAF_SERVICE_NAME} --no-pager -n 20; exit 1)",
                    f"test -f {TELEGRAF_CONFIG_PATH} "
                    "|| (echo 'TELEGRAF_METRICS_VERIFY:FAILED - config file missing'; "
                    "exit 1)",
                ])
            ],
        )

    def _build_env_file(self) -> str:
        return (
            f"INFLUXDB_URL={self.influxdb_url}\n"
            f"INFLUXDB_TOKEN={self.influxdb_token}\n"
            f"INFLUXDB_ORG={self.influxdb_org}\n"
            f"INFLUXDB_BUCKET={self.influxdb_bucket}\n"
            f"ANT_TESTNET_NAME={self.testnet_name}\n"
            f"ANT_HOST_ROLE={self.host_role}\n"
        )

    def _wants_node_memory(self) -> bool:
        """Whether this host gets the memory-composition collector.

        Gated on role as well as the flag: the collector's unit globs match
        only node-tier services, so enabling it for a CLIENT host would install
        a script that can never emit a line.
        """
        return self.collect_node_memory and self.host_role in NODE_MEMORY_ROLES

    def _build_telegraf_config(self) -> str:
        return build_telegraf_metrics_config(
            include_client_services=self.host_role == "CLIENT",
            include_node_memory=self._wants_node_memory(),
        )

    def _build_unit_file(self) -> str:
        capabilities = list(BASE_AMBIENT_CAPABILITIES)
        if self._wants_node_memory():
            capabilities += list(NODE_MEMORY_CAPABILITIES)
        template = _read_resource("telegraf-ant-metrics.service")
        return template.replace(
            "{{TELEGRAF_ENV_PATH}}", TELEGRAF_ENV_PATH
        ).replace(
            "{{TELEGRAF_CONFIG_DIR}}", TELEGRAF_CONFIG_DIR
        ).replace(
            "{{AMBIENT_CAPABILITIES}}", " ".join(capabilities)
        )


def _read_resource(filename: str) -> str:
    """Read a file from the monitoring resources directory."""
    path = get_resources_dir() / "monitoring" / filename
    return path.read_text()


def _read_script(filename: str) -> str:
    """Read a file from the scripts resources directory."""
    path = get_resources_dir() / "scripts" / filename
    return path.read_text()


def build_telegraf_metrics_config(
    include_client_services: bool = False,
    include_node_memory: bool = False,
) -> str:
    """Read the Telegraf metrics config from the resources directory.

    Uses environment variables for connection details, making the config
    portable across hosts. Can also be used standalone with monitoring info --telegraf-config.

    When ``include_client_services`` is True (CLIENT-role hosts), the per-process
    procstat + whole-service cgroup memory inputs for the client `ant` services
    (uploader/downloader/canary) are appended. They are gated to CLIENT hosts so
    node-tier configs stay byte-identical and the cgroup input's "path missing"
    error never fires on the node fleet.

    When ``include_node_memory`` is True (node-tier hosts, opt-in), the two
    ``inputs.exec`` blocks that collect per-process RSS composition and
    ``smaps_rollup`` are appended. Both gates default to False so that a
    deployment which asks for neither produces exactly the config it always
    did — the same discipline the client append follows.
    """
    config = _read_resource("telegraf_metrics.conf")
    if include_client_services:
        config += "\n" + _read_resource("telegraf_metrics_client.conf")
    if include_node_memory:
        config += "\n" + _read_resource(
            "telegraf_metrics_node_memory.conf"
        ).replace("{{COLLECTOR_PATH}}", NODE_MEMORY_COLLECTOR_PATH)
    return config
