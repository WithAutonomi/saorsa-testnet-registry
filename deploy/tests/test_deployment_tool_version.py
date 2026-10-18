"""The `saorsa-deploy` version recorded on a deployment row (V2-1626).

antops pins a tool version per job, so the question asked of a run months later
is which version STARTED it. That makes two properties load-bearing, and both
are tested here: the version is written when the row is created, and no later
command moves it -- a resumed `up`, a `record-results`, a re-provision of a
permanent kind all leave the creating version in place.
"""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from saorsa_db.db import Base
from saorsa_db.models import DeploymentStatus
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from saorsa_deploy import registry
from saorsa_deploy.cmd.ls import cmd_ls
from saorsa_deploy.cmd.slack_post import build_slack_blocks, render_payload_text
from saorsa_deploy.gate_report import render_report


@pytest.fixture()
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session)
    session.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def _reset_warned():
    """The warning is once-per-process, so the flag has to be reset per test."""
    registry._TOOL_VERSION_WARNED = False
    yield
    registry._TOOL_VERSION_WARNED = False


# --- creation ---------------------------------------------------------------


def test_created_deployment_records_the_running_tool_version(repo):
    with patch("saorsa_deploy.registry.version", return_value="1.1.0"):
        deployment_id = registry.record_deployment_created(
            "VERIFY-01", {"bootstrap_count": 1}, repo=repo
        )

    assert repo.get(deployment_id).saorsa_deploy_version == "1.1.0"


def test_unreadable_tool_version_stores_null_and_warns_once(repo, caplog):
    from importlib.metadata import PackageNotFoundError

    with caplog.at_level(logging.WARNING, logger="saorsa_deploy.registry"):
        with patch(
            "saorsa_deploy.registry.version",
            side_effect=PackageNotFoundError("saorsa-deploy"),
        ):
            first = registry.record_deployment_created("VERIFY-01", {}, repo=repo)
            second = registry.record_deployment_created("VERIFY-02", {}, repo=repo)

    assert repo.get(first).saorsa_deploy_version is None
    assert repo.get(second).saorsa_deploy_version is None
    warnings = [r for r in caplog.records if "saorsa-deploy version" in r.message]
    assert len(warnings) == 1


# --- a later run never moves it ---------------------------------------------


def test_recording_against_an_existing_row_leaves_the_version_alone(repo):
    """What a resumed `up` and every recording command do to the column.

    A resume skips the journalled step that created the row, so nothing calls
    `create` a second time; what a later run does call is the event and status
    writers. None of them may touch the version, or the row would name the
    version that last recorded against the run rather than the one that
    started it.
    """
    with patch("saorsa_deploy.registry.version", return_value="1.1.0"):
        deployment_id = registry.record_deployment_created(
            "VERIFY-01", {"bootstrap_count": 1}, repo=repo
        )

    # Everything a resumed run or a later `record-results` goes through, at a
    # tool version that has moved on since the row was created.
    with patch("saorsa_deploy.registry.version", return_value="9.9.9"):
        registry.record_event(
            "VERIFY-01",
            "nodes_provisioned",
            {"vm_count": 3},
            status=DeploymentStatus.RUNNING,
            repo=repo,
        )
    repo.update_deploy_args(deployment_id, {"bootstrap_count": 1, "vm_count": 3})

    assert repo.get(deployment_id).saorsa_deploy_version == "1.1.0"


# --- `ls` -------------------------------------------------------------------


def _ls_args(**overrides) -> SimpleNamespace:
    defaults = {"name": None, "status": None, "active": False, "recent": False}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_ls_renders_the_tool_column(capsys, repo):
    with patch("saorsa_deploy.registry.version", return_value="1.1.0"):
        registry.record_deployment_created("DEV-01", {"bootstrap_count": 3}, repo=repo)
    # A row from before the column existed, or one the web app created.
    repo.create(name="DEV-02", deploy_args={"bootstrap_count": 1})

    with patch("saorsa_deploy.cmd.ls.get_repository", return_value=repo):
        cmd_ls(_ls_args())

    out = capsys.readouterr().out
    assert "Tool" in out
    assert "1.1.0" in out
    # The null row renders as a dash rather than an empty cell.
    dev_02 = next(line for line in out.splitlines() if "DEV-02" in line)
    assert " - " in dev_02


