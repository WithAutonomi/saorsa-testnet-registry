"""Production node hardening provisioner.

Applies security hardening to a production bootstrap node:
- SSH lockdown (key-only, no root login, no password auth)
- fail2ban for SSH brute-force protection
- unattended-upgrades for automatic security patches
- sysctl network hardening (SYN flood, ICMP, connection tracking)
- iptables rate limiting on the node port
- Dedicated ant service user
"""

from io import StringIO

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import files, server, systemd

NODE_PORT = 10000
SERVICE_USER = "ant"


class ProductionHardeningProvisioner:
    def __init__(
        self,
        ip: str,
        ssh_key_path: str = "~/.ssh/ant-production",
        node_port: int = NODE_PORT,
        ssh_user: str = "root",
    ):
        self.ip = ip
        self.ssh_key_path = ssh_key_path
        self.node_port = node_port
        self.ssh_user = ssh_user

    def execute(self) -> None:
        connect_opts = {"ssh_user": self.ssh_user, "ssh_key": self.ssh_key_path}
        inventory = Inventory(
            ([(self.ip, connect_opts)], {})
        )
        config = Config()
        if self.ssh_user != "root":
            config.SUDO = True
            config.USE_SUDO_PASSWORD = False
        state = State(inventory=inventory, config=config)
        connect_all(state)
        try:
            self._install_packages(state)
            self._create_service_user(state)
            self._harden_ssh(state)
            self._configure_fail2ban(state)
            self._configure_unattended_upgrades(state)
            self._apply_sysctl_hardening(state)
            self._apply_rate_limiting(state)
            run_ops(state)
        finally:
            disconnect_all(state)

    def _install_packages(self, state: State) -> None:
        add_op(
            state,
            server.shell,
            name="Install hardening packages",
            commands=[
                "apt-get update -qq",
                "apt-get install -y -qq fail2ban unattended-upgrades iptables-persistent",
            ],
        )

    def _create_service_user(self, state: State) -> None:
        add_op(
            state,
            server.shell,
            name="Create ant service user",
            commands=[
                f"id -u {SERVICE_USER} >/dev/null 2>&1 || "
                f"useradd --system --create-home --shell /usr/sbin/nologin {SERVICE_USER}",
            ],
        )

    def _harden_ssh(self, state: State) -> None:
        cfg = "/etc/ssh/sshd_config"
        sshd_cmds = [
            f"sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin prohibit-password/' {cfg}",
            f"sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication no/' {cfg}",
            f"sed -i 's/^#\\?ChallengeResponseAuthentication.*"
            f"/ChallengeResponseAuthentication no/' {cfg}",
            f"sed -i 's/^#\\?MaxAuthTries.*/MaxAuthTries 3/' {cfg}",
            "systemctl restart ssh || systemctl restart sshd",
        ]
        add_op(
            state,
            server.shell,
            name="Harden SSH configuration",
            commands=sshd_cmds,
        )

    def _configure_fail2ban(self, state: State) -> None:
        jail_config = (
            "[sshd]\n"
            "enabled = true\n"
            "port = ssh\n"
            "filter = sshd\n"
            "logpath = /var/log/auth.log\n"
            "maxretry = 3\n"
            "bantime = 3600\n"
            "findtime = 600\n"
        )
        add_op(
            state,
            files.put,
            name="Configure fail2ban SSH jail",
            src=StringIO(jail_config),
            dest="/etc/fail2ban/jail.d/sshd.conf",
            mode="644",
            add_deploy_dir=False,
        )
        add_op(
            state,
            systemd.service,
            name="Enable and start fail2ban",
            service="fail2ban",
            running=True,
            enabled=True,
            restarted=True,
        )

    def _configure_unattended_upgrades(self, state: State) -> None:
        auto_upgrades_config = (
            'APT::Periodic::Update-Package-Lists "1";\n'
            'APT::Periodic::Unattended-Upgrade "1";\n'
            'APT::Periodic::AutocleanInterval "7";\n'
        )
        add_op(
            state,
            files.put,
            name="Configure automatic security updates",
            src=StringIO(auto_upgrades_config),
            dest="/etc/apt/apt.conf.d/20auto-upgrades",
            mode="644",
            add_deploy_dir=False,
        )

    def _apply_sysctl_hardening(self, state: State) -> None:
        sysctl_config = (
            "# Autonomi production node hardening\n"
            "net.ipv4.tcp_syncookies = 1\n"
            "net.ipv4.conf.all.accept_redirects = 0\n"
            "net.ipv4.conf.default.accept_redirects = 0\n"
            "net.ipv4.conf.all.send_redirects = 0\n"
            "net.ipv4.conf.default.send_redirects = 0\n"
            "net.ipv4.conf.all.rp_filter = 1\n"
            "net.ipv4.conf.default.rp_filter = 1\n"
            "net.ipv4.icmp_echo_ignore_broadcasts = 1\n"
            "net.ipv4.conf.all.log_martians = 1\n"
            "net.ipv4.tcp_max_syn_backlog = 2048\n"
            "net.core.somaxconn = 1024\n"
        )
        add_op(
            state,
            files.put,
            name="Apply sysctl network hardening",
            src=StringIO(sysctl_config),
            dest="/etc/sysctl.d/99-ant-hardening.conf",
            mode="644",
            add_deploy_dir=False,
        )
        add_op(
            state,
            server.shell,
            name="Reload sysctl settings",
            commands=["sysctl --system"],
        )

    def _apply_rate_limiting(self, state: State) -> None:
        p = self.node_port
        flush_udp = (
            f"iptables -D INPUT -p udp --dport {p}"
            " -m state --state NEW -j ANT_RATE_LIMIT"
            " 2>/dev/null || true"
        )
        flush_tcp = (
            f"iptables -D INPUT -p tcp --dport {p}"
            " -m state --state NEW -j ANT_RATE_LIMIT"
            " 2>/dev/null || true"
        )
        apply_udp = (
            f"iptables -I INPUT -p udp --dport {p}"
            " -m state --state NEW -j ANT_RATE_LIMIT"
        )
        apply_tcp = (
            f"iptables -I INPUT -p tcp --dport {p}"
            " -m state --state NEW -j ANT_RATE_LIMIT"
        )
        iptables_cmds = [
            # Flush any existing ant-node rate limit rules
            flush_udp,
            flush_tcp,
            "iptables -F ANT_RATE_LIMIT 2>/dev/null || true",
            "iptables -X ANT_RATE_LIMIT 2>/dev/null || true",
            # Create rate limit chain
            "iptables -N ANT_RATE_LIMIT",
            "iptables -A ANT_RATE_LIMIT -m hashlimit "
            "--hashlimit-name ant_node "
            "--hashlimit-above 50/sec "
            "--hashlimit-burst 100 "
            "--hashlimit-mode srcip "
            "--hashlimit-htable-expire 10000 "
            "-j DROP",
            "iptables -A ANT_RATE_LIMIT -j ACCEPT",
            # Apply to node port
            apply_udp,
            apply_tcp,
            # Save rules
            "netfilter-persistent save",
        ]
        add_op(
            state,
            server.shell,
            name="Apply iptables rate limiting on node port",
            commands=iptables_cmds,
        )


def build_systemd_unit(
    binary_path: str,
    data_dir: str,
    port: int = NODE_PORT,
    service_user: str = SERVICE_USER,
    bootstrap_peers: str | None = None,
    rewards_address: str | None = None,
    evm_network: str | None = None,
) -> str:
    """Build a systemd unit file for a production ant-node service."""
    exec_args = [
        binary_path,
        "--port", str(port),
        "--root-dir", data_dir,
        "--enable-logging",
    ]
    if bootstrap_peers:
        exec_args.extend(["--bootstrap", bootstrap_peers])
    if rewards_address:
        exec_args.extend(["--rewards-address", rewards_address])
    if evm_network:
        exec_args.extend(["--evm-network", evm_network])

    exec_start = " ".join(exec_args)

    return (
        "[Unit]\n"
        "Description=Autonomi Production Node\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={service_user}\n"
        f"ExecStart={exec_start}\n"
        "Restart=always\n"
        "RestartSec=20\n"
        "LimitNOFILE=65536\n"
        "MemoryMax=3G\n"
        "CPUQuota=180%\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
