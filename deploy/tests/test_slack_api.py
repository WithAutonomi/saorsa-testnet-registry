"""Unit tests for the Slack Web API client (saorsa_deploy.slack_api)."""
from unittest.mock import MagicMock, patch

import pytest
import requests

from saorsa_deploy.slack_api import (
    SlackApiError,
    SlackMessageRef,
    post_message,
    post_reply,
)


def _resp(ok=True, status=200, body=None):
    r = MagicMock()
    r.ok = ok
    r.status_code = status
    r.json.return_value = {} if body is None else body
    r.text = str(body)
    return r


def test_post_message_returns_channel_and_ts():
    body = {"ok": True, "channel": "C123", "ts": "1700000000.000100"}
    with patch(
        "saorsa_deploy.slack_api.requests.post", return_value=_resp(body=body)
    ) as m:
        ref = post_message("tok", "C123", "hello", blocks=[{"type": "section"}])

    assert ref == SlackMessageRef(channel="C123", ts="1700000000.000100")
    kwargs = m.call_args.kwargs
    assert kwargs["json"]["channel"] == "C123"
    assert kwargs["json"]["text"] == "hello"
    assert kwargs["json"]["blocks"] == [{"type": "section"}]
    assert kwargs["headers"]["Authorization"] == "Bearer tok"


def test_post_reply_sets_thread_ts():
    body = {"ok": True, "channel": "C123", "ts": "1700000000.000200"}
    with patch(
        "saorsa_deploy.slack_api.requests.post", return_value=_resp(body=body)
    ) as m:
        ref = post_reply("tok", "C123", "1700000000.000100", "reply", blocks=[])

    assert ref.ts == "1700000000.000200"
    assert m.call_args.kwargs["json"]["thread_ts"] == "1700000000.000100"


def test_api_level_error_raises_with_code():
    body = {"ok": False, "error": "channel_not_found"}
    with patch(
        "saorsa_deploy.slack_api.requests.post", return_value=_resp(body=body)
    ):
        with pytest.raises(SlackApiError) as ei:
            post_message("tok", "nope", "hi")
    assert ei.value.error == "channel_not_found"


def test_http_error_raises():
    with patch(
        "saorsa_deploy.slack_api.requests.post",
        return_value=_resp(ok=False, status=500, body={}),
    ):
        with pytest.raises(SlackApiError):
            post_message("tok", "C123", "hi")


def test_transport_error_raises():
    with patch(
        "saorsa_deploy.slack_api.requests.post",
        side_effect=requests.RequestException("boom"),
    ):
        with pytest.raises(SlackApiError):
            post_message("tok", "C123", "hi")


def test_missing_ts_in_ok_response_raises():
    with patch(
        "saorsa_deploy.slack_api.requests.post",
        return_value=_resp(body={"ok": True, "channel": "C123"}),
    ):
        with pytest.raises(SlackApiError):
            post_message("tok", "C123", "hi")
