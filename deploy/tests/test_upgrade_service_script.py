"""`deployments upgrade-service-script` (INFRA-484).

There was no way to ship a changed `prod_downloader.py` to PROD-DL-01:
`prod continuous-downloader` has only `deploy` and `destroy`, and `deploy` calls
`record_deployment_created` unconditionally, so a redeploy starts a fresh
registry row and breaks the continuous series the deployment exists to produce.
`upgrade-wasm-client` redeploys the same script but refuses
`client_kind: native`.

What is pinned here is what could go silently wrong: the unit each host group
restarts (one hard-coded name would restart a service that exists on neither of
a continuous uploader's two roles), the refusal of an unsupported kind, and that
one unreachable group does not abandon the others.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.cmd.upgrade_service_script import (
    KINDS_WITH_REGISTRY_CLIENT,
    SUPPORTED_KINDS,
    cmd_upgrade_service_script,
)
from saorsa_deploy.cmd.upgrade_wasm_client import SCRIPT_BY_KIND
from saorsa_deploy.provisioning.prod_continuous_uploader import (
    UPLOADER_SERVICE,
    VERIFIER_SERVICE,
)
from saorsa_deploy.provisioning.prod_downloader import (
    SERVICE_NAME as DOWNLOADER_SERVICE,
)

MODULE = "saorsa_deploy.cmd.upgrade_service_script"


def _args(**over):
    defaults = {"name": "PROD-DL-01", "ssh_key_path": "~/.ssh/id_rsa", "restart": True}
    defaults.update(over)
    return SimpleNamespace(**defaults)


def _repo(kind, *, client_kind="native", daily_at="00:00", name="PROD-DL-01"):
    deployment = SimpleNamespace(
        id=674,
        name=name,
        deploy_args={"kind": kind, "client_kind": client_kind, "daily_at": daily_at},
    )
    repo = MagicMock()
    repo.get_active_by_name.return_value = deployment
    repo.get_by_name.return_value = deployment
    return repo


class _RunRecorder:
    """Captures the commands each host group would run, without pyinfra."""

    def __init__(self, failing_ips=()):
        self.groups: list[tuple[list[str], list[str]]] = []
        self.failing_ips = set(failing_ips)
        #: Hosts the saorsa-db installer was folded in for, and whether it was
        #: added BEFORE that group's script op.
        self.wheel_installed_for: list[list[str]] = []
        self.wheel_before_script = True

    def install(self, stack):
        recorder = self

        class _Installer:
            """Stands in for SaorsaDbInstaller, recording where it ran."""

            def __init__(self, wheel_path=None, console=None):
                self.wheel_path = wheel_path

            def add_ops(self, state, install=True):
                recorder.wheel_installed_for.append(list(state._ips))
                # The script op has not been added for this group yet, which is
                # the ordering that matters: the script calls the repository
                # argument only the new wheel accepts.
                if any(ips == list(state._ips) for ips, _ in recorder.groups):
                    recorder.wheel_before_script = False

        def _inventory(spec):
            # `((hosts_data, {}))` -- the IPs are the first element of each
            # tuple. Stood in for rather than constructed, because a real
            # pyinfra Inventory needs a State attached before it will list its
            # hosts, and this test never builds one.
            hosts_data, _data = spec
            return SimpleNamespace(_ips=[ip for ip, _meta in hosts_data])

        def _state(*, inventory, config):
            return SimpleNamespace(failed_hosts=set(), _ips=list(inventory._ips))

        def _add_op(state, _op, name=None, commands=None):
            self.groups.append((list(state._ips), list(commands or [])))
            if self.failing_ips & set(state._ips):
                state.failed_hosts = {
                    SimpleNamespace(name=ip) for ip in state._ips if ip in self.failing_ips
                }

        stack.enter_context(patch(f"{MODULE}.SaorsaDbInstaller", _Installer))
        stack.enter_context(patch(f"{MODULE}.Inventory", _inventory))
        stack.enter_context(patch(f"{MODULE}.State", _state))
        stack.enter_context(patch(f"{MODULE}.connect_all", lambda _state: None))
        stack.enter_context(patch(f"{MODULE}.disconnect_all", lambda _state: None))
        stack.enter_context(patch(f"{MODULE}.run_ops", lambda _state: None))
        stack.enter_context(patch(f"{MODULE}.add_op", _add_op))
        stack.enter_context(patch(f"{MODULE}.clear_known_hosts", lambda *_a, **_k: None))
        stack.enter_context(patch(f"{MODULE}.record_event", lambda *_a, **_k: None))


def _run(args, repo, clients, failing_ips=(), missing_columns=(), wheel_error=None):
    import contextlib
    from pathlib import Path as _Path

    recorder = _RunRecorder(failing_ips)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch(f"{MODULE}.get_repository", return_value=repo))
        stack.enter_context(patch(f"{MODULE}.load_client_state", return_value={"clients": clients}))
        stack.enter_context(
            patch(f"{MODULE}.missing_registry_columns", return_value=list(missing_columns))
        )
        stack.enter_context(
            patch(
                f"{MODULE}.build_saorsa_db_wheel",
                side_effect=wheel_error,
                return_value=_Path("/tmp/saorsa_db-1.14.0-py3-none-any.whl"),
            )
        )
        recorder.install(stack)
        try:
            cmd_upgrade_service_script(args)
            code = 0
        except SystemExit as exc:
            code = exc.code
    return recorder, code


class TestScope:
    def test_only_the_two_continuous_kinds_are_supported(self):
        assert set(SUPPORTED_KINDS) == set(SCRIPT_BY_KIND)
        assert set(SUPPORTED_KINDS) == {
            "prod-continuous-downloader",
            "prod-continuous-uploader",
        }

    @pytest.mark.parametrize("kind", ["prod-downloaders", "prod-uploaders", "testnet", ""])
    def test_an_unsupported_kind_is_refused_by_name(self, kind):
        recorder, code = _run(_args(), _repo(kind), [{"ip": "10.0.0.1"}])
        assert code == 1
        assert recorder.groups == []

    def test_a_missing_deployment_is_refused(self):
        repo = MagicMock()
        repo.get_active_by_name.return_value = None
        repo.get_by_name.return_value = None
        recorder, code = _run(_args(), repo, [{"ip": "10.0.0.1"}])
        assert code == 1
        assert recorder.groups == []

    def test_no_registry_is_refused(self):
        with patch(f"{MODULE}.get_repository", return_value=None):
            with pytest.raises(SystemExit) as exc:
                cmd_upgrade_service_script(_args())
        assert exc.value.code == 1

    def test_no_client_ip_is_refused(self):
        recorder, code = _run(_args(), _repo("prod-continuous-downloader"), [{"role": "download"}])
        assert code == 1
        assert recorder.groups == []


class TestBothTransportsAreCovered:
    """The whole reason this is not `upgrade-wasm-client`."""

    @pytest.mark.parametrize("client_kind", ["native", "wasm"])
    def test_a_downloader_of_either_transport_is_upgraded(self, client_kind):
        recorder, code = _run(
            _args(),
            _repo("prod-continuous-downloader", client_kind=client_kind),
            [{"ip": "10.0.0.1", "role": "download"}],
        )
        assert code == 0
        assert len(recorder.groups) == 1
        ips, commands = recorder.groups[0]
        assert ips == ["10.0.0.1"]
        assert f"systemctl restart {DOWNLOADER_SERVICE}" in commands
        assert any("prod_downloader.py" in c for c in commands)


class TestOnlyTheHostsThatRunTheScript:
    def test_a_continuous_uploader_touches_only_its_uploader_vm(self):
        """The two roles run DIFFERENT scripts from DIFFERENT directories.

        Writing one script per KIND sent `prod_continuous_uploader.py` to a
        verifier host, whose `/root/ant-prod-continuous-uploader` does not
        exist, and then restarted `ant-prod-upload-verifier` -- so the write
        failed and the run reported a partly-upgraded deployment, and had that
        directory existed the verifier's own `prod_upload_verifier.py` would
        have been left in place under a claim of success. INFRA-485 states the
        rule from the other side: a verifier host in this command's output is a
        bug in the command.
        """
        recorder, code = _run(
            _args(name="PROD-UL-01"),
            _repo("prod-continuous-uploader", name="PROD-UL-01"),
            [
                {"ip": "10.0.0.1", "role": "upload"},
                {"ip": "10.0.0.2", "role": "verify"},
                {"ip": "10.0.0.3", "role": "verify"},
            ],
        )
        assert code == 0
        assert {ip for ips, _ in recorder.groups for ip in ips} == {"10.0.0.1"}
        restarts = [
            c
            for _ips, commands in recorder.groups
            for c in commands
            if c.startswith("systemctl restart")
        ]
        assert restarts == [f"systemctl restart {UPLOADER_SERVICE}"]
        assert not any(VERIFIER_SERVICE in c for _ips, cs in recorder.groups for c in cs)

    def test_a_wasm_uploader_s_verifiers_are_left_alone_too(self):
        """The transport changes nothing here: the verifier's script differs."""
        recorder, code = _run(
            _args(name="PROD-WUL-01"),
            _repo("prod-continuous-uploader", client_kind="wasm", name="PROD-WUL-01"),
            [
                {"ip": "10.0.0.1", "role": "upload"},
                {"ip": "10.0.0.2", "role": "verify"},
            ],
        )
        assert code == 0
        assert {ip for ips, _ in recorder.groups for ip in ips} == {"10.0.0.1"}

    def test_the_skipped_verifiers_are_reported_not_silently_dropped(self):
        printed: list[str] = []
        with patch(f"{MODULE}.Console") as console_cls:
            console_cls.return_value = SimpleNamespace(
                print=lambda *a, **_k: printed.append(" ".join(map(str, a))),
                rule=lambda *_a, **_k: None,
            )
            _run(
                _args(name="PROD-UL-01"),
                _repo("prod-continuous-uploader", name="PROD-UL-01"),
                [
                    {"ip": "10.0.0.1", "role": "upload"},
                    {"ip": "10.0.0.2", "role": "verify"},
                ],
            )
        left_alone = [line for line in printed if "leaving" in line]
        assert left_alone and "prod_upload_verifier.py" in left_alone[0]

    def test_a_deployment_with_only_verifiers_is_refused(self):
        """Nothing to upgrade is a refusal, not a silent success."""
        recorder, code = _run(
            _args(name="PROD-UL-01"),
            _repo("prod-continuous-uploader", name="PROD-UL-01"),
            [{"ip": "10.0.0.2", "role": "verify"}],
        )
        assert code == 1
        assert recorder.groups == []

    def test_every_unit_this_command_can_target_names_its_own_script(self):
        """The targeting is derived from the unit, so the data must be there."""
        from saorsa_deploy.prod_client_units import units_for_kind

        for kind in SUPPORTED_KINDS:
            scripts = {unit.script for unit in units_for_kind(kind)}
            assert None not in scripts, kind
            assert SCRIPT_BY_KIND[kind] in scripts, kind

    def test_a_spec_with_no_ip_is_dropped(self):
        recorder, code = _run(
            _args(),
            _repo("prod-continuous-downloader"),
            [{"ip": "10.0.0.1", "role": "download"}, {"role": "download"}],
        )
        assert code == 0
        assert [ip for ips, _ in recorder.groups for ip in ips] == ["10.0.0.1"]


