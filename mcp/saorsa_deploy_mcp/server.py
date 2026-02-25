import os
import sys

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://localhost:5000")

mcp = FastMCP("saorsa-testnet-registry")


@mcp.tool()
async def create_deployment(name: str, status: str = "pending") -> str:
    """Create a new testnet deployment.

    Args:
        name: Unique deployment name (e.g. "DEV-01")
        status: Initial status - one of: pending, running, stopped, failed
    """
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{REGISTRY_URL}/api/deployments",
                json={"name": name, "status": status},
            )
            resp.raise_for_status()
            data = resp.json()
            return f"Created deployment '{data['name']}' (id={data['id']}, status={data['status']})"
        except httpx.HTTPStatusError as e:
            error_body = e.response.json() if e.response.headers.get("content-type", "").startswith("application/json") else {"error": e.response.text}
            return f"Failed to create deployment: {error_body.get('error', str(e))}"
        except httpx.ConnectError:
            return f"Failed to connect to registry at {REGISTRY_URL}. Is the server running?"


@mcp.tool()
async def list_deployments() -> str:
    """List all testnet deployments."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{REGISTRY_URL}/api/deployments")
            resp.raise_for_status()
            deployments = resp.json()
            if not deployments:
                return "No deployments found."
            lines = []
            for d in deployments:
                lines.append(f"  {d['id']}: {d['name']} ({d['status']}) - created {d['created_at']}")
            return f"Deployments ({len(deployments)}):\n" + "\n".join(lines)
        except httpx.ConnectError:
            return f"Failed to connect to registry at {REGISTRY_URL}. Is the server running?"


@mcp.tool()
async def get_deployment(deployment_id: int) -> str:
    """Get details of a specific deployment.

    Args:
        deployment_id: The deployment ID
    """
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{REGISTRY_URL}/api/deployments/{deployment_id}")
            if resp.status_code == 404:
                return f"Deployment {deployment_id} not found."
            resp.raise_for_status()
            d = resp.json()
            return (
                f"Deployment: {d['name']}\n"
                f"  ID: {d['id']}\n"
                f"  Status: {d['status']}\n"
                f"  Created: {d['created_at']}\n"
                f"  Updated: {d['updated_at']}"
            )
        except httpx.ConnectError:
            return f"Failed to connect to registry at {REGISTRY_URL}. Is the server running?"


if __name__ == "__main__":
    print("Starting Saorsa MCP server...", file=sys.stderr)
    mcp.run(transport="stdio")
