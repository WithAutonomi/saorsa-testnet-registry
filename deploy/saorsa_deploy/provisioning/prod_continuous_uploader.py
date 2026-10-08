"""Provision the daily production write test's two VM roles (V2-837).

Both roles are here because they share almost everything — the ant binary, the
`saorsa-db` venv, the shape of the env file, the log-prune timer — and differ
only in which script they run, which variables they need and how systemd should
treat them dying.

The one structural departure from every other client provisioner in this
package: these services run under `/opt/ant-registry-client/bin/python` rather
than `/usr/bin/python3`, because they need `saorsa_db` to reach the registry.
"""

import json
from pathlib import Path

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.client_roles import WASM_MAX_FILE_BYTES
from saorsa_deploy.evm import ARBITRUM_ONE
from saorsa_deploy.prod_bootstraps import render_bootstrap_peers_toml
from saorsa_deploy.provisioning.authorized_keys import add_operator_authorized_keys_op
from saorsa_deploy.provisioning.monitoring import INFLUXDB_WASM_BUCKET
from saorsa_deploy.provisioning.saorsa_db_install import (
    VENV_PYTHON,
    SaorsaDbInstaller,
)
from saorsa_deploy.provisioning.service_script import (
    build_service_script_commands,
)
from saorsa_deploy.provisioning.wasm_client import (
    ANT_WASM_CLI_PATH,
    WASM_CLIENT_DIR,
    WASM_NODE_MAJOR,
    build_ant_wasm_cli_command,
    build_bundle_install_command,
    build_node_install_command,
)

ANT_INSTALL_PATH = "/root/.local/bin/ant"

UPLOADER_SERVICE = "ant-prod-continuous-uploader"
UPLOADER_DATA_DIR = "/var/lib/ant-prod-continuous-uploader"
UPLOADER_SCRIPTS_DIR = "/root/ant-prod-continuous-uploader"
UPLOADER_LOG_DIR = "/var/log/ant-prod-continuous-uploader"
UPLOADER_ENV_PATH = "/etc/ant-prod-continuous-uploader.env"
UPLOADER_SCRIPT_NAME = "prod_continuous_uploader.py"

VERIFIER_SERVICE = "ant-prod-upload-verifier"
VERIFIER_DATA_DIR = "/var/lib/ant-prod-upload-verifier"
VERIFIER_SCRIPTS_DIR = "/root/ant-prod-upload-verifier"
VERIFIER_LOG_DIR = "/var/log/ant-prod-upload-verifier"
VERIFIER_ENV_PATH = "/etc/ant-prod-upload-verifier.env"
VERIFIER_SCRIPT_NAME = "prod_upload_verifier.py"

REMOTE_BOOTSTRAP_PEERS_PATH = "/root/.config/ant/bootstrap_peers.toml"


def build_systemd_unit(
    *, description: str, env_path: str, script_path: str, restart: str
) -> str:
    """Both roles restart on failure and are meant to run indefinitely.

    Unlike `prod uploaders`, whose unit is `Restart=no` because it walks a
    fixed list once and exits, neither of these services has a natural end:
    the uploader sleeps until the next cycle and the verifier waits for work.
    A crash is therefore always something to recover from.
    """
    return f"""\
[Unit]
Description={description}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile={env_path}
Environment=PYTHONUNBUFFERED=1
ExecStart={VENV_PYTHON} -u {script_path}
StandardOutput=journal
StandardError=journal
{restart}

[Install]
WantedBy=multi-user.target
"""


