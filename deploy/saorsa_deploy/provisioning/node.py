import time

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.binary_source import RELEASE_ASSET_NAME, get_release_url
from saorsa_deploy.provisioning.apt import (
    DISABLE_UNATTENDED_UPGRADES_OP_NAME,
    build_disable_unattended_upgrades_command,
)
from saorsa_deploy.provisioning.authorized_keys import add_operator_authorized_keys_op
from saorsa_deploy.provisioning.bootstrap import BINARY_INSTALL_PATH, DEFAULT_REWARDS_ADDRESS
from saorsa_deploy.provisioning.clock_skew import (
    SKEW_HOURS,
    build_clock_skew_command,
    skew_direction_for_ip,
)
from saorsa_deploy.provisioning.full_nodes import (
    DEFAULT_FULL_HEADROOM_MB,
    build_fill_storage_command,
)
from saorsa_deploy.provisioning.nat import NAT_IP_EXEC_PREFIX, build_nat_setup_command
from saorsa_deploy.provisioning.progress import (
    OpTimingHandler,
    RichLiveProgressHandler,
    create_progress_handler,
    print_op_timing_summary,
)
from saorsa_deploy.provisioning.shell_ops import as_single_shell_command
from saorsa_deploy.provisioning.volume import (
    LOG_DIR,
    MOUNT_POINT,
    build_mount_volume_command,
    build_root_disk_dir_command,
)

# heaptrack traces for node memory profiling are written to the attached data
# volume (not the root disk) — 15 debug-build nodes generate large, growing
# ``.zst`` traces that would otherwise fill ``/``.
HEAPTRACK_DIR = f"{MOUNT_POINT}/heaptrack"

# Idempotent heaptrack apt install (mirrors the prod-uploader/client install op).
INSTALL_HEAPTRACK_COMMAND = (
    "command -v heaptrack >/dev/null && echo 'HEAPTRACK:SKIP' || "
    "(DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
    "heaptrack && echo 'HEAPTRACK:INSTALLED')"
)

# Core dumps for the prod-debug-nodes kind are written under the data path
# (attached volume, or the root disk on volume-less providers like contabo), so
# a panicking ant-node leaves an inspectable core even on OOM/SIGKILL.
COREDUMP_DIR = f"{MOUNT_POINT}/coredumps"

# Idempotent apt install of the core-dump inspection tools (prod-debug-nodes
# kind, gated on coredumps). ``gdb`` and ``eu-stack`` (from ``elfutils``) read
# the file cores under COREDUMP_DIR directly, so a symbolised backtrace can be
# produced on the box without shipping the 1 GiB cores elsewhere. NB the release
# ant-node binary is stripped, so symbols are limited without an unstripped
# build; systemd-coredump is deliberately NOT installed (it would either be inert
# against this file-based core_pattern or force the cores off the data volume).
INSTALL_DEBUG_TOOLS_COMMAND = (
    "command -v gdb >/dev/null && command -v eu-stack >/dev/null "
    "&& echo 'DEBUG_TOOLS:SKIP' || "
    "(DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
    "gdb elfutils && echo 'DEBUG_TOOLS:INSTALLED')"
)


def build_coredump_setup_command(coredump_dir: str = COREDUMP_DIR) -> str:
    """Enable core dumps globally, writing them under ``coredump_dir``.

    Sets ``kernel.core_pattern`` to an absolute path so the kernel writes cores
    to a known directory (paired with ``LimitCORE=infinity`` on the service unit,
    which lifts the per-service core-size limit). Idempotent. The single-quoted
    heredoc keeps the ``%e/%p/%t`` pattern literal.
    """
    pattern = f"{coredump_dir}/core.%e.%p.%t"
    return (
        f"mkdir -p {coredump_dir}\n"
        "cat > /etc/sysctl.d/99-ant-coredump.conf << 'COREDUMP_EOF'\n"
        f"kernel.core_pattern={pattern}\n"
        "kernel.core_uses_pid=1\n"
        "COREDUMP_EOF\n"
        "sysctl -p /etc/sysctl.d/99-ant-coredump.conf >/dev/null\n"
        "echo 'ANT_COREDUMP:CONFIGURED'"
    )