# --- the single-deployment summary (`deployments print`) --------------------


def _summary_text(deployment) -> str:
    return render_payload_text(build_slack_blocks(deployment, None, None))


def test_deployment_summary_prints_the_tool_version(repo):
    with patch("saorsa_deploy.registry.version", return_value="1.1.0"):
        deployment_id = registry.record_deployment_created(
            "DEV-01", {"kind": "testnet", "bootstrap_count": 1}, repo=repo
        )

    assert "*saorsa-deploy:* `1.1.0`" in _summary_text(repo.get(deployment_id))


def test_deployment_summary_names_a_missing_tool_version(repo):
    deployment = repo.create(name="DEV-01", deploy_args={"kind": "testnet"})

    assert "*saorsa-deploy:* _(not recorded)_" in _summary_text(deployment)


# --- the `compare` header ---------------------------------------------------


def _verdict(candidate_version, baseline_version, new_baseline=False) -> dict:
    return {
        "schema": 1,
        "overall": "PASS",
        "threshold_config_version": "2026-09-01",
        "outbound_ratio_metric": "outbound_bytes_per_gb",
        "window_label": "steady-state",
        "new_baseline": new_baseline,
        "candidate": {
            "deployment_id": 700,
            "name": "STG-01",
            "window_start": None,
            "window_end": None,
            "activity_coverage": None,
            "saorsa_deploy_version": candidate_version,
        },
        "baseline": {
            "deployment_id": None if new_baseline else 667,
            "name": None if new_baseline else "STG-01",
            "window_start": None,
            "window_end": None,
            "activity_coverage": None,
            "saorsa_deploy_version": baseline_version,
        },
        "warnings": [],
        "counts": {"PASS": 1, "FAIL": 0, "GRAY": 0, "NOT ASSESSED": 0},
        "gates": [],
    }


def test_compare_payload_carries_both_runs_tool_versions():
    """The real engine, so the field cannot be dropped between the two layers."""
    from datetime import datetime, timedelta, timezone

    from saorsa_deploy.gate_comparison import RunMetrics, compare_runs
    from saorsa_deploy.gate_config import load_gate_config

    t0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)

    def _run(deployment_id, tool_version):
        return RunMetrics(
            deployment_id=deployment_id,
            name="STG-01",
            window_label="steady-state",
            window_start=t0,
            window_end=t0 + timedelta(hours=3),
            saorsa_deploy_version=tool_version,
        )

    payload = compare_runs(_run(700, "1.1.0"), _run(667, "1.0.0"), load_gate_config())

    assert payload["candidate"]["saorsa_deploy_version"] == "1.1.0"
    assert payload["baseline"]["saorsa_deploy_version"] == "1.0.0"
    assert "- Candidate saorsa-deploy: `1.1.0`" in render_report(payload)


def test_compare_report_header_names_both_tool_versions():
    report = render_report(_verdict("1.1.0", "1.0.0"))

    assert "- Candidate saorsa-deploy: `1.1.0`" in report
    assert "- Baseline saorsa-deploy: `1.0.0`" in report


def test_compare_report_header_names_an_unrecorded_tool_version():
    report = render_report(_verdict("1.1.0", None))

    assert "- Candidate saorsa-deploy: `1.1.0`" in report
    assert "- Baseline saorsa-deploy: not recorded" in report


def test_compare_report_header_omits_the_baseline_on_a_new_baseline():
    report = render_report(_verdict("1.1.0", None, new_baseline=True))

    assert "- Candidate saorsa-deploy: `1.1.0`" in report
    assert "Baseline saorsa-deploy" not in report
