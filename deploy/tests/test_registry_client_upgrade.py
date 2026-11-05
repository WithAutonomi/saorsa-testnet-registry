"""What a service script needs on the VM besides the script (INFRA-484).

`prod-continuous-uploader` runs its service under `/opt/ant-registry-client` and
writes Postgres itself, so replacing its script is half an upgrade: the venv's
`saorsa-db` wheel was installed at provision time and nothing else updates it,
and the columns the new script writes have to exist already.

**Two** commands install that script — `deployments upgrade-service-script` and
`deployments upgrade-wasm-client`, which redeploys it alongside a new bundle.
The first round of this change handled the coupling in one of them only, which
left an operator using the other to break a running uploader (review finding 1).
So the facts live in one module and this file asserts both commands reach them.
"""

import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from saorsa_deploy.registry_client_upgrade import (
    KINDS_WITH_REGISTRY_CLIENT,
    REQUIRED_REGISTRY_COLUMNS,
    SAORSA_DB_DIST,
    WHEEL_PATH_ENV_VAR,
    WHEELHOUSE_ENV_VAR,
    WheelUnavailable,
    missing_registry_columns,
    needs_registry_client,
    release_wheel_url,
    resolve_saorsa_db_wheel,
    schema_problem,
    wheel_distribution,
    wheel_for_kinds,
    wheelhouse_dir,
)

MODULE = "saorsa_deploy.registry_client_upgrade"

#: Nothing a release ever carried, so a test that reaches a real wheelhouse or
#: a real release is a test that fails rather than one that quietly passes.
RUNNING = "9.9.9"


def _write_wheel(directory, version, *, declares=None, name="saorsa-db") -> Path:
    """A real, readable wheel: the version check reads METADATA, not the name.

    ``declares`` writes a different version INSIDE the archive from the one in
    the file name, which is the only way a wheelhouse lookup keyed on the name
    can hand back the wrong version at all.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{SAORSA_DB_DIST}-{version}-py3-none-any.whl"
    declared = declares or version
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{SAORSA_DB_DIST}-{declared}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {declared}\n"
            "\nName: not-this\nVersion: 0.0.0\n",
        )
    return path


@pytest.fixture(autouse=True)
def _pinned_version():
    """Every resolution test runs as a known version of the tool."""
    with patch(f"{MODULE}.running_version", return_value=RUNNING):
        yield


@pytest.fixture
def no_network():
    """The HTTP boundary, stubbed. No test may reach GitHub."""
    with patch(f"{MODULE}.requests.get") as get:
        get.side_effect = requests.RequestException("no network in tests")
        yield get


@pytest.fixture
def no_workspace(tmp_path):
    """A ``repo_root`` with no ``db/`` in it — a released install's situation."""
    root = tmp_path / "installed" / "lib" / "python3.13"
    root.mkdir(parents=True)
    return root


def _repo_for(metadata):
    import saorsa_db.models  # noqa: F401  (registers the tables on Base.metadata)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    metadata.create_all(engine)
    return SimpleNamespace(session=sessionmaker(bind=engine, future=True)())


class TestWhichKindsNeedIt:
    def test_only_the_continuous_uploader_does(self):
        assert KINDS_WITH_REGISTRY_CLIENT == {"prod-continuous-uploader"}

    @pytest.mark.parametrize(
        "kind,expected",
        [
            ("prod-continuous-uploader", True),
            ("prod-continuous-downloader", False),
            ("prod-downloaders", False),
            ("testnet", False),
            ("", False),
        ],
    )
    def test_needs_registry_client(self, kind, expected):
        assert needs_registry_client(kind) is expected

    def test_the_kind_that_needs_it_is_the_one_whose_script_writes_postgres(self):
        """The reason, asserted rather than only written down.

        Its service runs under the registry-client venv and its script passes
        the new keyword to the repository unconditionally, which is exactly
        what an un-upgraded wheel rejects. The kind that does NOT need it
        imports nothing of ours at all.
        """
        from saorsa_deploy.provisioning import prod_continuous_uploader as prov
        from saorsa_deploy.provisioning import prod_downloader as dl
        from saorsa_deploy.resources import get_resources_dir

        assert "/opt/ant-registry-client" in prov.__doc__
        script = (get_resources_dir() / "scripts" / prov.UPLOADER_SCRIPT_NAME).read_text()
        assert "upsert_prod_upload_result" in script
        assert "error_kind=error_kind or None" in script

        downloader = (get_resources_dir() / "scripts" / dl.SCRIPT_NAME).read_text()
        # The import, not a mention: the shared classifier block names the
        # module in a comment, and a comment runs nothing.
        assert "import saorsa_db" not in downloader
        assert "upsert_" not in downloader