class TestRestartFlag:
    def test_no_restart_writes_the_script_and_restarts_nothing(self):
        recorder, code = _run(
            _args(restart=False),
            _repo("prod-continuous-downloader"),
            [{"ip": "10.0.0.1", "role": "download"}],
        )
        assert code == 0
        _ips, commands = recorder.groups[0]
        assert any("prod_downloader.py" in c for c in commands)
        assert not any(c.startswith("systemctl restart") for c in commands)

    def test_the_timing_warning_names_the_deployment_s_own_anchor(self):
        """PROD-WDL-01 is at 02:00 and the other three at 00:00."""
        printed: list[str] = []
        with patch(f"{MODULE}.Console") as console_cls:
            console_cls.return_value = SimpleNamespace(
                print=lambda *a, **_k: printed.append(" ".join(map(str, a))),
                rule=lambda *_a, **_k: None,
            )
            _run(
                _args(name="PROD-WDL-01"),
                _repo("prod-continuous-downloader", daily_at="02:00", name="PROD-WDL-01"),
                [{"ip": "10.0.0.1", "role": "download"}],
            )
        warning = [line for line in printed if "Warning" in line]
        assert warning and "02:00 UTC" in warning[0]

    def test_no_warning_is_printed_without_a_restart(self):
        printed: list[str] = []
        with patch(f"{MODULE}.Console") as console_cls:
            console_cls.return_value = SimpleNamespace(
                print=lambda *a, **_k: printed.append(" ".join(map(str, a))),
                rule=lambda *_a, **_k: None,
            )
            _run(
                _args(restart=False),
                _repo("prod-continuous-downloader"),
                [{"ip": "10.0.0.1", "role": "download"}],
            )
        assert not [line for line in printed if "Warning" in line]


