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

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.registry_client_upgrade import (
    KINDS_WITH_REGISTRY_CLIENT,
    REQUIRED_REGISTRY_COLUMNS,
    missing_registry_columns,
    needs_registry_client,
    schema_problem,
    wheel_for_kinds,
)


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


class TestTheWheel:
    def test_no_wheel_is_built_for_kinds_that_do_not_need_one(self):
        wheel, problem = wheel_for_kinds(["prod-continuous-downloader"])
        assert (wheel, problem) == (None, None)

    def test_no_wheel_is_built_for_an_empty_target_set(self):
        assert wheel_for_kinds([]) == (None, None)

    def test_one_wheel_covers_a_mixed_invocation(self):
        """Built once, whichever of the named kinds needs it."""
        with patch("saorsa_deploy.registry_client_upgrade.build_saorsa_db_wheel") as build:
            build.return_value = "/tmp/saorsa_db-1.14.0-py3-none-any.whl"
            wheel, problem = wheel_for_kinds(
                ["prod-continuous-downloader", "prod-continuous-uploader"]
            )
        assert problem is None
        assert wheel == "/tmp/saorsa_db-1.14.0-py3-none-any.whl"
        assert build.call_count == 1

    def test_a_build_outside_the_checkout_is_a_problem_not_an_exception(self):
        """So the caller can refuse with nothing installed."""
        with patch(
            "saorsa_deploy.registry_client_upgrade.build_saorsa_db_wheel",
            side_effect=FileNotFoundError("no db/pyproject.toml under /usr/lib"),
        ):
            wheel, problem = wheel_for_kinds(["prod-continuous-uploader"])
        assert wheel is None
        assert "saorsa-testnet-registry checkout" in problem


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