class TestTheSchemaCheck:
    def test_a_current_schema_is_clean(self):
        from saorsa_db.db import Base

        assert missing_registry_columns(_repo_for(Base.metadata)) == []

    def test_an_empty_schema_reports_every_table(self):
        from sqlalchemy import MetaData

        missing = missing_registry_columns(_repo_for(MetaData()))
        assert len(missing) == len(REQUIRED_REGISTRY_COLUMNS)
        assert all("table absent" in entry for entry in missing)

    def test_a_pre_migration_schema_names_the_missing_column(self):
        """The table is there; only the column this change adds is not."""
        from sqlalchemy import Column, Integer, MetaData, String, Table

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
        assert missing_registry_columns(_repo_for(metadata)) == [
            "deployment_prod_upload_results.error_kind"
        ]

    def test_every_required_column_is_one_the_models_declare(self):
        """So the check cannot ask for something no upgrade would supply."""
        import saorsa_db.models  # noqa: F401
        from saorsa_db.db import Base

        for table, column in REQUIRED_REGISTRY_COLUMNS:
            assert column in Base.metadata.tables[table].c, (table, column)

    def test_the_problem_names_the_migration_command(self):
        from sqlalchemy import MetaData

        problem = schema_problem(_repo_for(MetaData()), "prod-continuous-uploader")
        assert "alembic upgrade head" in problem
        assert "pool claim" in problem

    def test_no_problem_is_reported_for_a_kind_that_does_not_need_it(self):
        """A downloader's script writes no Postgres, so the columns are moot."""
        from sqlalchemy import MetaData

        assert schema_problem(_repo_for(MetaData()), "prod-continuous-downloader") is None

    def test_a_current_schema_yields_no_problem(self):
        from saorsa_db.db import Base

        assert schema_problem(_repo_for(Base.metadata), "prod-continuous-uploader") is None


class TestWhichKindsGetAWheelAtAll:
    def test_no_wheel_is_resolved_for_kinds_that_do_not_need_one(self):
        wheel, problem = wheel_for_kinds(["prod-continuous-downloader"])
        assert (wheel, problem) == (None, None)

    def test_no_wheel_is_resolved_for_an_empty_target_set(self):
        assert wheel_for_kinds([]) == (None, None)

    def test_a_downloader_kind_reads_nothing_and_fetches_nothing(self):
        """Not merely "returns None": it must not look.

        A download kind needs neither the wheel nor the schema, so a resolution
        it cannot use must not cost it a `uv build`, a wheelhouse stat or a
        GitHub request.
        """
        with patch(f"{MODULE}.resolve_saorsa_db_wheel") as resolve:
            assert wheel_for_kinds(["prod-continuous-downloader"]) == (None, None)
        resolve.assert_not_called()

    def test_one_wheel_covers_a_mixed_invocation(self, tmp_path):
        """Resolved once, whichever of the named kinds needs it."""
        built = _write_wheel(tmp_path / "workspace-dist", RUNNING)
        with (
            patch(f"{MODULE}.workspace_has_package", return_value=True),
            patch(f"{MODULE}.build_saorsa_db_wheel", return_value=built) as build,
        ):
            wheel, problem = wheel_for_kinds(
                ["prod-continuous-downloader", "prod-continuous-uploader"]
            )
        assert problem is None
        assert wheel.path == built
        assert build.call_count == 1


