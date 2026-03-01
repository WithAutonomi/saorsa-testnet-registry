from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rich.console import Console

from saorsa_deploy.cmd.provision_genesis import _resolve_binary_source


class TestResolveBinarySource:
    def setup_method(self):
        self.console = Console()

    def test_returns_none_when_no_args(self):
        args = SimpleNamespace(branch_name=None, repo_owner=None, node_version=None)
        url, is_archive = _resolve_binary_source(args, self.console)
        assert url is None
        assert is_archive is True

    def test_exits_when_version_and_branch_both_set(self):
        args = SimpleNamespace(branch_name="feature-x", repo_owner="myorg", node_version="0.2.0")
        with pytest.raises(SystemExit):
            _resolve_binary_source(args, self.console)

    def test_exits_when_only_branch_set(self):
        args = SimpleNamespace(branch_name="feature-x", repo_owner=None, node_version=None)
        with pytest.raises(SystemExit):
            _resolve_binary_source(args, self.console)

    def test_exits_when_only_repo_owner_set(self):
        args = SimpleNamespace(branch_name=None, repo_owner="myorg", node_version=None)
        with pytest.raises(SystemExit):
            _resolve_binary_source(args, self.console)

    @patch("saorsa_deploy.cmd.provision_genesis.get_release_url")
    @patch("saorsa_deploy.cmd.provision_genesis.check_release_exists")
    def test_version_arg_returns_release_url(self, mock_check, mock_get_url):
        mock_check.return_value = True
        mock_get_url.return_value = "https://github.com/download/v0.2.0/asset.tar.gz"

        args = SimpleNamespace(branch_name=None, repo_owner=None, node_version="0.2.0")
        url, is_archive = _resolve_binary_source(args, self.console)

        assert url == "https://github.com/download/v0.2.0/asset.tar.gz"
        assert is_archive is True
        mock_check.assert_called_once_with("0.2.0")

    @patch("saorsa_deploy.cmd.provision_genesis.check_release_exists")
    def test_exits_when_version_not_found(self, mock_check):
        mock_check.return_value = False

        args = SimpleNamespace(branch_name=None, repo_owner=None, node_version="99.0.0")
        with pytest.raises(SystemExit):
            _resolve_binary_source(args, self.console)

    @patch("saorsa_deploy.cmd.provision_genesis.get_custom_build_url")
    @patch("saorsa_deploy.cmd.provision_genesis.check_custom_build_exists")
    def test_branch_args_return_custom_build_url(self, mock_check, mock_get_url):
        mock_check.return_value = True
        mock_get_url.return_value = "https://s3.amazonaws.com/builds/myorg/feature-x/saorsa-node"

        args = SimpleNamespace(branch_name="feature-x", repo_owner="myorg", node_version=None)
        url, is_archive = _resolve_binary_source(args, self.console)

        assert url == "https://s3.amazonaws.com/builds/myorg/feature-x/saorsa-node"
        assert is_archive is False
        mock_check.assert_called_once_with("myorg", "feature-x")

    @patch("saorsa_deploy.cmd.provision_genesis.check_custom_build_exists")
    def test_exits_when_custom_build_not_found(self, mock_check):
        mock_check.return_value = False

        args = SimpleNamespace(branch_name="nonexistent", repo_owner="myorg", node_version=None)
        with pytest.raises(SystemExit):
            _resolve_binary_source(args, self.console)
