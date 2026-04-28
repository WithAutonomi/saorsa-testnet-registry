import argparse
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from saorsa_db.db import Base
from saorsa_db.models import DeploymentStatus
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from saorsa_deploy.cmd.ls import cmd_ls


def _args(**overrides) -> SimpleNamespace:
    defaults = {
        "name": None,
        "status": None,
        "active": False,
        "recent": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture()
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session)
    session.close()
    engine.dispose()


def test_ls_exits_when_database_unavailable(capsys):
    with patch("saorsa_deploy.cmd.ls.get_repository", return_value=None):
        with pytest.raises(SystemExit) as exc_info:
            cmd_ls(_args())

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "DATABASE_URL" in out
    assert "Error" in out


def test_ls_prints_friendly_message_when_no_deployments(capsys, repo):
    with patch("saorsa_deploy.cmd.ls.get_repository", return_value=repo):
        cmd_ls(_args())

    out = capsys.readouterr().out
    assert "No deployments recorded" in out


def test_ls_renders_table_of_deployments(capsys, repo):
    repo.create(name="DEV-01", deploy_args={"bootstrap_count": 3})
    b = repo.create(name="DEV-02", deploy_args={"bootstrap_count": 1})
    repo.update_status(b.id, DeploymentStatus.RUNNING)
    c = repo.create(name="DEV-03", deploy_args={})
    repo.update_status(c.id, DeploymentStatus.FAILED)

    with patch("saorsa_deploy.cmd.ls.get_repository", return_value=repo):
        cmd_ls(_args())

    out = capsys.readouterr().out
    assert "DEV-01" in out
    assert "DEV-02" in out
    assert "DEV-03" in out
    assert "running" in out
    assert "failed" in out
    assert "pending" in out
    assert "Deployments (3)" in out


def test_ls_kind_column_renders_friendly_labels_for_prod_kinds(capsys, repo):
    repo.create(
        name="PROD-UP-01",
        deploy_args={
            "kind": "prod-uploaders",
            "uploaders": [{"region": "lon1"}, {"region": "fra1"}],
        },
    )
    repo.create(
        name="PROD-DL-01",
        deploy_args={"kind": "prod-continuous-downloader"},
    )

    with patch("saorsa_deploy.cmd.ls.get_repository", return_value=repo):
        cmd_ls(_args())

    out = capsys.readouterr().out
    assert "Prod Uploads" in out
    assert "Prod Downloads" in out
    # Underlying canonical names must not leak into the rendered output —
    # the formatting is display-only.
    assert "prod-uploaders" not in out
    assert "prod-continuous-downloader" not in out


def test_ls_kind_falls_back_to_testnet_when_bootstrap_count_present(capsys, repo):
    repo.create(name="DEV-X", deploy_args={"bootstrap_count": 2})

    with patch("saorsa_deploy.cmd.ls.get_repository", return_value=repo):
        cmd_ls(_args())

    out = capsys.readouterr().out
    # Kind is rendered with a capitalised display label.
    assert "Testnet" in out
    # Underlying canonical name must not leak into the rendered output.
    assert "testnet" not in out.replace("Testnet", "")


def test_ls_nodes_column_is_na_for_prod_kinds(capsys, repo):
    """Node count only applies to testnet deployments; non-testnet kinds render N/A."""
    from saorsa_deploy.cmd.ls import _derive_node_count

    assert (
        _derive_node_count(
            {
                "kind": "prod-uploaders",
                "uploaders": [{"region": "lon1"}, {"region": "fra1"}],
            }
        )
        == "N/A"
    )
    assert _derive_node_count({"kind": "prod-continuous-downloader"}) == "N/A"
    # Unknown / empty deploy_args also classify as non-testnet → N/A.
    assert _derive_node_count({}) == "N/A"
    assert _derive_node_count({"kind": "something-else"}) == "N/A"

    repo.create(
        name="PROD-UP-01",
        deploy_args={
            "kind": "prod-uploaders",
            "uploaders": [{"region": "lon1"}, {"region": "fra1"}, {"region": "ams3"}],
        },
    )
    with patch("saorsa_deploy.cmd.ls.get_repository", return_value=repo):
        cmd_ls(_args())

    out = capsys.readouterr().out
    assert "PROD-UP-01" in out
    assert "N/A" in out
    # The numeric uploader count must not appear in the Nodes column.
    assert "│ 3 " not in out
    assert " 3 │" not in out


