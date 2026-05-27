import subprocess
from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

from saorsa_deploy.binary_source import (
    BUILDS_BUCKET,
    BUILDS_KEY_PREFIX,
    BUILDS_REGION,
    check_custom_build_exists,
    check_release_exists,
    get_custom_build_url,
    get_release_url,
    remote_branch_exists,
    validate_branch_exists,
)


class TestGetReleaseUrl:
    @patch("saorsa_deploy.binary_source.requests.get")
    def test_latest_release_url(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "assets": [
                {
                    "name": "ant-node-cli-linux-x64.tar.gz",
                    "browser_download_url": "https://github.com/download/latest/asset.tar.gz",
                },
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        url = get_release_url()
        assert url == "https://github.com/download/latest/asset.tar.gz"
        mock_get.assert_called_once_with(
            "https://api.github.com/repos/WithAutonomi/ant-node/releases/latest",
            timeout=30,
        )

    @patch("saorsa_deploy.binary_source.requests.get")
    def test_versioned_release_url(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "assets": [
                {
                    "name": "ant-node-cli-linux-x64.tar.gz",
                    "browser_download_url": "https://github.com/download/v0.3.0/asset.tar.gz",
                },
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        url = get_release_url("0.3.0")
        assert url == "https://github.com/download/v0.3.0/asset.tar.gz"
        mock_get.assert_called_once_with(
            "https://api.github.com/repos/WithAutonomi/ant-node/releases/tags/v0.3.0",
            timeout=30,
        )

    @patch("saorsa_deploy.binary_source.requests.get")
    def test_raises_when_asset_not_found(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "assets": [{"name": "wrong-asset.tar.gz", "browser_download_url": "https://x"}],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        with pytest.raises(RuntimeError, match="Could not find asset"):
            get_release_url()

    @patch("saorsa_deploy.binary_source.requests.get")
    def test_raises_when_no_assets(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"assets": []}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        with pytest.raises(RuntimeError, match="Could not find asset"):
            get_release_url("1.0.0")

    @patch("saorsa_deploy.binary_source.requests.get")
    def test_versioned_error_message_includes_tag(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"assets": []}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        with pytest.raises(RuntimeError, match="v1.0.0"):
            get_release_url("1.0.0")


class TestGetCustomBuildUrl:
    def test_returns_correct_s3_url(self):
        url = get_custom_build_url("DEV-01", "myorg", "feature-branch")
        expected = (
            f"https://{BUILDS_BUCKET}.s3.{BUILDS_REGION}.amazonaws.com/"
            f"{BUILDS_KEY_PREFIX}/DEV-01/myorg/feature-branch/ant-node"
        )
        assert url == expected

    def test_different_owner_and_branch(self):
        url = get_custom_build_url("DEV-01", "saorsa-labs", "main")
        assert "/DEV-01/saorsa-labs/main/ant-node" in url


class TestCheckReleaseExists:
    @patch("saorsa_deploy.binary_source.requests.get")
    def test_returns_true_when_release_exists(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        assert check_release_exists("0.2.0") is True
        mock_get.assert_called_once_with(
            "https://api.github.com/repos/WithAutonomi/ant-node/releases/tags/v0.2.0",
            timeout=30,
        )

    @patch("saorsa_deploy.binary_source.requests.get")
    def test_returns_false_when_release_not_found(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        assert check_release_exists("99.99.99") is False


class TestCheckCustomBuildExists:
    @patch("saorsa_deploy.binary_source.boto3.client")
    def test_returns_true_when_object_exists(self, mock_boto_client):
        mock_s3 = MagicMock()
        mock_boto_client.return_value = mock_s3

        assert check_custom_build_exists("DEV-01", "myorg", "my-branch") is True
        mock_s3.head_object.assert_called_once_with(
            Bucket=BUILDS_BUCKET,
            Key=f"{BUILDS_KEY_PREFIX}/DEV-01/myorg/my-branch/ant-node",
        )

    @patch("saorsa_deploy.binary_source.boto3.client")
    def test_returns_false_when_object_not_found(self, mock_boto_client):
        mock_s3 = MagicMock()
        error = botocore.exceptions.ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )
        mock_s3.head_object.side_effect = error
        mock_boto_client.return_value = mock_s3

        assert check_custom_build_exists("DEV-01", "myorg", "nonexistent") is False


class TestRemoteBranchExists:
    @patch("saorsa_deploy.binary_source.subprocess.run")
    def test_true_when_ref_found(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="abc123\trefs/heads/feature-x\n", stderr=""
        )

        assert remote_branch_exists("myorg", "ant-client", "feature-x") is True
        mock_run.assert_called_once()
        call = mock_run.call_args
        assert call.args[0] == [
            "git",
            "ls-remote",
            "--heads",
            "--tags",
            "https://github.com/myorg/ant-client.git",
            "feature-x",
        ]
        assert call.kwargs["capture_output"] is True
        assert call.kwargs["text"] is True
        assert call.kwargs["timeout"] == 30
        # Git must be forced non-interactive so a 401 errors out instead of
        # prompting for credentials on the terminal.
        assert call.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"

    @patch("saorsa_deploy.binary_source.subprocess.run")
    def test_false_when_ref_absent(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        assert remote_branch_exists("myorg", "ant-node", "nope") is False

    @patch("saorsa_deploy.binary_source.subprocess.run")
    def test_raises_when_git_fails(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=128, stdout="", stderr="fatal: repository not found"
        )

        with pytest.raises(RuntimeError, match="repository not found"):
            remote_branch_exists("badorg", "ant-node", "main")

    @patch("saorsa_deploy.binary_source.subprocess.run")
    def test_raises_clear_message_on_401(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=128,
            stdout="",
            stderr="fatal: could not read Username for 'https://github.com': "
            "terminal prompts disabled",
        )

        with pytest.raises(RuntimeError, match="not found or is private"):
            remote_branch_exists("wrongowner", "ant-client", "some-branch")

    @patch("saorsa_deploy.binary_source.subprocess.run")
    def test_raises_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)

        with pytest.raises(RuntimeError, match="Timed out"):
            remote_branch_exists("myorg", "ant-node", "main")

    @patch("saorsa_deploy.binary_source.subprocess.run")
    def test_raises_when_git_missing(self, mock_run):
        mock_run.side_effect = FileNotFoundError()

        with pytest.raises(RuntimeError, match="git executable not found"):
            remote_branch_exists("myorg", "ant-node", "main")


class TestValidateBranchExists:
    @patch("saorsa_deploy.binary_source.remote_branch_exists", return_value=True)
    def test_passes_silently_when_found(self, _mock_exists):
        validate_branch_exists("myorg", "ant-client", "feature-x")

    @patch("saorsa_deploy.binary_source.remote_branch_exists", return_value=False)
    def test_raises_value_error_when_missing(self, _mock_exists):
        with pytest.raises(ValueError, match="was not found"):
            validate_branch_exists("myorg", "ant-client", "feature-x")