def build_log_prune_command(
    service_name: str, log_dir: str, retention_days: int
) -> str:
    """Install (or remove) a daily prune timer for the per-op logs.

    `retention_days <= 0` removes the timer so toggling it off on a re-provision
    actually takes effect, matching how the downloader's prune behaves.

    Unlike the crash-debug node prune, this one does NOT restart a log
    forwarder afterwards: nothing tails these files, so deleting them frees the
    space immediately.
    """
    prune_service_path = f"/etc/systemd/system/{service_name}-logprune.service"
    prune_timer_path = f"/etc/systemd/system/{service_name}-logprune.timer"
    if retention_days <= 0:
        return (
            f"systemctl disable --now {service_name}-logprune.timer "
            f"2>/dev/null || true; "
            f"rm -f {prune_timer_path} {prune_service_path}; "
            "systemctl daemon-reload"
        )
    days = int(retention_days)
    prune_service = f"""\
[Unit]
Description=Prune {service_name} per-op logs older than {days} days

[Service]
Type=oneshot
ExecStart=/usr/bin/find {log_dir} -type f -mtime +{days} -delete
"""
    prune_timer = f"""\
[Unit]
Description=Daily prune of {service_name} per-op logs

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
"""
    return (
        f"cat > {prune_service_path} << 'PRUNE_SVC_EOF'\n"
        f"{prune_service}PRUNE_SVC_EOF\n"
        f"cat > {prune_timer_path} << 'PRUNE_TIMER_EOF'\n"
        f"{prune_timer}PRUNE_TIMER_EOF\n"
        f"systemctl daemon-reload && "
        f"systemctl enable --now {service_name}-logprune.timer"
    )


def build_env_file_lines(values: dict) -> str:
    """Render an env file, omitting empties so defaults in the script apply.

    Values are not quoted: systemd's EnvironmentFile takes the rest of the line
    verbatim, and quoting would end up inside the value.
    """
    return "\n".join(f"{key}={value}" for key, value in values.items() if value != "")


def _install_ant_command(download_url: str, is_archive: bool) -> str:
    if is_archive:
        return (
            f"test -f {ANT_INSTALL_PATH} && echo 'ANT_BINARY:SKIP' || ("
            f"wget -q {download_url} -O /tmp/ant-archive.tar.gz && "
            f"tar -xzf /tmp/ant-archive.tar.gz -C /tmp/ && "
            f"find /tmp -name ant -type f -path '*/ant-*/*' "
            f"-exec mv {{}} {ANT_INSTALL_PATH} \\; && "
            f"chmod +x {ANT_INSTALL_PATH} && "
            f"rm -rf /tmp/ant-*/ /tmp/ant-archive.tar.gz && "
            f"echo 'ANT_BINARY:INSTALLED')"
        )
    return (
        f"test -f {ANT_INSTALL_PATH} && echo 'ANT_BINARY:SKIP' || ("
        f"wget -q {download_url} -O {ANT_INSTALL_PATH} && "
        f"chmod +x {ANT_INSTALL_PATH} && echo 'ANT_BINARY:INSTALLED')"
    )


