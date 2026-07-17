"""Tests for the Contabo REST client's auth handling."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy import contabo_api
from saorsa_deploy.contabo_api import ContaboClient, ContaboError


def _resp(status, body=None, text=None):
    return SimpleNamespace(
        status_code=status,
        text=text if text is not None else ("" if body is None else "{}"),
        json=lambda: (body or {}),
    )


def test_request_refreshes_token_and_retries_on_401():
    # A cached token expires mid-run: the first call 401s, the client must
    # refresh the token and retry once, transparently returning the retry's body.
    client = ContaboClient(console=MagicMock())
    responses = [_resp(401, text="Unauthorized"), _resp(200, {"data": {"ok": 1}})]
    with (
        patch.object(contabo_api, "_get_token", side_effect=["tok1", "tok2"]) as gt,
        patch.object(contabo_api.requests, "request", side_effect=responses) as req,
    ):
        out = client._request("GET", "/v1/compute/instances/123")

    assert out == {"data": {"ok": 1}}
    assert req.call_count == 2                       # original + one retry
    assert gt.call_count == 2                        # token refreshed once
    # The retry carried the refreshed bearer token.
    assert req.call_args_list[1].kwargs["headers"]["Authorization"] == "Bearer tok2"


def test_request_raises_if_401_persists_after_refresh():
    client = ContaboClient(console=MagicMock())
    with (
        patch.object(contabo_api, "_get_token", side_effect=["tok1", "tok2"]),
        patch.object(contabo_api.requests, "request", return_value=_resp(401, text="Unauthorized")),
        pytest.raises(ContaboError, match="401"),
    ):
        client._request("GET", "/v1/compute/instances")


def test_request_does_not_refresh_on_success():
    client = ContaboClient(console=MagicMock())
    with (
        patch.object(contabo_api, "_get_token", side_effect=["tok1"]) as gt,
        patch.object(contabo_api.requests, "request", return_value=_resp(200, {"data": []})) as req,
    ):
        client._request("GET", "/v1/secrets")
    assert req.call_count == 1
    assert gt.call_count == 1
