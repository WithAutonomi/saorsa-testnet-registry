"""Tests for `deployments record-results --update-slack`.

The flag posts a per-kind results summary as a threaded reply to the
deployment's original slack-post message. It is best-effort: a missing thread
reference, missing bot token, or Slack error must warn without failing the
recording.
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from saorsa_db.db import Base
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from saorsa_deploy.cmd.record_results import cmd_record_results
from saorsa_deploy.slack_api import SlackApiError, SlackMessageRef


@pytest.fixture()
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session)
    session.close()
    engine.dispose()


def _point(**overrides):
    base = {
        "_time": datetime.fromisoformat("2026-04-26T10:30:00+00:00"),
        "host": "prodhost-01",
        "region": "lon1",
        "status": 0,
        "total_ms": 12345,
        "upload_secs": 3.5,
        "file_size": 1048576,
        "start_ns": 1776700000000000000,
        "chunks_stored": 42,
        "chunks_failed": 0,
        "total_chunks": 42,
        "address": "addr-abc",
        "file_name": "file.bin",
        "source_url": "https://example.com/file.bin",
        "reason": "",
    }
    base.update(overrides)
    return base


def _args(name="PROD-UP-01", update_slack=True):
    return SimpleNamespace(
        name=name,
        monitoring_name=None,
        influx_url="https://influx.example.com",
        influx_token="tok",
        influx_org="ant",
        influx_bucket="testnet-metrics",
        window="-30d",
        update_slack=update_slack,
    )


def _run(repo, points, args):
    with (
        patch(
            "saorsa_deploy.cmd.record_results.get_repository", return_value=repo
        ),
        patch(
            "saorsa_deploy.cmd.record_results.fetch_all_prod_upload_points",
            return_value=points,
        ),
    ):
        cmd_record_results(args)


def test_posts_threaded_reply_when_ref_and_token_present(repo, monkeypatch):
    d = repo.create(name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"})
    repo.set_slack_message(d.id, "C999", "1700000000.000100")
    monkeypatch.setenv("ANT_DEPLOY_SLACK_BOT_TOKEN", "xoxb-test")

    ref = SlackMessageRef(channel="C999", ts="1700000000.000200")
    with patch(
        "saorsa_deploy.cmd.record_results.post_reply", return_value=ref
    ) as m:
        _run(repo, [_point(), _point(status=1, file_name="b.bin",
                                     source_url="u2")], _args())

    m.assert_called_once()
    assert m.call_args.args[0] == "xoxb-test"  # token
    assert m.call_args.args[1] == "C999"  # channel
    assert m.call_args.args[2] == "1700000000.000100"  # thread_ts
    blocks = m.call_args.kwargs["blocks"]
    assert blocks  # header + per-kind detail blocks


def test_skips_when_no_thread_ref(repo, monkeypatch):
    repo.create(name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"})
    monkeypatch.setenv("ANT_DEPLOY_SLACK_BOT_TOKEN", "xoxb-test")

    with patch("saorsa_deploy.cmd.record_results.post_reply") as m:
        _run(repo, [_point()], _args())

    m.assert_not_called()
    # Recording still happened.
    d = repo.get_by_name("PROD-UP-01")
    assert repo.list_prod_upload_results_for_deployment(d.id)


def test_skips_when_no_bot_token(repo, monkeypatch):
    d = repo.create(name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"})
    repo.set_slack_message(d.id, "C999", "1.1")
    monkeypatch.delenv("ANT_DEPLOY_SLACK_BOT_TOKEN", raising=False)

    with patch("saorsa_deploy.cmd.record_results.post_reply") as m:
        _run(repo, [_point()], _args())

    m.assert_not_called()


def test_slack_failure_does_not_fail_recording(repo, monkeypatch):
    d = repo.create(name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"})
    repo.set_slack_message(d.id, "C999", "1.1")
    monkeypatch.setenv("ANT_DEPLOY_SLACK_BOT_TOKEN", "xoxb-test")

    with patch(
        "saorsa_deploy.cmd.record_results.post_reply",
        side_effect=SlackApiError("rate_limited", error="rate_limited"),
    ):
        # Must not raise.
        _run(repo, [_point()], _args())

    assert repo.list_prod_upload_results_for_deployment(d.id)


def test_no_reply_without_flag(repo, monkeypatch):
    d = repo.create(name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"})
    repo.set_slack_message(d.id, "C999", "1.1")
    monkeypatch.setenv("ANT_DEPLOY_SLACK_BOT_TOKEN", "xoxb-test")

    with patch("saorsa_deploy.cmd.record_results.post_reply") as m:
        _run(repo, [_point()], _args(update_slack=False))

    m.assert_not_called()


def test_skips_reply_when_nothing_recorded(repo, monkeypatch):
    d = repo.create(name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"})
    repo.set_slack_message(d.id, "C999", "1.1")
    monkeypatch.setenv("ANT_DEPLOY_SLACK_BOT_TOKEN", "xoxb-test")

    with patch("saorsa_deploy.cmd.record_results.post_reply") as m:
        _run(repo, [], _args())  # no points → nothing recorded

    m.assert_not_called()
