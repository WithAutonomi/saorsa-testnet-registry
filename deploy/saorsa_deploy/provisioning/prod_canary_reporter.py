"""Pyinfra provisioner for the prod canary Slack reporter.

Installs the reporter script, env file, and a daily systemd timer on the
canary's monitoring VM (the same VM that hosts Influx + Grafana). The
reporter reads Influx via localhost and posts to a Slack incoming webhook.

Sequence:

1. Create directories.
2. Drop the runtime script under ``REPORTER_DIR``.
3. Write ``/etc/ant-prod-canary-reporter.env`` with Influx + Slack creds.
4. Write systemd ``.service`` (oneshot) and ``.timer`` units.
5. ``daemon-reload``, enable + start the timer.
6. Optionally trigger one immediate run so the operator gets a Slack
   message confirming the wiring works.
"""

from __future__ import annotations

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.provisioning.progress import (
    RichLiveProgressHandler,
    create_progress_handler,
)
from saorsa_deploy.resources import get_resources_dir

REPORTER_DIR = "/opt/ant-prod-canary-reporter"
REPORTER_SCRIPT = "prod_canary_reporter.py"
REPORTER_STATE_DIR = "/var/lib/ant-prod-canary-reporter"
REPORTER_STATE_FILE = f"{REPORTER_STATE_DIR}/last-report.json"
REPORTER_ENV_PATH = "/etc/ant-prod-canary-reporter.env"
SERVICE_NAME = "ant-prod-canary-reporter"
SERVICE_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"
TIMER_PATH = f"/etc/systemd/system/{SERVICE_NAME}.timer"


def _build_service_unit() -> str:
    return f"""\
[Unit]
Description=Ant Prod Canary Slack Reporter (one-shot)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile={REPORTER_ENV_PATH}
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -u {REPORTER_DIR}/{REPORTER_SCRIPT}
StandardOutput=journal
StandardError=journal
"""


def _build_timer_unit(on_calendar: str) -> str:
    return f"""\
[Unit]
Description=Daily trigger for the Ant Prod Canary Slack Reporter

[Timer]
OnCalendar={on_calendar}
Persistent=true
Unit={SERVICE_NAME}.service

[Install]
WantedBy=timers.target
"""


def _format_env_lines(env: dict[str, str]) -> str:
    return "\n".join(f"{k}={v}" for k, v in env.items() if v != "") + "\n"


