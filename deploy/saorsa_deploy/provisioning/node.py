from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.binary_source import RELEASE_ASSET_NAME, get_release_url
from saorsa_deploy.provisioning.apt import (
    DISABLE_UNATTENDED_UPGRADES_OP_NAME,
    build_disable_unattended_upgrades_command,
)
from saorsa_deploy.provisioning.bootstrap import BINARY_INSTALL_PATH, DEFAULT_REWARDS_ADDRESS
from saorsa_deploy.provisioning.clock_skew import (
    SKEW_HOURS,
    build_clock_skew_command,
    skew_direction_for_ip,
)
from saorsa_deploy.provisioning.full_nodes import (
    DEFAULT_FULL_HEADROOM_MB,
    build_fill_storage_command,
)
from saorsa_deploy.provisioning.nat import NAT_IP_EXEC_PREFIX, build_nat_setup_command
from saorsa_deploy.provisioning.progress import (
    RichLiveProgressHandler,
    create_progress_handler,
)
from saorsa_deploy.provisioning.volume import LOG_DIR, MOUNT_POINT, build_mount_volume_command

METRICS_PORT_BASE = 9100


def _build_node_exec_start(
    bootstrap_peers,
    root_dir,
    log_dir=None,
    port=None,
    log_level=None,
    testnet=False,
    upgrade_channel=None,
    metrics_port=None,
    rewards_address=DEFAULT_REWARDS_ADDRESS,
    evm_network="arbitrum-sepolia",
):
    """Build the ExecStart command line for a node service.

    Args:
        bootstrap_peers: List of "ip:port" strings for all bootstrap nodes.
    """
    parts = [BINARY_INSTALL_PATH]
    parts.append(f"--root-dir {root_dir}")
    for peer in bootstrap_peers:
        parts.append(f"--bootstrap {peer}")
    if port is not None:
        parts.append(f"--port {port}")
    # --enable-logging must precede the other log-related flags, otherwise
    # ant-node silently ignores them.
    parts.append("--enable-logging")
    if log_level:
        parts.append(f"--log-level {log_level}")
    parts.append("--log-format json")
    if log_dir:
        parts.append(f"--log-dir {log_dir}")
    if testnet:
        parts.append("--network-mode testnet")
    if upgrade_channel:
        parts.append(f"--upgrade-channel {upgrade_channel}")
    parts.append("--stop-on-upgrade")
    if evm_network:
        parts.append(f"--evm-network {evm_network}")
    if metrics_port is not None:
        parts.append(f"--metrics-port {metrics_port}")
    parts.append(f"--rewards-address {rewards_address}")
    return " ".join(parts)


def _build_node_unit_file(
    service_name,
    exec_start,
    is_nat=False,
    restart_policy="on-success",
    restart_sec=5,
    limit_nofile=None,
):
    """Build the systemd unit file content for a node service.

    When ``is_nat=True``, wrap ExecStart with ``ip netns exec ant_nat`` so the
    process runs inside the port-restricted-NAT namespace, and tag the
    Description so the NAT status is visible in ``systemctl status``. All
    other unit settings and the ant-node CLI flags are identical to the
    non-NAT unit by design — the reference SKILL.md only dictates the NAT
    mechanism, not the rest of the unit file.

    ``restart_policy``, ``restart_sec`` and ``limit_nofile`` are knobs the
    prod-migration command needs to match the existing prod fleet's units
    (``always`` / ``10`` / ``65536``); defaults preserve testnet behaviour.
    """
    description_suffix = " (port-restricted NAT)" if is_nat else ""
    wrapped_exec_start = (
        f"{NAT_IP_EXEC_PREFIX}{exec_start}" if is_nat else exec_start
    )
    limit_line = f"\nLimitNOFILE={limit_nofile}" if limit_nofile else ""
    return f"""\
[Unit]
Description=Ant Node ({service_name}){description_suffix}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={wrapped_exec_start}
Restart={restart_policy}
RestartSec={restart_sec}{limit_line}

[Install]
WantedBy=multi-user.target
"""