class TestTheResolutionOrder:
    """Four sources, first hit wins (INFRA-517).

    Each case stubs the lower-priority sources so the winner is unambiguous:
    if resolution silently fell through, the test would get the stub's answer
    rather than the one it asserts.
    """

    def test_an_explicit_path_wins_over_the_workspace(self, tmp_path, no_network):
        given = _write_wheel(tmp_path / "byhand", RUNNING)
        workspace = _write_wheel(tmp_path / "workspace-dist", RUNNING)
        with (
            patch(f"{MODULE}.workspace_has_package", return_value=True),
            patch(f"{MODULE}.build_saorsa_db_wheel", return_value=workspace) as build,
        ):
            resolved = resolve_saorsa_db_wheel(explicit=given)
        assert resolved.path == given
        assert resolved.source == "--saorsa-db-wheel"
        build.assert_not_called()

    def test_the_environment_variable_is_the_same_escape_hatch(
        self, tmp_path, monkeypatch, no_workspace, no_network
    ):
        given = _write_wheel(tmp_path / "byhand", RUNNING)
        monkeypatch.setenv(WHEEL_PATH_ENV_VAR, str(given))
        resolved = resolve_saorsa_db_wheel(repo_root=no_workspace)
        assert resolved.path == given
        assert resolved.source == f"${WHEEL_PATH_ENV_VAR}"

    def test_the_flag_wins_over_the_environment_variable(
        self, tmp_path, monkeypatch, no_workspace, no_network
    ):
        flag = _write_wheel(tmp_path / "flag", RUNNING)
        monkeypatch.setenv(WHEEL_PATH_ENV_VAR, str(_write_wheel(tmp_path / "env", RUNNING)))
        resolved = resolve_saorsa_db_wheel(explicit=flag, repo_root=no_workspace)
        assert resolved.path == flag
        assert resolved.source == "--saorsa-db-wheel"

    def test_a_named_file_that_is_not_there_is_refused_not_fallen_through(
        self, tmp_path, monkeypatch
    ):
        """An operator who named a file meant that file.

        Falling through would install a different wheel than the one asked for,
        which is how the wrong one reaches a production VM.
        """
        _write_wheel(tmp_path / "wheelhouse", RUNNING)
        monkeypatch.setenv(WHEELHOUSE_ENV_VAR, str(tmp_path / "wheelhouse"))
        with pytest.raises(WheelUnavailable) as exc:
            resolve_saorsa_db_wheel(explicit=tmp_path / "absent.whl")
        assert "absent.whl" in str(exc.value)
        assert "no such file" in str(exc.value)

    def test_the_workspace_wins_over_the_wheelhouse(self, tmp_path, monkeypatch, no_network):
        """The development flow is unchanged: a checkout builds its own code."""
        workspace = _write_wheel(tmp_path / "workspace-dist", RUNNING)
        _write_wheel(tmp_path / "wheelhouse", RUNNING)
        monkeypatch.setenv(WHEELHOUSE_ENV_VAR, str(tmp_path / "wheelhouse"))
        with (
            patch(f"{MODULE}.workspace_has_package", return_value=True),
            patch(f"{MODULE}.build_saorsa_db_wheel", return_value=workspace),
        ):
            resolved = resolve_saorsa_db_wheel()
        assert resolved.path == workspace
        assert "built from the workspace" in resolved.source

    def test_a_workspace_build_is_exempt_from_the_version_check_and_says_so(
        self, tmp_path, no_network
    ):
        """A working tree is deliberately allowed to be ahead of any release."""
        workspace = _write_wheel(tmp_path / "workspace-dist", "9.9.10")
        with (
            patch(f"{MODULE}.workspace_has_package", return_value=True),
            patch(f"{MODULE}.build_saorsa_db_wheel", return_value=workspace),
        ):
            resolved = resolve_saorsa_db_wheel()
        assert resolved.version == "9.9.10"
        assert "working-tree build" in resolved.warning

    def test_the_wheelhouse_wins_over_the_release_download(
        self, tmp_path, monkeypatch, no_workspace, no_network
    ):
        wheel = _write_wheel(tmp_path / "wheelhouse", RUNNING)
        monkeypatch.setenv(WHEELHOUSE_ENV_VAR, str(tmp_path / "wheelhouse"))
        resolved = resolve_saorsa_db_wheel(repo_root=no_workspace)
        assert resolved.path == wheel
        assert resolved.source.startswith("wheelhouse ")
        no_network.assert_not_called()

    def test_the_wheelhouse_default_is_the_state_directory(self, no_workspace, no_network):
        """The procedures' `--find-links` convention, as a documented default."""
        wheel = _write_wheel(wheelhouse_dir(), RUNNING)
        resolved = resolve_saorsa_db_wheel(repo_root=no_workspace)
        assert resolved.path == wheel

    def test_the_wheelhouse_takes_the_running_version_not_the_newest(
        self, tmp_path, monkeypatch, no_workspace, no_network
    ):
        """The operator box holds every version any run ever fetched.

        "Newest" is whichever release was installed last and has nothing to do
        with the tool that is running.
        """
        house = tmp_path / "wheelhouse"
        wanted = _write_wheel(house, RUNNING)
        later = _write_wheel(house, "10.0.0")
        import os

        os.utime(later, (10**9, 10**9 + 10**6))
        monkeypatch.setenv(WHEELHOUSE_ENV_VAR, str(house))
        assert resolve_saorsa_db_wheel(repo_root=no_workspace).path == wanted

    def test_the_release_is_the_last_resort(self, tmp_path, monkeypatch, no_workspace):
        """Stubbed at the HTTP boundary — nothing reaches GitHub."""
        payload = _write_wheel(tmp_path / "release", RUNNING).read_bytes()
        monkeypatch.setenv(WHEELHOUSE_ENV_VAR, str(tmp_path / "empty-wheelhouse"))
        response = MagicMock()
        response.iter_content.return_value = [payload]
        with patch(f"{MODULE}.requests.get", return_value=response) as get:
            resolved = resolve_saorsa_db_wheel(repo_root=no_workspace)
        assert get.call_args.args[0] == release_wheel_url(RUNNING)
        assert resolved.source == f"GitHub release v{RUNNING}"
        assert resolved.version == RUNNING
        assert resolved.path.is_file()

    def test_a_download_that_is_not_a_wheel_is_refused(self, tmp_path, monkeypatch, no_workspace):
        """A readable zip of the right distribution, or nothing."""
        monkeypatch.setenv(WHEELHOUSE_ENV_VAR, str(tmp_path / "empty-wheelhouse"))
        response = MagicMock()
        response.iter_content.return_value = [b"<html>404</html>"]
        with patch(f"{MODULE}.requests.get", return_value=response):
            with pytest.raises(ValueError) as exc:
                resolve_saorsa_db_wheel(repo_root=no_workspace)
        assert "not a readable wheel" in str(exc.value)