class ProdCanaryReporterProvisioner:
    """Provisions the Slack reporter onto the canary's monitoring VM."""

    def __init__(
        self,
        *,
        host_ip: str,
        deployment_name: str,
        slack_webhook_url: str,
        influx_url: str,
        influx_token: str,
        influx_org: str = "ant",
        influx_bucket: str = "testnet-metrics",
        on_calendar: str = "*-*-* 09:00:00",
        run_now: bool = True,
        ssh_key_path: str = "~/.ssh/id_rsa",
        console: Console | None = None,
    ):
        self.host_ip = host_ip
        self.deployment_name = deployment_name
        self.slack_webhook_url = slack_webhook_url
        self.influx_url = influx_url
        self.influx_token = influx_token
        self.influx_org = influx_org
        self.influx_bucket = influx_bucket
        self.on_calendar = on_calendar
        self.run_now = run_now
        self.ssh_key_path = ssh_key_path
        self.console = console or Console()

    def execute(self) -> None:
        if not self.host_ip:
            raise RuntimeError("Monitoring VM IP missing")
        if not self.slack_webhook_url:
            raise RuntimeError("Slack webhook URL is required")
        if not self.influx_url or not self.influx_token:
            raise RuntimeError("Influx URL + token are required")

        hosts_data = [(self.host_ip, {"ssh_user": "root", "ssh_key": self.ssh_key_path})]
        inventory = Inventory((hosts_data, {}))
        state = State(inventory=inventory, config=Config())

        progress = create_progress_handler(self.console)
        state.add_callback_handler(progress)
        if isinstance(progress, RichLiveProgressHandler):
            progress._live.start()

        try:
            self.console.print(f"Connecting to monitoring host {self.host_ip} as root...")
            connect_all(state)

            add_op(
                state,
                server.shell,
                name="Create reporter directories",
                commands=[
                    f"mkdir -p {REPORTER_DIR} {REPORTER_STATE_DIR} && "
                    f"chmod 0750 {REPORTER_STATE_DIR}"
                ],
            )

            scripts_dir = get_resources_dir() / "scripts"
            script_body = (scripts_dir / REPORTER_SCRIPT).read_text()
            add_op(
                state,
                server.shell,
                name=f"Deploy {REPORTER_SCRIPT}",
                commands=[
                    f"cat > {REPORTER_DIR}/{REPORTER_SCRIPT} << 'SCRIPT_EOF'\n"
                    f"{script_body}SCRIPT_EOF",
                    f"chmod 0755 {REPORTER_DIR}/{REPORTER_SCRIPT}",
                ],
            )

            env_lines = _format_env_lines(
                {
                    "DEPLOYMENT_NAME": self.deployment_name,
                    "SLACK_WEBHOOK_URL": self.slack_webhook_url,
                    "INFLUX_URL": self.influx_url,
                    "INFLUX_TOKEN": self.influx_token,
                    "INFLUX_ORG": self.influx_org,
                    "INFLUX_BUCKET": self.influx_bucket,
                    "STATE_FILE": REPORTER_STATE_FILE,
                }
            )
            add_op(
                state,
                server.shell,
                name="Write reporter env file",
                commands=[
                    f"cat > {REPORTER_ENV_PATH} << 'ENV_EOF'\n{env_lines}ENV_EOF",
                    f"chmod 0600 {REPORTER_ENV_PATH}",
                ],
            )

            add_op(
                state,
                server.shell,
                name=f"Write {SERVICE_NAME}.service",
                commands=[
                    f"cat > {SERVICE_PATH} << 'UNIT_EOF'\n{_build_service_unit()}UNIT_EOF",
                ],
            )
            add_op(
                state,
                server.shell,
                name=f"Write {SERVICE_NAME}.timer",
                commands=[
                    f"cat > {TIMER_PATH} << 'UNIT_EOF'\n"
                    f"{_build_timer_unit(self.on_calendar)}UNIT_EOF",
                ],
            )
            add_op(
                state,
                server.shell,
                name="Reload systemd and enable timer",
                commands=[
                    "systemctl daemon-reload",
                    f"systemctl enable --now {SERVICE_NAME}.timer",
                ],
            )

            if self.run_now:
                # `systemctl start` on a oneshot service runs synchronously
                # — exit status reflects the script's exit code, so a
                # misconfigured webhook surfaces immediately.
                add_op(
                    state,
                    server.shell,
                    name="Trigger immediate reporter run",
                    commands=[f"systemctl start {SERVICE_NAME}.service"],
                )

            run_ops(state)

            if isinstance(progress, RichLiveProgressHandler):
                progress.mark_all_done()
        finally:
            disconnect_all(state)
            if isinstance(progress, RichLiveProgressHandler):
                progress._live.stop()

        if state.failed_hosts:
            raise RuntimeError(f"{len(state.failed_hosts)} host(s) failed reporter provisioning")


class ProdCanaryReporterRemover:
    """Stop + disable the reporter timer and remove its on-disk artefacts.

    Used by ``prod canary unprovision-reporter``. Leaves the persisted
    ``last-report.json`` state file in place by default so a re-provision
    can resume delta computation; ``purge_state=True`` removes it.
    """

    def __init__(
        self,
        *,
        host_ip: str,
        purge_state: bool = False,
        ssh_key_path: str = "~/.ssh/id_rsa",
        console: Console | None = None,
    ):
        self.host_ip = host_ip
        self.purge_state = purge_state
        self.ssh_key_path = ssh_key_path
        self.console = console or Console()

    def execute(self) -> None:
        if not self.host_ip:
            raise RuntimeError("Monitoring VM IP missing")

        hosts_data = [(self.host_ip, {"ssh_user": "root", "ssh_key": self.ssh_key_path})]
        state = State(inventory=Inventory((hosts_data, {})), config=Config())

        progress = create_progress_handler(self.console)
        state.add_callback_handler(progress)
        if isinstance(progress, RichLiveProgressHandler):
            progress._live.start()

        try:
            connect_all(state)
            add_op(
                state,
                server.shell,
                name="Stop and disable reporter timer",
                commands=[
                    f"systemctl disable --now {SERVICE_NAME}.timer 2>/dev/null || true",
                    f"systemctl disable {SERVICE_NAME}.service 2>/dev/null || true",
                ],
            )
            add_op(
                state,
                server.shell,
                name="Remove reporter files",
                commands=[
                    f"rm -f {SERVICE_PATH} {TIMER_PATH}",
                    f"rm -rf {REPORTER_DIR}",
                    f"rm -f {REPORTER_ENV_PATH}",
                    "systemctl daemon-reload",
                ],
            )
            if self.purge_state:
                add_op(
                    state,
                    server.shell,
                    name="Purge reporter state",
                    commands=[f"rm -rf {REPORTER_STATE_DIR}"],
                )
            run_ops(state)
            if isinstance(progress, RichLiveProgressHandler):
                progress.mark_all_done()
        finally:
            disconnect_all(state)
            if isinstance(progress, RichLiveProgressHandler):
                progress._live.stop()
