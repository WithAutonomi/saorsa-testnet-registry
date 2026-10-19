"""`clients slack-post` — the extension's up/down post (V2-1639).

Two properties are what the command is for and are asserted here: it threads
under the DEPLOYMENT's recorded message when there is one, and it never fails
the run that calls it. It only ever runs as the best-effort tail of
``clients up`` / ``clients down``, where the cohort is already up (or already
gone) by the time it executes, so every failure path has to exit 0.

The teardown post is the other load-bearing bit: ``clients down`` deletes the
extension's state in the step before it, so the VM summary arrives as an
argument and no S3 read is attempted.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.clients_slack_post import cmd_clients_slack_post
from saorsa_deploy.slack_api import SlackApiError, SlackMessageRef

BROWSER_STATE = {
    "clients": [
        {
            "role": "browser-download",
            "region": "lon1",
            "vm_size": "s-4vcpu-8gb",
            "instances": 4,
            "round_period_secs": 600,
            "download_timeout_secs": 300,
            "client_index": 1,
        },
        {
            "role": "browser-download",
            "region": "nyc1",
            "vm_size": "s-4vcpu-8gb",
            "instances": 4,
            "round_period_secs": 600,
            "download_timeout_secs": 300,
            "client_index": 2,
        },
    ]
}


@pytest.fixture(autouse=True)
def _no_real_slack(monkeypatch):
    """A developer with the Slack vars exported must not post from pytest."""
    for var in (
        "ANT_DEPLOY_SLACK_BOT_TOKEN",
        "ANT_DEPLOY_SLACK_CHANNEL",
        "ANT_DEPLOY_SLACK_WEBHOOK_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def _args(**kwargs):
    base = {
        "name": "DEV-01",
        "extension": "DEV-01-browsers",
        "event": "up",
        "vms": None,
        "channel": None,
        "dry_run": False,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _repo(channel="C0AV86MEACQ", ts="1792422558.818619"):
    deployment = SimpleNamespace(
        id=684,
        name="DEV-01",
        slack_channel=channel,
        slack_message_ts=ts,
    )
    return SimpleNamespace(
        get_active_by_name=lambda name: deployment,
        get_by_name=lambda name: deployment,
    )


def _patches(repo, *, state=BROWSER_STATE, hot_files=None):
    return (
        patch("saorsa_deploy.cmd.clients_slack_post.get_repository", return_value=repo),
        patch("saorsa_deploy.cmd.clients_slack_post.load_client_state", return_value=state),
        patch("saorsa_deploy.hot_files.load_hot_files", return_value=hot_files),
    )


def test_up_threads_under_the_deployments_own_post(monkeypatch):
    monkeypatch.setenv("ANT_DEPLOY_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("ANT_DEPLOY_SLACK_CHANNEL", "C-ENV")
    reply, message = (
        patch(
            "saorsa_deploy.cmd.clients_slack_post.post_reply",
            return_value=SlackMessageRef("C0AV86MEACQ", "1.2"),
        ),
        patch("saorsa_deploy.cmd.clients_slack_post.post_message"),
    )
    repo_p, state_p, hot_p = _patches(_repo())
    with repo_p, state_p, hot_p, reply as mock_reply, message as mock_message:
        cmd_clients_slack_post(_args())

    mock_message.assert_not_called()
    args, kwargs = mock_reply.call_args
    assert args[0] == "xoxb-test"
    assert args[1] == "C0AV86MEACQ"
    assert args[2] == "1792422558.818619"
    assert "DEV-01-browsers" in kwargs["text"]


def test_up_without_a_recorded_reference_posts_top_level(monkeypatch):
    monkeypatch.setenv("ANT_DEPLOY_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("ANT_DEPLOY_SLACK_CHANNEL", "C-ENV")
    repo_p, state_p, hot_p = _patches(_repo(channel=None, ts=None))
    with (
        repo_p,
        state_p,
        hot_p,
        patch("saorsa_deploy.cmd.clients_slack_post.post_reply") as mock_reply,
        patch(
            "saorsa_deploy.cmd.clients_slack_post.post_message",
            return_value=SlackMessageRef("C-ENV", "1.2"),
        ) as mock_message,
    ):
        cmd_clients_slack_post(_args())

    mock_reply.assert_not_called()
    assert mock_message.call_args[0][1] == "C-ENV"


def test_no_database_url_exits_zero_and_posts_nothing(monkeypatch):
    monkeypatch.setenv("ANT_DEPLOY_SLACK_BOT_TOKEN", "xoxb-test")
    repo_p, state_p, hot_p = _patches(None)
    with (
        repo_p,
        state_p,
        hot_p,
        patch("saorsa_deploy.cmd.clients_slack_post.post_reply") as mock_reply,
        patch("saorsa_deploy.cmd.clients_slack_post.post_message") as mock_message,
    ):
        # No SystemExit at all: a finished `clients up` must not be failed by
        # the registry being out of reach.
        cmd_clients_slack_post(_args())

    mock_reply.assert_not_called()
    mock_message.assert_not_called()


def test_a_slack_api_error_exits_zero(monkeypatch):
    monkeypatch.setenv("ANT_DEPLOY_SLACK_BOT_TOKEN", "xoxb-test")
    repo_p, state_p, hot_p = _patches(_repo())
    with (
        repo_p,
        state_p,
        hot_p,
        patch(
            "saorsa_deploy.cmd.clients_slack_post.post_reply",
            side_effect=SlackApiError("Slack rejected the request: channel_not_found"),
        ),
    ):
        cmd_clients_slack_post(_args())


def test_down_reads_no_state_and_renders_the_summary_verbatim(monkeypatch):
    monkeypatch.setenv("ANT_DEPLOY_SLACK_BOT_TOKEN", "xoxb-test")
    summary = "35 VMs — browser-download: lon1 ×9, nyc1 ×9, sgp1 ×9, syd1 ×8"
    repo_p, state_p, hot_p = _patches(_repo())
    with (
        repo_p,
        state_p as mock_state,
        hot_p,
        patch(
            "saorsa_deploy.cmd.clients_slack_post.post_reply",
            return_value=SlackMessageRef("C0AV86MEACQ", "1.2"),
        ) as mock_reply,
    ):
        cmd_clients_slack_post(_args(event="down", vms=summary))

    # The state is gone by the time this runs; reading it would be a RuntimeError.
    mock_state.assert_not_called()
    blocks = mock_reply.call_args.kwargs["blocks"]
    assert any(f"*Was:* {summary}" == b["text"]["text"] for b in blocks)


def test_vms_with_event_up_is_refused():
    repo_p, state_p, hot_p = _patches(_repo())
    with repo_p, state_p, hot_p, pytest.raises(SystemExit) as excinfo:
        cmd_clients_slack_post(_args(vms="2 VMs — download: lon1 ×2"))
    assert excinfo.value.code == 2


def test_dry_run_renders_the_cohort(capsys):
    repo_p, state_p, hot_p = _patches(
        _repo(),
        hot_files={
            "files": [{"index": 0}, {"index": 1}, {"index": 2}],
            "size_bytes": 52428800,
            "selected_at": "2026-10-19T15:40:00+00:00",
        },
    )
    with (
        repo_p,
        state_p,
        hot_p,
        patch("saorsa_deploy.cmd.clients_slack_post.post_reply") as mock_reply,
        patch("saorsa_deploy.cmd.clients_slack_post.post_message") as mock_message,
    ):
        cmd_clients_slack_post(_args(dry_run=True))

    out = capsys.readouterr().out
    assert "4 Chromium, round 600s, deadline 300s" in out
    assert "Browser (WebRTC-Direct" in out
    assert "*Hot files:* 3 × 50 MiB, selected 2026-10-19T15:40:00+00:00" in out
    mock_reply.assert_not_called()
    mock_message.assert_not_called()
