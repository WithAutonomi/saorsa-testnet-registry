"""Tests for `prod canary provision-publisher` / `unprovision-publisher`.

The rendered artefacts are golden-tested because they are what runs
unattended on the monitoring VM at 09:30: a change to the unit's
``TimeoutStartSec``, the timer's ``Persistent``, or the env file's key set is
a change to the publisher's behaviour and should have to be stated here.

``run.sh`` itself is executed, against a stub `saorsa-deploy`, because the one
thing it exists to do beyond running two commands is record *which* of them
failed — and that is control flow, not text.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from saorsa_db.db import Base
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from saorsa_deploy.cmd.prod_canary_publisher import (
    DEFAULT_REGION,
    cmd_prod_canary_provision_publisher,
    load_publisher_env,
    parse_env_file,
)
from saorsa_deploy.provisioning.prod_canary_publisher import (
    ENV_PATH,
    PUBLISHER_DIR,
    RUN_SCRIPT_PATH,
    SERVICE_PATH,
    STATE_DIR,
    TIMER_PATH,
    VENV_DIR,
    WHEEL_DIR,
    build_env_file,
    build_removal_commands,
    build_run_script,
    build_service_unit,
    build_timer_unit,
    build_venv_install_commands,
)

# ---------------------------------------------------------------------------
# 1. Rendered artefacts (golden strings)
# ---------------------------------------------------------------------------


EXPECTED_SERVICE_UNIT = """\
[Unit]
Description=Ant Prod Canary dataset publisher (one-shot)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/ant-canary-publisher.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/ant-canary-publisher/run.sh
TimeoutStartSec=3h
StandardOutput=journal
StandardError=journal
"""

EXPECTED_TIMER_UNIT = """\
[Unit]
Description=Daily trigger for the Ant Prod Canary dataset publisher

[Timer]
OnCalendar=*-*-* 09:30:00
Persistent=true
RandomizedDelaySec=0
Unit=ant-canary-publisher.service

