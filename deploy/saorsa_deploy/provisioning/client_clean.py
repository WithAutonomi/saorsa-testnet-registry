"""Pyinfra-based teardown of the ant-uploader / ant-downloader services on
client VMs.

Idempotent: safe to re-run on hosts where services have already been removed or
were never provisioned. Removes the uploader/downloader units, scripts dir,
environment file, data dir, and the ant binary so a subsequent provision-clients
run starts from a clean slate.
"""

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.provisioning.client import (
    ANT_INSTALL_PATH,
    CLIENT_DATA_DIR,
    SCRIPTS_DIR,
)

ENV_FILE = "/etc/ant-client.env"
UPLOADER_UNIT = "/etc/systemd/system/ant-uploader.service"
DOWNLOADER_UNIT = "/etc/systemd/system/ant-downloader.service"


class ClientCleanProvisioner:
    """Removes ant-uploader/ant-downloader services + client artefacts."""

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
                name="Stop and disable ant-uploader and ant-downloader",
                commands=[
                    "systemctl stop ant-uploader 2>/dev/null || true",
                    "systemctl stop ant-downloader 2>/dev/null || true",
                    "systemctl disable ant-uploader 2>/dev/null || true",
                    "systemctl disable ant-downloader 2>/dev/null || true",
                ],
            )

            add_op(
                state,
                server.shell,
                name="Remove client unit, script, env, data and binary files",
                commands=[
                    f"rm -f {UPLOADER_UNIT}",
                    f"rm -f {DOWNLOADER_UNIT}",
                    "systemctl daemon-reload",
                    f"rm -rf {SCRIPTS_DIR}",
                    f"rm -f {ENV_FILE}",
                    f"rm -rf {CLIENT_DATA_DIR}",
                    f"rm -f {ANT_INSTALL_PATH}",
                ],
            )

            add_op(
                state,
                server.shell,
                name="Verify client artefacts are gone",
                commands=[
                    f"test ! -f {UPLOADER_UNIT} "
                    f"|| (echo 'CLEAN_VERIFY:FAILED - uploader unit still "
                    f"present'; exit 1)",
                    f"test ! -f {DOWNLOADER_UNIT} "
                    f"|| (echo 'CLEAN_VERIFY:FAILED - downloader unit still "
                    f"present'; exit 1)",
                    f"test ! -f {ENV_FILE} "
                    f"|| (echo 'CLEAN_VERIFY:FAILED - env file still "
                    f"present'; exit 1)",
                    f"test ! -d {CLIENT_DATA_DIR} "
                    f"|| (echo 'CLEAN_VERIFY:FAILED - data dir still "
                    f"present'; exit 1)",
                    f"test ! -f {ANT_INSTALL_PATH} "
                    f"|| (echo 'CLEAN_VERIFY:FAILED - ant binary still "
                    f"present'; exit 1)",
                ],
            )

            self.console.print("Running client cleanup operations...")
            run_ops(state)
        finally:
            disconnect_all(state)

        failed = state.failed_hosts
        total = len(self.host_ips)
        succeeded = total - len(failed)
        self.console.print(
            f"Client cleanup complete: {succeeded}/{total} hosts"
        )
        if failed:
            for host in failed:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError(
                f"{len(failed)} host(s) failed client cleanup"
            )
