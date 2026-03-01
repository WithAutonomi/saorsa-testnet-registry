from io import StringIO

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import files, server, systemd
from rich.console import Console

from saorsa_deploy.binary_source import RELEASE_ASSET_NAME, get_release_url

BINARY_INSTALL_PATH = "/usr/local/bin/saorsa-node"
SERVICE_NAME = "saorsa-genesis-node"
UNIT_FILE_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"


def _build_exec_start(port=None, ip_version="ipv4", log_level=None, testnet=False) -> str:
    """Build the ExecStart command line for the systemd service."""
    parts = [BINARY_INSTALL_PATH]
    if port:
        parts.append(f"--port {port}")
    if ip_version:
        parts.append(f"--ip-version {ip_version}")
    if log_level:
        parts.append(f"--log-level {log_level}")
    parts.append("--disable-payment-verification")
    if testnet:
        parts.append("--network-mode testnet")
    return " ".join(parts)


def _build_unit_file(exec_start: str) -> str:
    """Build the systemd unit file content."""
    return f"""\
[Unit]
Description=Saorsa Genesis Node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


class SaorsaGenesisNodeProvisioner:
    """Provisions the genesis node on a remote host using Pyinfra."""

    def __init__(
        self,
        ip: str,
        ssh_key_path: str = "~/.ssh/id_rsa",
        port: int | None = None,
        ip_version: str = "ipv4",
        log_level: str | None = None,
        testnet: bool = False,
        console: Console | None = None,
        binary_url: str | None = None,
        binary_is_archive: bool = True,
    ):
        self.ip = ip
        self.ssh_key_path = ssh_key_path
        self.port = port
        self.ip_version = ip_version
        self.log_level = log_level
        self.testnet = testnet
        self.console = console or Console()
        self.binary_url = binary_url
        self.binary_is_archive = binary_is_archive

    def execute(self) -> None:
        """Download the saorsa-node binary, install it, and start the genesis service."""
        if self.binary_url:
            download_url = self.binary_url
            self.console.print(f"Using binary URL: {download_url}")
        else:
            self.console.print("Fetching latest release from GitHub...")
            download_url = get_release_url()
            self.console.print(f"  Release URL: {download_url}")

        exec_start = _build_exec_start(
            port=self.port,
            ip_version=self.ip_version,
            log_level=self.log_level,
            testnet=self.testnet,
        )
        unit_content = _build_unit_file(exec_start)

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
            if self.binary_is_archive:
                install_cmd = (
                    f"test -f {BINARY_INSTALL_PATH} "
                    f"&& echo 'SAORSA_BINARY:SKIP' || "
                    f"(wget -q {download_url} -O /tmp/{RELEASE_ASSET_NAME} && "
                    f"tar -xzf /tmp/{RELEASE_ASSET_NAME} -C /tmp/ && "
                    f"mv /tmp/saorsa-node {BINARY_INSTALL_PATH} && "
                    f"chmod +x {BINARY_INSTALL_PATH} && "
                    f"rm -f /tmp/{RELEASE_ASSET_NAME} && "
                    f"echo 'SAORSA_BINARY:INSTALLED')"
                )
            else:
                install_cmd = (
                    f"test -f {BINARY_INSTALL_PATH} "
                    f"&& echo 'SAORSA_BINARY:SKIP' || "
                    f"(wget -q {download_url} -O {BINARY_INSTALL_PATH} && "
                    f"chmod +x {BINARY_INSTALL_PATH} && "
                    f"echo 'SAORSA_BINARY:INSTALLED')"
                )
            install_results = add_op(
                state,
                server.shell,
                name="Download and install saorsa-node binary",
                commands=[install_cmd],
            )

            put_results = add_op(
                state,
                files.put,
                name="Write systemd unit file",
                src=StringIO(unit_content),
                dest=UNIT_FILE_PATH,
                mode="644",
                add_deploy_dir=False,
            )

            add_op(
                state,
                systemd.daemon_reload,
                name="Reload systemd daemon",
            )

            svc_results = add_op(
                state,
                systemd.service,
                name="Enable and start genesis node service",
                service=SERVICE_NAME,
                running=True,
                enabled=True,
            )

            self.console.print("Running provisioning operations...")
            run_ops(state)
            self._report_results(install_results, put_results, svc_results)
        finally:
            disconnect_all(state)

    def _report_results(self, install_results, put_results, svc_results):
        """Print post-execution summary with idempotency information."""
        try:
            host = next(iter(install_results))
        except (StopIteration, TypeError):
            return

        install_meta = install_results[host]
        if any("SAORSA_BINARY:SKIP" in line for line in install_meta.stdout_lines):
            self.console.print("  Binary: already installed")
        else:
            self.console.print("  Binary: installed")

        put_meta = put_results[host]
        if put_meta.did_change():
            self.console.print(f"  Unit file: updated ({UNIT_FILE_PATH})")
        else:
            self.console.print("  Unit file: already up to date")

        svc_meta = svc_results[host]
        if svc_meta.did_change():
            self.console.print(f"  Service: {SERVICE_NAME} started and enabled")
        else:
            self.console.print(f"  Service: {SERVICE_NAME} already running")
