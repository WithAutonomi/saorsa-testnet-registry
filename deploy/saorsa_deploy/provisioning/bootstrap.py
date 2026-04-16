from io import StringIO

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import files, server, systemd
from rich.console import Console

from saorsa_deploy.binary_source import RELEASE_ASSET_NAME, get_release_url
from saorsa_deploy.provisioning.volume import LOG_DIR, MOUNT_POINT, build_mount_volume_command

BINARY_INSTALL_PATH = "/usr/local/bin/ant-node"

METRICS_PORT = 9100

DEFAULT_REWARDS_ADDRESS = "0x03B770D9cD32077cC0bF330c13C114a87643B124"


def _build_exec_start(
    bootstrap_index,
    bootstrap_peers=None,
    port=None,
    log_level=None,
    testnet=False,
    upgrade_channel=None,
    metrics_port=METRICS_PORT,
    rewards_address=DEFAULT_REWARDS_ADDRESS,
    evm_network="arbitrum-sepolia",
) -> str:
    """Build the ExecStart command line for a bootstrap node service.

    Args:
        bootstrap_index: Node index (1 = genesis with no bootstrap, 2+ = uses bootstrap_peers).
        bootstrap_peers: List of "ip:port" strings for the genesis node.
            Required for index > 1, ignored for index 1 (genesis).
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
    parts.append(f"--metrics-port {metrics_port}")
    parts.append(f"--rewards-address {rewards_address}")
    return " ".join(parts)


def _build_unit_file(service_name: str, exec_start: str) -> str:
    """Build the systemd unit file content for a bootstrap node."""
    return f"""\
[Unit]
Description=Ant Bootstrap Node ({service_name})
After=network-online.target
Wants=network-online.target

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
        connect_all(state)

        try:
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

            self.console.print("Running provisioning operations...")
            run_ops(state)
        finally:
            disconnect_all(state)

        self.console.print(
            f"[green]Bootstrap #{self.bootstrap_index} ({self.service_name}) "
            f"provisioned at {self.ip}[/green]"
        )