class TestOneFailureDoesNotStopTheRest:
    def test_a_failing_host_is_reported_and_the_run_exits_non_zero(self):
        recorder, code = _run(
            _args(),
            _repo("prod-continuous-downloader"),
            [{"ip": "10.0.0.1", "role": "download"}],
            failing_ips=["10.0.0.1"],
        )
        assert {ip for ips, _ in recorder.groups for ip in ips} == {"10.0.0.1"}
        assert code == 1


class TestTheVmSidePackageGoesWithTheScript:
    """The script is not the only thing the uploader VM needs to run it.

    A `prod-continuous-uploader` executes under `/opt/ant-registry-client` and
    writes Postgres itself. Its `saorsa-db` wheel was installed at provision
    time, so the upgraded script's unconditional `error_kind=` keyword raises
    `TypeError` on the service's FIRST upload -- including a successful one --
    before the result is recorded and before the pool claim it is holding is
    resolved. Neither the operator's CLI nor a Postgres migration updates that
    remote wheel (INFRA-484 review, finding 1).
    """

    def test_the_uploader_kind_is_the_one_that_needs_it(self):
        assert KINDS_WITH_REGISTRY_CLIENT == {"prod-continuous-uploader"}

    def test_the_wheel_is_installed_before_the_script_on_the_uploader(self):
        recorder, code = _run(
            _args(name="PROD-UL-01"),
            _repo("prod-continuous-uploader", name="PROD-UL-01"),
            [{"ip": "10.0.0.1", "role": "upload"}],
        )
        assert code == 0
        assert recorder.wheel_installed_for == [["10.0.0.1"]]
        assert recorder.wheel_before_script

    def test_a_downloader_gets_no_wheel(self):
        """It runs /usr/bin/python3 and imports nothing of ours."""
        recorder, code = _run(
            _args(),
            _repo("prod-continuous-downloader"),
            [{"ip": "10.0.0.1", "role": "download"}],
        )
        assert code == 0
        assert recorder.wheel_installed_for == []

    def test_a_missing_registry_column_refuses_before_any_host(self):
        """The VM writes Postgres itself, so the schema has to have moved."""
        recorder, code = _run(
            _args(name="PROD-UL-01"),
            _repo("prod-continuous-uploader", name="PROD-UL-01"),
            [{"ip": "10.0.0.1", "role": "upload"}],
            missing_columns=["deployment_prod_upload_results.error_kind"],
        )
        assert code == 1
        assert recorder.groups == []
        assert recorder.wheel_installed_for == []

    def test_the_refusal_names_the_migration_command(self):
        printed: list[str] = []
        with patch(f"{MODULE}.Console") as console_cls:
            console_cls.return_value = SimpleNamespace(
                print=lambda *a, **_k: printed.append(" ".join(map(str, a))),
                rule=lambda *_a, **_k: None,
            )
            _run(
                _args(name="PROD-UL-01"),
                _repo("prod-continuous-uploader", name="PROD-UL-01"),
                [{"ip": "10.0.0.1", "role": "upload"}],
                missing_columns=["deployment_prod_upload_results.error_kind"],
            )
        assert any("alembic upgrade head" in line for line in printed)

    def test_the_schema_is_not_checked_for_a_downloader(self):
        """It would refuse an upgrade that does not depend on those columns."""
        recorder, code = _run(
            _args(),
            _repo("prod-continuous-downloader"),
            [{"ip": "10.0.0.1", "role": "download"}],
            missing_columns=["deployment_prod_upload_results.error_kind"],
        )
        assert code == 0
        assert recorder.groups

    def test_a_wheel_that_cannot_be_built_refuses_before_any_host(self):
        """Running from an installed wheel instead of from the checkout."""
        recorder, code = _run(
            _args(name="PROD-UL-01"),
            _repo("prod-continuous-uploader", name="PROD-UL-01"),
            [{"ip": "10.0.0.1", "role": "upload"}],
            wheel_error=FileNotFoundError("no db/pyproject.toml under /usr/lib"),
        )
        assert code == 1
        assert recorder.groups == []