class _BaseProvisioner:
    def __init__(
        self,
        *,
        hosts: list,
        deployment_id: int,
        deployment_name: str,
        pool_database_url: str,
        bootstrap_peers: list,
        ssh_key_path: str = "~/.ssh/id_rsa",
        binary_url: str | None = None,
        binary_is_archive: bool = True,
        influx_url: str = "",
        influx_token: str = "",
        influx_org: str = "ant",
        influx_bucket: str = "testnet-metrics",
        evm_network: str = "arbitrum-one",
        log_level: str = "info",
        rust_log: str | None = None,
        bootstrap_cache: bool = True,
        log_retention_days: int = 7,
        wheel_path: Path | None = None,
        console: Console | None = None,
    ):
        self.hosts = hosts
        self.deployment_id = deployment_id
        self.deployment_name = deployment_name
        self.pool_database_url = pool_database_url
        self.bootstrap_peers = bootstrap_peers
        self.ssh_key_path = ssh_key_path
        self.binary_url = binary_url
        self.binary_is_archive = binary_is_archive
        self.influx_url = influx_url
        self.influx_token = influx_token
        self.influx_org = influx_org
        self.influx_bucket = influx_bucket
        self.evm_network = evm_network
        self.log_level = log_level
        self.rust_log = rust_log
        self.bootstrap_cache = bootstrap_cache
        self.log_retention_days = log_retention_days
        self.wheel_path = wheel_path
        self.console = console or Console()

    # -- subclass contract ------------------------------------------------
    service_name = ""
    data_dir = ""
    scripts_dir = ""
    log_dir = ""
    env_path = ""
    script_name = ""
    description = ""
    restart_directive = "Restart=on-failure\nRestartSec=30"

    def _per_host_env(self, host: dict) -> dict:
        raise NotImplementedError

    # ---------------------------------------------------------------------
    def execute(self) -> None:
        inventory = Inventory(
            (
                [
                    (h["ip"], {"ssh_user": "root", "ssh_key": self.ssh_key_path})
                    for h in self.hosts
                ],
                {},
            )
        )
        state = State(inventory, Config(SUDO=False))
        connect_all(state)

        add_operator_authorized_keys_op(state)
        add_op(
            state,
            server.shell,
            name="Disable unattended upgrades",
            commands=[
                "systemctl stop unattended-upgrades 2>/dev/null || true; "
                "systemctl disable unattended-upgrades 2>/dev/null || true"
            ],
        )
        add_op(
            state,
            server.shell,
            name=f"Create {self.service_name} directories",
            commands=[
                f"mkdir -p {self.data_dir} {self.data_dir}/files "
                f"{self.scripts_dir} {self.log_dir} /root/.local/bin "
                f"/root/.config/ant"
            ],
        )

        self._add_install_ant_op(state)
        self._add_install_wasm_ops(state)
        SaorsaDbInstaller(wheel_path=self.wheel_path, console=self.console).add_ops(
            state
        )
        self._add_script_op(state)
        self._add_env_op(state)
        self._add_unit_op(state)
        add_op(
            state,
            server.shell,
            name=f"Configure {self.service_name} log pruning",
            commands=[
                build_log_prune_command(
                    self.service_name, self.log_dir, self.log_retention_days
                )
            ],
        )

        run_ops(state)
        # Services are enabled but not started here, matching every other
        # client provisioner: starting is a separate, explicit step.

    def _add_install_ant_op(self, state: State) -> None:
        download_url = self.binary_url
        if not download_url:
            from saorsa_deploy.binary_source import get_ant_release_url

            self.console.print("Fetching latest ant release from GitHub...")
            download_url = get_ant_release_url()
        add_op(
            state,
            server.shell,
            name="Download and install ant binary",
            commands=[
                _install_ant_command(download_url, self.binary_is_archive)
            ],
        )

    def _add_install_wasm_ops(self, state: State) -> None:
        """No-op: only the uploader role can take the browser path.

        The verifiers stay native whatever this deployment uploads with, so
        nothing on a verifier VM ever runs `ant-wasm` and installing Node on it
        would slow every deploy for nothing.
        """

    def _add_script_op(self, state: State) -> None:
        add_op(
            state,
            server.shell,
            name=f"Deploy {self.script_name}",
            commands=build_service_script_commands(
                self.scripts_dir, self.script_name
            ),
        )

    def _add_env_op(self, state: State) -> None:
        # One op serves every host: the per-host values are dispatched by a
        # `case` on the VM's own primary address, the same idiom the other
        # multi-host client provisioners use.
        # The shared renderer, not a hand-rolled one: the file is a single
        # `peers = [...]` array, and emitting a repeated `addr = "…"` key makes
        # a TOML document that ant refuses to parse ("duplicate key"), failing
        # every upload before it touches the network.
        peers_toml = render_bootstrap_peers_toml(self.bootstrap_peers)
        case_blocks = []
        for host in self.hosts:
            env = self._per_host_env(host)
            rendered = build_env_file_lines(env)
            case_blocks.append(
                f'  "{host["ip"]}")\n'
                f"    cat > {self.env_path} << 'ENV_EOF'\n"
                f"{rendered}\n"
                f"ENV_EOF\n"
                f"    ;;"
            )
        script = (
            'MY_IP=$(hostname -I | awk \'{print $1}\')\n'
            'case "$MY_IP" in\n' + "\n".join(case_blocks) + "\nesac\n"
            f"cat > {REMOTE_BOOTSTRAP_PEERS_PATH} << 'PEERS_EOF'\n"
            f"{peers_toml}PEERS_EOF\n"
            f"chmod 600 {self.env_path}"
        )
        add_op(
            state,
            server.shell,
            name=f"Write {self.service_name} env file",
            commands=[script],
        )

    def _add_unit_op(self, state: State) -> None:
        unit = build_systemd_unit(
            description=self.description,
            env_path=self.env_path,
            script_path=f"{self.scripts_dir}/{self.script_name}",
            restart=self.restart_directive,
        )
        add_op(
            state,
            server.shell,
            name=f"Install {self.service_name} systemd unit",
            commands=[
                f"cat > /etc/systemd/system/{self.service_name}.service "
                f"<< 'UNIT_EOF'\n{unit}UNIT_EOF",
                "systemctl daemon-reload",
                f"systemctl enable {self.service_name}",
            ],
        )

    def _shared_env(self, host: dict) -> dict:
        return {
            "HOME": "/root",
            "DEPLOYMENT_ID": self.deployment_id,
            "DEPLOYMENT_NAME": self.deployment_name,
            "POOL_DATABASE_URL": self.pool_database_url,
            "REGION": host["region"],
            "ANT": ANT_INSTALL_PATH,
            "EVM_NETWORK": self.evm_network,
            "LOG_LEVEL": self.log_level,
            "RUST_LOG": self.rust_log or "",
            "BOOTSTRAP_CACHE": str(self.bootstrap_cache).lower(),
            "BOOTSTRAP_ARGS": " ".join(
                f"--bootstrap {peer}" for peer in self.bootstrap_peers
            ),
            "INFLUX_URL": self.influx_url,
            "INFLUX_TOKEN": self.influx_token,
            "INFLUX_ORG": self.influx_org,
            "INFLUX_BUCKET": self.influx_bucket,
        }


