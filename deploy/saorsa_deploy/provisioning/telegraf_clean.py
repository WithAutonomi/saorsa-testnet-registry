"""Pyinfra-based teardown of the telegraf-ant-elasticsearch log-forwarder.

Idempotent: safe to re-run on hosts where the service was already removed, or
never provisioned in the first place. Removes only the log-forwarder's
artifacts; leaves the telegraf deb, the metrics sidecar, and ant-node alone.
"""

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.provisioning.telegraf import (
    TELEGRAF_CONFIG_DIR,
    TELEGRAF_ENV_PATH,
    TELEGRAF_SERVICE_NAME,
    TELEGRAF_UNIT_PATH,
)


class TelegrafCleanProvisioner:
    """Removes the telegraf-ant-elasticsearch service + its config from VMs."""

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
        """Tear down telegraf-ant-elasticsearch on all hosts."""
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
                name=f"Stop and disable {TELEGRAF_SERVICE_NAME}",
                commands=[
                    f"systemctl stop {TELEGRAF_SERVICE_NAME} 2>/dev/null || true",
                    f"systemctl disable {TELEGRAF_SERVICE_NAME} 2>/dev/null "
                    f"|| true",
                ],
            )

            add_op(
                state,
                server.shell,
                name="Remove systemd unit and config files",
                commands=[
                    f"rm -f {TELEGRAF_UNIT_PATH}",
                    "systemctl daemon-reload",
                    f"rm -rf {TELEGRAF_CONFIG_DIR}",
                    f"rm -f {TELEGRAF_ENV_PATH}",
                ],
            )

            add_op(
                state,
                server.shell,
                name="Verify service is gone",
                commands=[
                    f"! systemctl list-unit-files {TELEGRAF_SERVICE_NAME}.service "
                    f"2>/dev/null | grep -q {TELEGRAF_SERVICE_NAME} "
                    f"|| (echo 'CLEAN_VERIFY:FAILED - unit still present'; "
                    f"exit 1)",
                    f"test ! -d {TELEGRAF_CONFIG_DIR} "
                    f"|| (echo 'CLEAN_VERIFY:FAILED - config dir still "
                    f"present'; exit 1)",
                ],
            )

            self.console.print("Running telegraf-logs cleanup operations...")
            run_ops(state)
        finally:
            disconnect_all(state)

        failed = state.failed_hosts
        total = len(self.host_ips)
        succeeded = total - len(failed)
        self.console.print(
            f"Telegraf-logs cleanup complete: {succeeded}/{total} hosts"
        )
        if failed:
            for host in failed:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError(
                f"{len(failed)} host(s) failed telegraf-logs cleanup"
            )
