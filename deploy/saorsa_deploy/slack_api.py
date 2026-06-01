"""Thin Slack Web API client for posting deployment messages and replies.

Unlike a Slack *incoming webhook* (which returns only the body ``"ok"`` and
gives no handle on the message it created), the Web API ``chat.postMessage``
endpoint returns the ``channel`` and message ``ts`` of the post. We persist
that pair (see ``Deployment.slack_channel`` / ``slack_message_ts``) so a later
``deployments record-results --update-slack`` can post a results summary as a
*threaded reply* by re-calling ``chat.postMessage`` with ``thread_ts`` set.

The bot token is read from ``ANT_DEPLOY_SLACK_BOT_TOKEN`` by callers and passed
in here; this module never touches the environment so it stays easy to test.
Posting is opt-in: when no bot token is configured, callers fall back to the
legacy incoming-webhook path and threading is simply unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
_DEFAULT_TIMEOUT = 10


class SlackApiError(RuntimeError):
    """Raised when the Slack Web API rejects a request.

    ``error`` is Slack's machine-readable error code (e.g. ``channel_not_found``,
    ``invalid_auth``) when the API responded with ``{"ok": false, ...}``; it is
    ``None`` for transport/HTTP failures.
    """

    def __init__(self, message: str, *, error: str | None = None):
        super().__init__(message)
        self.error = error


@dataclass(frozen=True)
class SlackMessageRef:
    """The ``(channel, ts)`` identifying a posted message — the handle needed
    to thread replies to it."""

    channel: str
    ts: str


def _post(token: str, payload: dict) -> SlackMessageRef:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    try:
        response = requests.post(
            _POST_MESSAGE_URL, json=payload, headers=headers, timeout=_DEFAULT_TIMEOUT
        )
    except requests.RequestException as e:
        raise SlackApiError(f"Slack request failed: {e}") from e

    if not response.ok:
        raise SlackApiError(
            f"Slack returned HTTP {response.status_code}: {response.text}"
        )

    try:
        body = response.json()
    except ValueError as e:
        raise SlackApiError(f"Slack returned a non-JSON response: {e}") from e

    if not body.get("ok"):
        err = body.get("error", "unknown_error")
        raise SlackApiError(f"Slack rejected the request: {err}", error=err)

    # chat.postMessage echoes the channel id it actually posted to (which may
    # differ from a channel *name* the caller supplied) plus the message ts.
    channel = body.get("channel") or payload.get("channel") or ""
    ts = body.get("ts") or ""
    if not ts:
        raise SlackApiError("Slack response did not include a message ts")
    return SlackMessageRef(channel=channel, ts=ts)


def post_message(
    token: str, channel: str, text: str, blocks: list[dict] | None = None
) -> SlackMessageRef:
    """Post a new top-level message via ``chat.postMessage``.

    Returns the ``SlackMessageRef`` (channel + ts) of the created message so the
    caller can persist it for later threading. Raises ``SlackApiError`` on any
    transport, HTTP, or API-level failure.
    """
    payload: dict = {"channel": channel, "text": text}
    if blocks is not None:
        payload["blocks"] = blocks
    return _post(token, payload)


def post_reply(
    token: str,
    channel: str,
    thread_ts: str,
    text: str,
    blocks: list[dict] | None = None,
) -> SlackMessageRef:
    """Post a threaded reply to the message identified by ``thread_ts`` in
    ``channel``. Same return/raise contract as :func:`post_message`."""
    payload: dict = {"channel": channel, "thread_ts": thread_ts, "text": text}
    if blocks is not None:
        payload["blocks"] = blocks
    return _post(token, payload)
