
import base64
from pathlib import PurePosixPath

import boto3
from botocore.config import Config as BotoConfig
from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.client_roles import (
    ROLE_POINTER,
    ROLE_UPLOAD,
    WASM_MAX_FILE_BYTES,
    is_pointer,
    is_upload,
    is_wasm,
    unit_for_role,
)
from saorsa_deploy.prod_bootstraps import (
    REMOTE_BOOTSTRAP_PEERS_TOML_PATH,
    render_bootstrap_peers_toml,
)
from saorsa_deploy.provisioning.apt import (
    DISABLE_UNATTENDED_UPGRADES_OP_NAME,
    SET_HOSTNAME_OP_NAME,
    build_disable_unattended_upgrades_command,
    build_set_hostname_command,
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
    WASM_CLIENT_DATA_DIR,
    WASM_CLIENT_DIR,
    WASM_CLIENT_ENV_PATH,
    WASM_CLIENT_LOG_DIR,
    WASM_NODE_MAJOR,
    WASM_SERVICE_ENTRY,
    build_bundle_install_command,
    build_node_install_command,
    render_env_file,
)
from saorsa_deploy.provisioning.wasm_client import (
    build_systemd_unit as build_wasm_systemd_unit,
)
from saorsa_deploy.resources import get_resources_dir

ANT_INSTALL_PATH = "/root/.local/bin/ant"
CLIENT_DATA_DIR = "/var/lib/ant-client"
SCRIPTS_DIR = "/root/ant-client"
#: Owner keys and the pointer ledger. Separate from CLIENT_DATA_DIR because the
#: contents are not disposable: a pointer's owner can never change, so a lost
#: key is a pointer that can never be updated again.
POINTER_DATA_DIR = "/var/lib/ant-pointer"
POINTER_UNIT = unit_for_role(ROLE_POINTER)


