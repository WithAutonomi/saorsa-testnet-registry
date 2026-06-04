from unittest.mock import Mock, patch

import pytest
import requests

from saorsa_deploy.prod_bootstraps import (
    PROD_BOOTSTRAPS_URL,
    ProdBootstrapsError,
    fetch_prod_bootstraps,
    render_bootstrap_peers_toml,
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


def test_render_bootstrap_peers_toml_round_trips_through_loader():
    import tomllib

    peers = ["207.148.94.42:10000", "45.77.50.10:10000"]
    rendered = render_bootstrap_peers_toml(peers)
    assert tomllib.loads(rendered) == {"peers": peers}


def test_render_bootstrap_peers_toml_empty_list_renders_empty_array():
    import tomllib

    rendered = render_bootstrap_peers_toml([])
    assert tomllib.loads(rendered) == {"peers": []}


def test_render_bootstrap_peers_toml_ends_with_newline():
    assert render_bootstrap_peers_toml(["1.2.3.4:10000"]).endswith("]\n")


class TestEnsureLocalBootstrapPeersFile:
    def test_existing_file_is_never_overwritten(self, tmp_path):
        from saorsa_deploy.prod_bootstraps import ensure_local_bootstrap_peers_file

        peers_file = tmp_path / "bootstrap_peers.toml"
        peers_file.write_text('peers = ["9.9.9.9:10000"]\n')
        with patch("saorsa_deploy.prod_bootstraps.requests.get") as mock_get:
            path, created = ensure_local_bootstrap_peers_file(peers_file)
        assert path == peers_file
        assert created is False
        mock_get.assert_not_called()
        assert peers_file.read_text() == 'peers = ["9.9.9.9:10000"]\n'

    def test_missing_file_created_from_fetched_list(self, tmp_path):
        import tomllib

        from saorsa_deploy.prod_bootstraps import ensure_local_bootstrap_peers_file

        peers_file = tmp_path / "ant" / "bootstrap_peers.toml"
        with patch("saorsa_deploy.prod_bootstraps.requests.get") as mock_get:
            mock_get.return_value = _mock_response(_VALID_TOML)
            path, created = ensure_local_bootstrap_peers_file(peers_file)
        assert path == peers_file
        assert created is True
        assert tomllib.loads(peers_file.read_text()) == {
            "peers": ["207.148.94.42:10000", "45.77.50.10:10000"]
        }

    def test_missing_file_and_failed_fetch_raises(self, tmp_path):
        from saorsa_deploy.prod_bootstraps import ensure_local_bootstrap_peers_file

        peers_file = tmp_path / "bootstrap_peers.toml"
        with patch("saorsa_deploy.prod_bootstraps.requests.get") as mock_get:
            mock_get.return_value = _mock_response("nope", status=500)
            with pytest.raises(ProdBootstrapsError):
                ensure_local_bootstrap_peers_file(peers_file)
        assert not peers_file.exists()
