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
)


class TestGetReleaseUrl:
    @patch("saorsa_deploy.binary_source.requests.get")
    def test_latest_release_url(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "assets": [
                {
                    "name": "saorsa-node-cli-linux-x64.tar.gz",
                    "browser_download_url": "https://github.com/download/latest/asset.tar.gz",
                },
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        url = get_release_url()
        assert url == "https://github.com/download/latest/asset.tar.gz"
        mock_get.assert_called_once_with(
            "https://api.github.com/repos/saorsa-labs/saorsa-node/releases/latest",
            timeout=30,
        )

    @patch("saorsa_deploy.binary_source.requests.get")
    def test_versioned_release_url(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "assets": [
                {
                    "name": "saorsa-node-cli-linux-x64.tar.gz",
                    "browser_download_url": "https://github.com/download/v0.3.0/asset.tar.gz",
                },
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        url = get_release_url("0.3.0")
        assert url == "https://github.com/download/v0.3.0/asset.tar.gz"
        mock_get.assert_called_once_with(
            "https://api.github.com/repos/saorsa-labs/saorsa-node/releases/tags/v0.3.0",
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
        url = get_custom_build_url("myorg", "feature-branch")
        expected = (
            f"https://{BUILDS_BUCKET}.s3.{BUILDS_REGION}.amazonaws.com/"
            f"{BUILDS_KEY_PREFIX}/myorg/feature-branch/saorsa-node"
        )
        assert url == expected

    def test_different_owner_and_branch(self):
        url = get_custom_build_url("saorsa-labs", "main")
        assert "/saorsa-labs/main/saorsa-node" in url


class TestCheckReleaseExists:
    @patch("saorsa_deploy.binary_source.requests.get")
    def test_returns_true_when_release_exists(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        assert check_release_exists("0.2.0") is True
        mock_get.assert_called_once_with(
            "https://api.github.com/repos/saorsa-labs/saorsa-node/releases/tags/v0.2.0",
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

        assert check_custom_build_exists("myorg", "my-branch") is True
        mock_s3.head_object.assert_called_once_with(
            Bucket=BUILDS_BUCKET,
            Key=f"{BUILDS_KEY_PREFIX}/myorg/my-branch/saorsa-node",
        )

    @patch("saorsa_deploy.binary_source.boto3.client")
    def test_returns_false_when_object_not_found(self, mock_boto_client):
        mock_s3 = MagicMock()
        error = botocore.exceptions.ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )
        mock_s3.head_object.side_effect = error
        mock_boto_client.return_value = mock_s3

        assert check_custom_build_exists("myorg", "nonexistent") is False
