from unittest.mock import Mock, patch

import pytest
import requests

from saorsa_deploy.prod_bootstraps import (
    PROD_BOOTSTRAPS_URL,
    ProdBootstrapsError,
    fetch_prod_bootstraps,
)

_VALID_TOML = """\
peers = [
    "207.148.94.42:10000",
    "45.77.50.10:10000",
]
"""


def _mock_response(text: str, status: int = 200) -> Mock:
    resp = Mock(spec=requests.Response)
    resp.text = text
    resp.status_code = status

    def _raise_for_status():
        if status >= 400:
            raise requests.HTTPError(f"{status} error")

    resp.raise_for_status = _raise_for_status
    return resp


def test_fetch_prod_bootstraps_success_returns_list_of_peers():
    with patch("saorsa_deploy.prod_bootstraps.requests.get") as mock_get:
        mock_get.return_value = _mock_response(_VALID_TOML)
        peers = fetch_prod_bootstraps()
    assert peers == ["207.148.94.42:10000", "45.77.50.10:10000"]


def test_fetch_prod_bootstraps_uses_default_url_when_none_supplied():
    with patch("saorsa_deploy.prod_bootstraps.requests.get") as mock_get:
        mock_get.return_value = _mock_response(_VALID_TOML)
        fetch_prod_bootstraps()
    call_url = mock_get.call_args.args[0]
    assert call_url == PROD_BOOTSTRAPS_URL


def test_fetch_prod_bootstraps_http_error_raises_wrapped_exception():
    with patch("saorsa_deploy.prod_bootstraps.requests.get") as mock_get:
        mock_get.side_effect = requests.ConnectionError("boom")
        with pytest.raises(ProdBootstrapsError, match="Failed to fetch"):
            fetch_prod_bootstraps()


def test_fetch_prod_bootstraps_http_status_raises_wrapped_exception():
    with patch("saorsa_deploy.prod_bootstraps.requests.get") as mock_get:
        mock_get.return_value = _mock_response("not found", status=404)
        with pytest.raises(ProdBootstrapsError, match="Failed to fetch"):
            fetch_prod_bootstraps()


def test_fetch_prod_bootstraps_malformed_toml_raises():
    with patch("saorsa_deploy.prod_bootstraps.requests.get") as mock_get:
        mock_get.return_value = _mock_response("peers = [this is not valid")
        with pytest.raises(ProdBootstrapsError, match="malformed"):
            fetch_prod_bootstraps()


def test_fetch_prod_bootstraps_missing_peers_key_raises():
    with patch("saorsa_deploy.prod_bootstraps.requests.get") as mock_get:
        mock_get.return_value = _mock_response('other_key = ["a", "b"]')
        with pytest.raises(ProdBootstrapsError, match="non-empty 'peers'"):
            fetch_prod_bootstraps()


def test_fetch_prod_bootstraps_empty_peers_array_raises():
    with patch("saorsa_deploy.prod_bootstraps.requests.get") as mock_get:
        mock_get.return_value = _mock_response("peers = []")
        with pytest.raises(ProdBootstrapsError, match="non-empty 'peers'"):
            fetch_prod_bootstraps()


def test_fetch_prod_bootstraps_non_string_entries_raise():
    with patch("saorsa_deploy.prod_bootstraps.requests.get") as mock_get:
        mock_get.return_value = _mock_response('peers = ["valid", 42]')
        with pytest.raises(ProdBootstrapsError, match="non-string"):
            fetch_prod_bootstraps()


def test_fetch_prod_bootstraps_accepts_custom_url():
    custom_url = "https://example.test/peers.toml"
    with patch("saorsa_deploy.prod_bootstraps.requests.get") as mock_get:
        mock_get.return_value = _mock_response(_VALID_TOML)
        fetch_prod_bootstraps(url=custom_url)
    assert mock_get.call_args.args[0] == custom_url