class AntNodeProvisioner:
    """Provisions ant-node services on multiple hosts using Pyinfra.

    NAT hosts (IPs in ``nat_ips``) get an extra namespace+iptables setup step
    and a wrapped systemd ExecStart. Services are ``enable``d but never
    started — ``nodes start`` is run afterwards to bring them up once NAT
    rules are in place.
    """

    def __init__(
        self,
        host_ips: list[str],
        bootstrap_peers: list[str],
        ssh_key_path: str = "~/.ssh/id_rsa",
        node_count: int = 1,
        initial_port: int | None = None,
        log_level: str | None = None,
        testnet: bool = False,
        console: Console | None = None,
        binary_url: str | None = None,
        binary_is_archive: bool = True,
        upgrade_channel: str | None = None,
        rewards_address: str = DEFAULT_REWARDS_ADDRESS,
        nat_ips: set[str] | None = None,
        clock_skew_ips: set[str] | None = None,
        full_ips: set[str] | None = None,
        full_headroom_mb: int = DEFAULT_FULL_HEADROOM_MB,
        evm_network: str | None = "arbitrum-sepolia",
        restart_policy: str = "on-success",
        restart_sec: int = 5,
        limit_nofile: int | None = None,
        extra_op_providers: list | None = None,
    ):
        self.host_ips = host_ips
        self.bootstrap_peers = bootstrap_peers
        self.ssh_key_path = ssh_key_path
        self.node_count = node_count
        self.initial_port = initial_port
        self.log_level = log_level
        self.testnet = testnet
        self.console = console or Console()
        self.binary_url = binary_url
        self.binary_is_archive = binary_is_archive
        self.upgrade_channel = upgrade_channel
        self.rewards_address = rewards_address
        self.nat_ips = set(nat_ips) if nat_ips else set()
        self.clock_skew_ips = set(clock_skew_ips) if clock_skew_ips else set()
        self.full_ips = set(full_ips) if full_ips else set()
        self.full_headroom_mb = full_headroom_mb
        self.evm_network = evm_network
        self.restart_policy = restart_policy
        self.restart_sec = restart_sec
        self.limit_nofile = limit_nofile
        # Optional providers (e.g. Telegraf forwarders) whose ops are folded
        # into this provisioner's single pyinfra pass, so node provisioning and
        # Telegraf install happen in ONE pass over the node VMs. Each exposes
        # ``add_ops(state, install=...)``; the first installs the Telegraf deb.
        self.extra_op_providers = list(extra_op_providers or [])

    def _build_unit_commands(self, is_nat: bool) -> list[str]:
        """Build the list of `cat > unit-file` commands for one host variant."""
        commands = []
        for i in range(self.node_count):
            service_name = f"ant-node-{i + 1}"
            node_port = (self.initial_port + i) if self.initial_port is not None else None
            root_dir = f"{MOUNT_POINT}/data/{service_name}"
            log_dir = f"{LOG_DIR}/{service_name}"
            metrics_port = METRICS_PORT_BASE + i
            exec_start = _build_node_exec_start(
                bootstrap_peers=self.bootstrap_peers,
                root_dir=root_dir,
                log_dir=log_dir,
                port=node_port,
                log_level=self.log_level,
                testnet=self.testnet,
                upgrade_channel=self.upgrade_channel,
                metrics_port=metrics_port,
                rewards_address=self.rewards_address,
                evm_network=self.evm_network,
            )
            unit_content = _build_node_unit_file(
                service_name,
                exec_start,
                is_nat=is_nat,
                restart_policy=self.restart_policy,
                restart_sec=self.restart_sec,
                limit_nofile=self.limit_nofile,
            )
            unit_path = f"/etc/systemd/system/{service_name}.service"
            commands.append(f"cat > {unit_path} << 'UNIT_EOF'\n{unit_content}UNIT_EOF")
        return commands

    def execute(self) -> None:
        """Provision all hosts with ant-node services."""
        if self.binary_url:
            download_url = self.binary_url
            self.console.print(f"Using binary URL: {download_url}")
        else:
            self.console.print("Fetching latest release from GitHub...")
            download_url = get_release_url()
            self.console.print(f"  Release URL: {download_url}")

        if self.nat_ips:
            self.console.print(
                f"[cyan]NAT simulation enabled on {len(self.nat_ips)}/"
                f"{len(self.host_ips)} host(s).[/cyan]"
            )

        if self.clock_skew_ips:
            self.console.print(
                f"[cyan]Clock skew enabled on {len(self.clock_skew_ips)}/"
                f"{len(self.host_ips)} host(s).[/cyan]"
            )

        if self.full_ips:
            self.console.print(
                f"[cyan]Storage-full simulation enabled on {len(self.full_ips)}/"
                f"{len(self.host_ips)} host(s) "
                f"(headroom {self.full_headroom_mb} MiB).[/cyan]"
            )

        hosts_data = [
            (ip, {"ssh_user": "root", "ssh_key": self.ssh_key_path}) for ip in self.host_ips
        ]
        inventory = Inventory((hosts_data, {}))
        config = Config()
        state = State(inventory=inventory, config=config)

        progress = create_progress_handler(self.console)
        state.add_callback_handler(progress)

        if isinstance(progress, RichLiveProgressHandler):
            progress._live.start()

        try:
            self.console.print(f"Connecting to {len(self.host_ips)} host(s) as root...")
            connect_all(state)

            active_hosts = list(state.inventory.get_active_hosts())
            nat_hosts = [h for h in active_hosts if h.name in self.nat_ips]
            plain_hosts = [h for h in active_hosts if h.name not in self.nat_ips]
            skew_hosts = [h for h in active_hosts if h.name in self.clock_skew_ips]
            full_hosts = [h for h in active_hosts if h.name in self.full_ips]

            add_op(
                state,
                server.shell,
                name=DISABLE_UNATTENDED_UPGRADES_OP_NAME,
                commands=[build_disable_unattended_upgrades_command()],
            )

            add_op(
                state,
                server.shell,
                name="Mount attached storage volume",
                commands=[build_mount_volume_command()],
            )

            mkdir_commands = [f"mkdir -p {LOG_DIR}"]
            for i in range(self.node_count):
                mkdir_commands.append(f"mkdir -p {LOG_DIR}/ant-node-{i + 1}")

            add_op(
                state,
                server.shell,
                name="Create log directories",
                commands=mkdir_commands,
            )

            if self.binary_is_archive:
                install_cmd = (
                    f"test -f {BINARY_INSTALL_PATH} "
                    f"&& echo 'ANT_BINARY:SKIP' || "
                    f"(wget -q {download_url} -O /tmp/{RELEASE_ASSET_NAME} && "
                    f"tar -xzf /tmp/{RELEASE_ASSET_NAME} -C /tmp/ && "
                    f"mv /tmp/ant-node {BINARY_INSTALL_PATH} && "
                    f"chmod +x {BINARY_INSTALL_PATH} && "
                    f"rm -f /tmp/{RELEASE_ASSET_NAME} && "
                    f"echo 'ANT_BINARY:INSTALLED')"
                )
            else:
                install_cmd = (
                    f"test -f {BINARY_INSTALL_PATH} "
                    f"&& echo 'ANT_BINARY:SKIP' || "
                    f"(wget -q {download_url} -O {BINARY_INSTALL_PATH} && "
                    f"chmod +x {BINARY_INSTALL_PATH} && "
                    f"echo 'ANT_BINARY:INSTALLED')"
                )
            install_results = add_op(
                state,
                server.shell,
                name="Download and install ant-node binary",
                commands=[install_cmd],
            )

            # Inject clock skew per-host: each selected host shifts by a fixed
            # magnitude in a per-host direction, so we add one op per host.
            for host in skew_hosts:
                direction = skew_direction_for_ip(host.name)
                offset_seconds = direction * SKEW_HOURS * 3600
                add_op(
                    state,
                    server.shell,
                    name=f"Inject clock skew ({'fast' if direction >= 0 else 'slow'})",
                    commands=[build_clock_skew_command(offset_seconds)],
                    host=[host],
                )

            # Pre-fill the data volume on storage-full hosts. Must run AFTER the
            # "Mount attached storage volume" op so it consumes the volume, not
            # the root disk. Orthogonal to NAT/skew — unit files are unchanged.
            if full_hosts:
                add_op(
                    state,
                    server.shell,
                    name="Pre-fill data volume (storage-full simulation)",
                    commands=[build_fill_storage_command(self.full_headroom_mb)],
                    host=full_hosts,
                )

            if nat_hosts:
                add_op(
                    state,
                    server.shell,
                    name="Configure port-restricted NAT (netns + iptables)",
                    commands=[build_nat_setup_command()],
                    host=nat_hosts,
                )
                add_op(
                    state,
                    server.shell,
                    name="Write systemd unit files (NAT variant)",
                    commands=self._build_unit_commands(is_nat=True),
                    host=nat_hosts,
                )

            if plain_hosts:
                add_op(
                    state,
                    server.shell,
                    name="Write systemd unit files",
                    commands=self._build_unit_commands(is_nat=False),
                    host=plain_hosts,
                )

            service_names = [f"ant-node-{i + 1}" for i in range(self.node_count)]
            enable_commands = ["systemctl daemon-reload"]
            for service_name in service_names:
                enable_commands.append(
                    f"systemctl is-enabled --quiet {service_name} "
                    f"&& echo 'ANT_SVC:ENABLED_ALREADY:{service_name}' "
                    f"|| (systemctl enable {service_name} "
                    f"&& echo 'ANT_SVC:ENABLED:{service_name}')"
                )

            svc_results = add_op(
                state,
                server.shell,
                name="Enable node services (not started — use nodes start)",
                commands=enable_commands,
            )

            # Fold in any extra op providers (Telegraf forwarders) so they ride
            # the same SSH pass. The first installs the Telegraf deb; the rest
            # reuse it. Ops are added after the node ops, so the node services
            # are already configured before Telegraf starts tailing/scraping.
            for idx, provider in enumerate(self.extra_op_providers):
                provider.add_ops(state, install=idx == 0)

            run_ops(state)

            if isinstance(progress, RichLiveProgressHandler):
                progress.mark_all_done()
        finally:
            disconnect_all(state)
            if isinstance(progress, RichLiveProgressHandler):
                progress._live.stop()

        failed = state.failed_hosts
        total = len(self.host_ips)
        succeeded = total - len(failed)
        self.console.print()
        self.console.print(
            f"[bold]Provisioning complete: {succeeded}/{total} hosts succeeded, "
            f"{self.node_count} node(s) per host[/bold]"
        )
        if failed:
            for host in failed:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError(f"{len(failed)} host(s) failed provisioning")

        self._report_results(install_results, svc_results)
        self.console.print(
            "[dim]Services enabled but not started. "
            "Run `saorsa-deploy nodes start --name <name>` to bring them up.[/dim]"
        )

    def _report_results(self, install_results, svc_results):
        """Print post-execution summary with idempotency information."""
        try:
            hosts = list(install_results.keys())
        except (TypeError, AttributeError):
            return

        binary_installed = 0
        binary_skipped = 0
        svcs_enabled = 0
        svcs_already = 0

        for host in hosts:
            install_meta = install_results[host]
            for line in install_meta.stdout_lines:
                if "ANT_BINARY:SKIP" in line:
                    binary_skipped += 1
                elif "ANT_BINARY:INSTALLED" in line:
                    binary_installed += 1

            svc_meta = svc_results[host]
            for line in svc_meta.stdout_lines:
                if "ANT_SVC:ENABLED_ALREADY:" in line:
                    svcs_already += 1
                elif "ANT_SVC:ENABLED:" in line:
                    svcs_enabled += 1

        total_hosts = len(hosts)
        if binary_skipped == total_hosts:
            self.console.print("  Binary: already installed on all hosts")
        elif binary_installed == total_hosts:
            self.console.print("  Binary: installed on all hosts")
        else:
            self.console.print(
                f"  Binary: installed on {binary_installed}, already installed on {binary_skipped}"
            )

        total_svcs = svcs_enabled + svcs_already
        if svcs_already == total_svcs and total_svcs > 0:
            self.console.print(f"  Services: all {svcs_already} already enabled")
        elif svcs_enabled == total_svcs and total_svcs > 0:
            self.console.print(f"  Services: {svcs_enabled} enabled")
        elif total_svcs > 0:
            self.console.print(
                f"  Services: {svcs_enabled} enabled, {svcs_already} already enabled"
            )
