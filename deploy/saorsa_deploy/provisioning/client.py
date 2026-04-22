
from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.provisioning.apt import (
    DISABLE_UNATTENDED_UPGRADES_OP_NAME,
    build_disable_unattended_upgrades_command,
)
from saorsa_deploy.provisioning.progress import (
    RichLiveProgressHandler,
    create_progress_handler,
)
from saorsa_deploy.resources import get_resources_dir

ANT_INSTALL_PATH = "/root/.local/bin/ant"
CLIENT_DATA_DIR = "/var/lib/ant-client"
SCRIPTS_DIR = "/root/ant-client"


def _build_env_file(
    bootstrap_peers: list[str],
    file_size_mb: int,
    store_concurrency: int = 8,
    influx_url: str = "",
    influx_token: str = "",
    influx_org: str = "ant",
    influx_bucket: str = "testnet-metrics",
) -> str:
    """Build the environment file content for client systemd services."""
    bootstrap_args = " ".join(f"--bootstrap {peer}" for peer in bootstrap_peers)
    file_size_kb = file_size_mb * 1024
    lines = [
        f'BOOTSTRAP_ARGS="{bootstrap_args}"',
        f"FILE_SIZE_KB={file_size_kb}",
        f"STORE_CONCURRENCY={store_concurrency}",
        "SECRET_KEY=placeholder",
        f"ANT={ANT_INSTALL_PATH}",
        f"ADDRESSES_FILE={CLIENT_DATA_DIR}/addresses.txt",
        f"UPLOAD_DIR={CLIENT_DATA_DIR}/uploads",
        f"DOWNLOAD_DIR={CLIENT_DATA_DIR}/downloads",
    ]
    if influx_url and influx_token:
        lines.append(f"INFLUX_URL={influx_url}")
        lines.append(f"INFLUX_TOKEN={influx_token}")
        lines.append(f"INFLUX_ORG={influx_org}")
        lines.append(f"INFLUX_BUCKET={influx_bucket}")
    return "\n".join(lines) + "\n"


def _build_uploader_unit() -> str:
    """Build the systemd unit file for the ant-uploader service."""
    return f"""\
[Unit]
Description=Ant Client Uploader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/ant-client.env
ExecStart=/bin/bash {SCRIPTS_DIR}/uploader.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


def _build_downloader_unit() -> str:
    """Build the systemd unit file for the ant-downloader service."""
    return f"""\
[Unit]
Description=Ant Client Downloader
After=network-online.target ant-uploader.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/ant-client.env
ExecStart=/bin/bash {SCRIPTS_DIR}/downloader.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


