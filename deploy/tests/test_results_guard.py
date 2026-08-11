"""Tests for the unrecorded-results teardown guard.

A remote upload's results live in the deployment's InfluxDB until
`record-results` pulls them into Postgres, and the teardown destroys that VM.
Anything unrecorded is then gone permanently — including any chance of closing
the file-pool claims the run took, which stay open forever with nothing able to
resolve them.

The guard warns; it does not block. Refusing on a non-TTY would strand billable
infrastructure, because the release skills run `down` unattended.
"""

from datetime import datetime, timezone

import pytest
from rich.console import Console
from saorsa_db.db import Base
from saorsa_db.models.deployment import Deployment
from saorsa_db.models.prod_canary import (
    DeploymentProdCanaryUpload,
    ProdCanaryInitialVerifyStatus,
)
from saorsa_db.models.prod_file_pool import ClaimSource, PoolFileKind
from saorsa_db.models.prod_upload_result import (
    DeploymentProdUploadResult,
    ProdUploadStatus,
)
from saorsa_db.repositories import (
    SqlAlchemyDeploymentRepository,
    SqlAlchemyProdFilePoolRepository,
)
from saorsa_db.size_class import SizeClass
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy.results_guard import ResultsStatus, assess, warn_if_unrecorded

MIB = 1024 * 1024
NOW = datetime(2026, 7, 30, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def env():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    deployment = Deployment(name="PROD-UL-05", deploy_args={})
    session.add(deployment)
    session.commit()
    yield (
        SqlAlchemyDeploymentRepository(session),
        SqlAlchemyProdFilePoolRepository(session),
        session,
        deployment,
    )
    session.close()


def _flat(text):
    return " ".join(text.split())


def _claim(pool_repo, deployment, name, source=ClaimSource.PROD_UPLOADERS):
    pool_repo.add_pool_file(
        PoolFileKind.URL, f"https://example.org/{name}", name, size_bytes=50 * MIB
    )
    return pool_repo.claim_pool_files(
        PoolFileKind.URL,
        {SizeClass.SMALL: 1},
        source,
        deployment_id=deployment.id,
        deployment_name=deployment.name,
    )[0]


def _result(session, deployment, name):
    session.add(
        DeploymentProdUploadResult(
            deployment_id=deployment.id,
            client_hostname="host-1",
            region="lon1",
            source_url=f"https://example.org/{name}",
            file_name=name,
            file_size_bytes=50 * MIB,
            status=ProdUploadStatus.SUCCESS,
            address="addr",
            started_at=NOW,
            finished_at=NOW,
        )
    )
    session.commit()


class TestAssess:
    def test_claims_give_an_exact_expectation(self, env):
        # The pool makes this precise in a way nothing else does: the claims say
        # exactly how many files the run took.
        repo, pool_repo, session, deployment = env
        _claim(pool_repo, deployment, "a.bin")
        _claim(pool_repo, deployment, "b.bin")
        _result(session, deployment, "a.bin")

        status = assess(repo, pool_repo, deployment, "prod-uploaders")
        assert status.expected == 2
        assert status.recorded == 1
        assert status.unrecorded == 1
        assert status.is_concerning

    def test_fully_recorded_is_not_concerning(self, env):
        repo, pool_repo, session, deployment = env
        _claim(pool_repo, deployment, "a.bin")
        _result(session, deployment, "a.bin")
        status = assess(repo, pool_repo, deployment, "prod-uploaders")
        assert status.unrecorded == 0
        assert not status.is_concerning

    def test_without_claims_there_is_no_expectation(self, env):
        # A hand-written URL list lives on the operator's disk and may have
        # changed, so the only honest check is "was anything recorded at all?".
        repo, pool_repo, session, deployment = env
        status = assess(repo, pool_repo, deployment, "prod-uploaders")
        assert status.expected is None
        assert status.unrecorded is None
        assert not status.is_concerning

    def test_nothing_recorded_with_open_claims_is_concerning(self, env):
        repo, pool_repo, session, deployment = env
        _claim(pool_repo, deployment, "a.bin")
        status = assess(repo, pool_repo, deployment, "prod-uploaders")
        assert status.recorded == 0
        assert status.open_claims == 1
        assert status.is_concerning

    def test_canary_reads_its_own_table(self, env):
        import uuid

        repo, pool_repo, session, deployment = env
        _claim(pool_repo, deployment, "a.bin", ClaimSource.PROD_CANARY)
        session.add(
            DeploymentProdCanaryUpload(
                canary_id=uuid.uuid4(),
                deployment_id=deployment.id,
                source_url="https://example.org/a.bin",
                file_name="a.bin",
                file_size_bytes=50 * MIB,
                top_address="c",
                num_chunks=1,
                uploaded_at=NOW,
                initial_verify_status=ProdCanaryInitialVerifyStatus.OK,
            )
        )
        session.commit()
        status = assess(repo, pool_repo, deployment, "prod-canary")
        assert status.recorded == 1
        assert not status.is_concerning

    def test_over_recording_does_not_go_negative(self, env):
        repo, pool_repo, session, deployment = env
        _claim(pool_repo, deployment, "a.bin")
        _result(session, deployment, "a.bin")
        _result(session, deployment, "b.bin")
        status = assess(repo, pool_repo, deployment, "prod-uploaders")
        assert status.unrecorded == 0


class TestWarning:
    def test_silent_when_everything_is_recorded(self, capsys):
        status = ResultsStatus(expected=3, recorded=3, open_claims=0)
        assert warn_if_unrecorded(
            Console(), "PROD-UL-05", status, interactive=False
        )
        assert capsys.readouterr().out == ""

    def test_warns_loudly_and_names_the_command(self, capsys):
        status = ResultsStatus(expected=7, recorded=2, open_claims=5)
        warn_if_unrecorded(Console(), "PROD-UL-05", status, interactive=False)
        out = _flat(capsys.readouterr().out)
        assert "Results have not been fully recorded" in out
        assert "2 of 7 expected file result(s)" in out
        assert "5 file-pool claim(s) are still open" in out
        assert "gone with it — permanently" in out or "permanently" in out
        assert "record-results --name PROD-UL-05" in out

    def test_non_interactive_proceeds(self, capsys):
        # Refusing here would strand billable infrastructure whenever `down`
        # runs unattended, which is how the release skills invoke it.
        status = ResultsStatus(expected=7, recorded=0, open_claims=7)
        assert warn_if_unrecorded(
            Console(), "PROD-UL-05", status, interactive=False
        )
        assert "continuing with the teardown" in _flat(capsys.readouterr().out)

    def test_interactive_yes_proceeds(self):
        status = ResultsStatus(expected=7, recorded=0, open_claims=7)
        assert warn_if_unrecorded(
            Console(), "X", status, interactive=True, input_fn=lambda _: "y"
        )

    def test_interactive_default_aborts(self, capsys):
        status = ResultsStatus(expected=7, recorded=0, open_claims=7)
        assert not warn_if_unrecorded(
            Console(), "X", status, interactive=True, input_fn=lambda _: ""
        )
        assert "Teardown aborted" in _flat(capsys.readouterr().out)

    def test_interactive_no_aborts(self):
        status = ResultsStatus(expected=7, recorded=0, open_claims=7)
        assert not warn_if_unrecorded(
            Console(), "X", status, interactive=True, input_fn=lambda _: "n"
        )

    def test_message_without_an_expectation(self, capsys):
        status = ResultsStatus(expected=None, recorded=0, open_claims=2)
        warn_if_unrecorded(Console(), "X", status, interactive=False)
        assert "No file results are in Postgres" in _flat(capsys.readouterr().out)


class TestCheckBeforeDestroy:
    def test_no_database_is_a_noop(self, monkeypatch, capsys):
        from saorsa_deploy import results_guard

        monkeypatch.setattr("saorsa_deploy.registry.get_repository", lambda: None)
        results_guard.check_before_destroy(Console(), "X", "prod-uploaders")
        assert capsys.readouterr().out == ""

    def test_unknown_deployment_is_a_noop(self, env, monkeypatch, capsys):
        from saorsa_deploy import results_guard

        repo, pool_repo, _, _ = env
        monkeypatch.setattr("saorsa_deploy.registry.get_repository", lambda: repo)
        monkeypatch.setattr(
            "saorsa_deploy.registry.get_file_pool_repository", lambda: pool_repo
        )
        results_guard.check_before_destroy(Console(), "NOPE", "prod-uploaders")
        assert capsys.readouterr().out == ""

    def test_a_broken_registry_read_never_blocks_teardown(
        self, env, monkeypatch, capsys
    ):
        # A guard that breaks teardown when it cannot read the registry would be
        # worse than the problem it exists to prevent.
        from saorsa_deploy import results_guard

        repo, pool_repo, _, _ = env
        monkeypatch.setattr("saorsa_deploy.registry.get_repository", lambda: repo)
        monkeypatch.setattr(
            "saorsa_deploy.registry.get_file_pool_repository", lambda: pool_repo
        )

        def _boom(*a, **kw):
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr(results_guard, "assess", _boom)
        results_guard.check_before_destroy(
            Console(), "PROD-UL-05", "prod-uploaders"
        )
        assert capsys.readouterr().out == ""
