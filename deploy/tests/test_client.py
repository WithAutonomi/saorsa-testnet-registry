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
from saorsa_deploy.cmd.infra_clients import (
    parse_downloader_spec,
    parse_uploader_spec,
)
from saorsa_deploy.state import (
    S3_BUCKET,
    S3_CLIENT_ADDRESSES_PREFIX,
    S3_CLIENT_KEY_PREFIX,
    delete_client_addresses_prefix,
    delete_client_state,
    load_client_state,
    save_client_state,
    update_client_state,
)

# --- Client spec parsing ---


class TestParseUploaderSpec:
    def test_provider_region_and_file_size(self):
        result = parse_uploader_spec("digital_ocean:lon1:50")
        assert result == {
            "role": "upload",
            "provider": "digitalocean",
            "region": "lon1",
            "file_size_mb": 50,
            "vm_size": "s-2vcpu-4gb",
            "payment_type": None,
            "ant_funding": "100",
            "eth_funding": "0.5",
            "clock_skew": False,
        }

    def test_provider_region_file_size_and_vm_size(self):
        result = parse_uploader_spec("digital_ocean:nyc1:100:s-4vcpu-8gb")
        assert result == {
            "role": "upload",
            "provider": "digitalocean",
            "region": "nyc1",
            "file_size_mb": 100,
            "vm_size": "s-4vcpu-8gb",
            "payment_type": None,
            "ant_funding": "100",
            "eth_funding": "0.5",
            "clock_skew": False,
        }

    @pytest.mark.parametrize(
        "token,internal",
        [
            ("digital_ocean", "digitalocean"),
            ("digitalocean", "digitalocean"),
            ("vultr", "vultr"),
            ("hetzner", "hetzner"),
            ("ovh", "ovh"),
        ],
    )
    def test_all_providers_and_alias(self, token, internal):
        result = parse_uploader_spec(f"{token}:somewhere:50")
        assert result["provider"] == internal
        assert result["region"] == "somewhere"

    def test_provider_specific_region_and_size_pass_through(self):
        # Region names and sizes vary per provider; they must pass verbatim.
        result = parse_uploader_spec("ovh:EU-WEST-PAR:100:b3-16")
        assert result["region"] == "EU-WEST-PAR"
        assert result["vm_size"] == "b3-16"

    def test_missing_provider_rejected(self):
        # A bare region:file_size (the old format) is now too few parts.
        with pytest.raises(ValueError, match="Invalid uploader spec"):
            parse_uploader_spec("lon1:50")

    def test_unknown_provider_rejected(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            parse_uploader_spec("aws:lon1:50")

    def test_invalid_too_few_parts(self):
        with pytest.raises(ValueError, match="Invalid uploader spec"):
            parse_uploader_spec("digital_ocean:lon1")

    def test_invalid_too_many_parts(self):
        with pytest.raises(ValueError, match="Invalid uploader spec"):
            parse_uploader_spec("digital_ocean:lon1:50:s-2vcpu-4gb:extra")

    def test_invalid_file_size(self):
        with pytest.raises(ValueError, match="Invalid file size"):
            parse_uploader_spec("digital_ocean:lon1:abc")

    def test_zero_file_size(self):
        result = parse_uploader_spec("digital_ocean:lon1:0")
        assert result["file_size_mb"] == 0

    @pytest.mark.parametrize("payment_type", ["merkle", "no-merkle"])
    def test_payment_with_default_vm_size(self, payment_type):
        result = parse_uploader_spec(
            f"digital_ocean:lon1:50:payment={payment_type}"
        )
        assert result["payment_type"] == payment_type
        assert result["vm_size"] == "s-2vcpu-4gb"

    def test_payment_with_explicit_vm_size(self):
        result = parse_uploader_spec(
            "digital_ocean:nyc1:100:s-4vcpu-8gb:payment=no-merkle"
        )
        assert result["payment_type"] == "no-merkle"
        assert result["vm_size"] == "s-4vcpu-8gb"

    def test_payment_invalid_value_rejected(self):
        with pytest.raises(ValueError, match="payment must be one of"):
            parse_uploader_spec("digital_ocean:lon1:50:payment=bogus")

    def test_payment_unknown_key_rejected(self):
        with pytest.raises(ValueError, match="unknown key"):
            parse_uploader_spec("digital_ocean:lon1:50:bogus=x")

    def test_payment_duplicate_rejected(self):
        with pytest.raises(ValueError, match="duplicate key"):
            parse_uploader_spec(
                "digital_ocean:lon1:50:payment=merkle:payment=no-merkle"
            )


class TestParseDownloaderSpec:
    def test_provider_and_region(self):
        result = parse_downloader_spec("digital_ocean:lon1")
        assert result == {
            "role": "download",
            "provider": "digitalocean",
            "region": "lon1",
            "vm_size": "s-2vcpu-4gb",
            "clock_skew": False,
        }

    def test_provider_region_and_vm_size(self):
        result = parse_downloader_spec("vultr:nrt:vc2-4c-8gb")
        assert result == {
            "role": "download",
            "provider": "vultr",
            "region": "nrt",
            "vm_size": "vc2-4c-8gb",
            "clock_skew": False,
        }

    def test_missing_provider_rejected(self):
        with pytest.raises(ValueError, match="Invalid downloader spec"):
            parse_downloader_spec("lon1")

    def test_unknown_provider_rejected(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            parse_downloader_spec("aws:lon1")

    def test_invalid_empty_region(self):
        with pytest.raises(ValueError, match="region cannot be empty"):
            parse_downloader_spec("digital_ocean:")

    def test_invalid_too_many_parts(self):
        with pytest.raises(ValueError, match="Invalid downloader spec"):
            parse_downloader_spec("digital_ocean:lon1:s-2vcpu-4gb:extra")


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


class TestClientStateRoundTripsRole:
    def test_role_survives_save_and_load(self, mock_s3):
        clients = [
            {
                "role": "upload",
                "region": "lon1",
                "file_size_mb": 50,
                "vm_size": "s-2vcpu-4gb",
                "client_index": 1,
                "ip": "10.0.0.1",
            },
            {
                "role": "download",
                "region": "lon1",
                "vm_size": "s-2vcpu-4gb",
                "client_index": 1,
                "ip": "10.0.0.2",
            },
        ]
        save_client_state("DEV-01", clients)

        # Whatever save_client_state put in S3 should round-trip via load
        body = mock_s3.put_object.call_args.kwargs["Body"]
        mock_body = MagicMock()
        mock_body.read.return_value = body.encode() if isinstance(body, str) else body
        mock_s3.get_object.return_value = {"Body": mock_body}

        result = load_client_state("DEV-01")
        roles = [c["role"] for c in result["clients"]]
        assert roles == ["upload", "download"]


class TestDeleteClientAddressesPrefix:
    def test_paginates_and_batch_deletes(self, mock_s3):
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": f"{S3_CLIENT_ADDRESSES_PREFIX}/DEV-01/upload-lon1-1.txt"},
                    {"Key": f"{S3_CLIENT_ADDRESSES_PREFIX}/DEV-01/upload-nyc1-1.txt"},
                ],
            },
            {
                "Contents": [
                    {"Key": f"{S3_CLIENT_ADDRESSES_PREFIX}/DEV-01/upload-ams3-1.txt"},
                ],
            },
        ]
        mock_s3.get_paginator.return_value = paginator

        deleted = delete_client_addresses_prefix("DEV-01")

        assert deleted == 3
        paginator.paginate.assert_called_once_with(
            Bucket=S3_BUCKET,
            Prefix=f"{S3_CLIENT_ADDRESSES_PREFIX}/DEV-01/",
        )
        # Two batch deletes (one per non-empty page)
        assert mock_s3.delete_objects.call_count == 2
        first_call = mock_s3.delete_objects.call_args_list[0].kwargs
        assert first_call["Bucket"] == S3_BUCKET
        assert len(first_call["Delete"]["Objects"]) == 2

    def test_empty_prefix_returns_zero(self, mock_s3):
        paginator = MagicMock()
        paginator.paginate.return_value = [{"Contents": []}]
        mock_s3.get_paginator.return_value = paginator

        deleted = delete_client_addresses_prefix("DEV-EMPTY")

        assert deleted == 0
        mock_s3.delete_objects.assert_not_called()


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
