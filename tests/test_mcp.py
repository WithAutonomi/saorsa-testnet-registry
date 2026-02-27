"""Tests for the MCP server tools in saorsa_deploy_mcp.server."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Set REGISTRY_URL before importing the module so it picks up our value.
os.environ["REGISTRY_URL"] = "http://test-registry:5000"

from saorsa_deploy_mcp.server import (  # noqa: E402
    create_deployment,
    destroy_deployment,
    track_deployment,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_response(*, status_code=200, json_data=None):
    """Build a MagicMock that behaves like an httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    return resp


def _make_mock_client(*, post_response=None, get_response=None):
    """Build an AsyncMock that behaves like an httpx.AsyncClient."""
    client = AsyncMock()
    if post_response is not None:
        client.post.return_value = post_response
    if get_response is not None:
        client.get.return_value = get_response
    return client


# ---------------------------------------------------------------------------
# create_deployment tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_deployment_calls_correct_endpoint():
    """create_deployment POSTs to {REGISTRY_URL}/api/deployments with the
    correct JSON body containing name and deploy_args."""
    response_data = {
        "id": 1,
        "name": "DEV-01",
        "status": "pending",
        "workflow_run_id": 42,
        "deploy_args": {
            "name": "DEV-01",
            "vm_count": 3,
            "node_count": 25,
            "genesis_port": 12000,
            "testnet": True,
        },
    }
    mock_response = _make_mock_response(status_code=201, json_data=response_data)
    mock_client = _make_mock_client(post_response=mock_response)

    with patch("saorsa_deploy_mcp.server.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client

        result = await create_deployment(
            name="DEV-01",
            vm_count=3,
            node_count=25,
            genesis_port=12000,
        )

    # Verify the endpoint URL
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "http://test-registry:5000/api/deployments"

    # Verify the JSON body
    body = call_args[1]["json"]
    assert body["name"] == "DEV-01"
    assert body["deploy_args"]["vm_count"] == 3
    assert body["deploy_args"]["node_count"] == 25
    assert body["deploy_args"]["genesis_port"] == 12000
    assert body["deploy_args"]["testnet"] is True

    # Verify the result string
    assert "DEV-01" in result
    assert "id=1" in result


@pytest.mark.asyncio
async def test_create_deployment_includes_optional_args():
    """When optional parameters are provided, they appear in deploy_args."""
    response_data = {
        "id": 2,
        "name": "DEV-02",
        "status": "pending",
        "workflow_run_id": 99,
        "deploy_args": {},
    }
    mock_response = _make_mock_response(status_code=201, json_data=response_data)
    mock_client = _make_mock_client(post_response=mock_response)

    with patch("saorsa_deploy_mcp.server.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client

        await create_deployment(
            name="DEV-02",
            vm_count=5,
            node_count=50,
            genesis_port=13000,
            attached_volume_size=100,
            ip_version="v6",
            region_counts="us-east-1:3,eu-west-1:2",
            log_level="debug",
            port=14000,
        )

    body = mock_client.post.call_args[1]["json"]
    deploy_args = body["deploy_args"]

    assert deploy_args["attached_volume_size"] == 100
    assert deploy_args["ip_version"] == "v6"
    assert deploy_args["region_counts"] == "us-east-1:3,eu-west-1:2"
    assert deploy_args["log_level"] == "debug"
    assert deploy_args["port"] == 14000


@pytest.mark.asyncio
async def test_create_deployment_omits_none_optional_args():
    """When optional parameters are not provided (None), their keys should not
    appear in deploy_args."""
    response_data = {
        "id": 3,
        "name": "DEV-03",
        "status": "pending",
        "workflow_run_id": 77,
        "deploy_args": {},
    }
    mock_response = _make_mock_response(status_code=201, json_data=response_data)
    mock_client = _make_mock_client(post_response=mock_response)

    with patch("saorsa_deploy_mcp.server.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client

        await create_deployment(
            name="DEV-03",
            vm_count=1,
            node_count=10,
            genesis_port=11000,
        )

    body = mock_client.post.call_args[1]["json"]
    deploy_args = body["deploy_args"]

    optional_keys = [
        "attached_volume_size",
        "region_counts",
        "ip_version",
        "log_level",
        "port",
    ]
    for key in optional_keys:
        assert key not in deploy_args, (
            f"Optional key '{key}' should not be present when unset"
        )

    # Required keys should still be present
    assert deploy_args["name"] == "DEV-03"
    assert deploy_args["vm_count"] == 1
    assert deploy_args["node_count"] == 10
    assert deploy_args["genesis_port"] == 11000
    assert deploy_args["testnet"] is True


# ---------------------------------------------------------------------------
# track_deployment tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_track_deployment_calls_correct_endpoint():
    """track_deployment GETs {REGISTRY_URL}/api/deployments/{deployment_id}."""
    response_data = {
        "id": 5,
        "name": "DEV-05",
        "status": "running",
        "workflow_run_id": 55,
        "events": [],
    }
    mock_response = _make_mock_response(status_code=200, json_data=response_data)
    mock_client = _make_mock_client(get_response=mock_response)

    with patch("saorsa_deploy_mcp.server.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client

        await track_deployment(deployment_id=5)

    mock_client.get.assert_called_once()
    call_args = mock_client.get.call_args
    assert call_args[0][0] == "http://test-registry:5000/api/deployments/5"


@pytest.mark.asyncio
async def test_track_deployment_reports_finished_status():
    """When a deployment has stopped, the result includes 'has finished with
    status: stopped'."""
    response_data = {
        "id": 6,
        "name": "DEV-06",
        "status": "stopped",
        "workflow_run_id": 66,
        "events": [],
    }
    mock_response = _make_mock_response(status_code=200, json_data=response_data)
    mock_client = _make_mock_client(get_response=mock_response)

    with patch("saorsa_deploy_mcp.server.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client

        result = await track_deployment(deployment_id=6)

    assert "has finished with status: stopped" in result


@pytest.mark.asyncio
async def test_track_deployment_reports_in_progress():
    """When a deployment is pending, the result indicates it is still
    pending."""
    response_data = {
        "id": 7,
        "name": "DEV-07",
        "status": "pending",
        "workflow_run_id": 77,
        "events": [],
    }
    mock_response = _make_mock_response(status_code=200, json_data=response_data)
    mock_client = _make_mock_client(get_response=mock_response)

    with patch("saorsa_deploy_mcp.server.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client

        result = await track_deployment(deployment_id=7)

    assert "still pending" in result


# ---------------------------------------------------------------------------
# destroy_deployment tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_destroy_deployment_calls_correct_endpoint():
    """destroy_deployment POSTs to {REGISTRY_URL}/api/deployments/destroy
    with the correct JSON body containing name."""
    response_data = {
        "id": 10,
        "name": "DEV-01",
        "status": "destroying",
        "workflow_run_id": 55555,
    }
    mock_response = _make_mock_response(status_code=200, json_data=response_data)
    mock_client = _make_mock_client(post_response=mock_response)

    with patch("saorsa_deploy_mcp.server.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client

        result = await destroy_deployment(name="DEV-01")

    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "http://test-registry:5000/api/deployments/destroy"

    body = call_args[1]["json"]
    assert body["name"] == "DEV-01"

    assert "DEV-01" in result
    assert "destroying" in result


@pytest.mark.asyncio
async def test_destroy_deployment_reports_error():
    """When the API returns an error, the result contains the error message."""
    error_data = {"error": "Deployment 'NOPE' not found"}
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.json.return_value = error_data
    mock_response.headers = {"content-type": "application/json"}

    import httpx

    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Not Found", request=MagicMock(), response=mock_response
    )
    mock_client = _make_mock_client(post_response=mock_response)

    with patch("saorsa_deploy_mcp.server.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client

        result = await destroy_deployment(name="NOPE")

    assert "Failed to destroy" in result
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_track_deployment_reports_destroyed_status():
    """When a deployment has been destroyed, the result includes 'has finished
    with status: destroyed'."""
    response_data = {
        "id": 11,
        "name": "DEV-11",
        "status": "destroyed",
        "workflow_run_id": 66666,
        "events": [],
    }
    mock_response = _make_mock_response(status_code=200, json_data=response_data)
    mock_client = _make_mock_client(get_response=mock_response)

    with patch("saorsa_deploy_mcp.server.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client

        result = await track_deployment(deployment_id=11)

    assert "has finished with status: destroyed" in result


@pytest.mark.asyncio
async def test_track_deployment_reports_destroying_in_progress():
    """When a deployment is destroying, the result indicates it is still
    in progress."""
    response_data = {
        "id": 12,
        "name": "DEV-12",
        "status": "destroying",
        "workflow_run_id": 77777,
        "events": [],
    }
    mock_response = _make_mock_response(status_code=200, json_data=response_data)
    mock_client = _make_mock_client(get_response=mock_response)

    with patch("saorsa_deploy_mcp.server.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client

        result = await track_deployment(deployment_id=12)

    assert "still destroying" in result
