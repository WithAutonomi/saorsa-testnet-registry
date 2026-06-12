"""Pyinfra provisioner for prod-continuous-downloader client VMs.

Sister of :mod:`saorsa_deploy.provisioning.prod_uploader` but targets the
download role: it runs a continuous loop over a per-host address list,
keeps per-op stdout/stderr logs on disk, and sleeps a configurable
interval between full cycles. None of the testnet ``downloader.sh`` /
S3-rotating address feed / shared ``ant-client.service`` machinery is
involved — production downloaders get their own service so changes here
never leak into testnet runs.

Per host the provisioner:

* installs the ``ant`` binary (release archive or custom build)
* writes ``/etc/ant-prod-downloader.env`` with bootstrap args, region,
  sleep interval, addresses-file path, and Influx config
* writes the per-host address list to
  ``/var/lib/ant-prod-downloader/addresses.txt``
* deploys ``prod_downloader.py`` to ``/root/ant-prod-downloader/``
* writes the ``ant-prod-downloader.service`` systemd unit (Type=simple,
  Restart=on-failure, RestartSec=30) and reloads systemd

Services are NOT started here. Use the start helper in
``cmd/prod_continuous_downloader.py`` after provisioning.
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
from saorsa_deploy.provisioning.clock_skew import (
    build_clock_skew_command,
    format_skew_offset,
)
from saorsa_deploy.provisioning.progress import (
    RichLiveProgressHandler,
    create_progress_handler,
)
from saorsa_deploy.resources import get_resources_dir

ANT_INSTALL_PATH = "/root/.local/bin/ant"
SERVICE_NAME = "ant-prod-downloader"
DATA_DIR = "/var/lib/ant-prod-downloader"
SCRIPTS_DIR = "/root/ant-prod-downloader"
LOG_DIR = "/var/log/ant-prod-downloader"
ADDRESSES_PATH = f"{DATA_DIR}/addresses.txt"
DOWNLOAD_DIR = f"{DATA_DIR}/downloads"
SERVICE_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"
ENV_PATH = "/etc/ant-prod-downloader.env"
# heaptrack memory-profile output dir (shared with the testnet --heaptrack path).
HEAPTRACK_DIR = "/var/log/ant-heaptrack"
# Daily timer that prunes per-op logs older than the retention window so a
# permanent downloader never fills its disk with stdout/stderr files.
LOG_PRUNE_NAME = "ant-prod-downloader-logprune"
LOG_PRUNE_SERVICE_PATH = f"/etc/systemd/system/{LOG_PRUNE_NAME}.service"
LOG_PRUNE_TIMER_PATH = f"/etc/systemd/system/{LOG_PRUNE_NAME}.timer"


def _build_systemd_unit(one_shot: bool = False) -> str:
    # Continuous mode: `Restart=on-failure` plus the long `RestartSec`
    # keeps the service alive across transient errors (network blips, ant
    # process crash) without busy-looping. The script itself runs forever
    # — it only exits cleanly when the address list is empty.
    #
    # One-shot mode: the script walks the address list once then exits 0.
    # `Restart=no` so systemd lets the service terminate naturally.
    description = (
        "Ant Production One-Shot Downloader"
        if one_shot
        else "Ant Production Continuous Downloader"
    )
    restart_line = "Restart=no" if one_shot else "Restart=on-failure\nRestartSec=30"
    return f"""\
[Unit]
Description={description}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile={ENV_PATH}
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -u {SCRIPTS_DIR}/prod_downloader.py
StandardOutput=journal
StandardError=journal
{restart_line}