class TestTheReleasedInstallRegression:
    """The bug itself: no workspace anywhere, and a wheelhouse that has it.

    Every production maintenance procedure runs the tool as a released,
    version-pinned install, so this is the only situation the two commands are
    ever actually invoked in — and it used to refuse in two seconds.
    """

    def test_a_wheelhouse_wheel_resolves_with_no_problem(
        self, tmp_path, monkeypatch, no_workspace, no_network
    ):
        wheel = _write_wheel(tmp_path / "wheelhouse", RUNNING)
        monkeypatch.setenv(WHEELHOUSE_ENV_VAR, str(tmp_path / "wheelhouse"))
        resolved, problem = wheel_for_kinds(["prod-continuous-uploader"], repo_root=no_workspace)
        assert problem is None
        assert resolved.path == wheel
        assert resolved.describe() == f"{wheel.name} (wheelhouse {tmp_path / 'wheelhouse'})"

    def test_nothing_resolvable_is_a_problem_not_an_exception(
        self, tmp_path, monkeypatch, no_workspace, no_network
    ):
        """So the caller still refuses before anything is built or installed."""
        monkeypatch.setenv(WHEELHOUSE_ENV_VAR, str(tmp_path / "empty-wheelhouse"))
        resolved, problem = wheel_for_kinds(["prod-continuous-uploader"], repo_root=no_workspace)
        assert resolved is None
        assert "--saorsa-db-wheel" in problem
        assert str(no_workspace) in problem
        assert "wheelhouse" in problem
        assert "GitHub release" in problem

    def test_the_refusal_never_tells_a_production_run_to_use_a_checkout(
        self, tmp_path, monkeypatch, no_workspace, no_network
    ):
        """The one piece of advice a maintenance procedure may not act on."""
        monkeypatch.setenv(WHEELHOUSE_ENV_VAR, str(tmp_path / "empty-wheelhouse"))
        _, problem = wheel_for_kinds(["prod-continuous-uploader"], repo_root=no_workspace)
        assert "checkout" not in problem.lower()

    def test_a_failed_download_is_reported_as_a_tried_source(
        self, tmp_path, monkeypatch, no_workspace, no_network
    ):
        """A network error arrives in the same `(None, problem)` shape.

        Neither caller catches anything; both refuse on the string.
        """
        monkeypatch.setenv(WHEELHOUSE_ENV_VAR, str(tmp_path / "empty-wheelhouse"))
        _, problem = wheel_for_kinds(["prod-continuous-uploader"], repo_root=no_workspace)
        assert "could not be fetched" in problem
        assert "no network in tests" in problem


