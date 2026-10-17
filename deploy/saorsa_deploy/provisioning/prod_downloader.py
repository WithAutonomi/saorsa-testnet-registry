"""Pyinfra provisioner for prod-continuous-downloader client VMs.

Sister of :mod:`saorsa_deploy.provisioning.prod_uploader` but targets the
download role: it walks a per-host address list once a day, anchored to a
fixed UTC wall-clock time, and keeps per-op stdout/stderr logs on disk.
None of the testnet ``downloader.sh`` / S3-rotating address feed / shared
``ant-client.service`` machinery is involved — production downloaders get
their own service so changes here never leak into testnet runs.

Per host the provisioner:

* installs the ``ant`` binary (release archive or custom build)
* writes ``/etc/ant-prod-downloader.env`` with bootstrap args, region, the
  daily anchor and its state-file path, the addresses-file path, and Influx
  config
* writes the per-host address list to
  ``/var/lib/ant-prod-downloader/addresses.txt``
* deploys ``prod_downloader.py`` to ``/root/ant-prod-downloader/``
* writes the ``ant-prod-downloader.service`` systemd unit (Type=simple,
  Restart=on-failure, RestartSec=30) and reloads systemd

Services are NOT started here. Use the start helper in
``cmd/prod_continuous_downloader.py`` after provisioning.
"""

