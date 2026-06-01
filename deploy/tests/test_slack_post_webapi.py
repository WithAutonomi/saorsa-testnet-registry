"""Tests for the Web API posting path of `deployments slack-post`.

When ANT_DEPLOY_SLACK_BOT_TOKEN is set, the command posts via the Web API and
persists the returned (channel, ts) on the deployment so a later
record-results can thread onto it. Without the token it falls back to the
incoming webhook and records nothing.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from saorsa_db.db import Base
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from saorsa_deploy.cmd.slack_post import cmd_slack_post
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


def _args(name="PROD-UP-01", channel=None, dry_run=False):
    return SimpleNamespace(name=name, channel=channel, dry_run=dry_run)


def _run(repo, args):
    """Run cmd_slack_post with S3 state loaders stubbed out (offline)."""
    with (
        patch("saorsa_deploy.cmd.slack_post.get_repository", return_value=repo),
        patch("saorsa_deploy.cmd.slack_post._try_load_state", return_value=None),
        patch(
            "saorsa_deploy.cmd.slack_post._try_load_client_state",
            return_value=None,
        ),
        patch(
            "saorsa_deploy.cmd.slack_post._try_load_monitoring_state",
            return_value=None,
        ),
        patch(
            "saorsa_deploy.cmd.slack_post._try_load_elastic_state",
            return_value=None,
        ),
    ):
        cmd_slack_post(args)


def test_web_api_path_persists_channel_and_ts(repo, monkeypatch):
    repo.create(name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"})
    monkeypatch.setenv("ANT_DEPLOY_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("ANT_DEPLOY_SLACK_CHANNEL", "C999")

    ref = SlackMessageRef(channel="C999", ts="1700000000.000100")
    with patch(
        "saorsa_deploy.cmd.slack_post.post_message", return_value=ref
    ) as m:
        _run(repo, _args())

    m.assert_called_once()
    assert m.call_args.args[0] == "xoxb-test"
    assert m.call_args.args[1] == "C999"

    stored = repo.get_by_name("PROD-UP-01")
    assert stored.slack_channel == "C999"
    assert stored.slack_message_ts == "1700000000.000100"


def test_channel_flag_overrides_env(repo, monkeypatch):
    repo.create(name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"})
    monkeypatch.setenv("ANT_DEPLOY_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("ANT_DEPLOY_SLACK_CHANNEL", "C-env")

    ref = SlackMessageRef(channel="C-flag", ts="1.1")
    with patch(
        "saorsa_deploy.cmd.slack_post.post_message", return_value=ref
    ) as m:
        _run(repo, _args(channel="C-flag"))

    assert m.call_args.args[1] == "C-flag"


def test_bot_token_without_channel_errors(repo, monkeypatch):
    repo.create(name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"})
    monkeypatch.setenv("ANT_DEPLOY_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("ANT_DEPLOY_SLACK_CHANNEL", raising=False)

    with pytest.raises(SystemExit):
        _run(repo, _args())


def test_web_api_failure_exits(repo, monkeypatch):
    repo.create(name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"})
    monkeypatch.setenv("ANT_DEPLOY_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("ANT_DEPLOY_SLACK_CHANNEL", "C999")

    with patch(
        "saorsa_deploy.cmd.slack_post.post_message",
        side_effect=SlackApiError("invalid_auth", error="invalid_auth"),
    ):
        with pytest.raises(SystemExit):
            _run(repo, _args())

    stored = repo.get_by_name("PROD-UP-01")
    assert stored.slack_channel is None


def test_webhook_path_used_when_no_bot_token(repo, monkeypatch):
    repo.create(name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"})
    monkeypatch.delenv("ANT_DEPLOY_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ANT_DEPLOY_SLACK_WEBHOOK_URL", "https://hooks.example/X")

    response = MagicMock(ok=True, status_code=200)
    with patch(
        "saorsa_deploy.cmd.slack_post.requests.post", return_value=response
    ) as m, patch("saorsa_deploy.cmd.slack_post.post_message") as web:
        _run(repo, _args())

    m.assert_called_once()  # webhook POST
    web.assert_not_called()  # Web API not used
    stored = repo.get_by_name("PROD-UP-01")
    assert stored.slack_channel is None  # nothing persisted on webhook path


def test_no_token_and_no_webhook_errors(repo, monkeypatch):
    repo.create(name="PROD-UP-01", deploy_args={"kind": "prod-uploaders"})
    monkeypatch.delenv("ANT_DEPLOY_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANT_DEPLOY_SLACK_WEBHOOK_URL", raising=False)

    with pytest.raises(SystemExit):
        _run(repo, _args())
