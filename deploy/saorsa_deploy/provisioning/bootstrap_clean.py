"""Pyinfra-based teardown of the ant-bootstrap-* services on bootstrap VMs.

Idempotent: safe to re-run on hosts where services have already been removed or
were never provisioned. Covers all ant-bootstrap-N services on the VM (including
the genesis node, bootstrap_index=1). Leaves the mounted volume + fstab entry
intact so the VM can be re-used by a subsequent nodes provision-genesis /
bootstraps provision run.
"""

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.provisioning.bootstrap import BINARY_INSTALL_PATH
from saorsa_deploy.provisioning.volume import LOG_DIR, MOUNT_POINT

SERVICE_GLOB = "ant-bootstrap-*"
UNIT_GLOB = f"/etc/systemd/system/{SERVICE_GLOB}.service"
DATA_GLOB = f"{MOUNT_POINT}/data/{SERVICE_GLOB}"
LOG_GLOB = f"{LOG_DIR}/{SERVICE_GLOB}"


class BootstrapCleanProvisioner:
    """Removes ant-bootstrap-* services + their data/log/binary artefacts."""

    def __init__(
        self,
        host_ips: list[str],
        ssh_key_path: str,
        ssh_user: str = "root",
        console: Console | None = None,
    ):
        self.host_ips = host_ips
        self.ssh_key_path = ssh_key_path
        self.ssh_user = ssh_user
        self.console = console or Console()

    def execute(self) -> None:
        hosts_data = [
            (ip, {"ssh_user": self.ssh_user, "ssh_key": self.ssh_key_path})
            for ip in self.host_ips
        ]
        inventory = Inventory((hosts_data, {}))
        config = Config()
        if self.ssh_user != "root":
            config.SUDO = True
            config.USE_SUDO_PASSWORD = False
        state = State(inventory=inventory, config=config)

        self.console.print(f"Connecting to {len(self.host_ips)} host(s)...")
        connect_all(state)

        try:
            add_op(
                state,
                server.shell,
                name="Stop and disable ant-bootstrap-* services",
                commands=[
                    "for u in " + UNIT_GLOB + "; do "
                    "[ -e \"$u\" ] || continue; "
                    "n=$(basename \"$u\" .service); "
                    "systemctl stop \"$n\" 2>/dev/null || true; "
                    "systemctl disable \"$n\" 2>/dev/null || true; "
                    "done",
                ],
            )

            add_op(
                state,
                server.shell,
                name="Remove ant-bootstrap unit, data, log and binary files",
                commands=[
                    f"rm -f {UNIT_GLOB}",
                    "systemctl daemon-reload",
                    f"rm -rf {DATA_GLOB}",
                    f"rm -rf {LOG_GLOB}",
                    f"rm -f {BINARY_INSTALL_PATH}",
                ],
            )

            add_op(
                state,
                server.shell,
                name="Verify ant-bootstrap artefacts are gone",
                commands=[
                    "! ls " + UNIT_GLOB + " >/dev/null 2>&1 "
                    "|| (echo 'CLEAN_VERIFY:FAILED - bootstrap unit(s) still "
                    "present'; exit 1)",
                    f"test ! -f {BINARY_INSTALL_PATH} "
                    f"|| (echo 'CLEAN_VERIFY:FAILED - ant-node binary still "
                    f"present'; exit 1)",
                ],
            )

            self.console.print("Running bootstrap cleanup operations...")
            run_ops(state)
        finally:
            disconnect_all(state)

        failed = state.failed_hosts
        total = len(self.host_ips)
        succeeded = total - len(failed)
        self.console.print(
            f"Bootstrap cleanup complete: {succeeded}/{total} hosts"
        )
        if failed:
            for host in failed:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError(
                f"{len(failed)} host(s) failed bootstrap cleanup"
            )