def _build_env_file(
    bootstrap_peers: list[str],
    file_size_mb: int,
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
    """Build the systemd unit file for the ant-downloader service.

    Downloaders run on dedicated VMs now, so there's no local ant-uploader to
    wait for — addresses are fetched from S3.
    """
    return f"""\
[Unit]
Description=Ant Client Downloader
After=network-online.target
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




def _build_pointer_unit() -> str:
    """systemd unit for the ant-pointer service.

    ``Restart=always``, unlike the transfer clients' ``on-failure``: the service
    is meant to run for the whole testnet, and its durable ledger means a
    restart resumes rather than starts over. A ledger-less restart would abandon
    pointers it had already paid for.
    """
    return f"""\
[Unit]
Description=Ant Pointer Client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/ant-client.env
ExecStart=/usr/bin/python3 {SCRIPTS_DIR}/pointer_client.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


#: Keys in the testnet WASM env file whose value is double-quoted. Explicit
#: because the quoting is part of the file's bytes: a multiaddr list, a
#: presigned URL and a RUST_LOG filter all contain characters a shell would
#: otherwise split on, while the numeric and identifier values never have.
WASM_ENV_QUOTED_KEYS = (
    "BOOTSTRAP_MULTIADDRS",
    "ADDRESSES_S3_BUCKET",
    "ADDRESSES_S3_PREFIX",
    "UPLOAD_PRESIGNED_URL",
    "SECRET_KEY",
    "RPC_URL",
    "PAYMENT_TYPE",
    "RUST_LOG",
)


def _normalize_secret(k: str) -> str:
    """0x-prefix a wallet key, tolerating whitespace and a missing prefix."""
    k = (k or "").strip()
    if not k:
        return ""
    if not k.startswith("0x"):
        k = "0x" + k
    return k


def _build_wasm_unit(role: str) -> str:
    """systemd unit for a WASM client service.

    Node rather than bash, and one unit shape for both roles (the program
    branches on ROLE from the environment file), because upload and download
    share the whole WASM setup -- loading the module, installing the WebRTC
    polyfill, bootstrapping from the bootstrap multiaddrs.
    """
    label = "Uploader" if role == "wasm-upload" else "Downloader"
    return build_wasm_systemd_unit(
        f"Ant WASM Client {label}",
        exec_start=f"/usr/bin/node {WASM_SERVICE_ENTRY}",
    )


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
        aws_access_key_id: str,
        aws_secret_access_key: str,
        addresses_s3_bucket: str,
        addresses_s3_prefix: str,
        ssh_key_path: str = "~/.ssh/id_rsa",
        console: Console | None = None,
        binary_url: str | None = None,
        binary_is_archive: bool = False,
        aws_region: str = "eu-west-2",
        influx_url: str = "",
        influx_token: str = "",
        influx_org: str = "ant",
        influx_bucket: str = "testnet-metrics",
        wallet_secret_key: str = "",
        addresses_file_content: str | None = None,
        heaptrack_targets: frozenset[str] = frozenset(),
        deployment_name: str = "",
        elasticsearch_url: str = "",
        elasticsearch_username: str = "",
        elasticsearch_password: str = "",
        forward_buffer_limit: int | None = None,
        wasm_bundle_url: str | None = None,
        bootstrap_webrtc_multiaddrs: list[str] | None = None,
        wasm_rpc_url: str | None = None,
        evm_network: str = "arbitrum-sepolia",
        rpc_url: str = "",
    ):
        self.clients = clients
        self.deployment_name = deployment_name
        self.bootstrap_peers = bootstrap_peers
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.aws_region = aws_region
        self.addresses_s3_bucket = addresses_s3_bucket
        self.addresses_s3_prefix = addresses_s3_prefix
        self.ssh_key_path = ssh_key_path
        self.console = console or Console()
        self.binary_url = binary_url
        self.binary_is_archive = binary_is_archive
        self.influx_url = influx_url
        self.influx_token = influx_token
        self.influx_org = influx_org
        self.influx_bucket = influx_bucket
        self.wallet_secret_key = wallet_secret_key
        self.addresses_file_content = addresses_file_content
        self.heaptrack_targets = heaptrack_targets
        self.elasticsearch_url = elasticsearch_url
        self.elasticsearch_username = elasticsearch_username
        self.elasticsearch_password = elasticsearch_password
        self.forward_buffer_limit = forward_buffer_limit
        # WASM (browser-path) clients. All empty on a deployment with none, in
        # which case not one of the WASM ops is added and every native client VM
        # is provisioned exactly as it was before this existed.
        self.wasm_bundle_url = wasm_bundle_url
        self.bootstrap_webrtc_multiaddrs = list(bootstrap_webrtc_multiaddrs or [])
        self.wasm_rpc_url = wasm_rpc_url
        # The chain the pointer client pays on, and the RPC it reads its own
        # native balance from. Every pointer write is a paid transaction and
        # `ant pointer create --json` reports no cost, so a balance delta either
        # side of the write is the only way to know what one actually cost.
        self.evm_network = evm_network
        self.rpc_url = rpc_url

    def _build_ip_to_env(self, fallback_secret: str) -> dict:
        """Per-VM service environment, keyed by IP.

        Each entry carries the client's ACTUAL role. It must never be
        normalised to "upload"/"download": the WASM roles are what select the
        WASM provisioning ops and the WASM systemd unit, and collapsing them
        here silently provisions a WASM VM as a native one.
        """
        ip_to_env = {}
        for c in self.clients:
            if not c.get("ip"):
                continue
            role = c.get("role", "upload")
            # heaptrack wraps the native `ant` invocation, so it is
            # meaningless on the WASM roles — keep it native-only.
            heap = (
                "1"
                if (role == "upload" and "uploader" in self.heaptrack_targets)
                or (role == "download" and "downloader" in self.heaptrack_targets)
                else "0"
            )
            bootstrap_cache = (
                "true" if c.get("bootstrap_cache", True) else "false"
            )
            forward_logs = "true" if c.get("forward_logs") else "false"
            rust_log = c.get("rust_log") or ""
            # The env entry carries the client's ACTUAL role, never a
            # normalised "upload"/"download". Collapsing it here made both
            # WASM roles look like native downloaders: `_add_wasm_ops` saw
            # no WASM hosts and installed nothing, while the native unit
            # writer put an `ant-downloader.service` on those VMs — and
            # `clients start`, which maps the real role to its unit, then
            # failed on a service that was never written.
            if is_upload(role):
                per_uploader_key = _normalize_secret(c.get("wallet_secret_key", ""))
                ip_to_env[c["ip"]] = {
                    "role": role,
                    # 0 for a pointer client: a pointer record is a fixed ~5 KB
                    # and there is no file to generate. The key is still present
                    # because every shared path reads it.
                    "file_size_kb": (c.get("file_size_mb") or 0) * 1024,
                    "region": c["region"],
                    "client_index": c["client_index"],
                    "payment_type": c.get("payment_type") or "",
                    "secret_key": per_uploader_key or fallback_secret,
                    "heaptrack": heap,
                    "bootstrap_cache": bootstrap_cache,
                    "forward_logs": forward_logs,
                    "rust_log": rust_log,
                    "upload_presigned_url": self._presigned_put_url(
                        role, c["region"], c["client_index"]
                    ),
                    **(self._pointer_env(c) if is_pointer(role) else {}),
                }
            else:
                ip_to_env[c["ip"]] = {
                    "role": role,
                    "file_size_kb": 0,
                    "region": c["region"],
                    "client_index": c["client_index"],
                    "payment_type": "",
                    "secret_key": fallback_secret,
                    "heaptrack": heap,
                    "bootstrap_cache": bootstrap_cache,
                    "forward_logs": forward_logs,
                    "rust_log": rust_log,
                    "upload_presigned_url": "",
                }
        return ip_to_env

    @staticmethod
    def _pointer_env(client: dict) -> dict:
        """The workload rates one pointer client runs at.

        Read from the client's own spec, not from a shared default, because the
        rates are what the run's cost and its Arbitrum Sepolia transaction rate
        are: two pointer clients in one deployment may legitimately differ.
        """
        return {
            "create_interval_secs": client.get("create_interval_secs", 60),
            "update_interval_secs": client.get("update_interval_secs", 30),
            "sweep_interval_secs": client.get("sweep_interval_secs", 600),
            "chain_fraction": client.get("chain_fraction", 0.10),
            "pointer_max": client.get("pointer_max", 0),
        }

    def _add_wasm_ops(self, state, ip_to_env: dict) -> None:
        """Provision the WASM client on every host running a wasm-* role.

        No-op when the deployment has no WASM clients, which keeps every
        existing testnet's client provisioning byte-for-byte unchanged.
        """
        wasm_ips = {ip for ip, env in ip_to_env.items() if is_wasm(env["role"])}
        if not wasm_ips:
            return
        if not self.wasm_bundle_url:
            raise RuntimeError(
                "WASM clients are configured but no bundle URL was resolved — "
                "run `build ant-wasm --name <name> --branch-name <branch> "
                "--repo-owner <owner>` first"
            )
        if not self.bootstrap_webrtc_multiaddrs:
            # Without a bootstrap address a WASM client cannot reach the network at
            # all, and the failure would only surface as an unexplained
            # connection error minutes after the VMs are up and billing.
            raise RuntimeError(
                "WASM clients are configured but the deployment state holds no "
                "WebRTC-Direct bootstrap addresses — run `deployments "
                "webrtc-addresses --name <name>` after the nodes are started"
            )

        wasm_hosts = [h for h in state.inventory.get_active_hosts() if h.name in wasm_ips]
        if not wasm_hosts:
            return

        self.console.print(
            f"[cyan]Provisioning {len(wasm_hosts)} WASM client host(s) "
            f"({len(self.bootstrap_webrtc_multiaddrs)} bootstrap address(es)).[/cyan]"
        )

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
                    self.wasm_bundle_url,
                    data_dirs=(WASM_CLIENT_DATA_DIR, WASM_CLIENT_LOG_DIR),
                )
            ],
            host=wasm_hosts,
        )

        bootstraps = ",".join(self.bootstrap_webrtc_multiaddrs)
        for host in wasm_hosts:
            env = ip_to_env[host.name]
            values = {
                "ROLE": env["role"],
                "REGION": env["region"],
                "CLIENT_INDEX": env["client_index"],
                "BOOTSTRAP_MULTIADDRS": bootstraps,
                "FILE_SIZE_KB": env["file_size_kb"],
                "WASM_DIR": f"{WASM_CLIENT_DIR}/pkg",
                "LOG_DIR": WASM_CLIENT_LOG_DIR,
                "ADDRESSES_FILE": f"{WASM_CLIENT_DATA_DIR}/addresses.txt",
                "ADDRESSES_S3_BUCKET": self.addresses_s3_bucket,
                "ADDRESSES_S3_PREFIX": self.addresses_s3_prefix,
                "UPLOAD_PRESIGNED_URL": env["upload_presigned_url"],
                "SECRET_KEY": env["secret_key"],
                "RPC_URL": self.wasm_rpc_url or "",
                # The manifest's `payment:` for this entry ("merkle" /
                # "no-merkle" / unset). The harness maps it to ant-core's
                # explicit payment mode; it must never rely on ant-core's
                # "auto" default, which needs a merkle wallet for big batches.
                "PAYMENT_TYPE": env["payment_type"],
                "RUST_LOG": env["rust_log"],
                # The address pool is shared with the native tier, whose 1000 MB
                # uploader publishes files ant-core's browser path cannot
                # download. Address lines carry the size, so the downloader
                # skips those instead of fetching a data map to be refused.
                "MAX_DOWNLOAD_BYTES": WASM_MAX_FILE_BYTES,
            }
            if self.influx_url and self.influx_token:
                values.update(
                    {
                        "INFLUX_URL": self.influx_url,
                        "INFLUX_TOKEN": self.influx_token,
                        "INFLUX_ORG": self.influx_org,
                        # WASM file operations go to their own bucket. They are
                        # the same `upload`/`download` measurements the native
                        # clients write, so sharing a bucket would let any
                        # dashboard panel or ad-hoc query average two transports
                        # measured ~2-4x apart. (Host metrics are unaffected — a
                        # WASM client VM's telegraf still reports to the shared
                        # client bucket.)
                        "INFLUX_BUCKET": INFLUXDB_WASM_BUCKET,
                    }
                )
            body = render_env_file(values, quoted=WASM_ENV_QUOTED_KEYS)
            unit = unit_for_role(env["role"])
            other = unit_for_role(
                "wasm-download" if env["role"] == "wasm-upload" else "wasm-upload"
            )
            add_op(
                state,
                server.shell,
                name="Write WASM client env file and unit",
                commands=[
                    f"cat > {WASM_CLIENT_ENV_PATH} << 'ENV_EOF'\n{body}ENV_EOF\n"
                    f"chmod 600 {WASM_CLIENT_ENV_PATH}\n"
                    f"cat > /etc/systemd/system/{unit}.service << 'UNIT_EOF'\n"
                    f"{_build_wasm_unit(env['role'])}UNIT_EOF\n"
                    f"rm -f /etc/systemd/system/{other}.service\n"
                    "systemctl daemon-reload"
                ],
                host=[host],
            )

    @property
    def wasm_clients(self) -> list[dict]:
        """Clients running the WASM transport (either role), with an IP."""
        return [
            c for c in self.clients if c.get("ip") and is_wasm(c.get("role", "upload"))
        ]

    #: Per-role aggregate log files that uploader.sh/downloader.sh append to
    #: when FORWARD_LOGS=true, and the role label tagged on forwarded lines.
    _FORWARD_ROLE_FILES = {
        "upload": (
            [
                "/var/log/ant-client-forward/uploader.stdout.log",
                "/var/log/ant-client-forward/uploader.stderr.log",
            ],
            "uploader",
        ),
        "download": (
            [
                "/var/log/ant-client-forward/downloader.stdout.log",
                "/var/log/ant-client-forward/downloader.stderr.log",
            ],
            "downloader",
        ),
        # The pointer service tees its own stdout/stderr into these, the same
        # way uploader.sh appends its per-op captures. Omitting the role here is
        # how the WASM roles silently forwarded nothing: the loop only visits
        # the roles named in this map.
        "pointer": (
            [
                "/var/log/ant-client-forward/pointer.stdout.log",
                "/var/log/ant-client-forward/pointer.stderr.log",
            ],
            "pointer",
        ),
    }

    def provision_log_forwarders(self) -> None:
        """Install the client→Elasticsearch telegraf forwarder on opted-in VMs.

        Groups opted-in clients by role and installs the forwarder against the
        per-role aggregate stdout/stderr files. No-op unless at least one client
        has ``forward_logs`` set; raises if one does but no ES creds are known.
        """
        forwarding = [
            c for c in self.clients if c.get("ip") and c.get("forward_logs")
        ]
        if not forwarding:
            return
        if not (
            self.elasticsearch_url
            and self.elasticsearch_username
            and self.elasticsearch_password
        ):
            raise RuntimeError(
                "forward_logs is set on one or more clients but no Elasticsearch "
                "credentials are available — ensure elastic is enabled and "
                "provisioned for this deployment before provisioning clients."
            )
        from saorsa_deploy.provisioning.telegraf_client import (
            ClientTelegrafProvisioner,
        )

        for role_key, (globs, role_label) in self._FORWARD_ROLE_FILES.items():
            ips = [
                c["ip"]
                for c in forwarding
                if c.get("role", "upload") == role_key
            ]
            if not ips:
                continue
            self.console.print(
                f"Installing client log forwarder on {len(ips)} {role_label} "
                f"host(s)..."
            )
            # Only override the forwarder's buffer when explicitly sized — leave
            # ClientTelegrafProvisioner's default in place otherwise.
            buffer_kwargs = (
                {"metric_buffer_limit": self.forward_buffer_limit}
                if self.forward_buffer_limit is not None
                else {}
            )
            ClientTelegrafProvisioner(
                host_ips=ips,
                ssh_key_path=self.ssh_key_path,
                testnet_name=self.deployment_name,
                elasticsearch_url=self.elasticsearch_url,
                elasticsearch_username=self.elasticsearch_username,
                elasticsearch_password=self.elasticsearch_password,
                role=role_label,
                tail_globs=globs,
                console=self.console,
                **buffer_kwargs,
            ).execute()

    def _presigned_put_url(self, role: str, region: str, client_index) -> str:
        """Mint a presigned PUT URL for one uploader's address-list object.

        The uploader flushes its address list with a plain curl PUT against
        this URL: presigned URLs carry the controller's signature, so the
        flush keeps working on clock-skewed VMs whose own SigV4 requests are
        rejected (~15min tolerance). Expiry is the SigV4 maximum of 7 days —
        re-run `clients provision` to refresh on longer-lived testnets.
        """
        if not (self.aws_access_key_id and self.aws_secret_access_key):
            return ""
        # Pin the regional endpoint: presigning via the global s3.amazonaws.com
        # endpoint yields 307 redirects on younger buckets, which curl -f does
        # not treat as failure — the flush would silently no-op.
        s3 = boto3.client(
            "s3",
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            region_name=self.aws_region,
            endpoint_url=f"https://s3.{self.aws_region}.amazonaws.com",
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
            ),
        )
        # The key is namespaced by role. client_index restarts at 1 for each
        # role, so a native and a WASM uploader in the same region would
        # otherwise both own `upload-<region>-1.txt` — and since each flush is a
        # whole-object PUT, each would repeatedly erase the other's address
        # list. The native name is unchanged, so existing deployments keep
        # writing exactly where they always did.
        key_role = "upload" if role == ROLE_UPLOAD else role
        key = f"{self.addresses_s3_prefix}/{key_role}-{region}-{client_index}.txt"
        return s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.addresses_s3_bucket, "Key": key},
            ExpiresIn=7 * 24 * 3600,
        )

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

            add_operator_authorized_keys_op(state)

            add_op(
                state,
                server.shell,
                name=DISABLE_UNATTENDED_UPGRADES_OP_NAME,
                commands=[build_disable_unattended_upgrades_command()],
            )

            # Set each VM's OS hostname to its inventory name. Only the
            # vultr-client terraform module sets the hostname from the VM name;
            # DO / OVH / Hetzner client images boot with a provider-assigned
            # machine-id hostname. uploader.sh / downloader.sh tag their Influx
            # points with `$(hostname)` and Telegraf captures it too, so an
            # unset hostname makes a client's metrics land under an opaque tag.
            # Apply it here (one op per host) so it is correct even when
            # monitoring is disabled and the Telegraf path never runs. The
            # hostname format matches vultr-client/main.tf and the inventory:
            # `<name>-ant-client-<role>-<region>-<index>`.
            ip_to_hostname = {
                c["ip"]: (
                    f"{self.deployment_name}-ant-client-"
                    f"{c.get('role', 'upload')}-{c['region']}-{c['client_index']}"
                )
                for c in self.clients
                if c.get("ip")
                and self.deployment_name
                and c.get("region") is not None
                and c.get("client_index") is not None
            }
            if ip_to_hostname:
                for host in state.inventory.get_active_hosts():
                    hostname = ip_to_hostname.get(host.name)
                    if not hostname:
                        continue
                    add_op(
                        state,
                        server.shell,
                        name=SET_HOSTNAME_OP_NAME,
                        commands=[build_set_hostname_command(hostname)],
                        host=[host],
                    )

            # Inject clock skew on any client whose spec sets a clock_skew
            # duration. Each such host shifts by its own explicit signed offset,
            # so we add one op per host.
            skew_by_ip = {
                c["ip"]: c["clock_skew"]
                for c in self.clients
                if c.get("ip") and c.get("clock_skew")
            }
            if skew_by_ip:
                self.console.print(
                    f"[cyan]Clock skew enabled on {len(skew_by_ip)}/"
                    f"{len(host_ips)} client host(s).[/cyan]"
                )
                active_hosts = list(state.inventory.get_active_hosts())
                for host in active_hosts:
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

            # Apply simulated residential link conditions on any client whose
            # spec sets a `network` profile. Secondary to the prod kinds: a shaped
            # client makes a run's transfer percentiles incomparable to an unshaped
            # baseline, so this must not be used on a staging gate run.
            network_by_ip = {
                c["ip"]: c["network"]
                for c in self.clients
                if c.get("ip") and c.get("network")
            }
            if network_by_ip:
                self.console.print(
                    f"[cyan]Network impairment on {len(network_by_ip)}/"
                    f"{len(host_ips)} client host(s).[/cyan]"
                )
                for host in list(state.inventory.get_active_hosts()):
                    profile = network_by_ip.get(host.name)
                    if not profile:
                        continue
                    self.console.print(
                        f"[cyan]  {host.name}: {format_network_label(profile)}[/cyan]"
                    )
                    add_op(
                        state,
                        server.shell,
                        name=f"Apply network impairment ({profile.name})",
                        commands=[build_network_impairment_command(profile)],
                        host=[host],
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
            pointer_script = (scripts_dir / "pointer_client.py").read_text()

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

            # The pointer workload, targeted at the pointer VMs only: it is a
            # few hundred lines of Python that a transfer client has no use for.
            pointer_ips = {
                c["ip"]
                for c in self.clients
                if c.get("ip") and is_pointer(c.get("role", ""))
            }
            pointer_hosts = [
                h for h in state.inventory.get_active_hosts() if h.name in pointer_ips
            ]
            if pointer_hosts:
                add_op(
                    state,
                    server.shell,
                    name="Deploy pointer client script",
                    commands=[
                        f"mkdir -p {POINTER_DATA_DIR}/keys && "
                        f"chmod 700 {POINTER_DATA_DIR}",
                        f"cat > {SCRIPTS_DIR}/pointer_client.py << 'SCRIPT_EOF'\n"
                        f"{pointer_script}SCRIPT_EOF",
                        f"chmod +x {SCRIPTS_DIR}/pointer_client.py",
                    ],
                    host=pointer_hosts,
                )

            if self.heaptrack_targets:
                add_op(
                    state,
                    server.shell,
                    name="Install heaptrack",
                    commands=[
                        "command -v heaptrack >/dev/null && echo 'HEAPTRACK:SKIP' || "
                        "(DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
                        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
                        "heaptrack && echo 'HEAPTRACK:INSTALLED')",
                    ],
                )

            # Install AWS CLI v2 via the official installer — uploaders flush
            # to S3 and downloaders fetch from S3. We avoid the distro `awscli`
            # package (CLI v1, deprecated, and missing from Ubuntu 24.04
            # minimal images) in favour of AWS's static binary.
            add_op(
                state,
                server.shell,
                name="Install AWS CLI v2",
                commands=[
                    "command -v aws >/dev/null && echo 'AWSCLI:SKIP' || "
                    "(DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
                    "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
                    "unzip curl ca-certificates && "
                    "curl -sSL 'https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip' "
                    "-o /tmp/awscliv2.zip && "
                    "unzip -q -o /tmp/awscliv2.zip -d /tmp && "
                    "/tmp/aws/install --update && "
                    "rm -rf /tmp/aws /tmp/awscliv2.zip && "
                    "echo 'AWSCLI:INSTALLED')",
                ],
            )

            # Build a case statement that maps IP to role-specific values.
            # Emits ROLE / FILE_SIZE_KB / REGION / CLIENT_INDEX.
            fallback_secret = _normalize_secret(self.wallet_secret_key)

            ip_to_env = self._build_ip_to_env(fallback_secret)

            case_entries = []
            for ip, env in ip_to_env.items():
                case_entries.append(
                    f'    "{ip}") ROLE={env["role"]}; '
                    f'FILE_SIZE_KB={env["file_size_kb"]}; '
                    f'REGION={env["region"]}; '
                    f'CLIENT_INDEX={env["client_index"]}; '
                    f'PAYMENT_TYPE="{env["payment_type"]}"; '
                    f'SECRET_KEY="{env["secret_key"]}"; '
                    f'HEAPTRACK={env["heaptrack"]}; '
                    f'BOOTSTRAP_CACHE={env["bootstrap_cache"]}; '
                    f'FORWARD_LOGS={env["forward_logs"]}; '
                    f'RUST_LOG="{env["rust_log"]}"; '
                    f'CREATE_INTERVAL_SECS={env.get("create_interval_secs", 0)}; '
                    f'UPDATE_INTERVAL_SECS={env.get("update_interval_secs", 0)}; '
                    f'SWEEP_INTERVAL_SECS={env.get("sweep_interval_secs", 0)}; '
                    f'CHAIN_FRACTION={env.get("chain_fraction", 0)}; '
                    f'POINTER_MAX={env.get("pointer_max", 0)}; '
                    f'UPLOAD_PRESIGNED_URL="{env["upload_presigned_url"]}" ;;'
                )
            case_block = "\n".join(case_entries)

            # The peers file is written unconditionally: ant only reads it when
            # invoked without --bootstrap (i.e. BOOTSTRAP_CACHE=true), so it is
            # inert on cache-disabled hosts.
            peers_toml = render_bootstrap_peers_toml(self.bootstrap_peers)
            add_op(
                state,
                server.shell,
                name="Write ant bootstrap_peers.toml",
                commands=[
                    f"mkdir -p {PurePosixPath(REMOTE_BOOTSTRAP_PEERS_TOML_PATH).parent} && "
                    f"cat > {REMOTE_BOOTSTRAP_PEERS_TOML_PATH} << 'PEERS_EOF'\n"
                    f"{peers_toml}PEERS_EOF",
                ],
            )

            bootstrap_args = " ".join(f"--bootstrap {peer}" for peer in self.bootstrap_peers)
            env_script = f"""\
MY_IP=$(hostname -I | awk '{{print $1}}')
case "$MY_IP" in
{case_block}
    *) ROLE=upload; FILE_SIZE_KB=51200; REGION=unknown; CLIENT_INDEX=0; \
PAYMENT_TYPE=""; SECRET_KEY="{fallback_secret}"; HEAPTRACK=0; BOOTSTRAP_CACHE=true; \
FORWARD_LOGS=false; RUST_LOG=""; CREATE_INTERVAL_SECS=0; UPDATE_INTERVAL_SECS=0; \
SWEEP_INTERVAL_SECS=0; CHAIN_FRACTION=0; POINTER_MAX=0; UPLOAD_PRESIGNED_URL="" ;;
esac
cat > /etc/ant-client.env << ENV_EOF
HOME=/root
ROLE=$ROLE
REGION=$REGION
CLIENT_INDEX=$CLIENT_INDEX
BOOTSTRAP_ARGS="{bootstrap_args}"
BOOTSTRAP_CACHE=$BOOTSTRAP_CACHE
FORWARD_LOGS=$FORWARD_LOGS
RUST_LOG=$RUST_LOG
FILE_SIZE_KB=$FILE_SIZE_KB
PAYMENT_TYPE=$PAYMENT_TYPE
SECRET_KEY=$SECRET_KEY
HEAPTRACK=$HEAPTRACK
ANT={ANT_INSTALL_PATH}
ADDRESSES_FILE={CLIENT_DATA_DIR}/addresses.txt
UPLOAD_DIR={CLIENT_DATA_DIR}/uploads
DOWNLOAD_DIR={CLIENT_DATA_DIR}/downloads
AWS_ACCESS_KEY_ID={self.aws_access_key_id}
AWS_SECRET_ACCESS_KEY={self.aws_secret_access_key}
AWS_DEFAULT_REGION={self.aws_region}
ADDRESSES_S3_BUCKET={self.addresses_s3_bucket}
ADDRESSES_S3_PREFIX={self.addresses_s3_prefix}
UPLOAD_PRESIGNED_URL=$UPLOAD_PRESIGNED_URL
ADDRESSES_TAIL_SIZE=10000
S3_FLUSH_INTERVAL_SECS=30
S3_FLUSH_EVERY_N=50
POINTER_DATA_DIR={POINTER_DATA_DIR}
CREATE_INTERVAL_SECS=$CREATE_INTERVAL_SECS
UPDATE_INTERVAL_SECS=$UPDATE_INTERVAL_SECS
SWEEP_INTERVAL_SECS=$SWEEP_INTERVAL_SECS
CHAIN_FRACTION=$CHAIN_FRACTION
POINTER_MAX=$POINTER_MAX
EVM_NETWORK={self.evm_network}
RPC_URL={self.rpc_url}"""

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

            # --- WASM (browser-path) clients ------------------------------
            # Targeted per host rather than gated by a case block, so a
            # deployment's native client VMs never see a Node install or a WASM
            # bundle they have no use for.
            self._add_wasm_ops(state, ip_to_env)

            # Write only the systemd unit matching each host's role. The old
            # unit (if any) is removed so stale services don't linger when a VM
            # flips role during reprovisioning.
            uploader_unit = _build_uploader_unit()
            downloader_unit = _build_downloader_unit()
            pointer_unit = _build_pointer_unit()

            # Native roles only: a WASM host's unit is written by
            # _add_wasm_ops, and must not be overwritten here.
            native_env = {
                ip: env for ip, env in ip_to_env.items() if not is_wasm(env["role"])
            }
            if native_env:
                unit_case_entries = [
                    f'    "{ip}") ROLE={env["role"]} ;;' for ip, env in native_env.items()
                ]
                unit_case_block = "\n".join(unit_case_entries)

                systemd_script = f"""\
MY_IP=$(hostname -I | awk '{{print $1}}')
case "$MY_IP" in
{unit_case_block}
    # No default. This op runs on every client host including the WASM ones,
    # whose units are written elsewhere; silently falling through to the
    # uploader unit would install a native ant service on a WASM VM. The
    # pointer role needs its own arm for the same reason in reverse: without
    # one it falls into the `else` and a pointer VM gets an ant-downloader unit
    # that `clients start` then cannot find (it maps the real role to its
    # unit).
    *) exit 0 ;;
esac
if [ "$ROLE" = "upload" ]; then
    cat > /etc/systemd/system/ant-uploader.service << 'UNIT_EOF'
{uploader_unit}UNIT_EOF
    rm -f /etc/systemd/system/ant-downloader.service /etc/systemd/system/{POINTER_UNIT}.service
elif [ "$ROLE" = "{ROLE_POINTER}" ]; then
    cat > /etc/systemd/system/{POINTER_UNIT}.service << 'UNIT_EOF'
{pointer_unit}UNIT_EOF
    rm -f /etc/systemd/system/ant-uploader.service /etc/systemd/system/ant-downloader.service
else
    cat > /etc/systemd/system/ant-downloader.service << 'UNIT_EOF'
{downloader_unit}UNIT_EOF
    rm -f /etc/systemd/system/ant-uploader.service /etc/systemd/system/{POINTER_UNIT}.service
fi
systemctl daemon-reload"""

                add_op(
                    state,
                    server.shell,
                    name="Write systemd unit files (role-aware)",
                    commands=[systemd_script],
                )

            if self.addresses_file_content is not None:
                download_ips = [
                    ip for ip, env in ip_to_env.items() if env["role"] == "download"
                ]
                if download_ips:
                    encoded = base64.b64encode(
                        self.addresses_file_content.encode("utf-8")
                    ).decode("ascii")
                    addresses_case_entries = [
                        f'    "{ip}") MATCH=1 ;;' for ip in download_ips
                    ]
                    addresses_case_block = "\n".join(addresses_case_entries)
                    addresses_script = f"""\
MY_IP=$(hostname -I | awk '{{print $1}}')
MATCH=0
case "$MY_IP" in
{addresses_case_block}
    *) MATCH=0 ;;
esac
if [ "$MATCH" = "1" ]; then
    mkdir -p {CLIENT_DATA_DIR}
    echo '{encoded}' | base64 -d > {CLIENT_DATA_DIR}/addresses.txt
fi"""
                    add_op(
                        state,
                        server.shell,
                        name="Write static addresses file (download role only)",
                        commands=[addresses_script],
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
            "Use 'clients start' to begin upload/download."
        )
        if failed:
            for host in failed:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError(f"{len(failed)} host(s) failed client provisioning")