[Install]
WantedBy=multi-user.target
"""


class ProdDownloaderProvisioner:
    """Provisions the prod continuous-downloader service across one or more
    client VMs.

    Each entry in ``downloaders`` must be a dict with keys ``ip``, ``region``,
    and ``addresses_file_content`` (the literal text of the per-host address
    list).
    """

    def __init__(
        self,
        downloaders: list[dict],
        bootstrap_peers: list[str],
        sleep_interval_secs: int,
        ssh_key_path: str = "~/.ssh/id_rsa",
        binary_url: str | None = None,
        binary_is_archive: bool = True,
        influx_url: str = "",
        influx_token: str = "",
        influx_org: str = "ant",
        influx_bucket: str = "testnet-metrics",
        evm_network: str = "arbitrum-one",
        one_shot: bool = False,
        rust_log: str | None = None,
        memory_profiling: bool = False,
        log_retention_days: int = 7,
        console: Console | None = None,
    ):
        self.downloaders = downloaders
        self.bootstrap_peers = bootstrap_peers
        self.sleep_interval_secs = sleep_interval_secs
        self.one_shot = one_shot
        self.ssh_key_path = ssh_key_path
        self.binary_url = binary_url
        self.binary_is_archive = binary_is_archive
        self.influx_url = influx_url
        self.influx_token = influx_token
        self.influx_org = influx_org
        self.influx_bucket = influx_bucket
        self.evm_network = evm_network
        self.rust_log = rust_log
        self.memory_profiling = memory_profiling
        self.log_retention_days = log_retention_days
        self.console = console or Console()

    def execute(self) -> None:
        host_ips = [d["ip"] for d in self.downloaders if d.get("ip")]
        if not host_ips:
            raise RuntimeError("No downloader IPs available for provisioning")

        hosts_data = [
            (d["ip"], {"ssh_user": "root", "ssh_key": self.ssh_key_path})
            for d in self.downloaders
            if d.get("ip")
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
                f"Connecting to {len(host_ips)} prod-downloader host(s) as root..."
            )
            connect_all(state)

            add_op(
                state,
                server.shell,
                name=DISABLE_UNATTENDED_UPGRADES_OP_NAME,
                commands=[build_disable_unattended_upgrades_command()],
            )

            self._add_clock_skew_ops(state)

            heaptrack_dirs = f" {HEAPTRACK_DIR}" if self.memory_profiling else ""
            add_op(
                state,
                server.shell,
                name="Create prod-downloader directories",
                commands=[
                    f"mkdir -p {DATA_DIR} {DOWNLOAD_DIR} {SCRIPTS_DIR} "
                    f"{LOG_DIR} /root/.local/bin{heaptrack_dirs}",
                ],
            )

            self._add_install_heaptrack_op(state)
            self._add_install_ant_op(state)
            self._add_deploy_service_script_op(state)
            self._add_env_and_addresses_op(state)
            self._add_systemd_unit_op(state)
            self._add_log_pruning_op(state)

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
            f"[bold]Prod downloader provisioning complete: {succeeded}/{total} "
            f"hosts succeeded[/bold]"
        )
        self.console.print(
            "  Service installed but NOT started. Use the start helper to "
            "begin downloading."
        )
        if failed:
            for host in failed:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError(
                f"{len(failed)} host(s) failed prod-downloader provisioning"
            )

    def _add_clock_skew_ops(self, state: State) -> None:
        """Inject clock skew on any downloader whose spec sets a clock_skew duration.

        Each such host shifts by its own explicit signed offset, so we add one op
        per host.
        """
        skew_by_ip = {
            d["ip"]: d["clock_skew"]
            for d in self.downloaders
            if d.get("ip") and d.get("clock_skew")
        }
        if not skew_by_ip:
            return
        self.console.print(
            f"[cyan]Clock skew enabled on {len(skew_by_ip)} downloader host(s).[/cyan]"
        )
        for host in state.inventory.get_active_hosts():
            offset = skew_by_ip.get(host.name)
            if not offset:
                continue
            add_op(
                state,
                server.shell,
                name=f"Inject clock skew ({format_skew_offset(offset)})",
                commands=[build_clock_skew_command(offset)],
                host=[host],
            )

    def _add_install_heaptrack_op(self, state: State) -> None:
        """Install the heaptrack apt package when memory profiling is enabled."""
        if not self.memory_profiling:
            return
        add_op(
            state,
            server.shell,
            name="Install heaptrack",
            commands=[
                "command -v heaptrack >/dev/null && echo 'HEAPTRACK:SKIP' || "
                "(DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
                "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
                "heaptrack && echo 'HEAPTRACK:INSTALLED')"
            ],
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
        service_script = (scripts_dir / "prod_downloader.py").read_text()
        add_op(
            state,
            server.shell,
            name="Deploy prod_downloader.py service script",
            commands=[
                f"cat > {SCRIPTS_DIR}/prod_downloader.py << 'SCRIPT_EOF'\n"
                f"{service_script}SCRIPT_EOF",
                f"chmod +x {SCRIPTS_DIR}/prod_downloader.py",
            ],
        )

    def _add_env_and_addresses_op(self, state: State) -> None:
        bootstrap_args = " ".join(
            f"--bootstrap {peer}" for peer in self.bootstrap_peers
        )

        case_entries = []
        for d in self.downloaders:
            if not d.get("ip"):
                continue
            addrs_b64 = base64.b64encode(
                d["addresses_file_content"].encode("utf-8")
            ).decode("ascii")
            bootstrap_cache = "true" if d.get("bootstrap_cache", True) else "false"
            case_entries.append(
                f'    "{d["ip"]}") '
                f'REGION={d["region"]}; '
                f'ADDRS_B64="{addrs_b64}"; '
                f"BOOTSTRAP_CACHE={bootstrap_cache} ;;"
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

        # The peers file is written unconditionally: ant only reads it when
        # invoked without --bootstrap (BOOTSTRAP_CACHE=true), so it is inert
        # on cache-disabled hosts.
        peers_toml = render_bootstrap_peers_toml(self.bootstrap_peers)
        peers_dir = PurePosixPath(REMOTE_BOOTSTRAP_PEERS_TOML_PATH).parent

        env_script = f"""\