import base64
import ipaddress
from pathlib import PurePosixPath
from urllib.parse import urlparse

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.client_roles import WASM_MAX_FILE_BYTES
from saorsa_deploy.prod_bootstraps import (
    REMOTE_BOOTSTRAP_PEERS_TOML_PATH,
    render_bootstrap_peers_toml,
)
from saorsa_deploy.provisioning.apt import (
    DISABLE_UNATTENDED_UPGRADES_OP_NAME,
    build_disable_unattended_upgrades_command,
)
from saorsa_deploy.provisioning.authorized_keys import add_operator_authorized_keys_op
from saorsa_deploy.provisioning.clock_skew import (
    build_clock_skew_command,
    format_skew_offset,
)
from saorsa_deploy.provisioning.monitoring import INFLUXDB_WASM_BUCKET
from saorsa_deploy.provisioning.network_profile import (
    build_network_impairment_command,
    format_network_label,
)
from saorsa_deploy.provisioning.progress import (
    RichLiveProgressHandler,
    create_progress_handler,
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
SERVICE_NAME = "ant-prod-downloader"
DATA_DIR = "/var/lib/ant-prod-downloader"
SCRIPTS_DIR = "/root/ant-prod-downloader"
SCRIPT_NAME = "prod_downloader.py"
LOG_DIR = "/var/log/ant-prod-downloader"
ADDRESSES_PATH = f"{DATA_DIR}/addresses.txt"
DOWNLOAD_DIR = f"{DATA_DIR}/downloads"
# The instant the last cycle COMPLETED. Lives in the data dir (created by the
# env op below) so the daily anchor survives a restart: without it every
# `systemctl restart` would start a fresh cycle and re-anchor the series.
LAST_CYCLE_PATH = f"{DATA_DIR}/last-cycle"
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
        "Ant Production One-Shot Downloader" if one_shot else "Ant Production Continuous Downloader"
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
        daily_at: str = "00:00",
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
        download_diagnostics: bool = False,
        exempt_endpoints: list = None,
        log_retention_days: int = 7,
        client_kind: str = "native",
        wasm_bundle_url: str | None = None,
        bootstrap_multiaddrs: list[str] | None = None,
        console: Console | None = None,
    ):
        self.downloaders = downloaders
        self.bootstrap_peers = bootstrap_peers
        # UTC HH:MM the daily cycle starts at. Inert under one_shot, which exits
        # after a single pass without ever consulting the anchor.
        self.daily_at = daily_at
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
        self.download_diagnostics = download_diagnostics
        # Extra IPs whose traffic bypasses the shaper (e.g. a client
        # Elasticsearch stood up by the deploy command).
        self.exempt_endpoints = exempt_endpoints or []
        self.log_retention_days = log_retention_days
        # "native" runs the `ant` binary; "wasm" runs ant-core compiled to WASM
        # over WebRTC-Direct through the `ant-wasm` CLI. One transport per
        # deployment: the same Python service, the same unit, the same per-op
        # logs — only the binary it spawns differs.
        self.client_kind = client_kind
        self.wasm_bundle_url = wasm_bundle_url
        self.bootstrap_multiaddrs = bootstrap_multiaddrs or []
        self.console = console or Console()
        if self.is_wasm and not self.wasm_bundle_url:
            raise RuntimeError(
                "client_kind='wasm' needs a WASM bundle URL — run `build "
                "ant-wasm --name <name> --branch-name <ref> --repo-owner <owner>`"
            )
        if self.is_wasm and not self.bootstrap_multiaddrs:
            # Without one a browser client cannot reach the network at all, and
            # the failure would surface only as an unexplained connection error
            # minutes after the VM is up and billing.
            raise RuntimeError(
                "client_kind='wasm' needs at least one bootstrap WebRTC "
                "multiaddr (see `prod bootstrap webrtc-addresses`)"
            )

    @property
    def is_wasm(self) -> bool:
        return self.client_kind == "wasm"

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
            self.console.print(f"Connecting to {len(host_ips)} prod-downloader host(s) as root...")
            connect_all(state)

            add_operator_authorized_keys_op(state)

            add_op(
                state,
                server.shell,
                name=DISABLE_UNATTENDED_UPGRADES_OP_NAME,
                commands=[build_disable_unattended_upgrades_command()],
            )

            self._add_clock_skew_ops(state)
            self._add_network_impairment_ops(state)

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
            if self.is_wasm:
                self._add_install_wasm_ops(state)
            else:
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
            "  Service installed but NOT started. Use the start helper to begin downloading."
        )
        if failed:
            for host in failed:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError(f"{len(failed)} host(s) failed prod-downloader provisioning")

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

    def _add_network_impairment_ops(self, state: State) -> None:
        """Shape any downloader whose spec sets a `network` profile.

        One op per host, because each VM carries its own profile. The InfluxDB and
        Elasticsearch endpoints are passed through as exempt so the shaped link is
        given entirely to the transfer — otherwise the VM's own instrumentation
        would compete with it for the capped bandwidth and the measurement would be
        of a home connection that is also busy shipping its own logs.
        """
        by_ip = {
            d["ip"]: d["network"] for d in self.downloaders if d.get("ip") and d.get("network")
        }
        if not by_ip:
            return
        exempt = self._observability_endpoints()
        for host in state.inventory.get_active_hosts():
            profile = by_ip.get(host.name)
            if not profile:
                continue
            self.console.print(
                f"[cyan]Network impairment on {host.name}: {format_network_label(profile)}[/cyan]"
            )
            add_op(
                state,
                server.shell,
                name=f"Apply network impairment ({profile.name})",
                commands=[build_network_impairment_command(profile, exempt)],
                host=[host],
            )

    def _observability_endpoints(self) -> list[str]:
        """IPs whose traffic must bypass the shaper (InfluxDB, Elasticsearch).

        InfluxDB is derived from ``influx_url``, which this provisioner already
        holds. Elasticsearch is stood up by the deploy command rather than here, so
        it arrives via ``exempt_endpoints``.

        Best-effort by design: an endpoint that cannot be resolved is simply not
        exempted. That degrades the measurement (instrumentation then competes with
        the transfer) but must never fail provisioning — a missing exemption is far
        less bad than a failed deploy. Non-IP hosts are dropped because the tc
        filters match on address; a hostname would silently match nothing.
        """
        endpoints: list[str] = []
        if self.influx_url:
            endpoints.append(urlparse(self.influx_url).hostname or "")
        endpoints.extend(str(e) for e in (self.exempt_endpoints or []))
        resolved: list[str] = []
        for endpoint in endpoints:
            host = str(endpoint).strip()
            if not host:
                continue
            try:
                ipaddress.IPv4Address(host)
            except ipaddress.AddressValueError:
                self.console.print(
                    f"[yellow]Network impairment: exempt endpoint '{host}' is not an "
                    f"IPv4 address; its traffic will be shaped along with the "
                    f"transfer.[/yellow]"
                )
                continue
            if host not in resolved:
                resolved.append(host)
        return resolved

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

    def _add_install_wasm_ops(self, state: State) -> None:
        """Install Node, the WASM bundle and the ``ant-wasm`` CLI.

        No `ant` binary is installed: nothing on this VM runs it. `require_cli`
        is set because a bundle built before the CLI existed would install
        cleanly and then fail every transfer with "cannot find module".
        """
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
                build_bundle_install_command(self.wasm_bundle_url, data_dirs=(), require_cli=True)
            ],
        )
        add_op(
            state,
            server.shell,
            name="Install ant-wasm CLI",
            commands=[build_ant_wasm_cli_command()],
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
        add_op(
            state,
            server.shell,
            name="Deploy prod_downloader.py service script",
            commands=build_service_script_commands(SCRIPTS_DIR, SCRIPT_NAME),
        )

    def _add_env_and_addresses_op(self, state: State) -> None:
        bootstrap_args = " ".join(f"--bootstrap {peer}" for peer in self.bootstrap_peers)

        case_entries = []
        for d in self.downloaders:
            if not d.get("ip"):
                continue
            addrs_b64 = base64.b64encode(d["addresses_file_content"].encode("utf-8")).decode(
                "ascii"
            )
            bootstrap_cache = "true" if d.get("bootstrap_cache", True) else "false"
            case_entries.append(
                f'    "{d["ip"]}") '
                f"REGION={d['region']}; "
                f'ADDRS_B64="{addrs_b64}"; '
                f"BOOTSTRAP_CACHE={bootstrap_cache} ;;"
            )
        case_block = "\n".join(case_entries)

        influx_lines = ""
        if self.influx_url and self.influx_token:
            # WASM file operations go to their own bucket. They are the same
            # `download` measurement the native client writes, so sharing a
            # bucket would let any dashboard panel or ad-hoc query average two
            # transports measured ~4x apart. Decided here rather than by the
            # caller because the provisioner is what knows the transport.
            bucket = INFLUXDB_WASM_BUCKET if self.is_wasm else self.influx_bucket
            influx_lines = (
                f"INFLUX_URL={self.influx_url}\n"
                f"INFLUX_TOKEN={self.influx_token}\n"
                f"INFLUX_ORG={self.influx_org}\n"
                f"INFLUX_BUCKET={bucket}\n"
            )

        # Appended only for a WASM deployment, so a native env file renders
        # byte-identically to before (the service defaults CLIENT_KIND to
        # "native" when the key is absent).
        wasm_lines = ""
        if self.is_wasm:
            wasm_lines = (
                "CLIENT_KIND=wasm\n"
                f"ANT_WASM={ANT_WASM_CLI_PATH}\n"
                f'BOOTSTRAP_MULTIADDRS="{",".join(self.bootstrap_multiaddrs)}"\n'
                f"WASM_DIR={WASM_CLIENT_DIR}/pkg\n"
                f"MAX_DOWNLOAD_BYTES={WASM_MAX_FILE_BYTES}\n"
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
DAILY_AT={self.daily_at}
LAST_CYCLE_FILE={LAST_CYCLE_PATH}
ONE_SHOT={"true" if self.one_shot else "false"}
RUST_LOG={self.rust_log or ""}
MEMORY_PROFILING={"true" if self.memory_profiling else "false"}
DOWNLOAD_DIAGNOSTICS={"true" if self.download_diagnostics else "false"}
HEAPTRACK_DIR={HEAPTRACK_DIR}
{wasm_lines}{influx_lines}ENV_EOF"""

        add_op(
            state,
            server.shell,
            name="Write environment file and per-host address list",
            commands=[env_script],
        )

    def _add_systemd_unit_op(self, state: State) -> None:
        unit = _build_systemd_unit(one_shot=self.one_shot)
        cmd = f"cat > {SERVICE_PATH} << 'UNIT_EOF'\n{unit}UNIT_EOF\nsystemctl daemon-reload"
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


def build_ant_binary_upgrade_command(download_url: str, binary_is_archive: bool = True) -> str:
    """Return the shell command that force-installs the ``ant`` binary.

    Unlike :meth:`ProdDownloaderProvisioner._add_install_ant_op`, this always
    overwrites ``ANT_INSTALL_PATH`` — that install op is gated on
    ``test -f ... && SKIP`` so a plain re-provision never picks up a new
    binary; upgrading an already-deployed VM in place needs the unconditional
    version.
    """
    if binary_is_archive:
        return (
            f"rm -f {ANT_INSTALL_PATH} && "
            f"wget -q {download_url} -O /tmp/ant-archive.tar.gz && "
            f"tar -xzf /tmp/ant-archive.tar.gz -C /tmp/ && "
            f"find /tmp -name ant -type f -path '*/ant-*/*' "
            f"-exec mv {{}} {ANT_INSTALL_PATH} \\; && "
            f"chmod +x {ANT_INSTALL_PATH} && "
            f"rm -rf /tmp/ant-*/ /tmp/ant-archive.tar.gz && "
            f"echo 'ANT_BINARY:UPGRADED'"
        )
    return (
        f"rm -f {ANT_INSTALL_PATH} && "
        f"wget -q {download_url} -O {ANT_INSTALL_PATH} && "
        f"chmod +x {ANT_INSTALL_PATH} && "
        f"echo 'ANT_BINARY:UPGRADED'"
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
