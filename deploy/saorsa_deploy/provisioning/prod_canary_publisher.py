"""Pyinfra provisioner for the prod canary dataset publisher.

Installs a daily timer on the canary's monitoring VM that runs
``deployments record-results`` and then ``prod canary export``, so the JSON
dataset behind ``canary.autonomi.com`` stays current without an operator in
the loop. Before this existed the registry held only as much probe history as
someone had thought to record by hand, while the rest sat in the monitoring
VM's InfluxDB waiting to expire.

**Why the monitoring VM.** ``prod canary destroy`` keeps it by default (the
dataset is the product), it already carries the daily Slack reporter, and
InfluxDB is local to it — so ``record-results`` reads over loopback instead of
across the internet.

**Why a uv-managed venv rather than the system Python.** The `deploy` package
requires Python >= 3.13 and Ubuntu 24.04 ships 3.12, so there is no system
interpreter that can run it. `uv` supplies both the interpreter and the venv,
and is pinned to one version here so a provision is reproducible.

**Why wheels over SSH rather than a git URL.** The VM holds no GitHub
credential and the repository may be private; the wheels are small and pyinfra
is already connected. Built fresh at provision time so the VM runs exactly the
code in this working tree — which is also how the publisher is upgraded: re-run
``provision-publisher``.

**Why the Influx URL and token are passed explicitly.** The VM has no
credential for the tfstate bucket, so a ``record-results`` that fell back to
``load_monitoring_state`` would die at 09:30 with a boto3 credentials error.
``deploy/tests/test_prod_canary_publisher.py`` pins that property.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import files, server
from rich.console import Console

from saorsa_deploy.provisioning.progress import (
    RichLiveProgressHandler,
    create_progress_handler,
)
from saorsa_deploy.provisioning.saorsa_db_install import build_workspace_wheel

PUBLISHER_DIR = "/opt/ant-canary-publisher"
VENV_DIR = f"{PUBLISHER_DIR}/venv"
VENV_BIN = f"{VENV_DIR}/bin/saorsa-deploy"
RUN_SCRIPT_PATH = f"{PUBLISHER_DIR}/run.sh"
ENV_PATH = "/etc/ant-canary-publisher.env"
STATE_DIR = "/var/lib/ant-canary-publisher"
STATE_FILE = f"{STATE_DIR}/last-run.json"
WHEEL_DIR = "/root/ant-canary-publisher-wheels"
SERVICE_NAME = "ant-canary-publisher"
SERVICE_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"
TIMER_PATH = f"/etc/systemd/system/{SERVICE_NAME}.timer"

#: Pinned so two provisions a month apart install the same tool. Bump it
#: deliberately; the install verifies the version it got.
UV_VERSION = "0.9.24"
UV_BIN = "/root/.local/bin/uv"

#: The interpreter the `deploy` package needs. Ubuntu 24.04 has 3.12.
PYTHON_VERSION = "3.13"

#: The workspace packages the publisher runs. `saorsa-deploy` is the CLI;
#: `saorsa-db` carries the models and repositories it writes through.
WHEEL_PACKAGES = ("saorsa-db", "saorsa-deploy")

#: `systemctl start` on a oneshot unit blocks until the script exits, and the
#: unit allows it three hours. Give ssh(1) enough rope to outlast that.
RUN_NOW_TIMEOUT_SECS = 11_000

_SSH_OPTS = [
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=15",
]


def build_service_unit() -> str:
    return f"""\
[Unit]
Description=Ant Prod Canary dataset publisher (one-shot)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile={ENV_PATH}
Environment=PYTHONUNBUFFERED=1
ExecStart={RUN_SCRIPT_PATH}
TimeoutStartSec=3h
StandardOutput=journal
StandardError=journal
"""


def build_timer_unit(on_calendar: str) -> str:
    # RandomizedDelaySec=0 on purpose: a single VM publishing one dataset has
    # nothing to spread load against, and a predictable publish time is what
    # makes "the site is stale" diagnosable.
    return f"""\
