"""Pyinfra provisioner for prod-uploader client VMs.

Sister of :mod:`saorsa_deploy.provisioning.client` but targets a single role
(prod-network upload-only) and a Python service that walks a per-host URL
list once and exits. None of the testnet uploader.sh / S3 sync /
ant-downloader machinery is involved.

Per host the provisioner:

* installs the ``ant`` binary (release archive or custom build)
* writes ``/etc/ant-prod-uploader.env`` with bootstrap args, secret key,
  region, client_index, and Influx config
* writes the per-host URL list to ``/var/lib/ant-prod-uploader/urls.txt``
* deploys the prod_uploader.py service script to /root/ant-prod-uploader/
* writes the ``ant-prod-uploader.service`` systemd unit (Type=simple,
  Restart=no) and reloads systemd

Services are NOT started here. Use the start helper in
``cmd/prod_uploaders.py`` after provisioning.
"""

import base64

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
SERVICE_NAME = "ant-prod-uploader"
DATA_DIR = "/var/lib/ant-prod-uploader"
SCRIPTS_DIR = "/root/ant-prod-uploader"
LOG_DIR = "/var/log/ant-prod-uploader"
URLS_PATH = f"{DATA_DIR}/urls.txt"
SERVICE_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"
ENV_PATH = "/etc/ant-prod-uploader.env"


def _build_systemd_unit() -> str:
    # `-u` plus `PYTHONUNBUFFERED=1` forces unbuffered stdout/stderr so
    # every print() lands in journald immediately. Without this, Python
    # block-buffers when stdout is a non-TTY pipe and you can wait minutes
    # for output to appear (or never, if the process is killed first).
    # StandardOutput / StandardError are journal by default on systemd
    # 240+, but spelled out here to remove any doubt.
    return f"""\
[Unit]
Description=Ant Production Uploader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile={ENV_PATH}
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -u {SCRIPTS_DIR}/prod_uploader.py
StandardOutput=journal
StandardError=journal
Restart=no

[Install]
WantedBy=multi-user.target
"""