class TestTheVersionCheck:
    """The invariant, stated instead of held by accident of the cwd.

    A release publishes both wheels at one version, and the wheel on the VM has
    to match the distribution writing the service script — or the script's
    first operation raises in the repository call while holding a pool claim.
    """

    def test_a_mismatched_explicit_wheel_is_refused_naming_both_versions(
        self, tmp_path, no_workspace, no_network
    ):
        given = _write_wheel(tmp_path / "byhand", "1.13.0")
        with pytest.raises(WheelUnavailable) as exc:
            resolve_saorsa_db_wheel(explicit=given, repo_root=no_workspace)
        assert "1.13.0" in str(exc.value)
        assert RUNNING in str(exc.value)

    def test_a_mismatched_wheelhouse_wheel_is_refused_naming_both_versions(
        self, tmp_path, monkeypatch, no_workspace, no_network
    ):
        """The file name says the running version; its METADATA does not."""
        _write_wheel(tmp_path / "wheelhouse", RUNNING, declares="1.13.0")
        monkeypatch.setenv(WHEELHOUSE_ENV_VAR, str(tmp_path / "wheelhouse"))
        resolved, problem = wheel_for_kinds(["prod-continuous-uploader"], repo_root=no_workspace)
        assert resolved is None
        assert "1.13.0" in problem
        assert RUNNING in problem

    def test_the_mismatch_can_be_allowed_explicitly_and_warns(
        self, tmp_path, no_workspace, no_network
    ):
        given = _write_wheel(tmp_path / "byhand", "1.13.0")
        resolved = resolve_saorsa_db_wheel(
            explicit=given, allow_version_mismatch=True, repo_root=no_workspace
        )
        assert resolved.path == given
        assert "pool claim" in resolved.warning

    def test_a_wheel_of_another_distribution_is_refused(self, tmp_path, no_workspace, no_network):
        given = _write_wheel(tmp_path / "byhand", RUNNING, name="saorsa-deploy")
        with pytest.raises(ValueError) as exc:
            resolve_saorsa_db_wheel(explicit=given, repo_root=no_workspace)
        assert "not saorsa-db" in str(exc.value)

    def test_the_version_is_read_from_metadata_not_from_the_file_name(self, tmp_path):
        path = _write_wheel(tmp_path, RUNNING, declares="1.2.3")
        assert wheel_distribution(path) == ("saorsa-db", "1.2.3")

    def test_metadata_headers_end_at_the_blank_line(self, tmp_path):
        """A long description may repeat the field names; the headers do not."""
        path = _write_wheel(tmp_path, RUNNING)
        assert wheel_distribution(path) == ("saorsa-db", RUNNING)

    def test_a_file_that_is_not_a_zip_is_refused(self, tmp_path):
        path = tmp_path / f"{SAORSA_DB_DIST}-{RUNNING}-py3-none-any.whl"
        path.write_bytes(b"not a zip")
        with pytest.raises(ValueError) as exc:
            wheel_distribution(path)
        assert "not a readable wheel" in str(exc.value)

    def test_a_zip_with_no_metadata_is_refused(self, tmp_path):
        path = tmp_path / f"{SAORSA_DB_DIST}-{RUNNING}-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("saorsa_db/__init__.py", "")
        with pytest.raises(ValueError) as exc:
            wheel_distribution(path)
        assert "no .dist-info/METADATA" in str(exc.value)


