import time
from io import StringIO

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import files, server, systemd
from rich.console import Console

from saorsa_deploy.binary_source import RELEASE_ASSET_NAME, get_release_url
from saorsa_deploy.provisioning.apt import (
    DISABLE_UNATTENDED_UPGRADES_OP_NAME,
    SET_HOSTNAME_OP_NAME,
    build_disable_unattended_upgrades_command,
    build_set_hostname_command,
)
from saorsa_deploy.provisioning.authorized_keys import add_operator_authorized_keys_op
from saorsa_deploy.provisioning.progress import (
    OpTimingHandler,
    RichLiveProgressHandler,
    create_progress_handler,
    print_op_timing_summary,
)
from saorsa_deploy.provisioning.shell_ops import as_single_shell_command
from saorsa_deploy.provisioning.volume import LOG_DIR, MOUNT_POINT, build_mount_volume_command

BINARY_INSTALL_PATH = "/usr/local/bin/ant-node"

DEFAULT_REWARDS_ADDRESS = "0x03B770D9cD32077cC0bF330c13C114a87643B124"


def _build_binary_install_command(download_url: str, is_archive: bool) -> str:
    """Return the idempotent shell command that installs the ant-node binary.

    Skips the download when the binary is already present (``test -f``), so a
    re-run is a no-op. Shared by the single-host and multi-host bootstrap
    provisioners so both stay byte-identical.
    """
    if is_archive:
        return (
            f"test -f {BINARY_INSTALL_PATH} "
            f"&& echo 'ANT_BINARY:SKIP' || "
            f"(wget -q {download_url} -O /tmp/{RELEASE_ASSET_NAME} && "
            f"tar -xzf /tmp/{RELEASE_ASSET_NAME} -C /tmp/ && "
            f"mv /tmp/ant-node {BINARY_INSTALL_PATH} && "
            f"chmod +x {BINARY_INSTALL_PATH} && "
            f"rm -f /tmp/{RELEASE_ASSET_NAME} && "
            f"echo 'ANT_BINARY:INSTALLED')"
        )
    return (
        f"test -f {BINARY_INSTALL_PATH} "
        f"&& echo 'ANT_BINARY:SKIP' || "
        f"(wget -q {download_url} -O {BINARY_INSTALL_PATH} && "
        f"chmod +x {BINARY_INSTALL_PATH} && "
        f"echo 'ANT_BINARY:INSTALLED')"
    )


def _build_exec_start(
    bootstrap_index,
    bootstrap_peers=None,
    port=None,
    log_level=None,
    testnet=False,
    upgrade_channel=None,
    rewards_address=DEFAULT_REWARDS_ADDRESS,
    evm_network="arbitrum-sepolia",
    webrtc_port=None,
) -> str:
    """Build the ExecStart command line for a bootstrap node service.

    Args:
        bootstrap_index: Node index (1 = genesis with no bootstrap, 2+ = uses bootstrap_peers).
        bootstrap_peers: List of "ip:port" strings for the genesis node.
            Required for index > 1, ignored for index 1 (genesis).
        webrtc_port: When set, pin the WebRTC-Direct listener to this UDP port
            via ``--webrtc-port``. Bootstraps are the one tier where the browser
            address must not move: a web client seeds itself from a bootstrap
            multiaddr, and ant-node otherwise *derives* the port from the QUIC
            port (``automatic_webrtc_port``), which silently changes the
            advertised address if the QUIC port ever changes. Left unset the
            flag is omitted entirely, so the rendered unit is byte-for-byte
            what it was before this option existed.

            NB the port is only half of a stable address -- the multiaddr also
            embeds the DTLS certificate hash. ant-node persists that
            certificate at ``<root_dir>/webrtc-direct.pem``, so it survives a
            restart, but wiping a bootstrap's data directory rotates it and
            invalidates every cached browser address.
    """
    service_name = f"ant-bootstrap-{bootstrap_index}"
    root_dir = f"{MOUNT_POINT}/data/{service_name}"

    parts = [BINARY_INSTALL_PATH]
    parts.append(f"--root-dir {root_dir}")
    if bootstrap_index > 1 and bootstrap_peers:
        for peer in bootstrap_peers:
            parts.append(f"--bootstrap {peer}")
    if port:
        parts.append(f"--port {port}")
    if webrtc_port:
        parts.append(f"--webrtc-port {webrtc_port}")
    # --enable-logging must precede the other log-related flags, otherwise
    # ant-node silently ignores them.
    parts.append("--enable-logging")
    if log_level:
        parts.append(f"--log-level {log_level}")
    parts.append("--log-format json")
    parts.append(f"--log-dir {LOG_DIR}/{service_name}")
    if testnet:
        parts.append("--network-mode testnet")
    if upgrade_channel:
        parts.append(f"--upgrade-channel {upgrade_channel}")
    parts.append("--stop-on-upgrade")
    if evm_network:
        parts.append(f"--evm-network {evm_network}")
    parts.append(f"--rewards-address {rewards_address}")
    return " ".join(parts)


