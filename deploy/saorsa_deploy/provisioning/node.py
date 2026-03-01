from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.binary_source import RELEASE_ASSET_NAME, get_release_url
from saorsa_deploy.provisioning.genesis import BINARY_INSTALL_PATH
from saorsa_deploy.provisioning.progress import (
    RichLiveProgressHandler,
    create_progress_handler,
)


def _build_node_exec_start(
    bootstrap_ip,
    bootstrap_port,
    port=None,
    ip_version="ipv4",
    log_level=None,
    testnet=False,
):
    """Build the ExecStart command line for a node service."""
    parts = [BINARY_INSTALL_PATH]
    parts.append(f"--bootstrap {bootstrap_ip}:{bootstrap_port}")
    if port is not None:
        parts.append(f"--port {port}")
    if ip_version:
        parts.append(f"--ip-version {ip_version}")
    if log_level:
        parts.append(f"--log-level {log_level}")
    parts.append("--disable-payment-verification")
    if testnet:
        parts.append("--network-mode testnet")
    return " ".join(parts)


def _build_node_unit_file(service_name, exec_start):
    """Build the systemd unit file content for a node service."""
    return f"""\
[Unit]
Description=Saorsa Node ({service_name})
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


class SaorsaNodeProvisioner:
    """Provisions saorsa-node services on multiple hosts using Pyinfra."""

    def __init__(
        self,
        host_ips: list[str],
        bootstrap_ip: str,
        bootstrap_port: int,
        ssh_key_path: str = "~/.ssh/id_rsa",
        node_count: int = 1,
        initial_port: int | None = None,
        ip_version: str = "ipv4",
        log_level: str | None = None,
        testnet: bool = False,
        console: Console | None = None,
        binary_url: str | None = None,
        binary_is_archive: bool = True,
    ):
        self.host_ips = host_ips
        self.bootstrap_ip = bootstrap_ip
        self.bootstrap_port = bootstrap_port
        self.ssh_key_path = ssh_key_path
        self.node_count = node_count
        self.initial_port = initial_port
        self.ip_version = ip_version
        self.log_level = log_level
        self.testnet = testnet
        self.console = console or Console()
        self.binary_url = binary_url
        self.binary_is_archive = binary_is_archive

    def execute(self) -> None:
        """Provision all hosts with saorsa-node services."""
        if self.binary_url:
            download_url = self.binary_url
            self.console.print(f"Using binary URL: {download_url}")
        else:
            self.console.print("Fetching latest release from GitHub...")
            download_url = get_release_url()
            self.console.print(f"  Release URL: {download_url}")

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

            unit_commands = []
            service_names = []
            for i in range(self.node_count):
                service_name = f"saorsa-node-{i + 1}"
                service_names.append(service_name)
                node_port = (self.initial_port + i) if self.initial_port is not None else None
                exec_start = _build_node_exec_start(
                    bootstrap_ip=self.bootstrap_ip,
                    bootstrap_port=self.bootstrap_port,
                    port=node_port,
                    ip_version=self.ip_version,
                    log_level=self.log_level,
                    testnet=self.testnet,
                )
                unit_content = _build_node_unit_file(service_name, exec_start)
                unit_path = f"/etc/systemd/system/{service_name}.service"
                unit_commands.append(f"cat > {unit_path} << 'UNIT_EOF'\n{unit_content}UNIT_EOF")

            add_op(
                state,
                server.shell,
                name="Write systemd unit files",
                commands=unit_commands,
            )

            enable_commands = ["systemctl daemon-reload"]
            for service_name in service_names:
                enable_commands.append(
                    f"systemctl is-active --quiet {service_name} "
                    f"&& echo 'SAORSA_SVC:RUNNING:{service_name}' "
                    f"|| (systemctl enable --now {service_name} "
                    f"&& echo 'SAORSA_SVC:STARTED:{service_name}')"
                )

            svc_results = add_op(
                state,
                server.shell,
                name="Enable and start node services",
                commands=enable_commands,
            )

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

    def _report_results(self, install_results, svc_results):
        """Print post-execution summary with idempotency information."""
        try:
            hosts = list(install_results.keys())
        except (TypeError, AttributeError):
            return

        binary_installed = 0
        binary_skipped = 0
        svcs_started = 0
        svcs_running = 0

        for host in hosts:
            install_meta = install_results[host]
            for line in install_meta.stdout_lines:
                if "SAORSA_BINARY:SKIP" in line:
                    binary_skipped += 1
                elif "SAORSA_BINARY:INSTALLED" in line:
                    binary_installed += 1

            svc_meta = svc_results[host]
            for line in svc_meta.stdout_lines:
                if "SAORSA_SVC:RUNNING:" in line:
                    svcs_running += 1
                elif "SAORSA_SVC:STARTED:" in line:
                    svcs_started += 1

        total_hosts = len(hosts)
        if binary_skipped == total_hosts:
            self.console.print("  Binary: already installed on all hosts")
        elif binary_installed == total_hosts:
            self.console.print("  Binary: installed on all hosts")
        else:
            self.console.print(
                f"  Binary: installed on {binary_installed}, already installed on {binary_skipped}"
            )

        total_svcs = svcs_started + svcs_running
        if svcs_running == total_svcs and total_svcs > 0:
            self.console.print(f"  Services: all {svcs_running} already running")
        elif svcs_started == total_svcs and total_svcs > 0:
            self.console.print(f"  Services: {svcs_started} started")
        elif total_svcs > 0:
            self.console.print(
                f"  Services: {svcs_started} started, {svcs_running} already running"
            )