class ProdUploaderProvisioner:
    """Provisions the prod uploader service across one or more client VMs.

    Each entry in ``uploaders`` must be a dict with keys ``ip``, ``region``,
    ``client_index``, and ``urls_file_content`` (the literal text of that
    uploader's URL list).
    """

    def __init__(
        self,
        uploaders: list[dict],
        bootstrap_peers: list[str],
        wallet_secret_key: str,
        ssh_key_path: str = "~/.ssh/id_rsa",
        binary_url: str | None = None,
        binary_is_archive: bool = True,
        influx_url: str = "",
        influx_token: str = "",
        influx_org: str = "ant",
        influx_bucket: str = "testnet-metrics",
        evm_network: str = "arbitrum-one",
        console: Console | None = None,
    ):
        self.uploaders = uploaders
        self.bootstrap_peers = bootstrap_peers
        self.wallet_secret_key = wallet_secret_key
        self.ssh_key_path = ssh_key_path
        self.binary_url = binary_url
        self.binary_is_archive = binary_is_archive
        self.influx_url = influx_url
        self.influx_token = influx_token
        self.influx_org = influx_org
        self.influx_bucket = influx_bucket
        self.evm_network = evm_network
        self.console = console or Console()

    def execute(self) -> None:
        host_ips = [u["ip"] for u in self.uploaders if u.get("ip")]
        if not host_ips:
            raise RuntimeError("No uploader IPs available for provisioning")

        hosts_data = [
            (u["ip"], {"ssh_user": "root", "ssh_key": self.ssh_key_path})
            for u in self.uploaders
            if u.get("ip")
        ]
        inventory = Inventory((hosts_data, {}))
        config = Config()
        state = State(inventory=inventory, config=config)

        progress = create_progress_handler(self.console)
        state.add_callback_handler(progress)
        if isinstance(progress, RichLiveProgressHandler):
            progress._live.start()

        try:
            self.console.print(
                f"Connecting to {len(host_ips)} prod-uploader host(s) as root..."
            )
            connect_all(state)

            add_op(
                state,
                server.shell,
                name=DISABLE_UNATTENDED_UPGRADES_OP_NAME,
                commands=[build_disable_unattended_upgrades_command()],
            )

            add_op(
                state,
                server.shell,
                name="Create prod-uploader directories",
                commands=[
                    f"mkdir -p {DATA_DIR} {DATA_DIR}/files {SCRIPTS_DIR} "
                    f"{LOG_DIR} /root/.local/bin",
                ],
            )

            self._add_install_ant_op(state)
            self._add_deploy_service_script_op(state)
            self._add_env_and_urls_op(state)
            self._add_systemd_unit_op(state)

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
            f"[bold]Prod uploader provisioning complete: {succeeded}/{total} "
            f"hosts succeeded[/bold]"
        )
        self.console.print(
            "  Service installed but NOT started. Use the start helper to "
            "begin uploads."
        )
        if failed:
            for host in failed:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError(
                f"{len(failed)} host(s) failed prod-uploader provisioning"
            )

    def _add_install_ant_op(self, state: State) -> None:
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

    def _add_deploy_service_script_op(self, state: State) -> None:
        scripts_dir = get_resources_dir() / "scripts"
        service_script = (scripts_dir / "prod_uploader.py").read_text()
        add_op(
            state,
            server.shell,
            name="Deploy prod_uploader.py service script",
            commands=[
                f"cat > {SCRIPTS_DIR}/prod_uploader.py << 'SCRIPT_EOF'\n"
                f"{service_script}SCRIPT_EOF",
                f"chmod +x {SCRIPTS_DIR}/prod_uploader.py",
            ],
        )

    def _add_env_and_urls_op(self, state: State) -> None:
        bootstrap_args = " ".join(
            f"--bootstrap {peer}" for peer in self.bootstrap_peers
        )
        secret_key = self.wallet_secret_key
        if secret_key and not secret_key.startswith("0x"):
            secret_key = "0x" + secret_key

        # Build a per-IP case block carrying region / client_index / urls
        # payload. The URL list goes in base64 to survive arbitrary content
        # via a heredoc-emitted shell variable.
        case_entries = []
        for u in self.uploaders:
            if not u.get("ip"):
                continue
            urls_b64 = base64.b64encode(
                u["urls_file_content"].encode("utf-8")
            ).decode("ascii")
            case_entries.append(
                f'    "{u["ip"]}") '
                f'REGION={u["region"]}; '
                f'CLIENT_INDEX={u["client_index"]}; '
                f'URLS_B64="{urls_b64}" ;;'
            )
        case_block = "\n".join(case_entries)

        influx_lines = ""
        if self.influx_url and self.influx_token:
            influx_lines = (
                f"INFLUX_URL={self.influx_url}\n"
                f"INFLUX_TOKEN={self.influx_token}\n"
                f"INFLUX_ORG={self.influx_org}\n"
                f"INFLUX_BUCKET={self.influx_bucket}\n"
            )

        env_script = f"""\
MY_IP=$(hostname -I | awk '{{print $1}}')
case "$MY_IP" in
{case_block}
    *) REGION=unknown; CLIENT_INDEX=0; URLS_B64="" ;;
esac
mkdir -p {DATA_DIR}
echo "$URLS_B64" | base64 -d > {URLS_PATH}
cat > {ENV_PATH} << ENV_EOF
HOME=/root
REGION=$REGION
CLIENT_INDEX=$CLIENT_INDEX
BOOTSTRAP_ARGS="{bootstrap_args}"
SECRET_KEY={secret_key}
ANT={ANT_INSTALL_PATH}
URLS_FILE={URLS_PATH}
WORK_DIR={DATA_DIR}/files
EVM_NETWORK={self.evm_network}
{influx_lines}ENV_EOF"""

        add_op(
            state,
            server.shell,
            name="Write environment file and per-host URL list",
            commands=[env_script],
        )

    def _add_systemd_unit_op(self, state: State) -> None:
        unit = _build_systemd_unit()
        cmd = (
            f"cat > {SERVICE_PATH} << 'UNIT_EOF'\n"
            f"{unit}UNIT_EOF\n"
            f"systemctl daemon-reload"
        )
        add_op(
            state,
            server.shell,
            name="Write ant-prod-uploader.service systemd unit",
            commands=[cmd],
        )