def _build_node_exec_start(
    bootstrap_peers,
    root_dir,
    log_dir=None,
    port=None,
    log_level=None,
    testnet=False,
    upgrade_channel=None,
    rewards_address=DEFAULT_REWARDS_ADDRESS,
    evm_network="arbitrum-sepolia",
    heaptrack_base=None,
):
    """Build the ExecStart command line for a node service.

    Args:
        bootstrap_peers: List of "ip:port" strings for all bootstrap nodes.
        heaptrack_base: When set, wrap the ant-node invocation with
            ``heaptrack -o <heaptrack_base>`` for memory profiling. heaptrack
            writes ``<base>.<pid>.zst`` incrementally, so even an OOM/SIGKILL
            leaves an analysable trace. The wrapper is prepended to the whole
            command, so when the unit is also a NAT variant the outer
            ``ip netns exec ant_nat`` prefix (added in ``_build_node_unit_file``)
            still sits outermost — heaptrack profiles the node, not netns.
    """
    parts = []
    if heaptrack_base:
        parts.extend(["heaptrack", "-o", heaptrack_base])
    parts.append(BINARY_INSTALL_PATH)
    parts.append(f"--root-dir {root_dir}")
    for peer in bootstrap_peers:
        parts.append(f"--bootstrap {peer}")
    if port is not None:
        parts.append(f"--port {port}")
    # --enable-logging must precede the other log-related flags, otherwise
    # ant-node silently ignores them.
    parts.append("--enable-logging")
    if log_level:
        parts.append(f"--log-level {log_level}")
    parts.append("--log-format json")
    if log_dir:
        parts.append(f"--log-dir {log_dir}")
    if testnet:
        parts.append("--network-mode testnet")
    if upgrade_channel:
        parts.append(f"--upgrade-channel {upgrade_channel}")
    parts.append("--stop-on-upgrade")
    if evm_network:
        parts.append(f"--evm-network {evm_network}")
    parts.append(f"--rewards-address {rewards_address}")
    return " ".join(parts)


def _build_node_unit_file(
    service_name,
    exec_start,
    is_nat=False,
    restart_policy="on-success",
    restart_sec=5,
    limit_nofile=None,
    rust_backtrace=None,
    coredumps=False,
    colorbt_show_hidden=False,
):
    """Build the systemd unit file content for a node service.

    When ``is_nat=True``, wrap ExecStart with ``ip netns exec ant_nat`` so the
    process runs inside the port-restricted-NAT namespace, and tag the
    Description so the NAT status is visible in ``systemctl status``. All
    other unit settings and the ant-node CLI flags are identical to the
    non-NAT unit by design — the reference SKILL.md only dictates the NAT
    mechanism, not the rest of the unit file.

    ``restart_policy``, ``restart_sec`` and ``limit_nofile`` are knobs the
    prod-migration command needs to match the existing prod fleet's units
    (``always`` / ``10`` / ``65536``); defaults preserve testnet behaviour.

    ``rust_backtrace`` (e.g. ``"1"`` / ``"full"``) adds an
    ``Environment=RUST_BACKTRACE=`` line, ``colorbt_show_hidden`` adds
    ``Environment=COLORBT_SHOW_HIDDEN=1`` (ant-node's color-backtrace filters
    every frame otherwise, printing ``<empty backtrace>``), and ``coredumps``
    adds ``LimitCORE=infinity`` — all used by the prod-debug-nodes kind. They
    default off, so every other caller's unit is byte-for-byte unchanged. The
    ``Environment=`` values propagate through the NAT ``ip netns exec`` wrapper
    (systemd sets them for the whole service), so they can't be inlined on
    ExecStart.
    """
    description_suffix = " (port-restricted NAT)" if is_nat else ""
    wrapped_exec_start = (
        f"{NAT_IP_EXEC_PREFIX}{exec_start}" if is_nat else exec_start
    )
    env_lines = ""
    if rust_backtrace:
        env_lines += f"\nEnvironment=RUST_BACKTRACE={rust_backtrace}"
    if colorbt_show_hidden:
        env_lines += "\nEnvironment=COLORBT_SHOW_HIDDEN=1"
    limit_line = f"\nLimitNOFILE={limit_nofile}" if limit_nofile else ""
    core_line = "\nLimitCORE=infinity" if coredumps else ""
    return f"""\
[Unit]
Description=Ant Node ({service_name}){description_suffix}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple{env_lines}
ExecStart={wrapped_exec_start}
Restart={restart_policy}
RestartSec={restart_sec}{limit_line}{core_line}

[Install]
WantedBy=multi-user.target
"""