class TestUpgradeWasmClientReachesAllOfIt:
    """The command this round's finding was about.

    It redeploys the same script alongside a new bundle, so it needs the same
    wheel and the same schema. PROD-WUL-01 is a `prod-continuous-uploader`
    running `client_kind: wasm`, which is exactly the deployment both commands
    can target.
    """

    MODULE = "saorsa_deploy.cmd.upgrade_wasm_client"

    def _target(self, kind="prod-continuous-uploader"):
        from saorsa_deploy.cmd.upgrade_wasm_client import UpgradeTarget

        return UpgradeTarget(
            name="PROD-WUL-01",
            kind=kind,
            service="ant-prod-continuous-uploader",
            host_ips=["10.0.0.1"],
            old_commit=None,
        )

    def test_the_target_can_carry_a_wheel(self):
        assert self._target().saorsa_db_wheel is None

    def test_the_install_is_folded_in_before_the_script(self):
        from pathlib import Path

        from saorsa_deploy.cmd.upgrade_wasm_client import _install_on_target

        target = self._target()
        target.saorsa_db_wheel = Path("/tmp/saorsa_db-1.14.0-py3-none-any.whl")
        order: list[str] = []
        state = SimpleNamespace(failed_hosts=set())
        with (
            patch(f"{self.MODULE}.clear_known_hosts", lambda *_a, **_k: None),
            patch(f"{self.MODULE}.Inventory", lambda spec: SimpleNamespace()),
            patch(f"{self.MODULE}.State", lambda **_k: state),
            patch(f"{self.MODULE}.connect_all", lambda _s: None),
            patch(f"{self.MODULE}.disconnect_all", lambda _s: None),
            patch(f"{self.MODULE}.run_ops", lambda _s: None),
            patch(
                f"{self.MODULE}.add_install_ops",
                lambda _s, _w, console=None: order.append("wheel"),
            ),
            patch(
                f"{self.MODULE}.add_op",
                lambda *_a, **_k: order.append("script"),
            ),
        ):
            _install_on_target(
                target,
                "https://example.invalid/bundle.tar.gz",
                "~/.ssh/id_rsa",
                SimpleNamespace(print=lambda *_a, **_k: None),
            )
        assert order == ["wheel", "script"]

    def test_a_downloader_target_gets_no_install(self):
        from saorsa_deploy.cmd.upgrade_wasm_client import _install_on_target

        target = self._target(kind="prod-continuous-downloader")
        target.service = "ant-prod-downloader"
        order: list[str] = []
        state = SimpleNamespace(failed_hosts=set())
        with (
            patch(f"{self.MODULE}.clear_known_hosts", lambda *_a, **_k: None),
            patch(f"{self.MODULE}.Inventory", lambda spec: SimpleNamespace()),
            patch(f"{self.MODULE}.State", lambda **_k: state),
            patch(f"{self.MODULE}.connect_all", lambda _s: None),
            patch(f"{self.MODULE}.disconnect_all", lambda _s: None),
            patch(f"{self.MODULE}.run_ops", lambda _s: None),
            patch(
                f"{self.MODULE}.add_install_ops",
                lambda _s, _w, console=None: order.append("wheel"),
            ),
            patch(f"{self.MODULE}.add_op", lambda *_a, **_k: order.append("script")),
        ):
            _install_on_target(
                target,
                "https://example.invalid/bundle.tar.gz",
                "~/.ssh/id_rsa",
                SimpleNamespace(print=lambda *_a, **_k: None),
            )
        assert order == ["script"]

    def test_a_pre_migration_registry_is_refused_before_the_build(self):
        """The build is ~10 minutes on a billing VM, so this comes first."""
        from saorsa_deploy.cmd.upgrade_wasm_client import _resolve_targets

        deployment = SimpleNamespace(
            id=673,
            name="PROD-WUL-01",
            deploy_args={
                "kind": "prod-continuous-uploader",
                "client_kind": "wasm",
                "wasm_build": {"commit": "abc"},
            },
        )
        repo = MagicMock()
        repo.get_active_by_name.return_value = deployment
        repo.get_by_name.return_value = deployment
        printed: list[str] = []
        console = SimpleNamespace(print=lambda *a, **_k: printed.append(" ".join(map(str, a))))
        with (
            patch(f"{self.MODULE}.get_repository", return_value=repo),
            patch(
                f"{self.MODULE}.load_client_state",
                return_value={"clients": [{"ip": "10.0.0.1", "role": "upload"}]},
            ),
            patch(
                f"{self.MODULE}.schema_problem",
                return_value="missing deployment_prod_upload_results.error_kind",
            ),
            patch(f"{self.MODULE}._manifest_ref", return_value=None),
        ):
            with pytest.raises(SystemExit) as exc:
                _resolve_targets(["PROD-WUL-01"], console)
        assert exc.value.code == 1
        assert any("nothing was built or installed" in line for line in printed)
        assert any("deployment_prod_upload_results.error_kind" in line for line in printed)
