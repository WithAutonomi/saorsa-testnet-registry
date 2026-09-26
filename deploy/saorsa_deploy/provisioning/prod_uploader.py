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
from pathlib import PurePosixPath

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.client_roles import WASM_MAX_FILE_BYTES
from saorsa_deploy.evm import ARBITRUM_ONE
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
from saorsa_deploy.provisioning.wasm_client import (
    ANT_WASM_CLI_PATH,
    WASM_CLIENT_DIR,
    WASM_NODE_MAJOR,
    build_ant_wasm_cli_command,
    build_bundle_install_command,
    build_node_install_command,
)
from saorsa_deploy.resources import get_resources_dir

ANT_INSTALL_PATH = "/root/.local/bin/ant"
SERVICE_NAME = "ant-prod-uploader"
DATA_DIR = "/var/lib/ant-prod-uploader"
SCRIPTS_DIR = "/root/ant-prod-uploader"
LOG_DIR = "/var/log/ant-prod-uploader"
# heaptrack memory-profile output dir (shared with the testnet --heaptrack path).
HEAPTRACK_DIR = "/var/log/ant-heaptrack"
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
        wasm_bundle_url: str | None = None,
        bootstrap_multiaddrs: list[str] | None = None,
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
        # Browser-path uploaders. Both empty on a native-only deployment, in
        # which case not one of the WASM ops is added and every env file renders
        # byte-identically to before this existed.
        self.wasm_bundle_url = wasm_bundle_url
        self.bootstrap_multiaddrs = list(bootstrap_multiaddrs or [])
        self.console = console or Console()
        if self._wasm_uploaders() and not self.wasm_bundle_url:
            raise RuntimeError(
                "a WASM uploader needs a WASM bundle URL — run `build ant-wasm "
                "--name <name> --branch-name <ref> --repo-owner <owner>`"
            )
        if self._wasm_uploaders() and not self.bootstrap_multiaddrs:
            # Without one a browser client cannot reach the network at all, and
            # the failure would surface only as an unexplained connection error
            # minutes after the VM is up and billing.
            raise RuntimeError(
                "a WASM uploader needs at least one bootstrap WebRTC multiaddr "
                "(see `prod bootstrap webrtc-addresses`)"
            )

    def _wasm_uploaders(self) -> list[dict]:
        return [u for u in self.uploaders if u.get("client_kind") == "wasm"]

    def _native_uploaders(self) -> list[dict]:
        return [u for u in self.uploaders if u.get("client_kind") != "wasm"]

    @property
    def any_wasm(self) -> bool:
        return bool(self._wasm_uploaders())

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

            add_operator_authorized_keys_op(state)

            add_op(
                state,
                server.shell,
                name=DISABLE_UNATTENDED_UPGRADES_OP_NAME,
                commands=[build_disable_unattended_upgrades_command()],
            )

            self._add_clock_skew_ops(state)
            self._add_network_impairment_ops(state)

            heaptrack_dirs = f" {HEAPTRACK_DIR}" if self._any_memory_profiling() else ""
            add_op(
                state,
                server.shell,
                name="Create prod-uploader directories",
                commands=[
                    f"mkdir -p {DATA_DIR} {DATA_DIR}/files {SCRIPTS_DIR} "
                    f"{LOG_DIR} /root/.local/bin{heaptrack_dirs}",
                ],
            )

            self._add_install_heaptrack_op(state)
            self._add_install_ant_op(state)
            self._add_install_wasm_ops(state)
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

    def _add_network_impairment_ops(self, state: State) -> None:
        """Shape any uploader whose spec sets a `network` profile.

        This is the primary use case for the feature: a cloud VM's ~gigabit uplink
        is nothing like the connection a real user uploads from, and upstream rate
        is what dominates an upload. One op per host — each VM carries its own
        profile, so a run can compare a shaped uploader against an unshaped control.
        """
        by_ip = {
            u["ip"]: u["network"]
            for u in self.uploaders
            if u.get("ip") and u.get("network")
        }
        if not by_ip:
            return
        for host in state.inventory.get_active_hosts():
            profile = by_ip.get(host.name)
            if not profile:
                continue
            self.console.print(
                f"[cyan]Network impairment on {host.name}: "
                f"{format_network_label(profile)}[/cyan]"
            )
            add_op(
                state,
                server.shell,
                name=f"Apply network impairment ({profile.name})",
                commands=[build_network_impairment_command(profile)],
                host=[host],
            )

    def _add_clock_skew_ops(self, state: State) -> None:
        """Inject clock skew on any uploader whose spec sets a clock_skew duration.

        Each such host shifts by its own explicit signed offset, so we add one op
        per host.
        """
        skew_by_ip = {
            u["ip"]: u["clock_skew"]
            for u in self.uploaders
            if u.get("ip") and u.get("clock_skew")
        }
        if not skew_by_ip:
            return
        self.console.print(
            f"[cyan]Clock skew enabled on {len(skew_by_ip)} uploader host(s).[/cyan]"
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

    def _any_memory_profiling(self) -> bool:
        return any(u.get("memory_profiling") for u in self.uploaders)

    def _add_install_heaptrack_op(self, state: State) -> None:
        """Install the heaptrack apt package — only when a host opts into profiling.

        Installed across the whole inventory (cheap, idempotent) rather than
        per-host; the service script only wraps ant with heaptrack on hosts whose
        MEMORY_PROFILING env is true, so installing everywhere is harmless.
        """
        if not self._any_memory_profiling():
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

    def _hosts_for(self, state: State, uploaders: list[dict]) -> list:
        """The pyinfra hosts matching the given uploader entries.

        Targeting is by IP because that is what the inventory is keyed on. Used
        so a native VM never installs the WASM bundle and a browser-path VM never
        installs `ant`: nothing on either runs the other's binary, and on a
        WASM-only deployment there may be no `ant` build to fetch at all.
        """
        wanted = {u["ip"] for u in uploaders if u.get("ip")}
        return [h for h in state.inventory.get_active_hosts() if h.name in wanted]

    def _add_install_ant_op(self, state: State) -> None:
        native_hosts = (
            self._hosts_for(state, self._native_uploaders())
            if self.any_wasm
            else None
        )
        if self.any_wasm and not native_hosts:
            return
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

        # `host=None` (a native-only deployment) means the whole inventory, which
        # is what it has always meant here — so the op renders unchanged.
        add_op(
            state,
            server.shell,
            name="Download and install ant binary",
            commands=[install_cmd],
            **({"host": native_hosts} if native_hosts is not None else {}),
        )

    def _add_install_wasm_ops(self, state: State) -> None:
        """Install Node, the WASM bundle and the ``ant-wasm`` CLI on WASM hosts.

        Targeted per host rather than across the inventory: a native uploader
        never runs any of it, and installing Node on it would slow every deploy
        for nothing. ``require_cli`` is set because a bundle built before the CLI
        existed would install cleanly and then fail every upload with "cannot
        find module".
        """
        wasm_hosts = self._hosts_for(state, self._wasm_uploaders())
        if not wasm_hosts:
            return
        add_op(
            state,
            server.shell,
            name=f"Install Node.js {WASM_NODE_MAJOR}",
            commands=[build_node_install_command()],
            host=wasm_hosts,
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
            host=wasm_hosts,
        )
        add_op(
            state,
            server.shell,
            name="Install ant-wasm CLI",
            commands=[build_ant_wasm_cli_command()],
            host=wasm_hosts,
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
            payment_type = u.get("payment_type") or ""
            log_level = u.get("log_level") or "info"
            bootstrap_cache = "true" if u.get("bootstrap_cache", True) else "false"
            rust_log = u.get("rust_log") or ""
            memory_profiling = "true" if u.get("memory_profiling") else "false"
            entry = (
                f'    "{u["ip"]}") '
                f'REGION={u["region"]}; '
                f'CLIENT_INDEX={u["client_index"]}; '
                f'URLS_B64="{urls_b64}"; '
                f'PAYMENT_TYPE="{payment_type}"; '
                f'LOG_LEVEL="{log_level}"; '
                f'RUST_LOG="{rust_log}"; '
                f"MEMORY_PROFILING={memory_profiling}; "
                f"BOOTSTRAP_CACHE={bootstrap_cache}"
            )
            if self.any_wasm:
                # Per-host, because a mixed deployment's two tiers are the same
                # unit reading the same env file template: the transport and the
                # bucket differ per VM. Added only when a WASM uploader exists so
                # a native-only deployment's env file is byte-identical.
                #
                # WASM operations go to their OWN Influx bucket. They are the
                # same `prod_upload` measurement the native tier writes, so one
                # bucket would let any dashboard panel or ad-hoc query average
                # two transports measured several times apart.
                is_wasm = u.get("client_kind") == "wasm"
                bucket = INFLUXDB_WASM_BUCKET if is_wasm else self.influx_bucket
                entry += (
                    f'; CLIENT_KIND="{"wasm" if is_wasm else "native"}"'
                    f'; HOST_INFLUX_BUCKET="{bucket}"'
                )
            case_entries.append(f"{entry} ;;")
        case_block = "\n".join(case_entries)

        influx_lines = ""
        if self.influx_url and self.influx_token:
            bucket_value = (
                "$HOST_INFLUX_BUCKET" if self.any_wasm else self.influx_bucket
            )
            influx_lines = (
                f"INFLUX_URL={self.influx_url}\n"
                f"INFLUX_TOKEN={self.influx_token}\n"
                f"INFLUX_ORG={self.influx_org}\n"
                f"INFLUX_BUCKET={bucket_value}\n"
            )

        # Appended only for a deployment with a WASM uploader, so a native env
        # file renders byte-identically (the service defaults CLIENT_KIND to
        # "native" when the key is absent). Inert on a native host: the service
        # zeroes MAX_UPLOAD_BYTES unless the transport is WASM, and nothing else
        # here is read by the `ant` path.
        wasm_lines = ""
        if self.any_wasm:
            wasm_lines = (
                "CLIENT_KIND=$CLIENT_KIND\n"
                f"ANT_WASM={ANT_WASM_CLI_PATH}\n"
                f'BOOTSTRAP_MULTIADDRS="{",".join(self.bootstrap_multiaddrs)}"\n'
                f"WASM_DIR={WASM_CLIENT_DIR}/pkg\n"
                f"MAX_UPLOAD_BYTES={WASM_MAX_FILE_BYTES}\n"
                # The browser path's payment callbacks sign with viem against a
                # plain JSON-RPC endpoint, so the chain is named by URL rather
                # than by `--evm-network`. Arbitrum One: prod uploads cost real
                # ETH.
                f"RPC_URL={ARBITRUM_ONE['rpc_url']}\n"
            )

        # The peers file is written unconditionally: ant only reads it when
        # invoked without --bootstrap (BOOTSTRAP_CACHE=true), so it is inert
        # on cache-disabled hosts.
        peers_toml = render_bootstrap_peers_toml(self.bootstrap_peers)
        peers_dir = PurePosixPath(REMOTE_BOOTSTRAP_PEERS_TOML_PATH).parent

        # The fallback arm has to define every variable the body expands, or an
        # unmatched IP would write an env file with an empty bucket name.
        default_wasm_arm = (
            f'; CLIENT_KIND="native"; HOST_INFLUX_BUCKET="{self.influx_bucket}"'
            if self.any_wasm
            else ""
        )

        env_script = f"""\
MY_IP=$(hostname -I | awk '{{print $1}}')
case "$MY_IP" in
{case_block}
    *) REGION=unknown; CLIENT_INDEX=0; URLS_B64=""; PAYMENT_TYPE=""; \
LOG_LEVEL="info"; RUST_LOG=""; MEMORY_PROFILING=false; BOOTSTRAP_CACHE=true\
{default_wasm_arm} ;;
esac
mkdir -p {DATA_DIR}
echo "$URLS_B64" | base64 -d > {URLS_PATH}
mkdir -p {peers_dir}
cat > {REMOTE_BOOTSTRAP_PEERS_TOML_PATH} << 'PEERS_EOF'
{peers_toml}PEERS_EOF
cat > {ENV_PATH} << ENV_EOF
HOME=/root
REGION=$REGION
CLIENT_INDEX=$CLIENT_INDEX
BOOTSTRAP_ARGS="{bootstrap_args}"
BOOTSTRAP_CACHE=$BOOTSTRAP_CACHE
SECRET_KEY={secret_key}
ANT={ANT_INSTALL_PATH}
URLS_FILE={URLS_PATH}
WORK_DIR={DATA_DIR}/files
EVM_NETWORK={self.evm_network}
PAYMENT_TYPE=$PAYMENT_TYPE
LOG_LEVEL=$LOG_LEVEL
RUST_LOG=$RUST_LOG
MEMORY_PROFILING=$MEMORY_PROFILING
HEAPTRACK_DIR={HEAPTRACK_DIR}
{wasm_lines}{influx_lines}ENV_EOF"""

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