[Unit]
Description=Daily trigger for the Ant Prod Canary dataset publisher

[Timer]
OnCalendar={on_calendar}
Persistent=true
RandomizedDelaySec=0
Unit={SERVICE_NAME}.service

[Install]
WantedBy=timers.target
"""


def _systemd_quote(value: str) -> str:
    """Quote one ``EnvironmentFile`` value.

    systemd applies shell-like unquoting to these lines: in an *unquoted*
    value a backslash is an escape and edge whitespace is stripped, so a
    database password containing ``\\`` would reach the VM altered — and
    silently, as a credential that simply does not authenticate. Emitting
    every value double-quoted, with ``\\`` and ``"`` escaped, makes the file
    carry exactly what the operator supplied.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_env_file(
    *,
    database_url: str,
    aws_access_key_id: str,
    aws_secret_access_key: str,
    region: str,
    canary_name: str,
    bucket: str,
    prefix: str,
    title: str,
    influx_token: str,
    influx_org: str,
    influx_bucket: str,
    influx_url: str = "http://localhost:8086",
) -> str:
    """The VM's ``EnvironmentFile``. Every key is written, never dropped when
    empty — a silently absent key would surface as an unset-variable abort
    inside ``run.sh`` rather than as a provisioning failure here."""
    env = {
        "DATABASE_URL": database_url,
        "AWS_ACCESS_KEY_ID": aws_access_key_id,
        "AWS_SECRET_ACCESS_KEY": aws_secret_access_key,
        "AWS_DEFAULT_REGION": region,
        "CANARY_NAME": canary_name,
        "CANARY_BUCKET": bucket,
        "CANARY_PREFIX": prefix,
        "CANARY_TITLE": title,
        "INFLUX_URL": influx_url,
        "INFLUX_TOKEN": influx_token,
        "INFLUX_ORG": influx_org,
        "INFLUX_BUCKET": influx_bucket,
    }
    return "".join(f"{k}={_systemd_quote(v)}\n" for k, v in env.items())


_RUN_SCRIPT_HEADER = """\
#!/usr/bin/env bash
# Pull the canary's InfluxDB points into Postgres, then export the public
# dataset to S3. Installed by `saorsa-deploy prod canary provision-publisher`;
# driven by the ant-canary-publisher.timer.
#
# Deliberately NOT `set -e`: the point of last-run.json is to say which step
# failed, and `-e` would exit before anything could write it, leaving the file
# reading "running" forever.
set -uo pipefail

"""

