import os
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.build_droplet import (
    BUILD_IMAGE,
    BUILD_REGIONS,
    BUILD_SIZE,
    SSH_KEY_IDS,
    create_build_vm,
    destroy_build_vm,
    wait_for_ssh,
)


class TestCreateBuildVm:
    @patch("saorsa_deploy.build_droplet.time.sleep")
    @patch("saorsa_deploy.build_droplet.requests")
    def test_creates_droplet_with_correct_params(self, mock_requests, _mock_sleep):
        os.environ["DO_TOKEN"] = "test-token"
        try:
            # No existing droplet
            mock_list_resp = MagicMock()
            mock_list_resp.json.return_value = {"droplets": []}
            mock_list_resp.raise_for_status = MagicMock()

            # Create droplet
            mock_create_resp = MagicMock()
            mock_create_resp.status_code = 202
            mock_create_resp.json.return_value = {"droplet": {"id": 12345}}
            mock_create_resp.raise_for_status = MagicMock()

            # Wait for active
            mock_active_resp = MagicMock()
            mock_active_resp.json.return_value = {
                "droplet": {
                    "status": "active",
                    "networks": {
                        "v4": [{"type": "public", "ip_address": "1.2.3.4"}],
                    },
                }
            }
            mock_active_resp.raise_for_status = MagicMock()

            mock_requests.get.side_effect = [mock_list_resp, mock_active_resp]
            mock_requests.post.return_value = mock_create_resp

            result = create_build_vm("DEV-01", "myorg", "feature-branch")

            assert result["droplet_id"] == 12345
            assert result["droplet_name"] == "saorsa-build-DEV-01-myorg-feature-branch"
            assert result["ip_address"] == "1.2.3.4"

            # Verify create was called with correct params
            create_call = mock_requests.post.call_args
            body = create_call.kwargs["json"]
            assert body["name"] == "saorsa-build-DEV-01-myorg-feature-branch"
            assert body["region"] == BUILD_REGIONS[0]
            assert body["size"] == BUILD_SIZE
            assert body["image"] == BUILD_IMAGE
            assert body["ssh_keys"] == SSH_KEY_IDS
        finally:
            os.environ.pop("DO_TOKEN", None)

    @patch("saorsa_deploy.build_droplet.time.sleep")
    @patch("saorsa_deploy.build_droplet.requests")
    def test_reuses_existing_droplet(self, mock_requests, mock_sleep):
        os.environ["DO_TOKEN"] = "test-token"
        try:
            # Existing droplet found
            mock_list_resp = MagicMock()
            mock_list_resp.json.return_value = {
                "droplets": [
                    {
                        "name": "saorsa-build-TEST-myorg-branch",
                        "id": 99999,
                        "networks": {"v4": [{"type": "public", "ip_address": "5.6.7.8"}]},
                    }
                ]
            }
            mock_list_resp.raise_for_status = MagicMock()
            mock_requests.get.return_value = mock_list_resp

            result = create_build_vm("TEST", "myorg", "branch")

            assert result["droplet_id"] == 99999
            assert result["ip_address"] == "5.6.7.8"
            assert result["reused"] is True
            mock_requests.post.assert_not_called()
        finally:
            os.environ.pop("DO_TOKEN", None)

    @patch("saorsa_deploy.build_droplet.time.sleep")
    @patch("saorsa_deploy.build_droplet.requests")
    def test_falls_back_to_next_region_on_422(self, mock_requests, _mock_sleep):
        os.environ["DO_TOKEN"] = "test-token"
        try:
            # No existing droplet
            mock_list_resp = MagicMock()
            mock_list_resp.json.return_value = {"droplets": []}
            mock_list_resp.raise_for_status = MagicMock()

            # First region returns 422
            mock_422_resp = MagicMock()
            mock_422_resp.status_code = 422
            mock_422_resp.json.return_value = {"message": "Size is not available in this region."}

            # Second region succeeds
            mock_create_resp = MagicMock()
            mock_create_resp.status_code = 202
            mock_create_resp.json.return_value = {"droplet": {"id": 12345}}
            mock_create_resp.raise_for_status = MagicMock()

            # Wait for active
            mock_active_resp = MagicMock()
            mock_active_resp.json.return_value = {
                "droplet": {
                    "status": "active",
                    "networks": {
                        "v4": [{"type": "public", "ip_address": "1.2.3.4"}],
                    },
                }
            }
            mock_active_resp.raise_for_status = MagicMock()

            mock_requests.get.side_effect = [mock_list_resp, mock_active_resp]
            mock_requests.post.side_effect = [mock_422_resp, mock_create_resp]

            result = create_build_vm("DEV-01", "myorg", "feature-branch")

            assert result["droplet_id"] == 12345
            assert mock_requests.post.call_count == 2
            # First call used first region, second call used second region
            first_body = mock_requests.post.call_args_list[0].kwargs["json"]
            second_body = mock_requests.post.call_args_list[1].kwargs["json"]
            assert first_body["region"] == BUILD_REGIONS[0]
            assert second_body["region"] == BUILD_REGIONS[1]
        finally:
            os.environ.pop("DO_TOKEN", None)

    @patch("saorsa_deploy.build_droplet.requests")
    def test_raises_when_all_regions_unavailable(self, mock_requests):
        os.environ["DO_TOKEN"] = "test-token"
        try:
            mock_list_resp = MagicMock()
            mock_list_resp.json.return_value = {"droplets": []}
            mock_list_resp.raise_for_status = MagicMock()
            mock_requests.get.return_value = mock_list_resp

            mock_422_resp = MagicMock()
            mock_422_resp.status_code = 422
            mock_422_resp.json.return_value = {"message": "Size is not available in this region."}
            mock_requests.post.return_value = mock_422_resp

            with pytest.raises(RuntimeError, match="not available in any region"):
                create_build_vm("DEV-01", "myorg", "feature-branch")

            assert mock_requests.post.call_count == len(BUILD_REGIONS)
        finally:
            os.environ.pop("DO_TOKEN", None)

    def test_raises_without_do_token(self):
        os.environ.pop("DO_TOKEN", None)
        with pytest.raises(RuntimeError, match="DO_TOKEN"):
            create_build_vm("TEST", "myorg", "branch")


class TestDestroyBuildVm:
    @patch("saorsa_deploy.build_droplet.requests")
    def test_deletes_droplet(self, mock_requests):
        os.environ["DO_TOKEN"] = "test-token"
        try:
            destroy_build_vm(12345)
            mock_requests.delete.assert_called_once()
            delete_url = mock_requests.delete.call_args[0][0]
            assert "12345" in delete_url
        finally:
            os.environ.pop("DO_TOKEN", None)


class TestWaitForSsh:
    @patch("saorsa_deploy.build_droplet.socket.create_connection")
    def test_returns_when_ssh_available(self, mock_conn):
        mock_sock = MagicMock()
        mock_conn.return_value = mock_sock

        wait_for_ssh("1.2.3.4", timeout=10)
        mock_conn.assert_called_once_with(("1.2.3.4", 22), timeout=5)
        mock_sock.close.assert_called_once()

    @patch("saorsa_deploy.build_droplet.time.sleep")
    @patch("saorsa_deploy.build_droplet.time.monotonic")
    @patch("saorsa_deploy.build_droplet.socket.create_connection")
    def test_raises_on_timeout(self, mock_conn, mock_monotonic, _mock_sleep):
        mock_conn.side_effect = ConnectionRefusedError()
        mock_monotonic.side_effect = [0, 0, 301]

        with pytest.raises(TimeoutError, match="SSH.*not available"):
            wait_for_ssh("1.2.3.4", timeout=300)