def _build_unit_file(service_name: str, exec_start: str) -> str:
    """Build the systemd unit file content for a bootstrap node.

    ``RequiresMountsFor`` stops the node starting before its data volume is
    mounted; see :func:`saorsa_deploy.provisioning.node._build_node_unit_file`
    for why it is unconditional and what it costs.
    """
    return f"""\
[Unit]
Description=Ant Bootstrap Node ({service_name})
After=network-online.target
Wants=network-online.target
RequiresMountsFor={MOUNT_POINT}

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-success
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


class AntBootstrapNodeProvisioner:
    """Provisions a bootstrap node on a remote host using Pyinfra.

    For the genesis node (bootstrap_index=1), no --bootstrap argument is used.
    For additional bootstrap nodes (index 2+), the genesis address is used as --bootstrap.
    """

    def __init__(
        self,
        ip: str,
        bootstrap_index: int,
        ssh_key_path: str = "~/.ssh/id_rsa",
        bootstrap_peers: list[str] | None = None,
        port: int | None = None,
        log_level: str | None = None,
        testnet: bool = False,
        console: Console | None = None,
        binary_url: str | None = None,
        binary_is_archive: bool = True,
        upgrade_channel: str | None = None,
        rewards_address: str = DEFAULT_REWARDS_ADDRESS,
        testnet_name: str | None = None,
        extra_op_providers: list | None = None,
        webrtc_port: int | None = None,
    ):
        self.ip = ip
        self.bootstrap_index = bootstrap_index
        self.ssh_key_path = ssh_key_path
        self.bootstrap_peers = bootstrap_peers or []
        self.port = port
        self.log_level = log_level
        self.testnet = testnet
        self.console = console or Console()
        self.binary_url = binary_url
        self.binary_is_archive = binary_is_archive
        self.upgrade_channel = upgrade_channel
        self.rewards_address = rewards_address
        self.testnet_name = testnet_name
        self.webrtc_port = webrtc_port
        # Telegraf forwarders folded into this single-host pyinfra pass.
        self.extra_op_providers = list(extra_op_providers or [])

    @property
    def hostname(self) -> str | None:
        """Inventory-matching OS hostname, or ``None`` if no testnet_name given."""
        if not self.testnet_name:
            return None
        return f"{self.testnet_name}-ant-bootstrap-{self.bootstrap_index}"

    @property
    def service_name(self) -> str:
        return f"ant-bootstrap-{self.bootstrap_index}"

    def execute(self) -> None:
        """Download the ant-node binary, install it, and start the bootstrap service."""
        if self.binary_url:
            download_url = self.binary_url
            self.console.print(f"Using binary URL: {download_url}")
        else:
            self.console.print("Fetching latest release from GitHub...")
            download_url = get_release_url()
            self.console.print(f"  Release URL: {download_url}")

        exec_start = _build_exec_start(
            bootstrap_index=self.bootstrap_index,
            bootstrap_peers=self.bootstrap_peers,
            port=self.port,
            log_level=self.log_level,
            testnet=self.testnet,
            upgrade_channel=self.upgrade_channel,
            rewards_address=self.rewards_address,
            webrtc_port=self.webrtc_port,
        )
        unit_content = _build_unit_file(self.service_name, exec_start)
        unit_file_path = f"/etc/systemd/system/{self.service_name}.service"

        self.console.print(f"Connecting to {self.ip} as root...")
        inventory = Inventory(
            (
                [
                    (
                        self.ip,
                        {"ssh_user": "root", "ssh_key": self.ssh_key_path},
                    ),
                ],
                {},
            ),
        )
        state = State(inventory=inventory, config=Config())
        timing = OpTimingHandler()
        state.add_callback_handler(timing)
        _connect_start = time.monotonic()
        connect_all(state)
        connect_seconds = time.monotonic() - _connect_start

        try:
            add_operator_authorized_keys_op(state)

            add_op(
                state,
                server.shell,
                name=DISABLE_UNATTENDED_UPGRADES_OP_NAME,
                commands=[build_disable_unattended_upgrades_command()],
            )

            if self.hostname:
                add_op(
                    state,
                    server.shell,
                    name=SET_HOSTNAME_OP_NAME,
                    commands=[build_set_hostname_command(self.hostname)],
                )

            add_op(
                state,
                server.shell,
                name="Mount attached storage volume",
                commands=[build_mount_volume_command()],
            )

            add_op(
                state,
                server.shell,
                name="Create log directory",
                commands=[f"mkdir -p {LOG_DIR}/{self.service_name}"],
            )

            install_cmd = _build_binary_install_command(
                download_url, self.binary_is_archive
            )
            add_op(
                state,
                server.shell,
                name="Download and install ant-node binary",
                commands=[install_cmd],
            )

            add_op(
                state,
                files.put,
                name="Write systemd unit file",
                src=StringIO(unit_content),
                dest=unit_file_path,
                mode="644",
                add_deploy_dir=False,
            )

            add_op(
                state,
                systemd.daemon_reload,
                name="Reload systemd daemon",
            )

            add_op(
                state,
                systemd.service,
                name=f"Enable and start {self.service_name}",
                service=self.service_name,
                running=True,
                enabled=True,
            )

            for idx, provider in enumerate(self.extra_op_providers):
                provider.add_ops(state, install=idx == 0)

            self.console.print("Running provisioning operations...")
            run_ops(state)
        finally:
            disconnect_all(state)

        print_op_timing_summary(
            self.console,
            timing,
            title=f"provision-genesis ({self.service_name})",
            connect_seconds=connect_seconds,
        )
        self.console.print(
            f"[green]Bootstrap #{self.bootstrap_index} ({self.service_name}) "
            f"provisioned at {self.ip}[/green]"
        )


class AntBootstrapNodesProvisioner:
    """Provisions multiple additional bootstrap nodes (index 2+) in parallel.

    Mirrors :class:`~saorsa_deploy.provisioning.node.AntNodeProvisioner`: every
    host lives in a single pyinfra ``Inventory`` so pyinfra connects and runs
    operations across all hosts concurrently, and a
    :class:`~saorsa_deploy.provisioning.progress.RichLiveProgressHandler` renders
    a live per-host progress table (the same table ``nodes provision`` shows).

    Each bootstrap host differs only by its ``bootstrap_index`` — which drives
    the service name (``ant-bootstrap-{index}``), unit-file contents, log
    directory and OS hostname — so those steps are added as per-host operations
    (``add_op(..., host=[host])``) while the host-agnostic steps (disable
    unattended-upgrades, mount the data volume, install the binary, reload
    systemd) are added once for the whole fleet.

    Genesis (index 1) is NOT handled here — it is provisioned on its own by the
    single-host :class:`AntBootstrapNodeProvisioner` via ``nodes provision-genesis``.
    """

    def __init__(
        self,
        nodes: list[dict],
        bootstrap_peers: list[str],
        ssh_key_path: str = "~/.ssh/id_rsa",
        port: int | None = None,
        log_level: str | None = None,
        testnet: bool = False,
        console: Console | None = None,
        binary_url: str | None = None,
        binary_is_archive: bool = True,
        upgrade_channel: str | None = None,
        rewards_address: str = DEFAULT_REWARDS_ADDRESS,
        testnet_name: str | None = None,
        extra_op_providers: list | None = None,
        webrtc_port: int | None = None,
    ):
        """``nodes`` is a list of dicts, each with ``ip`` and ``bootstrap_index``
        (``region`` is accepted and ignored — it is only used for display by the
        caller)."""
        self.nodes = nodes
        self.bootstrap_peers = bootstrap_peers
        self.ssh_key_path = ssh_key_path
        self.port = port
        self.log_level = log_level
        self.testnet = testnet
        self.console = console or Console()
        self.binary_url = binary_url
        self.binary_is_archive = binary_is_archive
        self.upgrade_channel = upgrade_channel
        self.rewards_address = rewards_address
        self.testnet_name = testnet_name
        self.webrtc_port = webrtc_port
        self._index_by_ip = {n["ip"]: n["bootstrap_index"] for n in nodes}
        # Telegraf forwarders folded into this single pyinfra pass over the fleet.
        self.extra_op_providers = list(extra_op_providers or [])

    def _service_name(self, bootstrap_index: int) -> str:
        return f"ant-bootstrap-{bootstrap_index}"

    def _hostname(self, bootstrap_index: int) -> str | None:
        if not self.testnet_name:
            return None
        return f"{self.testnet_name}-ant-bootstrap-{bootstrap_index}"

    def _build_config_commands(self, bootstrap_index: int) -> list[str]:
        """Per-host setup: hostname, log dir, and the systemd unit file.

        Runs after the shared "mount data volume" op so the log directory lands
        on the mounted volume, mirroring the single-host provisioner's ordering.
        """
        service_name = self._service_name(bootstrap_index)
        exec_start = _build_exec_start(
            bootstrap_index=bootstrap_index,
            bootstrap_peers=self.bootstrap_peers,
            port=self.port,
            log_level=self.log_level,
            testnet=self.testnet,
            upgrade_channel=self.upgrade_channel,
            rewards_address=self.rewards_address,
            webrtc_port=self.webrtc_port,
        )
        unit_content = _build_unit_file(service_name, exec_start)
        unit_path = f"/etc/systemd/system/{service_name}.service"

        commands = []
        hostname = self._hostname(bootstrap_index)
        if hostname:
            commands.append(build_set_hostname_command(hostname))
        commands.append(f"mkdir -p {LOG_DIR}/{service_name}")
        commands.append(f"cat > {unit_path} << 'UNIT_EOF'\n{unit_content}UNIT_EOF")
        return commands

    def execute(self) -> None:
        """Provision every bootstrap host concurrently in one pyinfra session."""
        if self.binary_url:
            download_url = self.binary_url
            self.console.print(f"Using binary URL: {download_url}")
        else:
            self.console.print("Fetching latest release from GitHub...")
            download_url = get_release_url()
            self.console.print(f"  Release URL: {download_url}")

        hosts_data = [
            (n["ip"], {"ssh_user": "root", "ssh_key": self.ssh_key_path})
            for n in self.nodes
        ]
        inventory = Inventory((hosts_data, {}))
        state = State(inventory=inventory, config=Config())

        progress = create_progress_handler(self.console)
        state.add_callback_handler(progress)
        timing = OpTimingHandler()
        state.add_callback_handler(timing)
        if isinstance(progress, RichLiveProgressHandler):
            progress._live.start()

        connect_seconds = None
        try:
            self.console.print(
                f"Connecting to {len(self.nodes)} bootstrap host(s) as root..."
            )
            _connect_start = time.monotonic()
            connect_all(state)
            connect_seconds = time.monotonic() - _connect_start

            active_hosts = list(state.inventory.get_active_hosts())

            # Host-agnostic steps for the whole fleet.
            add_operator_authorized_keys_op(state)
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
            add_op(
                state,
                server.shell,
                name="Download and install ant-node binary",
                commands=[
                    _build_binary_install_command(download_url, self.binary_is_archive)
                ],
            )

            # Per-host setup (hostname / log dir / unit file): each host's
            # service name embeds its bootstrap index, so these must be targeted.
            for host in active_hosts:
                index = self._index_by_ip[host.name]
                add_op(
                    state,
                    server.shell,
                    name=f"Configure {self._service_name(index)}",
                    commands=[as_single_shell_command(self._build_config_commands(index))],
                    host=[host],
                )

            # One daemon-reload for the fleet, after all unit files are written.
            add_op(
                state,
                systemd.daemon_reload,
                name="Reload systemd daemon",
            )

            # Enable + start each host's service.
            for host in active_hosts:
                index = self._index_by_ip[host.name]
                service_name = self._service_name(index)
                add_op(
                    state,
                    systemd.service,
                    name=f"Enable and start {service_name}",
                    service=service_name,
                    running=True,
                    enabled=True,
                    host=[host],
                )

            for idx, provider in enumerate(self.extra_op_providers):
                provider.add_ops(state, install=idx == 0)

            self.console.print("Running provisioning operations...")
            run_ops(state)

            if isinstance(progress, RichLiveProgressHandler):
                progress.mark_all_done()
        finally:
            disconnect_all(state)
            if isinstance(progress, RichLiveProgressHandler):
                progress._live.stop()

        failed = state.failed_hosts
        total = len(self.nodes)
        succeeded = total - len(failed)
        self.console.print()
        self.console.print(
            f"[bold]Bootstrap provisioning complete: "
            f"{succeeded}/{total} host(s) succeeded[/bold]"
        )
        print_op_timing_summary(
            self.console,
            timing,
            title="bootstraps provision",
            connect_seconds=connect_seconds,
        )
        if failed:
            for host in failed:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError(f"{len(failed)} bootstrap host(s) failed provisioning")