[Install]
WantedBy=timers.target
"""

EXPECTED_ENV_FILE = """\
DATABASE_URL=postgresql://pub:pw@db.example.com/registry
AWS_ACCESS_KEY_ID=AKIAPUB
AWS_SECRET_ACCESS_KEY=secretpub
AWS_DEFAULT_REGION=eu-west-2
CANARY_NAME=PROD-CANARY-04
CANARY_BUCKET=canary-datasets
CANARY_PREFIX=v1
CANARY_TITLE=Autonomi canary
INFLUX_URL=http://localhost:8086
INFLUX_TOKEN=influx-tok
INFLUX_ORG=ant
INFLUX_BUCKET=testnet-metrics
"""


def _env_file_kwargs():
    return {
        "database_url": "postgresql://pub:pw@db.example.com/registry",
        "aws_access_key_id": "AKIAPUB",
        "aws_secret_access_key": "secretpub",
        "region": "eu-west-2",
        "canary_name": "PROD-CANARY-04",
        "bucket": "canary-datasets",
        "prefix": "v1",
        "title": "Autonomi canary",
        "influx_token": "influx-tok",
        "influx_org": "ant",
        "influx_bucket": "testnet-metrics",
    }


class TestRenderedArtefacts:
    def test_service_unit(self):
        assert build_service_unit() == EXPECTED_SERVICE_UNIT

    def test_timer_unit(self):
        assert build_timer_unit("*-*-* 09:30:00") == EXPECTED_TIMER_UNIT

    def test_env_file(self):
        assert build_env_file(**_env_file_kwargs()) == EXPECTED_ENV_FILE

    def test_run_script_invokes_both_steps_with_the_venv_binary(self):
        script = build_run_script()
        assert script.startswith("#!/usr/bin/env bash\n")
        # Not `-e`: the failing-step record has to be written before exit.
        assert "set -uo pipefail" in script
        assert not any(
            line.strip() in ("set -e", "set -eu", "set -euo pipefail")
            for line in script.splitlines()
        )
        assert f'BIN="{PUBLISHER_DIR}/venv/bin/saorsa-deploy"' in script
        assert f'STATE_DIR="{STATE_DIR}"' in script
        assert '"$BIN" deployments record-results \\\n  --name "$CANARY_NAME"' in script
        assert '"$BIN" prod canary export \\\n  --name "$CANARY_NAME"' in script
        # The URL and token are passed explicitly, which is the whole reason
        # the VM needs no tfstate credential.
        assert '--influx-url "$INFLUX_URL"' in script
        assert '--influx-token "$INFLUX_TOKEN"' in script

    def test_run_script_is_valid_bash(self):
        proc = subprocess.run(
            ["bash", "-n", "-"], input=build_run_script(), text=True, capture_output=True
        )
        assert proc.returncode == 0, proc.stderr

    def test_wheels_are_installed_by_exact_name(self):
        commands = build_venv_install_commands(
            ["saorsa_db-0.1.0-py3-none-any.whl", "saorsa_deploy-0.1.0-py3-none-any.whl"]
        )
        joined = "\n".join(commands)
        assert "*.whl" not in joined
        assert f"{WHEEL_DIR}/saorsa_db-0.1.0-py3-none-any.whl" in joined
        assert f"{WHEEL_DIR}/saorsa_deploy-0.1.0-py3-none-any.whl" in joined
        assert f"--python {VENV_DIR}/bin/python --reinstall" in joined


# ---------------------------------------------------------------------------
# 2. Env-file validation, before any build or SSH
# ---------------------------------------------------------------------------


class TestParseEnvFile:
    def test_ignores_comments_and_blanks_and_strips_quotes(self):
        parsed = parse_env_file(
            "# a comment\n"
            "\n"
            "CANARY_PUBLISHER_DATABASE_URL='postgresql://x/y'\n"
            'export CANARY_PUBLISHER_REGION="us-east-1"\n'
            "not-an-assignment\n"
        )
        assert parsed == {
            "CANARY_PUBLISHER_DATABASE_URL": "postgresql://x/y",
            "CANARY_PUBLISHER_REGION": "us-east-1",
        }


class TestLoadPublisherEnv:
    def test_region_defaults(self, tmp_path):
        path = tmp_path / "pub.env"
        path.write_text(
            "CANARY_PUBLISHER_DATABASE_URL=postgresql://x/y\n"
            "CANARY_PUBLISHER_AWS_ACCESS_KEY_ID=AKIA\n"
            "CANARY_PUBLISHER_AWS_SECRET_ACCESS_KEY=sek\n"
        )
        loaded = load_publisher_env(str(path))
        assert loaded["region"] == DEFAULT_REGION
        assert loaded["database_url"] == "postgresql://x/y"

    def test_missing_file_names_the_required_keys(self, tmp_path):
        with pytest.raises(ValueError, match="CANARY_PUBLISHER_DATABASE_URL"):
            load_publisher_env(str(tmp_path / "nope.env"))

    def test_every_missing_key_is_named_at_once(self, tmp_path):
        path = tmp_path / "pub.env"
        path.write_text("CANARY_PUBLISHER_AWS_ACCESS_KEY_ID=AKIA\n")
        with pytest.raises(ValueError) as exc:
            load_publisher_env(str(path))
        message = str(exc.value)
        assert "CANARY_PUBLISHER_DATABASE_URL" in message
        assert "CANARY_PUBLISHER_AWS_SECRET_ACCESS_KEY" in message
        assert "2 required key(s)" in message

    def test_empty_value_counts_as_missing(self, tmp_path):
        path = tmp_path / "pub.env"
        path.write_text(
            "CANARY_PUBLISHER_DATABASE_URL=\n"
            "CANARY_PUBLISHER_AWS_ACCESS_KEY_ID=AKIA\n"
            "CANARY_PUBLISHER_AWS_SECRET_ACCESS_KEY=sek\n"
        )
        with pytest.raises(ValueError, match="CANARY_PUBLISHER_DATABASE_URL"):
            load_publisher_env(str(path))


def _provision_args(tmp_path, name="PROD-CANARY-04", env_text=None):
    env_file = tmp_path / "pub.env"
    if env_text is None:
        env_text = (
            "CANARY_PUBLISHER_DATABASE_URL=postgresql://x/y\n"
            "CANARY_PUBLISHER_AWS_ACCESS_KEY_ID=AKIA\n"
            "CANARY_PUBLISHER_AWS_SECRET_ACCESS_KEY=sek\n"
        )
    env_file.write_text(env_text)
    return SimpleNamespace(
        name=name,
        bucket="canary-datasets",
        prefix="v1",
        title=None,
        on_calendar="*-*-* 09:30:00",
        env_file=str(env_file),
        run_now=False,
        ssh_key_path="~/.ssh/id_rsa",
    )


def _exploding(*_a, **_k):
    raise AssertionError("must not be reached before the env file is validated")


class TestProvisionPreflightOrder:
    def test_missing_keys_refused_before_registry_state_or_build(self, tmp_path):
        args = _provision_args(tmp_path, env_text="CANARY_PUBLISHER_AWS_ACCESS_KEY_ID=AKIA\n")
        with (
            patch("saorsa_deploy.cmd.prod_canary_publisher.get_repository", _exploding),
            patch("saorsa_deploy.cmd.prod_canary_publisher.load_monitoring_state", _exploding),
            patch("saorsa_deploy.cmd.prod_canary_publisher.build_publisher_wheels", _exploding),
            patch("saorsa_deploy.cmd.prod_canary_publisher.wait_for_ssh", _exploding),
        ):
            with pytest.raises(SystemExit) as exc:
                cmd_prod_canary_provision_publisher(args)
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# 3. A non-canary deployment is refused
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session)
    session.close()
    engine.dispose()


class TestKindGuard:
    def test_non_canary_refused(self, tmp_path, repo):
        repo.create(name="PROD-DL-01", deploy_args={"kind": "prod-downloaders"})
        args = _provision_args(tmp_path, name="PROD-DL-01")
        with (
            patch(
                "saorsa_deploy.cmd.prod_canary_publisher.get_repository",
                return_value=repo,
            ),
            patch("saorsa_deploy.cmd.prod_canary_publisher.load_monitoring_state", _exploding),
            patch("saorsa_deploy.cmd.prod_canary_publisher.build_publisher_wheels", _exploding),
        ):
            with pytest.raises(SystemExit) as exc:
                cmd_prod_canary_provision_publisher(args)
        assert exc.value.code == 1

    def test_unknown_deployment_refused(self, tmp_path, repo):
        args = _provision_args(tmp_path, name="NOPE-01")
        with (
            patch(
                "saorsa_deploy.cmd.prod_canary_publisher.get_repository",
                return_value=repo,
            ),
            patch("saorsa_deploy.cmd.prod_canary_publisher.load_monitoring_state", _exploding),
        ):
            with pytest.raises(SystemExit) as exc:
                cmd_prod_canary_provision_publisher(args)
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# 4. run.sh behaviour, executed against a stub `saorsa-deploy`
# ---------------------------------------------------------------------------


_STUB = """\
#!/usr/bin/env bash
# Stands in for the venv's saorsa-deploy. Records the argv it was handed and
# exits with whatever the test asked for.
echo "$@" >> "$STUB_CALLS"
if [ "$1" = "deployments" ]; then
  exit "${STUB_RECORD_EXIT:-0}"