_RUN_SCRIPT_BODY = """
mkdir -p "$STATE_DIR"

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
epoch() { date -u +%s; }

STARTED_AT="$(now)"
STARTED_EPOCH="$(epoch)"

cat > "$STATE_FILE" <<EOF
{"started_at": "$STARTED_AT", "status": "running"}
EOF

write_result() {
  local exit_code="$1" failed_step="$2" record_results_s="$3" export_s="$4"
  local finished_at finished_epoch step_json
  finished_at="$(now)"
  finished_epoch="$(epoch)"
  if [ -z "$failed_step" ]; then
    step_json="null"
  else
    step_json="\\"$failed_step\\""
  fi
  cat > "$STATE_FILE" <<EOF
{
  "started_at": "$STARTED_AT",
  "finished_at": "$finished_at",
  "duration_s": $((finished_epoch - STARTED_EPOCH)),
  "exit_code": $exit_code,
  "failed_step": $step_json,
  "record_results_s": $record_results_s,
  "export_s": $export_s
}
EOF
}

echo "[publisher] record-results: $CANARY_NAME"
RECORD_START="$(epoch)"
"$BIN" deployments record-results \\
  --name "$CANARY_NAME" \\
  --influx-url "$INFLUX_URL" \\
  --influx-token "$INFLUX_TOKEN" \\
  --influx-org "$INFLUX_ORG" \\
  --influx-bucket "$INFLUX_BUCKET"
RECORD_CODE=$?
RECORD_SECS=$(( $(epoch) - RECORD_START ))
if [ "$RECORD_CODE" -ne 0 ]; then
  echo "[publisher] record-results failed with exit $RECORD_CODE" >&2
  write_result "$RECORD_CODE" "record-results" "$RECORD_SECS" "null"
  exit "$RECORD_CODE"
fi

echo "[publisher] export: $CANARY_NAME -> s3://$CANARY_BUCKET/$CANARY_PREFIX"
EXPORT_START="$(epoch)"
"$BIN" prod canary export \\
  --name "$CANARY_NAME" \\
  --bucket "$CANARY_BUCKET" \\
  --prefix "$CANARY_PREFIX" \\
  --title "$CANARY_TITLE"
EXPORT_CODE=$?
EXPORT_SECS=$(( $(epoch) - EXPORT_START ))
if [ "$EXPORT_CODE" -ne 0 ]; then
  echo "[publisher] export failed with exit $EXPORT_CODE" >&2
  write_result "$EXPORT_CODE" "export" "$RECORD_SECS" "$EXPORT_SECS"
  exit "$EXPORT_CODE"
fi

write_result 0 "" "$RECORD_SECS" "$EXPORT_SECS"
echo "[publisher] done in $(( $(epoch) - STARTED_EPOCH ))s"
"""


def build_run_script(*, venv_bin: str = VENV_BIN, state_dir: str = STATE_DIR) -> str:
    """Render ``run.sh``.

    The two paths are shell variables so the script's control flow can be
    exercised locally against a stub binary — the rest of the body is a
    constant and so is literally what runs on the VM.
    """
    return (
        _RUN_SCRIPT_HEADER
        + f'BIN="{venv_bin}"\n'
        + f'STATE_DIR="{state_dir}"\n'
        + 'STATE_FILE="$STATE_DIR/last-run.json"\n'
        + _RUN_SCRIPT_BODY
    )


def build_uv_install_commands() -> list[str]:
    """Install the pinned `uv` and the interpreter the publisher needs.

    The version is verified rather than assumed: the installer resolves a URL,
    and a redirect landing on a different build would otherwise be discovered
    later, by a venv that behaves unlike the one that was tested.
    """
    return [
        # UV_NO_MODIFY_PATH keeps the installer out of root's shell profile:
        # every command here names {UV_BIN} absolutely, so a PATH line would
        # be an edit to a shared VM's login environment for no gain.
        f"test -x {UV_BIN} && {UV_BIN} --version | grep -q ' {UV_VERSION}$' || "
        f"(DEBIAN_FRONTEND=noninteractive apt-get update -q && "
        f"DEBIAN_FRONTEND=noninteractive apt-get install -y -q curl && "
        f"curl -LsSf https://astral.sh/uv/{UV_VERSION}/install.sh "
        f"| env UV_NO_MODIFY_PATH=1 sh)",
        f"{UV_BIN} --version | grep -q ' {UV_VERSION}$' || "
        f'(echo "uv is not {UV_VERSION}: $({UV_BIN} --version)" >&2; exit 1)',
        f"{UV_BIN} python install {PYTHON_VERSION}",
    ]


def build_venv_install_commands(wheel_names: list[str]) -> list[str]:
    """Create the venv and install the uploaded wheels into it.

    Wheels are named exactly rather than globbed: the directory is cleared
    before upload, but a glob that matched a leftover from an interrupted run
    would install two versions of one package and the failure would be a
    mystery.
    """
    wheels = " ".join(f"{WHEEL_DIR}/{name}" for name in wheel_names)
    return [
        # `--clear` is not optional for a command whose re-run IS the upgrade
        # path: uv 0.9.24 only warns that it "will require --clear to replace"
        # an existing venv, so without it the next UV_VERSION bump turns every
        # re-provision into a hard failure.
        f"{UV_BIN} venv --clear --python {PYTHON_VERSION} {VENV_DIR}",
        f"{UV_BIN} pip install --python {VENV_DIR}/bin/python --reinstall {wheels}",
    ]


