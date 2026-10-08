"""Pyinfra provisioners for prod canary upload + probe VMs.

Shared shape mirrors :mod:`saorsa_deploy.provisioning.prod_uploader`: install
``ant``, drop a Python service script under ``/root/ant-prod-canary/``, write
``/etc/ant-prod-canary.env``, write a systemd unit, ``daemon-reload``.

* :class:`ProdCanaryUploadProvisioner` provisions the single wallet-bearing
  upload VM. Service unit is ``Type=simple Restart=no`` — the script walks
  the URL list once, exits, and the VM idles.
* :class:`ProdCanaryProbeProvisioner` provisions one or more wallet-free
  probe VMs. Service unit is ``Type=simple Restart=always`` — the script
  loops forever until the VM is destroyed.

Services ARE started by both provisioners (no separate start step).
"""

import base64
from pathlib import PurePosixPath

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.prod_bootstraps import (
    REMOTE_BOOTSTRAP_PEERS_TOML_PATH,
    render_bootstrap_peers_toml,
)
from saorsa_deploy.provisioning.apt import (
    DISABLE_UNATTENDED_UPGRADES_OP_NAME,
    build_disable_unattended_upgrades_command,
)
from saorsa_deploy.provisioning.authorized_keys import add_operator_authorized_keys_op
from saorsa_deploy.provisioning.progress import (
    RichLiveProgressHandler,
    create_progress_handler,
)
from saorsa_deploy.resources import get_resources_dir

ANT_INSTALL_PATH = "/root/.local/bin/ant"
DATA_DIR = "/var/lib/ant-prod-canary"
SCRIPTS_DIR = "/root/ant-prod-canary"
LOG_DIR = "/var/log/ant-prod-canary"
ENV_PATH = "/etc/ant-prod-canary.env"

UPLOAD_SERVICE_NAME = "ant-prod-canary-upload"
PROBE_SERVICE_NAME = "ant-prod-canary-probe"
UPLOAD_SERVICE_PATH = f"/etc/systemd/system/{UPLOAD_SERVICE_NAME}.service"
PROBE_SERVICE_PATH = f"/etc/systemd/system/{PROBE_SERVICE_NAME}.service"

UPLOAD_SCRIPT = "prod_canary_upload.py"
PROBE_SCRIPT = "prod_canary_probe.py"
UPLOAD_URLS_PATH = f"{DATA_DIR}/urls.txt"
UPLOAD_POSITION_PATH = f"{DATA_DIR}/urls.position"
PROBE_STATE_PATH = f"{DATA_DIR}/probe-state.db"


def _build_upload_unit() -> str:
    return f"""\
[Unit]
Description=Ant Prod Canary Upload
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile={ENV_PATH}
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -u {SCRIPTS_DIR}/{UPLOAD_SCRIPT}
StandardOutput=journal
StandardError=journal
Restart=no

[Install]
WantedBy=multi-user.target
"""


def _build_probe_unit() -> str:
    return f"""\
[Unit]
Description=Ant Prod Canary Probe
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile={ENV_PATH}
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -u {SCRIPTS_DIR}/{PROBE_SCRIPT}
StandardOutput=journal
StandardError=journal
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
"""


def _install_ant_command(binary_url: str, binary_is_archive: bool) -> str:
    if binary_is_archive:
        return (
            f"test -f {ANT_INSTALL_PATH} && echo 'ANT_BINARY:SKIP' || "
            f"(wget -q {binary_url} -O /tmp/ant-archive.tar.gz && "
            f"tar -xzf /tmp/ant-archive.tar.gz -C /tmp/ && "
            f"find /tmp -name ant -type f -path '*/ant-*/*' "
            f"-exec mv {{}} {ANT_INSTALL_PATH} \\; && "
            f"chmod +x {ANT_INSTALL_PATH} && "
            f"rm -rf /tmp/ant-*/ /tmp/ant-archive.tar.gz && "
            f"echo 'ANT_BINARY:INSTALLED')"
        )
    return (
        f"test -f {ANT_INSTALL_PATH} && echo 'ANT_BINARY:SKIP' || "
        f"(wget -q {binary_url} -O {ANT_INSTALL_PATH} && "
        f"chmod +x {ANT_INSTALL_PATH} && echo 'ANT_BINARY:INSTALLED')"
    )


def _resolve_binary_url(binary_url: str | None, console: Console) -> str:
    if binary_url:
        console.print(f"Using binary URL: {binary_url}")
        return binary_url
    from saorsa_deploy.binary_source import get_ant_release_url

    console.print("Fetching latest ant release from GitHub...")
    url = get_ant_release_url()
    console.print(f"  Release URL: {url}")
    return url


