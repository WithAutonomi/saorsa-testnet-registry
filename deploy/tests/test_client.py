import json
from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

from saorsa_deploy.binary_source import (
    BUILDS_BUCKET,
    BUILDS_KEY_PREFIX,
    BUILDS_REGION,
    check_ant_custom_build_exists,
    get_ant_custom_build_url,
    get_ant_release_url,
)
from saorsa_deploy.cmd.infra_client import parse_client_spec
from saorsa_deploy.state import (
    S3_BUCKET,
    S3_CLIENT_KEY_PREFIX,
    delete_client_state,
    load_client_state,
    save_client_state,
    update_client_state,
)

# --- Client spec parsing ---


class TestParseClientSpec:
    def test_region_and_file_size(self):
        result = parse_client_spec("lon1:50")
        assert result == {
            "region": "lon1",
            "file_size_mb": 50,
            "vm_size": "s-2vcpu-4gb",
        }

    def test_region_file_size_and_vm_size(self):
        result = parse_client_spec("nyc1:100:s-4vcpu-8gb")
        assert result == {
            "region": "nyc1",
            "file_size_mb": 100,
            "vm_size": "s-4vcpu-8gb",
        }

    def test_invalid_too_few_parts(self):
        with pytest.raises(ValueError, match="Invalid client spec"):
            parse_client_spec("lon1")

    def test_invalid_too_many_parts(self):
        with pytest.raises(ValueError, match="Invalid client spec"):
            parse_client_spec("lon1:50:s-2vcpu-4gb:extra")

    def test_invalid_file_size(self):
        with pytest.raises(ValueError, match="Invalid file size"):
            parse_client_spec("lon1:abc")

    def test_zero_file_size(self):
        result = parse_client_spec("lon1:0")
        assert result["file_size_mb"] == 0


# --- Client state management ---


@pytest.fixture
def mock_s3():
    with patch("saorsa_deploy.state.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        yield mock_client


class TestSaveClientState:
    def test_puts_json_to_s3(self, mock_s3):
        clients = [
            {
                "region": "lon1",
                "file_size_mb": 50,
                "vm_size": "s-2vcpu-4gb",
                "client_index": 1,
                "ip": "10.0.0.1",
            },
            {
                "region": "nyc1",
                "file_size_mb": 100,
                "vm_size": "s-4vcpu-8gb",
                "client_index": 2,
                "ip": "10.0.0.2",
            },
        ]
        save_client_state("DEV-01", clients)

        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == S3_BUCKET
        assert call_kwargs["Key"] == f"{S3_CLIENT_KEY_PREFIX}/DEV-01.json"
        assert call_kwargs["ContentType"] == "application/json"

        body = json.loads(call_kwargs["Body"])
        assert body["name"] == "DEV-01"
        assert len(body["clients"]) == 2
        assert body["clients"][0]["region"] == "lon1"
        assert body["clients"][0]["file_size_mb"] == 50
        assert body["clients"][1]["ip"] == "10.0.0.2"


class TestLoadClientState:
    def test_loads_json_from_s3(self, mock_s3):
        state = {
            "name": "DEV-01",
            "clients": [
                {"region": "lon1", "file_size_mb": 50, "ip": "10.0.0.1", "client_index": 1}
            ],
        }
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(state).encode()
        mock_s3.get_object.return_value = {"Body": mock_body}

        result = load_client_state("DEV-01")

        mock_s3.get_object.assert_called_once_with(
            Bucket=S3_BUCKET,
            Key=f"{S3_CLIENT_KEY_PREFIX}/DEV-01.json",
        )
        assert result["name"] == "DEV-01"
        assert len(result["clients"]) == 1

    def test_raises_on_not_found(self, mock_s3):
        error_class = type("NoSuchKey", (Exception,), {})
        mock_s3.exceptions.NoSuchKey = error_class
        mock_s3.get_object.side_effect = error_class("not found")

        with pytest.raises(RuntimeError, match="No client state found"):
            load_client_state("NONEXISTENT")


class TestUpdateClientState:
    def test_merges_updates(self, mock_s3):
        existing = {"name": "DEV-01", "clients": []}
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(existing).encode()
        mock_s3.get_object.return_value = {"Body": mock_body}

        update_client_state("DEV-01", {"provisioned": True})

        body = json.loads(mock_s3.put_object.call_args.kwargs["Body"])
        assert body["name"] == "DEV-01"
        assert body["provisioned"] is True


class TestDeleteClientState:
    def test_deletes_from_s3(self, mock_s3):
        delete_client_state("DEV-01")

        mock_s3.delete_object.assert_called_once_with(
            Bucket=S3_BUCKET,
            Key=f"{S3_CLIENT_KEY_PREFIX}/DEV-01.json",
        )


# --- Ant binary source ---


class TestGetAntCustomBuildUrl:
    def test_returns_correct_url(self):
        url = get_ant_custom_build_url("DEV-01", "WithAutonomi", "feat/my-change")
        expected = (
            f"https://{BUILDS_BUCKET}.s3.{BUILDS_REGION}.amazonaws.com/"
            f"{BUILDS_KEY_PREFIX}/DEV-01/WithAutonomi/feat/my-change/ant"
        )
        assert url == expected


class TestCheckAntCustomBuildExists:
    @patch("saorsa_deploy.binary_source.boto3")
    def test_returns_true_when_exists(self, mock_boto3):
        mock_s3 = MagicMock()
        mock_boto3.client.return_value = mock_s3

        result = check_ant_custom_build_exists("DEV-01", "owner", "branch")

        assert result is True
        mock_s3.head_object.assert_called_once_with(
            Bucket=BUILDS_BUCKET,
            Key=f"{BUILDS_KEY_PREFIX}/DEV-01/owner/branch/ant",
        )

    @patch("saorsa_deploy.binary_source.boto3")
    def test_returns_false_when_not_found(self, mock_boto3):
        mock_s3 = MagicMock()
        mock_boto3.client.return_value = mock_s3
        mock_s3.head_object.side_effect = botocore.exceptions.ClientError(
            {"Error": {"Code": "404"}}, "HeadObject"
        )

        result = check_ant_custom_build_exists("DEV-01", "owner", "branch")
        assert result is False


class TestGetAntReleaseUrl:
    @patch("saorsa_deploy.binary_source.requests.get")
    def test_latest_release(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "assets": [
                {
                    "name": "ant-0.1.4-x86_64-unknown-linux-musl.tar.gz",
                    "browser_download_url": "https://github.com/download/latest/ant.tar.gz",
                },
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        url = get_ant_release_url()
        assert url == "https://github.com/download/latest/ant.tar.gz"

    @patch("saorsa_deploy.binary_source.requests.get")
    def test_versioned_release(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "assets": [
                {
                    "name": "ant-0.1.4-x86_64-unknown-linux-musl.tar.gz",
                    "browser_download_url": "https://github.com/download/v0.3.0/ant.tar.gz",
                },
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        url = get_ant_release_url("0.3.0")
        assert url == "https://github.com/download/v0.3.0/ant.tar.gz"
        mock_get.assert_called_once_with(
            "https://api.github.com/repos/WithAutonomi/ant-client/releases/tags/ant-cli-v0.3.0",
            timeout=30,
        )

    @patch("saorsa_deploy.binary_source.requests.get")
    def test_raises_when_asset_not_found(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"assets": []}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        with pytest.raises(RuntimeError, match="Could not find asset"):
            get_ant_release_url()