def build_verify_commands() -> list[str]:
    """Prove the venv can actually run both halves of ``run.sh``.

    Output is captured and replayed on failure so the provisioning error names
    the import or argparse problem instead of just a non-zero exit. The
    ``export`` check is separate from ``--help`` on purpose: a `saorsa-deploy`
    that predates ``prod canary export`` would install cleanly and then fail
    every night at step 2.
    """
    log = "/tmp/ant-canary-publisher-verify.log"
    return [
        f"{VENV_BIN} --help > {log} 2>&1 || (cat {log} >&2; exit 1)",
        f"{VENV_BIN} prod canary export --help > {log} 2>&1 || (cat {log} >&2; exit 1)",
    ]


def build_removal_commands(*, purge: bool) -> list[str]:
    """Commands for ``unprovision-publisher``.

    Pure so a test can assert on exactly which paths are touched. Nothing here
    names the Slack reporter, InfluxDB, Grafana or Telegraf — they share the
    VM and are not this command's business.
    """
    commands = [
        f"systemctl disable --now {SERVICE_NAME}.timer 2>/dev/null || true",
        f"systemctl disable {SERVICE_NAME}.service 2>/dev/null || true",
        f"rm -f {SERVICE_PATH} {TIMER_PATH}",
        f"rm -f {RUN_SCRIPT_PATH}",
        f"rm -f {ENV_PATH}",
        "systemctl daemon-reload",
    ]
    if purge:
        commands += [
            f"rm -rf {VENV_DIR}",
            f"rm -rf {WHEEL_DIR}",
            f"rm -rf {STATE_DIR}",
            # The venv was the only thing left in it; leaving an empty
            # directory behind would make a purged VM look provisioned.
            f"rmdir --ignore-fail-on-non-empty {PUBLISHER_DIR} || true",
        ]
    return commands


#: A wheel this far over the clean build's size (1.3 MB for `saorsa-deploy`,
#: 85 KB for `saorsa-db`) means the build swept up something that is not
#: source. That already happened once: hatchling honours only the ROOT
#: `.gitignore`, so `resources/wasm-client/node_modules/` — ignored by a nested
#: `.gitignore` alone — went into the wheel and made it 23.7 MB, i.e. the VM
#: would have run whatever was on the operator's disk. `deploy/pyproject.toml`
#: excludes those paths now; this is the tripwire for the next one.
MAX_WHEEL_BYTES = 8 * 1024 * 1024


def build_publisher_wheels(repo_root: Path | None = None, console=None) -> list[Path]:
    """Build the two workspace wheels the VM installs."""
    wheels = []
    for package in WHEEL_PACKAGES:
        wheel = build_workspace_wheel(package, repo_root)
        size = wheel.stat().st_size
        if size > MAX_WHEEL_BYTES:
            raise RuntimeError(
                f"{wheel.name} is {size / 1024 / 1024:.1f} MB, over the "
                f"{MAX_WHEEL_BYTES / 1024 / 1024:.0f} MB sanity limit — the build has "
                "picked up something that is not source. Check the build "
                "excludes in the package's pyproject.toml (hatchling reads "
                "only the ROOT .gitignore, so a nested one does not protect "
                "a directory like node_modules)."
            )
        if console:
            console.print(f"  Built wheel: {wheel.name} ({size / 1024 / 1024:.2f} MB)")
        wheels.append(wheel)
    return wheels


