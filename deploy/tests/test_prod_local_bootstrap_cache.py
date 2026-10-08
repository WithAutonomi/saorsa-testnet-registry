"""Tests for the local-uploads/downloads `_resolve_bootstrap_args` helpers."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rich.console import Console

from saorsa_deploy.cmd import prod_local_downloads, prod_local_uploads
from saorsa_deploy.prod_bootstraps import ProdBootstrapsError


@pytest.fixture
def console():
    return Console(quiet=True)


@pytest.mark.parametrize("module", [prod_local_uploads, prod_local_downloads])
class TestResolveBootstrapArgs:
    def test_cache_enabled_returns_empty_and_ensures_file(self, module, console):
        args = SimpleNamespace(bootstrap_cache=True)
        with patch.object(module, "ensure_local_bootstrap_peers_file") as mock_ensure:
            mock_ensure.return_value = ("/home/x/.config/ant/bootstrap_peers.toml", False)
            result = module._resolve_bootstrap_args(args, console)
        assert result == []
        mock_ensure.assert_called_once_with()

    def test_cache_enabled_writes_missing_file(self, module, console):
        args = SimpleNamespace(bootstrap_cache=True)
        with patch.object(module, "ensure_local_bootstrap_peers_file") as mock_ensure:
            mock_ensure.return_value = ("/home/x/.config/ant/bootstrap_peers.toml", True)
            result = module._resolve_bootstrap_args(args, console)
        assert result == []

    def test_cache_enabled_fetch_failure_exits(self, module, console):
        args = SimpleNamespace(bootstrap_cache=True)
        with patch.object(module, "ensure_local_bootstrap_peers_file") as mock_ensure:
            mock_ensure.side_effect = ProdBootstrapsError("boom")
            with pytest.raises(SystemExit):
                module._resolve_bootstrap_args(args, console)

    def test_cache_disabled_builds_bootstrap_args(self, module, console):
        args = SimpleNamespace(bootstrap_cache=False)
        with patch.object(module, "fetch_prod_bootstraps") as mock_fetch:
            mock_fetch.return_value = ["1.2.3.4:10000", "5.6.7.8:10000"]
            result = module._resolve_bootstrap_args(args, console)
        assert result == [
            "--bootstrap",
            "1.2.3.4:10000",
            "--bootstrap",
            "5.6.7.8:10000",
        ]

    def test_cache_disabled_fetch_failure_exits(self, module, console):
        args = SimpleNamespace(bootstrap_cache=False)
        with patch.object(module, "fetch_prod_bootstraps") as mock_fetch:
            mock_fetch.side_effect = ProdBootstrapsError("boom")
            with pytest.raises(SystemExit):
                module._resolve_bootstrap_args(args, console)