class TestTheSchemaCheckItself:
    """`missing_registry_columns` against a real schema, both ways."""

    def _repo_for(self, metadata):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        metadata.create_all(engine)
        return SimpleNamespace(session=sessionmaker(bind=engine, future=True)())

    def test_a_current_schema_is_clean(self):
        from saorsa_db.db import Base

        from saorsa_deploy.cmd.upgrade_service_script import missing_registry_columns

        assert missing_registry_columns(self._repo_for(Base.metadata)) == []

    def test_an_empty_schema_reports_every_table(self):
        from sqlalchemy import MetaData

        from saorsa_deploy.cmd.upgrade_service_script import (
            REQUIRED_REGISTRY_COLUMNS,
            missing_registry_columns,
        )

        missing = missing_registry_columns(self._repo_for(MetaData()))
        assert len(missing) == len(REQUIRED_REGISTRY_COLUMNS)
        assert all("table absent" in entry for entry in missing)

    def test_a_pre_migration_schema_names_the_missing_column(self):
        """The table is there; only the column this change adds is not."""
        from sqlalchemy import Column, Integer, MetaData, String, Table

        from saorsa_deploy.cmd.upgrade_service_script import missing_registry_columns

        metadata = MetaData()
        Table(
            "deployment_prod_upload_results",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("reason", String),
        )
        Table(
            "deployment_upload_records",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("error_kind", String(32)),
            Column("reason", String),
        )
        assert missing_registry_columns(self._repo_for(metadata)) == [
            "deployment_prod_upload_results.error_kind"
        ]