def _common_dirs_command() -> str:
    return (
        f"mkdir -p {DATA_DIR} {DATA_DIR}/files {DATA_DIR}/probe-files "
        f"{SCRIPTS_DIR} {LOG_DIR} /root/.local/bin"
    )


def _format_env_lines(env: dict[str, str]) -> str:
    """Render env-file lines. Values are written verbatim (no quoting); this
    matches the pattern used by the other prod-* provisioners."""
    return "\n".join(f"{k}={v}" for k, v in env.items() if v != "") + "\n"


class ProdCanaryUploadProvisioner:
    """Provisions the single canary upload VM."""

    def __init__(
        self,
        *,
        host_ip: str,
        region: str,
        deployment_name: str,
        bootstrap_peers: list[str],
        wallet_secret_key: str,
        urls_file_content: str,
        upload_sleep_secs: int,
        ssh_key_path: str = "~/.ssh/id_rsa",
        binary_url: str | None = None,
        binary_is_archive: bool = True,
        influx_url: str = "",
        influx_token: str = "",
        influx_org: str = "ant",
        influx_bucket: str = "testnet-metrics",
        evm_network: str = "arbitrum-one",
        bootstrap_cache: bool = True,
        rust_log: str | None = None,
        console: Console | None = None,
    ):
        self.host_ip = host_ip
        self.region = region
        self.deployment_name = deployment_name
        self.bootstrap_peers = bootstrap_peers
        self.bootstrap_cache = bootstrap_cache
        self.wallet_secret_key = wallet_secret_key
        self.urls_file_content = urls_file_content
        self.upload_sleep_secs = upload_sleep_secs
        self.ssh_key_path = ssh_key_path
        self.binary_url = binary_url
        self.binary_is_archive = binary_is_archive
        self.influx_url = influx_url
        self.influx_token = influx_token
        self.influx_org = influx_org
        self.influx_bucket = influx_bucket
        self.evm_network = evm_network
        self.rust_log = rust_log
        self.console = console or Console()

    def execute(self) -> None:
        if not self.host_ip:
            raise RuntimeError("Upload host IP missing")
        if not self.influx_url or not self.influx_token:
            raise RuntimeError("Influx URL + token are required for canary uploads")

        hosts_data = [(self.host_ip, {"ssh_user": "root", "ssh_key": self.ssh_key_path})]
        inventory = Inventory((hosts_data, {}))
        config = Config()
        state = State(inventory=inventory, config=config)

        progress = create_progress_handler(self.console)
        state.add_callback_handler(progress)
        if isinstance(progress, RichLiveProgressHandler):
            progress._live.start()

        try:
            self.console.print(f"Connecting to canary upload host {self.host_ip} as root...")
            connect_all(state)

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
                name="Create prod-canary directories",
                commands=[_common_dirs_command()],
            )

            binary_url = _resolve_binary_url(self.binary_url, self.console)
            add_op(
                state,
                server.shell,
                name="Download and install ant binary",
                commands=[_install_ant_command(binary_url, self.binary_is_archive)],
            )

            scripts_dir = get_resources_dir() / "scripts"
            service_script = (scripts_dir / UPLOAD_SCRIPT).read_text()
            add_op(
                state,
                server.shell,
                name=f"Deploy {UPLOAD_SCRIPT} service script",
                commands=[
                    f"cat > {SCRIPTS_DIR}/{UPLOAD_SCRIPT} << 'SCRIPT_EOF'\n"
                    f"{service_script}SCRIPT_EOF",
                    f"chmod +x {SCRIPTS_DIR}/{UPLOAD_SCRIPT}",
                ],
            )

            self._add_env_op(state)

            add_op(
                state,
                server.shell,
                name=f"Write {UPLOAD_SERVICE_NAME}.service systemd unit",
                commands=[
                    f"cat > {UPLOAD_SERVICE_PATH} << 'UNIT_EOF'\n"
                    f"{_build_upload_unit()}UNIT_EOF\n"
                    f"systemctl daemon-reload",
                ],
            )
            add_op(
                state,
                server.shell,
                name=f"Enable and start {UPLOAD_SERVICE_NAME}",
                commands=[
                    f"systemctl enable {UPLOAD_SERVICE_NAME}",
                    f"systemctl start {UPLOAD_SERVICE_NAME}",
                ],
            )

            run_ops(state)

            if isinstance(progress, RichLiveProgressHandler):
                progress.mark_all_done()
        finally:
            disconnect_all(state)
            if isinstance(progress, RichLiveProgressHandler):
                progress._live.stop()

        if state.failed_hosts:
            raise RuntimeError(
                f"{len(state.failed_hosts)} host(s) failed canary upload provisioning"
            )

    def _add_env_op(self, state: State) -> None:
        bootstrap_args = " ".join(f"--bootstrap {peer}" for peer in self.bootstrap_peers)
        secret_key = self.wallet_secret_key
        if secret_key and not secret_key.startswith("0x"):
            secret_key = "0x" + secret_key

        urls_b64 = base64.b64encode(self.urls_file_content.encode("utf-8")).decode("ascii")

        env = {
            "HOME": "/root",
            "REGION": self.region,
            "DEPLOYMENT_NAME": self.deployment_name,
            "BOOTSTRAP_ARGS": f'"{bootstrap_args}"',
            "BOOTSTRAP_CACHE": "true" if self.bootstrap_cache else "false",
            "SECRET_KEY": secret_key,
            "ANT": ANT_INSTALL_PATH,
            "URLS_FILE": UPLOAD_URLS_PATH,
            "POSITION_FILE": UPLOAD_POSITION_PATH,
            "WORK_DIR": f"{DATA_DIR}/files",
            "LOG_DIR": LOG_DIR,
            "EVM_NETWORK": self.evm_network,
            "UPLOAD_SLEEP_SECS": str(self.upload_sleep_secs),
            "INFLUX_URL": self.influx_url,
            "INFLUX_TOKEN": self.influx_token,
            "INFLUX_ORG": self.influx_org,
            "INFLUX_BUCKET": self.influx_bucket,
        }
        if self.rust_log:
            env["RUST_LOG"] = self.rust_log
        env_lines = _format_env_lines(env)

        # The peers file is written unconditionally: ant only reads it when
        # invoked without --bootstrap (BOOTSTRAP_CACHE=true), so it is inert
        # on cache-disabled hosts.
        peers_toml = render_bootstrap_peers_toml(self.bootstrap_peers)
        peers_dir = PurePosixPath(REMOTE_BOOTSTRAP_PEERS_TOML_PATH).parent

        script = (
            f"mkdir -p {DATA_DIR}\n"
            f'echo "{urls_b64}" | base64 -d > {UPLOAD_URLS_PATH}\n'
            f"mkdir -p {peers_dir}\n"
            f"cat > {REMOTE_BOOTSTRAP_PEERS_TOML_PATH} << 'PEERS_EOF'\n"
            f"{peers_toml}PEERS_EOF\n"
            f"cat > {ENV_PATH} << 'ENV_EOF'\n"
            f"{env_lines}ENV_EOF"
        )
        add_op(
            state,
            server.shell,
            name="Write environment file and URL list",
            commands=[script],
        )


