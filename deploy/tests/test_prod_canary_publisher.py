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
import shlex
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
    MAX_WHEEL_BYTES,
    PUBLISHER_DIR,
    RUN_SCRIPT_PATH,
    SERVICE_PATH,
    STATE_DIR,
    TIMER_PATH,
    UV_BIN,
    VENV_DIR,
    WHEEL_DIR,
    build_env_file,
    build_publisher_wheels,
    build_removal_commands,
    build_run_script,
    build_service_unit,
    build_timer_unit,
    build_uv_install_commands,
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

# Every value double-quoted: systemd treats a backslash in an *unquoted*
# EnvironmentFile value as an escape and strips edge whitespace, so an
# unquoted password containing `\` would reach the VM altered.
EXPECTED_ENV_FILE = """\
DATABASE_URL="postgresql://pub:pw@db.example.com/registry"
AWS_ACCESS_KEY_ID="AKIAPUB"
AWS_SECRET_ACCESS_KEY="secretpub"
AWS_DEFAULT_REGION="eu-west-2"
CANARY_NAME="PROD-CANARY-04"
CANARY_BUCKET="canary-datasets"
CANARY_PREFIX="v1"
CANARY_TITLE="Autonomi canary"
INFLUX_URL="http://localhost:8086"
INFLUX_TOKEN="influx-tok"
INFLUX_ORG="ant"
INFLUX_BUCKET="testnet-metrics"
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

    def test_env_values_survive_systemd_unquoting(self):
        """A credential with a backslash or a quote in it must arrive intact.

        systemd unquotes these values, so an unquoted `pa\\ss` would reach the
        VM as `pass` — a credential that simply fails to authenticate, with
        nothing on the VM to say why.
        """
        awkward = 'postgresql://u:pa\\ss"word@db/x'
        kwargs = _env_file_kwargs()
        kwargs["database_url"] = awkward
        line = next(
            line
            for line in build_env_file(**kwargs).splitlines()
            if line.startswith("DATABASE_URL=")
        )
        assert line == 'DATABASE_URL="postgresql://u:pa\\\\ss\\"word@db/x"'
        # The property that matters: what systemd's unquoting hands the
        # process back is byte-for-byte what the operator supplied.
        assert shlex.split(line[len("DATABASE_URL=") :])[0] == awkward

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

    def test_venv_is_cleared_so_reprovision_stays_idempotent(self):
        """`uv venv --clear`, not bare `uv venv`.

        Re-running `provision-publisher` IS the upgrade path. uv 0.9.24 only
        warns that it "will require --clear to replace" an existing venv, so
        without the flag the next UV_VERSION bump turns every re-provision
        into a hard failure.
        """
        commands = build_venv_install_commands(["saorsa_db-0.1.0-py3-none-any.whl"])
        venv_command = next(c for c in commands if " venv " in c)
        assert "--clear" in venv_command

    def test_uv_installer_does_not_edit_the_vms_shell_profile(self):
        joined = "\n".join(build_uv_install_commands())
        assert "UV_NO_MODIFY_PATH=1" in joined
        # Every command names uv absolutely, so no PATH entry is needed.
        assert UV_BIN in joined


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
# Stands in for the venv's saorsa-deploy. Records the argv it was handed, one
# argument per line so the exact argv can be replayed against the real CLI
# parser, and exits with whatever the test asked for.
printf '%s\\n' "--CALL--" "$@" >> "$STUB_CALLS"
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

    # One argv per "--CALL--" marker, so a caller can assert on the exact
    # arguments as well as on how many calls happened.
    argvs: list[list[str]] = []
    for line in calls.read_text().splitlines() if calls.exists() else []:
        if line == "--CALL--":
            argvs.append([])
        else:
            argvs[-1].append(line)
    return proc, last_run, argvs


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
        assert calls[0][:4] == ["deployments", "record-results", "--name", "PROD-CANARY-04"]
        assert calls[1][:5] == ["prod", "canary", "export", "--name", "PROD-CANARY-04"]

    def test_record_results_failure_stops_the_run(self, tmp_path):
        proc, last_run, calls = _run_publisher_script(tmp_path, record_exit=3)
        assert proc.returncode == 3
        assert last_run["exit_code"] == 3
        assert last_run["failed_step"] == "record-results"
        # export_s is null because export never ran — and it never ran, which
        # is the property: a stale Postgres must not be published as current.
        assert last_run["export_s"] is None
        assert len(calls) == 1
        assert "export" not in calls[0]  # the whole argv, not just its head

    def test_both_steps_parse_against_the_real_cli(self, tmp_path):
        """`run.sh` holds two argv as text, and nothing else checks them.

        The script is written here and the two commands are parsed elsewhere,
        so a renamed or removed flag on either would be caught only at 09:30
        on the VM, by a timer whose failure nobody is watching. Replaying the
        exact argv the script emits through `main()` closes that gap —
        `prod canary export` in particular landed in a separate change.
        """
        from saorsa_deploy.main import main

        _proc, _last_run, calls = _run_publisher_script(tmp_path)
        assert len(calls) == 2

        captured = {}

        def _capture(key):
            def _handler(args):
                captured[key] = args

            return _handler

        with (
            patch(
                "saorsa_deploy.cmd.record_results.cmd_record_results",
                _capture("record"),
            ),
            patch(
                "saorsa_deploy.cmd.prod_canary_export.cmd_prod_canary_export",
                _capture("export"),
            ),
        ):
            for argv in calls:
                main(argv)

        assert captured["record"].name == "PROD-CANARY-04"
        assert captured["record"].influx_url == "http://localhost:8086"
        assert captured["record"].influx_token == "tok"
        assert captured["record"].influx_org == "ant"
        assert captured["record"].influx_bucket == "testnet-metrics"

        assert captured["export"].name == "PROD-CANARY-04"
        assert captured["export"].bucket == "canary-datasets"
        assert captured["export"].prefix == "v1"
        assert captured["export"].title == "Autonomi canary"

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

    @staticmethod
    def _args(influx_url="http://localhost:8086", influx_token="tok"):
        return SimpleNamespace(
            name="PROD-CANARY-04",
            monitoring_name=None,
            influx_url=influx_url,
            influx_token=influx_token,
            influx_org="ant",
            influx_bucket="testnet-metrics",
            window="-30d",
            update_slack=False,
        )

    def test_without_explicit_creds_the_guard_fires(self, repo):
        """The negative twin of the test below.

        Without it, an import-binding change — `record_results` importing
        `load_monitoring_state` from somewhere this file does not patch —
        would make the positive test pass for the wrong reason, and the
        property it protects would be gone with no test failing.
        """
        from saorsa_deploy.cmd.record_results import cmd_record_results

        repo.create(name="PROD-CANARY-04", deploy_args={"kind": "prod-canary"})

        def _no_s3(*_a, **_k):
            raise AssertionError("reached S3")

        with (
            patch(
                "saorsa_deploy.cmd.record_results.get_repository",
                return_value=repo,
            ),
            patch("saorsa_deploy.cmd.record_results.load_monitoring_state", _no_s3),
        ):
            with pytest.raises(AssertionError, match="reached S3"):
                cmd_record_results(self._args(influx_url=None, influx_token=None))

    def test_no_monitoring_state_and_no_boto3_client(self, repo):
        import boto3

        from saorsa_deploy.cmd.record_results import cmd_record_results

        repo.create(name="PROD-CANARY-04", deploy_args={"kind": "prod-canary"})
        args = self._args()

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

    def test_an_oversized_wheel_is_refused(self, tmp_path):
        """The tripwire for a build that swept up something that is not source.

        hatchling honours only the ROOT `.gitignore`, so
        `resources/wasm-client/node_modules/` — ignored by a nested one alone
        — went into the `saorsa-deploy` wheel and took it from 1.3 MB to
        23.7 MB (12,478 of 12,888 entries). `deploy/pyproject.toml` excludes
        it now; this catches the next such path, because the consequence is a
        VM running whatever happens to be on the operator's disk.
        """
        fat = tmp_path / "saorsa_deploy-0.0.0-py3-none-any.whl"
        fat.write_bytes(b"\0" * (MAX_WHEEL_BYTES + 1))

        with patch(
            "saorsa_deploy.provisioning.prod_canary_publisher.build_workspace_wheel",
            return_value=fat,
        ):
            with pytest.raises(RuntimeError, match="sanity limit"):
                build_publisher_wheels()

    def test_the_real_wheels_are_within_the_limit(self):
        """Guards the exclude list in `deploy/pyproject.toml` itself.

        Built from this working tree, so it fails on the machine of anyone
        who has run `build ant-wasm-check` if the excludes regress.
        """
        wheels = build_publisher_wheels()
        assert [w.name.split("-")[0] for w in wheels] == ["saorsa_db", "saorsa_deploy"]
        for wheel in wheels:
            assert wheel.stat().st_size <= MAX_WHEEL_BYTES, wheel.name


class TestSecretsNeverReachTheCommandLine:
    """The env file carries a database password, an AWS secret key and the
    Influx token.

    A `cat > … <<EOF` op puts all three in the VM's /proc/<pid>/cmdline for
    the op's duration and in any pyinfra verbose log, and leaves the file
    world-readable until a following chmod. So the env file and run.sh are
    staged locally and uploaded with `files.put`, which creates them 0600 /
    0755 outright.
    """

    def test_env_file_and_run_script_are_uploaded_not_interpolated(self):
        from pyinfra.api.state import BaseStateCallback

        from saorsa_deploy.provisioning import prod_canary_publisher as mod

        recorded = []

        def _fake_add_op(_state, op, **kwargs):
            recorded.append((getattr(op, "__name__", str(op)), kwargs))

        provisioner = mod.ProdCanaryPublisherProvisioner(
            host_ip="10.0.0.1",
            deployment_name="PROD-CANARY-04",
            bucket="canary-datasets",
            prefix="v1",
            title="Autonomi canary",
            database_url="postgresql://pub:s3cr3t-db@db/x",
            aws_access_key_id="AKIAPUB",
            aws_secret_access_key="s3cr3t-aws",
            region="eu-west-2",
            influx_token="s3cr3t-influx",
            influx_org="ant",
            influx_bucket="testnet-metrics",
            on_calendar="*-*-* 09:30:00",
            wheels=[Path("/tmp/saorsa_db-0.0.0-py3-none-any.whl")],
        )

        with (
            patch.object(mod, "add_op", _fake_add_op),
            patch.object(mod, "connect_all", lambda _s: None),
            patch.object(mod, "disconnect_all", lambda _s: None),
            patch.object(mod, "run_ops", lambda _s: None),
            patch.object(mod, "create_progress_handler", lambda _c: BaseStateCallback()),
        ):
            provisioner.execute()

        secrets = ("s3cr3t-db", "s3cr3t-aws", "s3cr3t-influx")
        shell_text = "\n".join(
            "\n".join(kwargs.get("commands", [])) for name, kwargs in recorded if name == "shell"
        )
        for secret in secrets:
            assert secret not in shell_text, f"{secret} reached a shell command"

        puts = {kwargs["dest"]: kwargs for name, kwargs in recorded if name == "put"}
        assert puts[ENV_PATH]["mode"] == "600"
        assert puts[RUN_SCRIPT_PATH]["mode"] == "755"