fi
exit "${STUB_EXPORT_EXIT:-0}"
"""


def _run_publisher_script(tmp_path, *, record_exit=0, export_exit=0):
    stub = tmp_path / "saorsa-deploy"
    stub.write_text(_STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    state_dir = tmp_path / "state"
    script = tmp_path / "run.sh"
    script.write_text(build_run_script(venv_bin=str(stub), state_dir=str(state_dir)))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    calls = tmp_path / "calls.txt"
    env = {
        **os.environ,
        "STUB_CALLS": str(calls),
        "STUB_RECORD_EXIT": str(record_exit),
        "STUB_EXPORT_EXIT": str(export_exit),
        "CANARY_NAME": "PROD-CANARY-04",
        "CANARY_BUCKET": "canary-datasets",
        "CANARY_PREFIX": "v1",
        "CANARY_TITLE": "Autonomi canary",
        "INFLUX_URL": "http://localhost:8086",
        "INFLUX_TOKEN": "tok",
        "INFLUX_ORG": "ant",
        "INFLUX_BUCKET": "testnet-metrics",
    }
    proc = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=60
    )
    last_run = json.loads((state_dir / "last-run.json").read_text())
    call_lines = calls.read_text().splitlines() if calls.exists() else []
    return proc, last_run, call_lines


class TestRunScriptBehaviour:
    def test_success_records_both_steps(self, tmp_path):
        proc, last_run, calls = _run_publisher_script(tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert last_run["exit_code"] == 0
        assert last_run["failed_step"] is None
        assert isinstance(last_run["record_results_s"], int)
        assert isinstance(last_run["export_s"], int)
        assert last_run["started_at"].endswith("Z")
        assert last_run["finished_at"].endswith("Z")
        assert len(calls) == 2
        assert calls[0].startswith("deployments record-results --name PROD-CANARY-04")
        assert calls[1].startswith("prod canary export --name PROD-CANARY-04")

    def test_record_results_failure_stops_the_run(self, tmp_path):
        proc, last_run, calls = _run_publisher_script(tmp_path, record_exit=3)
        assert proc.returncode == 3
        assert last_run["exit_code"] == 3
        assert last_run["failed_step"] == "record-results"
        # export_s is null because export never ran — and it never ran, which
        # is the property: a stale Postgres must not be published as current.
        assert last_run["export_s"] is None
        assert len(calls) == 1
        assert "export" not in calls[0]

    def test_export_failure_is_attributed_to_export(self, tmp_path):
        proc, last_run, calls = _run_publisher_script(tmp_path, export_exit=4)
        assert proc.returncode == 4
        assert last_run["exit_code"] == 4
        assert last_run["failed_step"] == "export"
        assert isinstance(last_run["record_results_s"], int)
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# 5. record-results with an explicit Influx URL + token reads no S3 state
# ---------------------------------------------------------------------------


class TestRecordResultsNeedsNoS3:
    """The VM holds no credential for the tfstate bucket.

    If `record-results` ever fell back to `load_monitoring_state`, the timer
    would fail nightly with a boto3 credentials error — and nothing on the VM
    would say why, because the fallback is a reasonable thing for the command
    to do when it is run by an operator.
    """

    def test_no_monitoring_state_and_no_boto3_client(self, repo):
        import boto3

        from saorsa_deploy.cmd.record_results import cmd_record_results

        repo.create(name="PROD-CANARY-04", deploy_args={"kind": "prod-canary"})
        args = SimpleNamespace(
            name="PROD-CANARY-04",
            monitoring_name=None,
            influx_url="http://localhost:8086",
            influx_token="tok",
            influx_org="ant",
            influx_bucket="testnet-metrics",
            window="-30d",
            update_slack=False,
        )

        def _no_s3(*_a, **_k):
            raise AssertionError("record-results must not reach S3 with explicit Influx creds")

        with (
            patch(
                "saorsa_deploy.cmd.record_results.get_repository",
                return_value=repo,
            ),
            patch("saorsa_deploy.cmd.record_results.load_monitoring_state", _no_s3),
            patch("saorsa_deploy.state.load_monitoring_state", _no_s3),
            patch("saorsa_deploy.state._get_s3_client", _no_s3),
            patch.object(boto3, "client", _no_s3),
            patch(
                "saorsa_deploy.registry.get_file_pool_repository",
                return_value=None,
            ),
            patch(
                "saorsa_deploy.cmd.prod_canary_results.fetch_canary_upload_points",
                return_value=[],
            ),
            patch(
                "saorsa_deploy.cmd.prod_canary_results.fetch_canary_chunk_points",
                return_value=[],
            ),
            patch(
                "saorsa_deploy.cmd.prod_canary_results.fetch_canary_probe_points",
                return_value=[],
            ),
        ):
            cmd_record_results(args)


# ---------------------------------------------------------------------------
# 6. unprovision-publisher touches exactly the publisher's paths
# ---------------------------------------------------------------------------


#: Everything else that lives on the canary's monitoring VM. None of it is the
#: publisher's business, and `unprovision-publisher` naming any of it would be
#: an operator losing the Slack digest, the dashboards or the dataset itself.
_FOREIGN_PATHS = (
    "/opt/ant-prod-canary-reporter",
    "/var/lib/ant-prod-canary-reporter",
    "/etc/ant-prod-canary-reporter.env",
    "/etc/systemd/system/ant-prod-canary-reporter.service",
    "/etc/systemd/system/ant-prod-canary-reporter.timer",
    "/var/lib/influxdb2",
    "/var/lib/grafana",
    "/etc/telegraf",
)


def _removed_paths(commands: list[str]) -> set[str]:
    paths: set[str] = set()
    for command in commands:
        for verb in ("rm -rf ", "rm -f ", "rmdir --ignore-fail-on-non-empty "):
            if command.startswith(verb):
                paths.update(command[len(verb) :].split(" || ")[0].split())
    return paths


class TestRemovalCommands:
    def test_default_keeps_the_venv_and_state(self):
        commands = build_removal_commands(purge=False)
        assert _removed_paths(commands) == {
            SERVICE_PATH,
            TIMER_PATH,
            RUN_SCRIPT_PATH,
            ENV_PATH,
        }
        assert "systemctl daemon-reload" in commands

    def test_purge_removes_exactly_the_documented_paths(self):
        commands = build_removal_commands(purge=True)
        assert _removed_paths(commands) == {
            SERVICE_PATH,
            TIMER_PATH,
            RUN_SCRIPT_PATH,
            ENV_PATH,
            VENV_DIR,
            WHEEL_DIR,
            STATE_DIR,
            PUBLISHER_DIR,
        }

    @pytest.mark.parametrize("purge", [False, True])
    def test_nothing_else_on_the_vm_is_touched(self, purge):
        joined = "\n".join(build_removal_commands(purge=purge))
        for path in _FOREIGN_PATHS:
            assert path not in joined

    def test_timer_is_disabled_before_its_units_are_removed(self):
        commands = build_removal_commands(purge=False)
        disable = next(i for i, c in enumerate(commands) if "disable --now" in c)
        remove = next(i for i, c in enumerate(commands) if c.startswith(f"rm -f {SERVICE_PATH}"))
        assert disable < remove


# ---------------------------------------------------------------------------
# The wheel-build helper the publisher shares with the uploader VMs
# ---------------------------------------------------------------------------


class TestWorkspaceWheelHelper:
    def test_builds_both_publisher_packages(self):
        from saorsa_deploy.provisioning.saorsa_db_install import build_workspace_wheel

        calls = []

        def _fake_run(argv, **_kwargs):
            calls.append(argv)
            out_dir = Path(argv[argv.index("--out-dir") + 1])
            package = argv[argv.index("--package") + 1]
            (out_dir / f"{package.replace('-', '_')}-9.9.9-py3-none-any.whl").write_text("")
            return SimpleNamespace(returncode=0)

        with patch(
            "saorsa_deploy.provisioning.saorsa_db_install.subprocess.run",
            _fake_run,
        ):
            db_wheel = build_workspace_wheel("saorsa-db")
            deploy_wheel = build_workspace_wheel("saorsa-deploy")

        assert db_wheel.name == "saorsa_db-9.9.9-py3-none-any.whl"
        assert deploy_wheel.name == "saorsa_deploy-9.9.9-py3-none-any.whl"
        assert [argv[argv.index("--package") + 1] for argv in calls] == [
            "saorsa-db",
            "saorsa-deploy",
        ]

    def test_unknown_package_refused(self):
        from saorsa_deploy.provisioning.saorsa_db_install import build_workspace_wheel

        with pytest.raises(ValueError, match="not a workspace package"):
            build_workspace_wheel("saorsa-web")