class ProdCanaryProbeProvisioner:
    """Provisions one or more probe VMs.

    ``probes`` is a list of dicts with keys ``ip``, ``region``,
    ``shard_index``, plus the deployment-wide ``shard_count`` is passed
    separately.
    """

    def __init__(
        self,
        *,
        probes: list[dict],
        shard_count: int,
        deployment_name: str,
        bootstrap_peers: list[str],
        probe_concurrency: int,
        ssh_key_path: str = "~/.ssh/id_rsa",
        binary_url: str | None = None,
        binary_is_archive: bool = True,
        influx_url: str = "",
        influx_token: str = "",
        influx_org: str = "ant",
        influx_bucket: str = "testnet-metrics",
        bootstrap_cache: bool = True,
        rust_log: str | None = None,
        console: Console | None = None,
    ):
        self.probes = probes
        self.shard_count = shard_count
        self.deployment_name = deployment_name
        self.bootstrap_peers = bootstrap_peers
        self.bootstrap_cache = bootstrap_cache
        self.probe_concurrency = probe_concurrency
        self.ssh_key_path = ssh_key_path
        self.binary_url = binary_url
        self.binary_is_archive = binary_is_archive
        self.influx_url = influx_url
        self.influx_token = influx_token
        self.influx_org = influx_org
        self.influx_bucket = influx_bucket
        self.rust_log = rust_log
        self.console = console or Console()

    def execute(self) -> None:
        host_ips = [p["ip"] for p in self.probes if p.get("ip")]
        if not host_ips:
            raise RuntimeError("No probe IPs available for provisioning")
        if not self.influx_url or not self.influx_token:
            raise RuntimeError("Influx URL + token are required for canary probes")

        hosts_data = [
            (p["ip"], {"ssh_user": "root", "ssh_key": self.ssh_key_path})
            for p in self.probes
            if p.get("ip")
        ]
        inventory = Inventory((hosts_data, {}))
        config = Config()
        state = State(inventory=inventory, config=config)

        progress = create_progress_handler(self.console)
        state.add_callback_handler(progress)
        if isinstance(progress, RichLiveProgressHandler):
            progress._live.start()

        try:
            self.console.print(f"Connecting to {len(host_ips)} canary probe host(s) as root...")
            connect_all(state)

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
                name="Create prod-canary directories",
                commands=[_common_dirs_command()],
            )

            binary_url = _resolve_binary_url(self.binary_url, self.console)
            add_op(
                state,
                server.shell,
                name="Download and install ant binary",
                commands=[_install_ant_command(binary_url, self.binary_is_archive)],
            )

            scripts_dir = get_resources_dir() / "scripts"
            service_script = (scripts_dir / PROBE_SCRIPT).read_text()
            add_op(
                state,
                server.shell,
                name=f"Deploy {PROBE_SCRIPT} service script",
                commands=[
                    f"cat > {SCRIPTS_DIR}/{PROBE_SCRIPT} << 'SCRIPT_EOF'\n"
                    f"{service_script}SCRIPT_EOF",
                    f"chmod +x {SCRIPTS_DIR}/{PROBE_SCRIPT}",
                ],
            )

            self._add_env_op(state)

            add_op(
                state,
                server.shell,
                name=f"Write {PROBE_SERVICE_NAME}.service systemd unit",
                commands=[
                    f"cat > {PROBE_SERVICE_PATH} << 'UNIT_EOF'\n"
                    f"{_build_probe_unit()}UNIT_EOF\n"
                    f"systemctl daemon-reload",
                ],
            )
            add_op(
                state,
                server.shell,
                name=f"Enable and start {PROBE_SERVICE_NAME}",
                commands=[
                    f"systemctl enable {PROBE_SERVICE_NAME}",
                    f"systemctl start {PROBE_SERVICE_NAME}",
                ],
            )

            run_ops(state)

            if isinstance(progress, RichLiveProgressHandler):
                progress.mark_all_done()
        finally:
            disconnect_all(state)
            if isinstance(progress, RichLiveProgressHandler):
                progress._live.stop()

        if state.failed_hosts:
            raise RuntimeError(
                f"{len(state.failed_hosts)} host(s) failed canary probe provisioning"
            )

    def _add_env_op(self, state: State) -> None:
        bootstrap_args = " ".join(f"--bootstrap {peer}" for peer in self.bootstrap_peers)
        # Dispatch shard_index/region by IP, mirroring the prod-uploader
        # provisioner pattern.
        case_entries = []
        for p in self.probes:
            if not p.get("ip"):
                continue
            case_entries.append(
                f'    "{p["ip"]}") REGION={p["region"]}; SHARD_INDEX={p["shard_index"]} ;;'
            )
        case_block = "\n".join(case_entries)

        probe_env = {
            "HOME": "/root",
            "DEPLOYMENT_NAME": self.deployment_name,
            "BOOTSTRAP_ARGS": f'"{bootstrap_args}"',
            "BOOTSTRAP_CACHE": "true" if self.bootstrap_cache else "false",
            "ANT": ANT_INSTALL_PATH,
            "WORK_DIR": f"{DATA_DIR}/probe-files",
            "STATE_FILE": PROBE_STATE_PATH,
            "LOG_DIR": LOG_DIR,
            "PROBE_CONCURRENCY": str(self.probe_concurrency),
            "PROBE_SHARD_COUNT": str(self.shard_count),
            "INFLUX_URL": self.influx_url,
            "INFLUX_TOKEN": self.influx_token,
            "INFLUX_ORG": self.influx_org,
            "INFLUX_BUCKET": self.influx_bucket,
        }
        if self.rust_log:
            probe_env["RUST_LOG"] = self.rust_log
        env_lines = _format_env_lines(probe_env)

        # The peers file is written unconditionally: ant only reads it when
        # invoked without --bootstrap (BOOTSTRAP_CACHE=true), so it is inert
        # on cache-disabled hosts.
        peers_toml = render_bootstrap_peers_toml(self.bootstrap_peers)
        peers_dir = PurePosixPath(REMOTE_BOOTSTRAP_PEERS_TOML_PATH).parent

        script = f"""\
MY_IP=$(hostname -I | awk '{{print $1}}')
case "$MY_IP" in
{case_block}
    *) REGION=unknown; SHARD_INDEX=0 ;;
esac
mkdir -p {DATA_DIR}
mkdir -p {peers_dir}
cat > {REMOTE_BOOTSTRAP_PEERS_TOML_PATH} << 'PEERS_EOF'
{peers_toml}PEERS_EOF
cat > {ENV_PATH} << 'ENV_EOF'
{env_lines}ENV_EOF
echo "REGION=$REGION" >> {ENV_PATH}
echo "PROBE_SHARD_INDEX=$SHARD_INDEX" >> {ENV_PATH}"""

        add_op(
            state,
            server.shell,
            name="Write environment file (per-host shard + region)",
            commands=[script],
        )