class ProdContinuousUploaderProvisioner(_BaseProvisioner):
    service_name = UPLOADER_SERVICE
    data_dir = UPLOADER_DATA_DIR
    scripts_dir = UPLOADER_SCRIPTS_DIR
    log_dir = UPLOADER_LOG_DIR
    env_path = UPLOADER_ENV_PATH
    script_name = UPLOADER_SCRIPT_NAME
    description = "Ant Production Daily Write Test (uploader)"

    def __init__(
        self,
        *,
        pool_request: dict,
        wallet_secret_key: str,
        sleep_interval_secs: int = 86400,
        pool_min_size: str | None = None,
        pool_max_size: str | None = None,
        payment_type: str | None = None,
        readback_verify: bool = True,
        client_kind: str = "native",
        wasm_bundle_url: str | None = None,
        bootstrap_multiaddrs: list | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.pool_request = pool_request
        self.wallet_secret_key = wallet_secret_key
        self.sleep_interval_secs = sleep_interval_secs
        self.pool_min_size = pool_min_size
        self.pool_max_size = pool_max_size
        self.payment_type = payment_type
        self.readback_verify = readback_verify
        # "native" runs the `ant` binary; "wasm" runs ant-core compiled to WASM
        # over WebRTC-Direct through the `ant-wasm` CLI. One transport per
        # deployment, and on a native one not a single WASM op is added and the
        # env file renders byte-identically to before this existed.
        self.client_kind = client_kind
        self.wasm_bundle_url = wasm_bundle_url
        self.bootstrap_multiaddrs = list(bootstrap_multiaddrs or [])
        if self.is_wasm and not self.wasm_bundle_url:
            raise RuntimeError(
                "client_kind='wasm' needs a WASM bundle URL — run `build "
                "ant-wasm --name <name> --branch-name <ref> --repo-owner <owner>`"
            )
        if self.is_wasm and not self.bootstrap_multiaddrs:
            # Without one a browser client cannot reach the network at all, and
            # the failure would surface only as an unexplained connection error
            # hours later, on a service that sleeps between cycles.
            raise RuntimeError(
                "client_kind='wasm' needs at least one bootstrap WebRTC "
                "multiaddr (see `prod bootstrap webrtc-addresses`)"
            )

    @property
    def is_wasm(self) -> bool:
        return self.client_kind == "wasm"

    def _add_install_ant_op(self, state: State) -> None:
        """Nothing on a browser-path uploader runs `ant`, not even the read-back.

        Skipped rather than installed-and-unused because a WASM deployment may
        have no `ant` build to fetch at all, and an install that fails would fail
        the whole provision for a binary nothing executes.
        """
        if self.is_wasm:
            return
        super()._add_install_ant_op(state)

    def _add_install_wasm_ops(self, state: State) -> None:
        """Install Node, the WASM bundle and the ``ant-wasm`` CLI.

        ``require_cli`` is set because a bundle built before the CLI existed
        would install cleanly and then fail every upload with "cannot find
        module".
        """
        if not self.is_wasm:
            return
        add_op(
            state,
            server.shell,
            name=f"Install Node.js {WASM_NODE_MAJOR}",
            commands=[build_node_install_command()],
        )
        add_op(
            state,
            server.shell,
            name="Install WASM client bundle",
            commands=[
                build_bundle_install_command(
                    self.wasm_bundle_url, data_dirs=(), require_cli=True
                )
            ],
        )
        add_op(
            state,
            server.shell,
            name="Install ant-wasm CLI",
            commands=[build_ant_wasm_cli_command()],
        )

    def _per_host_env(self, host: dict) -> dict:
        secret = self.wallet_secret_key
        if secret and not secret.startswith("0x"):
            secret = f"0x{secret}"
        env = self._shared_env(host)
        env.update(
            {
                "SECRET_KEY": secret,
                "WORK_DIR": f"{self.data_dir}/files",
                "POOL_REQUEST": json.dumps(self.pool_request, separators=(",", ":")),
                "POOL_MIN_SIZE": self.pool_min_size or "",
                "POOL_MAX_SIZE": self.pool_max_size or "",
                "SLEEP_INTERVAL_SECS": self.sleep_interval_secs,
                "PAYMENT_TYPE": self.payment_type or "",
                "READBACK_VERIFY": str(self.readback_verify).lower(),
            }
        )
        if self.is_wasm:
            env.update(
                {
                    "CLIENT_KIND": "wasm",
                    "ANT_WASM": ANT_WASM_CLI_PATH,
                    "BOOTSTRAP_MULTIADDRS": ",".join(self.bootstrap_multiaddrs),
                    "WASM_DIR": f"{WASM_CLIENT_DIR}/pkg",
                    "MAX_UPLOAD_BYTES": WASM_MAX_FILE_BYTES,
                    # The browser path's payment callbacks sign with viem against
                    # a plain JSON-RPC endpoint, so the chain is named by URL
                    # rather than by `--evm-network`. Arbitrum One: these uploads
                    # cost real ETH, every day.
                    "RPC_URL": ARBITRUM_ONE["rpc_url"],
                    # WASM operations go to their OWN Influx bucket. They write
                    # the same `prod_upload` measurement the native tier does, so
                    # one bucket would let any dashboard panel average two
                    # transports measured several times apart.
                    "INFLUX_BUCKET": INFLUXDB_WASM_BUCKET,
                }
            )
        return env


class ProdUploadVerifierProvisioner(_BaseProvisioner):
    service_name = VERIFIER_SERVICE
    data_dir = VERIFIER_DATA_DIR
    scripts_dir = VERIFIER_SCRIPTS_DIR
    log_dir = VERIFIER_LOG_DIR
    env_path = VERIFIER_ENV_PATH
    script_name = VERIFIER_SCRIPT_NAME
    description = "Ant Production Daily Write Test (read-back verifier)"
    # Always-on rather than on-failure: this service is idle most of the time
    # and must be running the moment a new upload appears.
    restart_directive = "Restart=always\nRestartSec=30"

    def __init__(
        self,
        *,
        verify_interval_secs: int = 3600,
        verify_batch_limit: int = 50,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.verify_interval_secs = verify_interval_secs
        self.verify_batch_limit = verify_batch_limit

    def _per_host_env(self, host: dict) -> dict:
        env = self._shared_env(host)
        env.update(
            {
                "WORK_DIR": f"{self.data_dir}/downloads",
                "VERIFY_INTERVAL_SECS": self.verify_interval_secs,
                "VERIFY_BATCH_LIMIT": self.verify_batch_limit,
                "PROVIDER": host.get("provider", "") or "",
            }
        )
        # No wallet key: verification only downloads, which costs nothing. Not
        # shipping it keeps a funded mainnet key off two more VMs.
        return env
