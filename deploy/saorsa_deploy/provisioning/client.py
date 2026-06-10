
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
CLIENT_DATA_DIR = "/var/lib/ant-client"
SCRIPTS_DIR = "/root/ant-client"


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
            ).execute()

    def _presigned_put_url(self, region: str, client_index) -> str:
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
        key = f"{self.addresses_s3_prefix}/upload-{region}-{client_index}.txt"
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
            def _normalize_secret(k: str) -> str:
                k = (k or "").strip()
                if not k:
                    return ""
                if not k.startswith("0x"):
                    k = "0x" + k
                return k

            fallback_secret = _normalize_secret(self.wallet_secret_key)

            ip_to_env = {}
            for c in self.clients:
                if not c.get("ip"):
                    continue
                role = c.get("role", "upload")
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
                if role == "upload":
                    per_uploader_key = _normalize_secret(c.get("wallet_secret_key", ""))
                    ip_to_env[c["ip"]] = {
                        "role": "upload",
                        "file_size_kb": c["file_size_mb"] * 1024,
                        "region": c["region"],
                        "client_index": c["client_index"],
                        "payment_type": c.get("payment_type") or "",
                        "secret_key": per_uploader_key or fallback_secret,
                        "heaptrack": heap,
                        "bootstrap_cache": bootstrap_cache,
                        "forward_logs": forward_logs,
                        "rust_log": rust_log,
                        "upload_presigned_url": self._presigned_put_url(
                            c["region"], c["client_index"]
                        ),
                    }
                else:
                    ip_to_env[c["ip"]] = {
                        "role": "download",
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
FORWARD_LOGS=false; RUST_LOG=""; UPLOAD_PRESIGNED_URL="" ;;
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
S3_FLUSH_EVERY_N=50"""

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

            # Write only the systemd unit matching each host's role. The old
            # unit (if any) is removed so stale services don't linger when a VM
            # flips role during reprovisioning.
            uploader_unit = _build_uploader_unit()
            downloader_unit = _build_downloader_unit()

            unit_case_entries = []
            for ip, env in ip_to_env.items():
                unit_case_entries.append(f'    "{ip}") ROLE={env["role"]} ;;')
            unit_case_block = "\n".join(unit_case_entries)

            systemd_script = f"""\
MY_IP=$(hostname -I | awk '{{print $1}}')
case "$MY_IP" in
{unit_case_block}
    *) ROLE=upload ;;
esac
if [ "$ROLE" = "upload" ]; then
    cat > /etc/systemd/system/ant-uploader.service << 'UNIT_EOF'
{uploader_unit}UNIT_EOF
    rm -f /etc/systemd/system/ant-downloader.service
else
    cat > /etc/systemd/system/ant-downloader.service << 'UNIT_EOF'
{downloader_unit}UNIT_EOF
    rm -f /etc/systemd/system/ant-uploader.service
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