class AntClientProvisioner:
    """Provisions ant client upload/download services on hosts using Pyinfra.

    Each host gets:
    - The ant binary
    - uploader.sh and downloader.sh scripts
    - systemd units for ant-uploader and ant-downloader (disabled by default)
    - Per-host environment file with bootstrap args, file size, and optional InfluxDB config
    """

    def __init__(
        self,
        clients: list[dict],
        bootstrap_peers: list[str],
        ssh_key_path: str = "~/.ssh/id_rsa",
        console: Console | None = None,
        binary_url: str | None = None,
        binary_is_archive: bool = False,
        influx_url: str = "",
        influx_token: str = "",
        influx_org: str = "ant",
        influx_bucket: str = "testnet-metrics",
        wallet_secret_key: str = "",
    ):
        self.clients = clients
        self.bootstrap_peers = bootstrap_peers
        self.ssh_key_path = ssh_key_path
        self.console = console or Console()
        self.binary_url = binary_url
        self.binary_is_archive = binary_is_archive
        self.influx_url = influx_url
        self.influx_token = influx_token
        self.influx_org = influx_org
        self.influx_bucket = influx_bucket
        self.wallet_secret_key = wallet_secret_key

    def execute(self) -> None:
        """Provision all client hosts with uploader/downloader services."""
        host_ips = [c["ip"] for c in self.clients if c.get("ip")]
        if not host_ips:
            raise RuntimeError("No client IPs available for provisioning")

        # Build per-host inventory with host-specific data
        hosts_data = []
        for client in self.clients:
            if not client.get("ip"):
                continue
            hosts_data.append(
                (client["ip"], {"ssh_user": "root", "ssh_key": self.ssh_key_path})
            )

        inventory = Inventory((hosts_data, {}))
        config = Config()
        state = State(inventory=inventory, config=config)

        progress = create_progress_handler(self.console)
        state.add_callback_handler(progress)

        if isinstance(progress, RichLiveProgressHandler):
            progress._live.start()

        try:
            self.console.print(f"Connecting to {len(host_ips)} client host(s) as root...")
            connect_all(state)

            add_op(
                state,
                server.shell,
                name=DISABLE_UNATTENDED_UPGRADES_OP_NAME,
                commands=[build_disable_unattended_upgrades_command()],
            )

            # Create directories
            add_op(
                state,
                server.shell,
                name="Create client directories",
                commands=[
                    f"mkdir -p {CLIENT_DATA_DIR}/uploads "
                    f"{CLIENT_DATA_DIR}/downloads "
                    f"{SCRIPTS_DIR} "
                    f"/root/.local/bin",
                ],
            )

            # Install ant binary
            if self.binary_url:
                download_url = self.binary_url
                self.console.print(f"Using binary URL: {download_url}")
            else:
                from saorsa_deploy.binary_source import get_ant_release_url

                self.console.print("Fetching latest ant release from GitHub...")
                download_url = get_ant_release_url()
                self.console.print(f"  Release URL: {download_url}")

            if self.binary_is_archive:
                install_cmd = (
                    f"test -f {ANT_INSTALL_PATH} "
                    f"&& echo 'ANT_BINARY:SKIP' || "
                    f"(wget -q {download_url} -O /tmp/ant-archive.tar.gz && "
                    f"tar -xzf /tmp/ant-archive.tar.gz -C /tmp/ && "
                    f"find /tmp -name ant -type f -path '*/ant-*/*' "
                    f"-exec mv {{}} {ANT_INSTALL_PATH} \\; && "
                    f"chmod +x {ANT_INSTALL_PATH} && "
                    f"rm -rf /tmp/ant-*/ /tmp/ant-archive.tar.gz && "
                    f"echo 'ANT_BINARY:INSTALLED')"
                )
            else:
                install_cmd = (
                    f"test -f {ANT_INSTALL_PATH} "
                    f"&& echo 'ANT_BINARY:SKIP' || "
                    f"(wget -q {download_url} -O {ANT_INSTALL_PATH} && "
                    f"chmod +x {ANT_INSTALL_PATH} && "
                    f"echo 'ANT_BINARY:INSTALLED')"
                )

            add_op(
                state,
                server.shell,
                name="Download and install ant binary",
                commands=[install_cmd],
            )

            # Deploy scripts
            scripts_dir = get_resources_dir() / "scripts"
            uploader_script = (scripts_dir / "uploader.sh").read_text()
            downloader_script = (scripts_dir / "downloader.sh").read_text()

            add_op(
                state,
                server.shell,
                name="Deploy uploader and downloader scripts",
                commands=[
                    f"cat > {SCRIPTS_DIR}/uploader.sh << 'SCRIPT_EOF'\n"
                    f"{uploader_script}SCRIPT_EOF",
                    f"chmod +x {SCRIPTS_DIR}/uploader.sh",
                    f"cat > {SCRIPTS_DIR}/downloader.sh << 'SCRIPT_EOF'\n"
                    f"{downloader_script}SCRIPT_EOF",
                    f"chmod +x {SCRIPTS_DIR}/downloader.sh",
                ],
            )

            # Write per-host environment files
            # Since Pyinfra runs the same ops on all hosts, we write per-host
            # env files using a shell command that reads the hostname to determine
            # the per-client values. We prepare a mapping script.
            ip_to_env = {
                c["ip"]: {
                    "file_size_kb": c["file_size_mb"] * 1024,
                    "store_concurrency": c.get("store_concurrency", 8),
                }
                for c in self.clients
                if c.get("ip")
            }

            # Build a case statement that maps IP to per-client values
            case_entries = []
            for ip, env in ip_to_env.items():
                case_entries.append(
                    f'    "{ip}") FILE_SIZE_KB={env["file_size_kb"]}; '
                    f'STORE_CONCURRENCY={env["store_concurrency"]} ;;'
                )
            case_block = "\n".join(case_entries)

            bootstrap_args = " ".join(f"--bootstrap {peer}" for peer in self.bootstrap_peers)
            secret_key = self.wallet_secret_key
            if not secret_key.startswith("0x"):
                secret_key = "0x" + secret_key
            env_script = f"""\
MY_IP=$(hostname -I | awk '{{print $1}}')
case "$MY_IP" in
{case_block}
    *) FILE_SIZE_KB=51200; STORE_CONCURRENCY=8 ;;
esac
cat > /etc/ant-client.env << ENV_EOF
HOME=/root
BOOTSTRAP_ARGS="{bootstrap_args}"
FILE_SIZE_KB=$FILE_SIZE_KB
STORE_CONCURRENCY=$STORE_CONCURRENCY
SECRET_KEY={secret_key}
ANT={ANT_INSTALL_PATH}
ADDRESSES_FILE={CLIENT_DATA_DIR}/addresses.txt
UPLOAD_DIR={CLIENT_DATA_DIR}/uploads
DOWNLOAD_DIR={CLIENT_DATA_DIR}/downloads"""

            if self.influx_url and self.influx_token:
                env_script += f"""
INFLUX_URL={self.influx_url}
INFLUX_TOKEN={self.influx_token}
INFLUX_ORG={self.influx_org}
INFLUX_BUCKET={self.influx_bucket}"""

            env_script += """
ENV_EOF"""

            add_op(
                state,
                server.shell,
                name="Write environment file",
                commands=[env_script],
            )

            # Write systemd unit files
            uploader_unit = _build_uploader_unit()
            downloader_unit = _build_downloader_unit()

            add_op(
                state,
                server.shell,
                name="Write systemd unit files",
                commands=[
                    f"cat > /etc/systemd/system/ant-uploader.service << 'UNIT_EOF'\n"
                    f"{uploader_unit}UNIT_EOF",
                    f"cat > /etc/systemd/system/ant-downloader.service << 'UNIT_EOF'\n"
                    f"{downloader_unit}UNIT_EOF",
                    "systemctl daemon-reload",
                ],
            )

            run_ops(state)

            if isinstance(progress, RichLiveProgressHandler):
                progress.mark_all_done()
        finally:
            disconnect_all(state)
            if isinstance(progress, RichLiveProgressHandler):
                progress._live.stop()

        failed = state.failed_hosts
        total = len(host_ips)
        succeeded = total - len(failed)
        self.console.print()
        self.console.print(
            f"[bold]Client provisioning complete: {succeeded}/{total} hosts succeeded[/bold]"
        )
        self.console.print(
            "  Services created but NOT started. "
            "Use 'start-clients' to begin upload/download."
        )
        if failed:
            for host in failed:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError(f"{len(failed)} host(s) failed client provisioning")