class ProdCanaryPublisherProvisioner:
    """Installs the publisher venv, run script, env file and timer."""

    def __init__(
        self,
        *,
        host_ip: str,
        deployment_name: str,
        bucket: str,
        prefix: str,
        title: str,
        database_url: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        region: str,
        influx_token: str,
        influx_org: str,
        influx_bucket: str,
        on_calendar: str,
        wheels: list[Path],
        ssh_key_path: str = "~/.ssh/id_rsa",
        console: Console | None = None,
    ):
        self.host_ip = host_ip
        self.deployment_name = deployment_name
        self.bucket = bucket
        self.prefix = prefix
        self.title = title
        self.database_url = database_url
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.region = region
        self.influx_token = influx_token
        self.influx_org = influx_org
        self.influx_bucket = influx_bucket
        self.on_calendar = on_calendar
        self.wheels = wheels
        self.ssh_key_path = ssh_key_path
        self.console = console or Console()

    def execute(self) -> None:
        if not self.host_ip:
            raise RuntimeError("Monitoring VM IP missing")
        if not self.influx_token:
            raise RuntimeError("Influx token is required")
        if not self.wheels:
            raise RuntimeError("No wheels to install")

        hosts_data = [(self.host_ip, {"ssh_user": "root", "ssh_key": self.ssh_key_path})]
        state = State(inventory=Inventory((hosts_data, {})), config=Config())

        progress = create_progress_handler(self.console)
        state.add_callback_handler(progress)
        if isinstance(progress, RichLiveProgressHandler):
            progress._live.start()

        # The env file and run.sh are staged as local files and uploaded,
        # never interpolated into a shell command: the env file carries a
        # database password, an AWS secret key and the Influx token, and a
        # `cat > … <<EOF` op puts all three in the VM's /proc/<pid>/cmdline
        # for the op's duration and in any pyinfra verbose log. `files.put`
        # also creates the file 0600 outright, where the heredoc left it
        # world-readable until a following chmod.
        staging = tempfile.TemporaryDirectory(prefix="ant-canary-publisher-")

        try:
            self.console.print(f"Connecting to monitoring host {self.host_ip} as root...")
            connect_all(state)

            add_op(
                state,
                server.shell,
                name="Create publisher directories",
                commands=[
                    f"mkdir -p {PUBLISHER_DIR} {STATE_DIR} && chmod 0750 {STATE_DIR}",
                    # Cleared rather than merged so the install below can name
                    # the wheels exactly.
                    f"rm -rf {WHEEL_DIR} && mkdir -p {WHEEL_DIR}",
                ],
            )
            for wheel in self.wheels:
                add_op(
                    state,
                    files.put,
                    name=f"Upload {wheel.name}",
                    src=str(wheel),
                    dest=f"{WHEEL_DIR}/{wheel.name}",
                    mode="644",
                    add_deploy_dir=False,
                )
            add_op(
                state,
                server.shell,
                name=f"Install uv {UV_VERSION} and Python {PYTHON_VERSION}",
                commands=build_uv_install_commands(),
            )
            add_op(
                state,
                server.shell,
                name="Install the publisher venv",
                commands=build_venv_install_commands([w.name for w in self.wheels]),
            )
            add_op(
                state,
                server.shell,
                name="Verify the publisher CLI",
                commands=build_verify_commands(),
            )

            env_text = build_env_file(
                database_url=self.database_url,
                aws_access_key_id=self.aws_access_key_id,
                aws_secret_access_key=self.aws_secret_access_key,
                region=self.region,
                canary_name=self.deployment_name,
                bucket=self.bucket,
                prefix=self.prefix,
                title=self.title,
                influx_token=self.influx_token,
                influx_org=self.influx_org,
                influx_bucket=self.influx_bucket,
            )
            staged_env = Path(staging.name) / "ant-canary-publisher.env"
            staged_env.write_text(env_text)
            staged_env.chmod(0o600)
            add_op(
                state,
                files.put,
                name="Write publisher env file",
                src=str(staged_env),
                dest=ENV_PATH,
                mode="600",
                add_deploy_dir=False,
            )

            staged_run = Path(staging.name) / "run.sh"
            staged_run.write_text(build_run_script())
            add_op(
                state,
                files.put,
                name="Write run.sh",
                src=str(staged_run),
                dest=RUN_SCRIPT_PATH,
                mode="755",
                add_deploy_dir=False,
            )
            add_op(
                state,
                server.shell,
                name=f"Write {SERVICE_NAME}.service",
                commands=[f"cat > {SERVICE_PATH} << 'UNIT_EOF'\n{build_service_unit()}UNIT_EOF"],
            )
            add_op(
                state,
                server.shell,
                name=f"Write {SERVICE_NAME}.timer",
                commands=[
                    f"cat > {TIMER_PATH} << 'UNIT_EOF'\n"
                    f"{build_timer_unit(self.on_calendar)}UNIT_EOF"
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

            run_ops(state)

            if isinstance(progress, RichLiveProgressHandler):
                progress.mark_all_done()
        finally:
            disconnect_all(state)
            if isinstance(progress, RichLiveProgressHandler):
                progress._live.stop()
            # Only now: pyinfra reads `src` during run_ops, not at add_op.
            staging.cleanup()

        if state.failed_hosts:
            raise RuntimeError(f"{len(state.failed_hosts)} host(s) failed publisher provisioning")

    def run_once(self) -> tuple[int, str]:
        """Start the service synchronously and read back ``last-run.json``.

        Run over plain ssh rather than as a pyinfra op because both halves
        matter to the operator: the exit code becomes this command's, and the
        state file says which step produced it.
        """
        try:
            start = self._ssh(
                f"systemctl start {SERVICE_NAME}.service",
                timeout=RUN_NOW_TIMEOUT_SECS,
            )
        except subprocess.TimeoutExpired:
            # The unit's own TimeoutStartSec is 3h and this wait outlasts it,
            # so getting here means ssh itself hung rather than the run
            # overrunning. Say where to look instead of raising a traceback
            # out of a command that has already installed everything.
            raise RuntimeError(
                f"the immediate run did not return within {RUN_NOW_TIMEOUT_SECS}s. "
                f"The publisher is installed; check it on the VM with "
                f"`journalctl -u {SERVICE_NAME}.service` and "
                f"`cat {STATE_FILE}`."
            ) from None

        last_run = self._ssh(f"cat {STATE_FILE} 2>/dev/null || true")
        if start.returncode != 0:
            journal = self._ssh(f"journalctl -u {SERVICE_NAME}.service -n 40 --no-pager")
            if journal.stdout:
                # markup=False: run.sh's own step markers are `[publisher] …`,
                # which rich would read as a style tag and swallow — losing
                # exactly the lines that say which step failed.
                self.console.print(journal.stdout.strip(), markup=False, highlight=False)
        return start.returncode, (last_run.stdout or "").strip()

    def _ssh(self, command: str, *, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "ssh",
                "-i",
                self.ssh_key_path,
                *_SSH_OPTS,
                f"root@{self.host_ip}",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )


class ProdCanaryPublisherRemover:
    """Stop the timer and remove the publisher's artefacts."""

    def __init__(
        self,
        *,
        host_ip: str,
        purge: bool = False,
        ssh_key_path: str = "~/.ssh/id_rsa",
        console: Console | None = None,
    ):
        self.host_ip = host_ip
        self.purge = purge
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
                name="Remove the canary publisher",
                commands=build_removal_commands(purge=self.purge),
            )
            run_ops(state)
            if isinstance(progress, RichLiveProgressHandler):
                progress.mark_all_done()
        finally:
            disconnect_all(state)
            if isinstance(progress, RichLiveProgressHandler):
                progress._live.stop()

        if state.failed_hosts:
            raise RuntimeError(f"{len(state.failed_hosts)} host(s) failed publisher removal")