class AntNodeProvisioner:
    """Provisions ant-node services on multiple hosts using Pyinfra.

    NAT hosts (IPs in ``nat_ips``) get an extra namespace+iptables setup step
    and a wrapped systemd ExecStart. Services are ``enable``d but never
    started — ``nodes start`` is run afterwards to bring them up once NAT
    rules are in place.
    """

    def __init__(
        self,
        host_ips: list[str],
        bootstrap_peers: list[str],
        ssh_key_path: str = "~/.ssh/id_rsa",
        node_count: int = 1,
        initial_port: int | None = None,
        log_level: str | None = None,
        testnet: bool = False,
        console: Console | None = None,
        binary_url: str | None = None,
        binary_is_archive: bool = True,
        upgrade_channel: str | None = None,
        rewards_address: str = DEFAULT_REWARDS_ADDRESS,
        nat_ips: set[str] | None = None,
        clock_skew_ips: set[str] | None = None,
        full_ips: set[str] | None = None,
        root_disk_ips: set[str] | None = None,
        full_headroom_mb: int = DEFAULT_FULL_HEADROOM_MB,
        evm_network: str | None = "arbitrum-sepolia",
        restart_policy: str = "on-success",
        restart_sec: int = 5,
        limit_nofile: int | None = None,
        extra_op_providers: list | None = None,
        heaptrack: bool = False,
        heaptrack_dir: str = HEAPTRACK_DIR,
        rust_backtrace: str | None = None,
        coredumps: bool = False,
        coredump_dir: str = COREDUMP_DIR,
        colorbt_show_hidden: bool = False,
    ):
        self.host_ips = host_ips
        self.bootstrap_peers = bootstrap_peers
        self.ssh_key_path = ssh_key_path
        self.node_count = node_count
        self.initial_port = initial_port
        self.log_level = log_level
        self.testnet = testnet
        self.console = console or Console()
        self.binary_url = binary_url
        self.binary_is_archive = binary_is_archive
        self.upgrade_channel = upgrade_channel
        self.rewards_address = rewards_address
        self.nat_ips = set(nat_ips) if nat_ips else set()
        self.clock_skew_ips = set(clock_skew_ips) if clock_skew_ips else set()
        self.full_ips = set(full_ips) if full_ips else set()
        # Hosts with NO attached volume (OVH 3-AZ regions have no block storage):
        # they get a root-disk /mnt/ant-node-storage directory instead of a mount.
        self.root_disk_ips = set(root_disk_ips) if root_disk_ips else set()
        self.full_headroom_mb = full_headroom_mb
        self.evm_network = evm_network
        self.restart_policy = restart_policy
        self.restart_sec = restart_sec
        self.limit_nofile = limit_nofile
        # Memory profiling: when enabled, every node service's ExecStart is
        # wrapped with heaptrack (traces under ``heaptrack_dir`` on the data
        # volume, one base per service so the 15 nodes don't collide).
        self.heaptrack = heaptrack
        self.heaptrack_dir = heaptrack_dir
        # Crash-debug knobs (prod-debug-nodes kind), default off so other
        # callers' units are unchanged: RUST_BACKTRACE + COLORBT_SHOW_HIDDEN on
        # the service env, plus LimitCORE=infinity + a global kernel.core_pattern
        # under coredump_dir, and gdb/eu-stack installed to read the file cores.
        self.rust_backtrace = rust_backtrace
        self.coredumps = coredumps
        self.coredump_dir = coredump_dir
        self.colorbt_show_hidden = colorbt_show_hidden
        # Optional providers (e.g. Telegraf forwarders) whose ops are folded
        # into this provisioner's single pyinfra pass, so node provisioning and
        # Telegraf install happen in ONE pass over the node VMs. Each exposes
        # ``add_ops(state, install=...)``; the first installs the Telegraf deb.
        self.extra_op_providers = list(extra_op_providers or [])

    def _build_unit_commands(self, is_nat: bool) -> list[str]:
        """Build the list of `cat > unit-file` commands for one host variant."""
        commands = []
        for i in range(self.node_count):
            service_name = f"ant-node-{i + 1}"
            node_port = (self.initial_port + i) if self.initial_port is not None else None
            root_dir = f"{MOUNT_POINT}/data/{service_name}"
            log_dir = f"{LOG_DIR}/{service_name}"
            heaptrack_base = (
                f"{self.heaptrack_dir}/{service_name}" if self.heaptrack else None
            )
            exec_start = _build_node_exec_start(
                bootstrap_peers=self.bootstrap_peers,
                root_dir=root_dir,
                log_dir=log_dir,
                port=node_port,
                log_level=self.log_level,
                testnet=self.testnet,
                upgrade_channel=self.upgrade_channel,
                rewards_address=self.rewards_address,
                evm_network=self.evm_network,
                heaptrack_base=heaptrack_base,
            )
            unit_content = _build_node_unit_file(
                service_name,
                exec_start,
                is_nat=is_nat,
                restart_policy=self.restart_policy,
                restart_sec=self.restart_sec,
                limit_nofile=self.limit_nofile,
                rust_backtrace=self.rust_backtrace,
                coredumps=self.coredumps,
                colorbt_show_hidden=self.colorbt_show_hidden,
            )
            unit_path = f"/etc/systemd/system/{service_name}.service"
            commands.append(f"cat > {unit_path} << 'UNIT_EOF'\n{unit_content}UNIT_EOF")
        return commands

    def execute(self) -> None:
        """Provision all hosts with ant-node services."""
        if self.binary_url:
            download_url = self.binary_url
            self.console.print(f"Using binary URL: {download_url}")
        else:
            self.console.print("Fetching latest release from GitHub...")
            download_url = get_release_url()
            self.console.print(f"  Release URL: {download_url}")

        if self.heaptrack:
            self.console.print(
                f"[cyan]heaptrack memory profiling enabled "
                f"(traces → {self.heaptrack_dir}/ant-node-<i>.<pid>.zst on each VM; "
                f"fetch via scp before destroy).[/cyan]"
            )

        if self.nat_ips:
            self.console.print(
                f"[cyan]NAT simulation enabled on {len(self.nat_ips)}/"
                f"{len(self.host_ips)} host(s).[/cyan]"
            )

        if self.clock_skew_ips:
            self.console.print(
                f"[cyan]Clock skew enabled on {len(self.clock_skew_ips)}/"
                f"{len(self.host_ips)} host(s).[/cyan]"
            )

        if self.full_ips:
            self.console.print(
                f"[cyan]Storage-full simulation enabled on {len(self.full_ips)}/"
                f"{len(self.host_ips)} host(s) "
                f"(headroom {self.full_headroom_mb} MiB).[/cyan]"
            )

        hosts_data = [
            (ip, {"ssh_user": "root", "ssh_key": self.ssh_key_path}) for ip in self.host_ips
        ]
        inventory = Inventory((hosts_data, {}))
        config = Config()
        state = State(inventory=inventory, config=config)

        progress = create_progress_handler(self.console)
        state.add_callback_handler(progress)
        timing = OpTimingHandler()
        state.add_callback_handler(timing)

        if isinstance(progress, RichLiveProgressHandler):
            progress._live.start()

        connect_seconds = None
        try:
            self.console.print(f"Connecting to {len(self.host_ips)} host(s) as root...")
            _connect_start = time.monotonic()
            connect_all(state)
            connect_seconds = time.monotonic() - _connect_start

            active_hosts = list(state.inventory.get_active_hosts())
            nat_hosts = [h for h in active_hosts if h.name in self.nat_ips]
            plain_hosts = [h for h in active_hosts if h.name not in self.nat_ips]
            skew_hosts = [h for h in active_hosts if h.name in self.clock_skew_ips]
            # OVH 3-AZ hosts have no attached volume; the rest mount one.
            root_disk_hosts = [h for h in active_hosts if h.name in self.root_disk_ips]
            volume_hosts = [h for h in active_hosts if h.name not in self.root_disk_ips]
            # Exclude root-disk hosts from storage-full simulation — it would fill
            # the root disk (/mnt/ant-node-storage is a plain dir there), not a
            # dedicated volume, risking OS instability.
            full_hosts = [
                h
                for h in active_hosts
                if h.name in self.full_ips and h.name not in self.root_disk_ips
            ]

            add_operator_authorized_keys_op(state)

            add_op(
                state,
                server.shell,
                name=DISABLE_UNATTENDED_UPGRADES_OP_NAME,
                commands=[build_disable_unattended_upgrades_command()],
            )

            if self.root_disk_ips:
                # Mixed deployment including OVH 3-AZ hosts (no block storage):
                # mount a volume on the volume-backed hosts, use a root-disk
                # directory on the 3-AZ hosts. Only split when 3-AZ hosts are
                # present so the common single-op path (and its ordering) is
                # unchanged.
                if volume_hosts:
                    add_op(
                        state,
                        server.shell,
                        name="Mount attached storage volume",
                        commands=[build_mount_volume_command()],
                        host=volume_hosts,
                    )
                if root_disk_hosts:
                    add_op(
                        state,
                        server.shell,
                        name="Set up root-disk storage directory (no volume)",
                        commands=[build_root_disk_dir_command()],
                        host=root_disk_hosts,
                    )
            else:
                add_op(
                    state,
                    server.shell,
                    name="Mount attached storage volume",
                    commands=[build_mount_volume_command()],
                )

            # One `mkdir -p` with every directory as an argument — a single SSH
            # round-trip per host rather than one per node service.
            log_dirs = [LOG_DIR]
            log_dirs += [f"{LOG_DIR}/ant-node-{i + 1}" for i in range(self.node_count)]
            if self.heaptrack:
                # On the data volume (mounted just above), not the root disk.
                log_dirs.append(self.heaptrack_dir)
            if self.coredumps:
                # Under the data path too, so cores land off the root disk.
                log_dirs.append(self.coredump_dir)

            add_op(
                state,
                server.shell,
                name="Create log directories",
                commands=["mkdir -p " + " ".join(log_dirs)],
            )

            if self.heaptrack:
                add_op(
                    state,
                    server.shell,
                    name="Install heaptrack",
                    commands=[INSTALL_HEAPTRACK_COMMAND],
                )

            if self.coredumps:
                add_op(
                    state,
                    server.shell,
                    name="Enable core dumps (kernel.core_pattern)",
                    commands=[build_coredump_setup_command(self.coredump_dir)],
                )
                add_op(
                    state,
                    server.shell,
                    name="Install core-dump tools (gdb, elfutils)",
                    commands=[INSTALL_DEBUG_TOOLS_COMMAND],
                )

            if self.binary_is_archive:
                install_cmd = (
                    f"test -f {BINARY_INSTALL_PATH} "
                    f"&& echo 'ANT_BINARY:SKIP' || "
                    f"(wget -q {download_url} -O /tmp/{RELEASE_ASSET_NAME} && "
                    f"tar -xzf /tmp/{RELEASE_ASSET_NAME} -C /tmp/ && "
                    f"mv /tmp/ant-node {BINARY_INSTALL_PATH} && "
                    f"chmod +x {BINARY_INSTALL_PATH} && "
                    f"rm -f /tmp/{RELEASE_ASSET_NAME} && "
                    f"echo 'ANT_BINARY:INSTALLED')"
                )
            else:
                install_cmd = (
                    f"test -f {BINARY_INSTALL_PATH} "
                    f"&& echo 'ANT_BINARY:SKIP' || "
                    f"(wget -q {download_url} -O {BINARY_INSTALL_PATH} && "
                    f"chmod +x {BINARY_INSTALL_PATH} && "
                    f"echo 'ANT_BINARY:INSTALLED')"
                )
            install_results = add_op(
                state,
                server.shell,
                name="Download and install ant-node binary",
                commands=[install_cmd],
            )

            # Inject clock skew per-host: each selected host shifts by a fixed
            # magnitude in a per-host direction, so we add one op per host.
            for host in skew_hosts:
                direction = skew_direction_for_ip(host.name)
                offset_seconds = direction * SKEW_HOURS * 3600
                add_op(
                    state,
                    server.shell,
                    name=f"Inject clock skew ({'fast' if direction >= 0 else 'slow'})",
                    commands=[build_clock_skew_command(offset_seconds)],
                    host=[host],
                )

            # Pre-fill the data volume on storage-full hosts. Must run AFTER the
            # "Mount attached storage volume" op so it consumes the volume, not
            # the root disk. Orthogonal to NAT/skew — unit files are unchanged.
            if full_hosts:
                add_op(
                    state,
                    server.shell,
                    name="Pre-fill data volume (storage-full simulation)",
                    commands=[build_fill_storage_command(self.full_headroom_mb)],
                    host=full_hosts,
                )

            if nat_hosts:
                add_op(
                    state,
                    server.shell,
                    name="Configure port-restricted NAT (netns + iptables)",
                    commands=[build_nat_setup_command()],
                    host=nat_hosts,
                )
                add_op(
                    state,
                    server.shell,
                    name="Write systemd unit files (NAT variant)",
                    commands=[as_single_shell_command(self._build_unit_commands(is_nat=True))],
                    host=nat_hosts,
                )

            if plain_hosts:
                add_op(
                    state,
                    server.shell,
                    name="Write systemd unit files",
                    commands=[as_single_shell_command(self._build_unit_commands(is_nat=False))],
                    host=plain_hosts,
                )

            service_names = [f"ant-node-{i + 1}" for i in range(self.node_count)]
            enable_commands = ["systemctl daemon-reload"]
            for service_name in service_names:
                enable_commands.append(
                    f"systemctl is-enabled --quiet {service_name} "
                    f"&& echo 'ANT_SVC:ENABLED_ALREADY:{service_name}' "
                    f"|| (systemctl enable {service_name} "
                    f"&& echo 'ANT_SVC:ENABLED:{service_name}')"
                )
            # Collapse daemon-reload + per-service enable into one round-trip.
            enable_commands = [as_single_shell_command(enable_commands)]

            svc_results = add_op(
                state,
                server.shell,
                name="Enable node services (not started — use nodes start)",
                commands=enable_commands,
            )

            # Fold in any extra op providers (Telegraf forwarders) so they ride
            # the same SSH pass. The first installs the Telegraf deb; the rest
            # reuse it. Ops are added after the node ops, so the node services
            # are already configured before Telegraf starts tailing/scraping.
            for idx, provider in enumerate(self.extra_op_providers):
                provider.add_ops(state, install=idx == 0)

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
        print_op_timing_summary(
            self.console, timing, title="nodes provision", connect_seconds=connect_seconds
        )
        if failed:
            for host in failed:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError(f"{len(failed)} host(s) failed provisioning")

        self._report_results(install_results, svc_results)
        self.console.print(
            "[dim]Services enabled but not started. "
            "Run `saorsa-deploy nodes start --name <name>` to bring them up.[/dim]"
        )

    def _report_results(self, install_results, svc_results):
        """Print post-execution summary with idempotency information."""
        try:
            hosts = list(install_results.keys())
        except (TypeError, AttributeError):
            return

        binary_installed = 0
        binary_skipped = 0
        svcs_enabled = 0
        svcs_already = 0

        for host in hosts:
            install_meta = install_results[host]
            for line in install_meta.stdout_lines:
                if "ANT_BINARY:SKIP" in line:
                    binary_skipped += 1
                elif "ANT_BINARY:INSTALLED" in line:
                    binary_installed += 1

            svc_meta = svc_results[host]
            for line in svc_meta.stdout_lines:
                if "ANT_SVC:ENABLED_ALREADY:" in line:
                    svcs_already += 1
                elif "ANT_SVC:ENABLED:" in line:
                    svcs_enabled += 1

        total_hosts = len(hosts)
        if binary_skipped == total_hosts:
            self.console.print("  Binary: already installed on all hosts")
        elif binary_installed == total_hosts:
            self.console.print("  Binary: installed on all hosts")
        else:
            self.console.print(
                f"  Binary: installed on {binary_installed}, already installed on {binary_skipped}"
            )

        total_svcs = svcs_enabled + svcs_already
        if svcs_already == total_svcs and total_svcs > 0:
            self.console.print(f"  Services: all {svcs_already} already enabled")
        elif svcs_enabled == total_svcs and total_svcs > 0:
            self.console.print(f"  Services: {svcs_enabled} enabled")
        elif total_svcs > 0:
            self.console.print(
                f"  Services: {svcs_enabled} enabled, {svcs_already} already enabled"
            )