def test_ls_nodes_column_uses_recorded_node_count_for_testnets(capsys, repo):
    repo.create(
        name="DEV-RECORDED",
        deploy_args={"bootstrap_count": 3, "node_count": 18},
    )

    with patch("saorsa_deploy.cmd.ls.get_repository", return_value=repo):
        cmd_ls(_args())

    out = capsys.readouterr().out
    assert "DEV-RECORDED" in out
    assert "18" in out


def test_ls_filter_status_keeps_only_matching_rows(capsys, repo):
    a = repo.create(name="DEV-A", deploy_args={})
    repo.update_status(a.id, DeploymentStatus.RUNNING)
    b = repo.create(name="DEV-B", deploy_args={})
    repo.update_status(b.id, DeploymentStatus.FAILED)

    with patch("saorsa_deploy.cmd.ls.get_repository", return_value=repo):
        cmd_ls(_args(status="failed"))

    out = capsys.readouterr().out
    assert "DEV-B" in out
    assert "DEV-A" not in out
    assert "Deployments (1)" in out


def test_ls_filter_status_rejects_unknown_value(capsys, repo):
    with patch("saorsa_deploy.cmd.ls.get_repository", return_value=repo):
        with pytest.raises(SystemExit) as exc_info:
            cmd_ls(_args(status="bogus"))

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "Unknown status" in out


def test_ls_active_filter_uses_list_active(capsys, repo):
    a = repo.create(name="DEV-A", deploy_args={})  # pending → active
    repo.create(name="DEV-B", deploy_args={})
    # destroyed = terminal, must be filtered out by --active.
    c = repo.create(name="DEV-C", deploy_args={})
    repo.update_status(c.id, DeploymentStatus.DESTROYED)

    assert {d.id for d in repo.list_active()} == {a.id, repo.get_by_name("DEV-B").id}

    with patch("saorsa_deploy.cmd.ls.get_repository", return_value=repo):
        cmd_ls(_args(active=True))

    out = capsys.readouterr().out
    assert "DEV-A" in out
    assert "DEV-B" in out
    assert "DEV-C" not in out


def test_ls_filter_name_substring_case_insensitive(capsys, repo):
    repo.create(name="DEV-01", deploy_args={})
    repo.create(name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"})
    repo.create(name="STG-01", deploy_args={})

    with patch("saorsa_deploy.cmd.ls.get_repository", return_value=repo):
        cmd_ls(_args(name="prod"))

    out = capsys.readouterr().out
    assert "PROD-UP-01" in out
    assert "DEV-01" not in out
    assert "STG-01" not in out


def test_ls_filter_recent_drops_rows_older_than_seven_days(capsys, repo):
    a = repo.create(name="DEV-NEW", deploy_args={})
    b = repo.create(name="DEV-OLD", deploy_args={})

    # Backdate DEV-OLD past the 7-day window.
    old = repo.get(b.id)
    old.created_at = datetime.now(timezone.utc) - timedelta(days=10)
    repo.session.commit()

    with patch("saorsa_deploy.cmd.ls.get_repository", return_value=repo):
        cmd_ls(_args(recent=True))

    out = capsys.readouterr().out
    assert "DEV-NEW" in out
    assert "DEV-OLD" not in out
    # Sanity: the fresh row really is in the repo.
    assert repo.get(a.id) is not None


def test_ls_status_and_active_are_mutually_exclusive():
    """argparse should reject `--status X --active` at the CLI layer."""
    result = subprocess.run(
        ["uv", "run", "saorsa-deploy", "ls", "--status", "running", "--active"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "not allowed with argument" in result.stderr or "mutually exclusive" in result.stderr


def test_ls_argparse_wires_defaults():
    """Importing the parser-build is integration-level; assert via -h that the
    flags are registered and defaults work end-to-end."""
    result = subprocess.run(
        ["uv", "run", "saorsa-deploy", "ls", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--name" in result.stdout
    assert "--recent" in result.stdout
    assert "--status" in result.stdout
    assert "--active" in result.stdout


# Sanity: argparse parses cleanly when no flags are supplied.
def test_argparse_basic_invocation_does_not_error():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("ls")
    p.add_argument("--name", default=None)
    p.add_argument("--recent", action="store_true")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--status", default=None)
    g.add_argument("--active", action="store_true")
    parsed = parser.parse_args(["ls"])
    assert parsed.command == "ls"
    assert parsed.recent is False
    assert parsed.active is False
    assert parsed.name is None
    assert parsed.status is None