MY_IP=$(hostname -I | awk '{{print $1}}')
case "$MY_IP" in
{case_block}
    *) REGION=unknown; ADDRS_B64=""; BOOTSTRAP_CACHE=true ;;
esac
mkdir -p {DATA_DIR}
echo "$ADDRS_B64" | base64 -d > {ADDRESSES_PATH}
mkdir -p {peers_dir}
cat > {REMOTE_BOOTSTRAP_PEERS_TOML_PATH} << 'PEERS_EOF'
{peers_toml}PEERS_EOF
cat > {ENV_PATH} << ENV_EOF
HOME=/root
REGION=$REGION
BOOTSTRAP_ARGS="{bootstrap_args}"
BOOTSTRAP_CACHE=$BOOTSTRAP_CACHE
ANT={ANT_INSTALL_PATH}
ADDRESSES_FILE={ADDRESSES_PATH}
DOWNLOAD_DIR={DOWNLOAD_DIR}
EVM_NETWORK={self.evm_network}
SLEEP_INTERVAL_SECS={self.sleep_interval_secs}
ONE_SHOT={'true' if self.one_shot else 'false'}
RUST_LOG={self.rust_log or ''}
MEMORY_PROFILING={'true' if self.memory_profiling else 'false'}
HEAPTRACK_DIR={HEAPTRACK_DIR}
{influx_lines}ENV_EOF"""

        add_op(
            state,
            server.shell,
            name="Write environment file and per-host address list",
            commands=[env_script],
        )

    def _add_systemd_unit_op(self, state: State) -> None:
        unit = _build_systemd_unit(one_shot=self.one_shot)
        cmd = (
            f"cat > {SERVICE_PATH} << 'UNIT_EOF'\n"
            f"{unit}UNIT_EOF\n"
            f"systemctl daemon-reload"
        )
        add_op(
            state,
            server.shell,
            name="Write ant-prod-downloader.service systemd unit",
            commands=[cmd],
        )

    def _add_log_pruning_op(self, state: State) -> None:
        """Install (or remove) the daily log-pruning systemd timer.

        Delegates the command building to :func:`build_log_prune_command` so
        the shell content is unit-testable without a pyinfra State.
        """
        days = int(self.log_retention_days)
        if days <= 0:
            name = "Disable ant-prod-downloader log pruning"
        else:
            name = f"Install log-pruning timer ({days}-day retention)"
        add_op(
            state,
            server.shell,
            name=name,
            commands=[build_log_prune_command(days)],
        )


def build_log_prune_command(retention_days: int) -> str:
    """Return the shell command that installs/removes the log-prune timer.

    A permanent downloader leaves a stdout/stderr pair on disk for every
    download; without pruning the VM eventually fills up. A oneshot service
    runs ``find ... -mtime +N -delete`` and a ``.timer`` fires it daily.
    ``retention_days <= 0`` disables pruning entirely (keep forever): the
    previously-installed timer is removed so toggling the flag off on a
    re-provision actually takes effect.
    """
    if retention_days <= 0:
        return (
            f"systemctl disable --now {LOG_PRUNE_NAME}.timer 2>/dev/null || true; "
            f"rm -f {LOG_PRUNE_TIMER_PATH} {LOG_PRUNE_SERVICE_PATH}; "
            "systemctl daemon-reload"
        )

    days = int(retention_days)
    prune_service = f"""\
[Unit]
Description=Prune ant-prod-downloader per-op logs older than {days} days

[Service]
Type=oneshot
ExecStart=/usr/bin/find {LOG_DIR} -type f -mtime +{days} -delete
"""
    prune_timer = """\
[Unit]
Description=Daily prune of ant-prod-downloader per-op logs

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
"""
    return (
        f"cat > {LOG_PRUNE_SERVICE_PATH} << 'PRUNE_SVC_EOF'\n"
        f"{prune_service}PRUNE_SVC_EOF\n"
        f"cat > {LOG_PRUNE_TIMER_PATH} << 'PRUNE_TIMER_EOF'\n"
        f"{prune_timer}PRUNE_TIMER_EOF\n"
        f"systemctl daemon-reload && "
        f"systemctl enable --now {LOG_PRUNE_NAME}.timer"
    )
