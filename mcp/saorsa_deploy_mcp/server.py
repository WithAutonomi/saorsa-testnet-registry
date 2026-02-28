import os
import sys

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://localhost:5000")
REGISTRY_API_TOKEN = os.environ.get("REGISTRY_API_TOKEN", "")


def _auth_headers() -> dict[str, str]:
    """Return authorization headers for registry API calls."""
    if REGISTRY_API_TOKEN:
        return {"Authorization": f"Bearer {REGISTRY_API_TOKEN}"}
    return {}


mcp = FastMCP(
    "saorsa-testnet-registry",
    instructions=(
        "Use this server when the user asks to deploy, launch, or create a testnet. "
        "The create_deployment tool triggers a deployment workflow. "
        "The track_deployment tool checks deployment progress. "
        "Read the testnet://docs/deployment-config resource for parameter reference. "
        "Common requests: 'deploy a testnet', 'launch DEV-01', 'create a network', "
        "'check deployment status', 'wait for deployment to complete'. "
        "The destroy_deployment tool triggers a destroy workflow to tear down infrastructure."
    ),
)

DEPLOYMENT_CONFIG_DOCS = """# Testnet Deployment Configuration Reference

## Required Parameters

- **name**: Deployment name used as a prefix in VM names
  - Use DEV-01 through DEV-08 for development testnets
  - Must be unique across all active deployments

- **vm_count**: Number of VMs to create per cloud provider per region
  - Typical values: 1-5 for dev, 5-20 for larger testnets

- **node_count**: Number of node services to run per VM
  - Typical values: 10-50 depending on VM size

- **genesis_port**: Port the genesis node will listen on
  - This port is used to construct the bootstrap address for other nodes
  - Typical value: 12000

## Optional Parameters

- **testnet**: Whether to run in testnet mode (default: true)
  - When true, uses Digital Ocean only in the lon1 region
  - Nodes are started with the --testnet flag

- **attached_volume_size**: Size of attached storage volume in GB (default: 20)
  - Increase for testnets that will store significant data

- **region_counts**: Comma-separated count of regions to use per provider (default: "3")
  - Example: "3" means 3 regions per cloud provider
  - VMs are distributed across the specified number of regions

- **ip_version**: IP version for nodes to use, either "v4" or "v6"
  - Omit to use the provider default

- **log_level**: Logging level for all nodes
  - Common values: "trace", "debug", "info", "warn", "error"

- **port**: Beginning of a port range for non-genesis nodes
  - If omitted, random ports are assigned
  - When set, nodes use ports from PORT to PORT+N

## Node Binary Options

These options control which saorsa-node binary is deployed. They are
**mutually exclusive** — you may specify `node_version` OR
`branch_name`/`repo_owner`, but not both. If neither is set, the latest
released binary is used.

- **node_version**: Deploy a specific released version of saorsa-node
  - Example: "0.3.3"
  - The binary is fetched from the GitHub release for this version

- **branch_name**: Git branch of the saorsa-node repo to build from source
  - Example: "feat/my-change"
  - Triggers a build step that compiles the binary on an ephemeral VM
  - Must NOT be combined with node_version

- **repo_owner**: Repository owner for custom branch builds (default: "maidsafe")
  - Only used when branch_name is set
  - Example: "maidsafe" or a fork owner

## Examples

### Minimal development testnet
Deploy a testnet called DEV-01 with 1 VM, 10 nodes, genesis port 12000.

### Standard development testnet
Deploy a DEV-02 testnet with 3 VMs, 25 nodes per VM, genesis port 12000,
40GB volumes, log level debug, in testnet mode.

### Deploying with a specific node version
Deploy a DEV-04 testnet with 2 VMs, 10 nodes, genesis port 12000,
using saorsa-node version 0.3.3.

### Deploying with a custom node binary from a branch
Deploy a DEV-05 testnet with 2 VMs, 10 nodes, genesis port 12000,
building saorsa-node from the feat/my-change branch (maidsafe repo).

## Tracking Deployments

After creating a deployment, use the track_deployment tool with the
deployment ID to monitor progress. The background monitor polls GitHub
Actions every 60 seconds and updates the deployment status automatically.

Deployment statuses: pending -> running -> stopped (success) or failed.

## Destroying Deployments

Use the destroy_deployment tool with the deployment name to tear down
infrastructure. The deployment must be in 'running' or 'stopped' state.

The destroy process:
1. Dispatches the destroy-testnet.yml workflow
2. Sets status to 'destroying'
3. Monitor tracks the workflow and sets status to 'destroyed' on success

Destroy statuses: running/stopped -> destroying -> destroyed (success) or failed.
"""


@mcp.resource("testnet://docs/deployment-config")
def get_deployment_config_docs() -> str:
    """Testnet deployment configuration documentation.

    Provides reference documentation for all configuration parameters
    available when deploying testnets, including VM counts, node counts,
    port configuration, and workflow options.
    """
    return DEPLOYMENT_CONFIG_DOCS


@mcp.tool()
async def create_deployment(
    name: str,
    vm_count: int,
    node_count: int,
    genesis_port: int,
    testnet: bool = True,
    attached_volume_size: int | None = None,
    region_counts: str | None = None,
    ip_version: str | None = None,
    log_level: str | None = None,
    port: int | None = None,
    node_version: str | None = None,
    branch_name: str | None = None,
    repo_owner: str | None = None,
) -> str:
    """Create a new testnet deployment and trigger the launch workflow.

    Read the testnet://docs/deployment-config resource for full parameter
    documentation, defaults, and usage examples.

    Args:
        name: Unique deployment name (e.g. "DEV-01")
        vm_count: Number of VMs per provider per region
        node_count: Number of node services per VM
        genesis_port: Port the genesis node will run on
        testnet: Run in testnet mode (default: true)
        attached_volume_size: Size of attached volume in GB
        region_counts: Comma-separated region counts per provider (default: "3")
        ip_version: IP version the nodes will run with (v4 or v6)
        log_level: Logging level the nodes will run with
        port: Beginning of a port range for non-genesis nodes
        node_version: Specific released version of saorsa-node to deploy (e.g. "0.3.3")
        branch_name: Git branch of the saorsa-node repo to build from source
        repo_owner: Repository owner for custom branch builds (default: "maidsafe")
    """
    if node_version is not None and branch_name is not None:
        return (
            "Error: node_version and branch_name are mutually exclusive. "
            "Specify a released version OR a branch to build from, not both."
        )

    deploy_args = {
        "name": name,
        "vm_count": vm_count,
        "node_count": node_count,
        "genesis_port": genesis_port,
        "testnet": testnet,
    }
    if attached_volume_size is not None:
        deploy_args["attached_volume_size"] = attached_volume_size
    if region_counts is not None:
        deploy_args["region_counts"] = region_counts
    if ip_version is not None:
        deploy_args["ip_version"] = ip_version
    if log_level is not None:
        deploy_args["log_level"] = log_level
    if port is not None:
        deploy_args["port"] = port
    if node_version is not None:
        deploy_args["node_version"] = node_version
    if branch_name is not None:
        deploy_args["branch_name"] = branch_name
        deploy_args["repo_owner"] = repo_owner or "maidsafe"

    async with httpx.AsyncClient(timeout=60.0, headers=_auth_headers()) as client:
        try:
            resp = await client.post(
                f"{REGISTRY_URL}/api/deployments",
                json={
                    "name": name,
                    "deploy_args": deploy_args,
                    "branch": "main",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return (
                f"Created deployment '{data['name']}' (id={data['id']})\n"
                f"  Status: {data['status']}\n"
                f"  Workflow Run ID: {data['workflow_run_id']}"
            )
        except httpx.HTTPStatusError as e:
            error_body = (
                e.response.json()
                if e.response.headers.get("content-type", "").startswith(
                    "application/json"
                )
                else {"error": e.response.text}
            )
            return f"Failed to create deployment: {error_body.get('error', str(e))}"
        except httpx.ConnectError:
            return f"Failed to connect to registry at {REGISTRY_URL}. Is the server running?"


@mcp.tool()
async def destroy_deployment(name: str) -> str:
    """Destroy a testnet deployment and tear down its infrastructure.

    Dispatches the destroy workflow for the named deployment. The deployment
    must be in 'running' or 'stopped' state.

    Args:
        name: Name of the deployment to destroy (e.g. "DEV-01")
    """
    async with httpx.AsyncClient(timeout=60.0, headers=_auth_headers()) as client:
        try:
            resp = await client.post(
                f"{REGISTRY_URL}/api/deployments/destroy",
                json={"name": name},
            )
            resp.raise_for_status()
            data = resp.json()
            return (
                f"Destroy triggered for deployment '{data['name']}' (id={data['id']})\n"
                f"  Status: {data['status']}\n"
                f"  Workflow Run ID: {data['workflow_run_id']}"
            )
        except httpx.HTTPStatusError as e:
            error_body = (
                e.response.json()
                if e.response.headers.get("content-type", "").startswith(
                    "application/json"
                )
                else {"error": e.response.text}
            )
            return f"Failed to destroy deployment: {error_body.get('error', str(e))}"
        except httpx.ConnectError:
            return f"Failed to connect to registry at {REGISTRY_URL}. Is the server running?"


@mcp.tool()
async def list_deployments() -> str:
    """List all testnet deployments."""
    async with httpx.AsyncClient(headers=_auth_headers()) as client:
        try:
            resp = await client.get(f"{REGISTRY_URL}/api/deployments")
            resp.raise_for_status()
            deployments = resp.json()
            if not deployments:
                return "No deployments found."
            lines = []
            for d in deployments:
                lines.append(
                    f"  {d['id']}: {d['name']} ({d['status']}) - created {d['created_at']}"
                )
            return f"Deployments ({len(deployments)}):\n" + "\n".join(lines)
        except httpx.ConnectError:
            return f"Failed to connect to registry at {REGISTRY_URL}. Is the server running?"


@mcp.tool()
async def get_deployment(deployment_id: int) -> str:
    """Get details of a specific deployment including events.

    Args:
        deployment_id: The deployment ID
    """
    async with httpx.AsyncClient(headers=_auth_headers()) as client:
        try:
            resp = await client.get(f"{REGISTRY_URL}/api/deployments/{deployment_id}")
            if resp.status_code == 404:
                return f"Deployment {deployment_id} not found."
            resp.raise_for_status()
            d = resp.json()
            lines = [
                f"Deployment: {d['name']}",
                f"  ID: {d['id']}",
                f"  Status: {d['status']}",
                f"  Workflow Run ID: {d['workflow_run_id']}",
                f"  Created: {d['created_at']}",
                f"  Updated: {d['updated_at']}",
            ]
            events = d.get("events", [])
            if events:
                lines.append(f"  Events ({len(events)}):")
                for e in events:
                    lines.append(f"    [{e['created_at']}] {e['event_type']}")
            return "\n".join(lines)
        except httpx.ConnectError:
            return f"Failed to connect to registry at {REGISTRY_URL}. Is the server running?"


@mcp.tool()
async def track_deployment(deployment_id: int) -> str:
    """Track the status of a deployment. Use this to check progress.

    Returns the current status, workflow run ID, and recent events.
    You can poll this tool periodically to wait for deployment completion.

    Args:
        deployment_id: The deployment ID to track
    """
    async with httpx.AsyncClient(headers=_auth_headers()) as client:
        try:
            resp = await client.get(f"{REGISTRY_URL}/api/deployments/{deployment_id}")
            if resp.status_code == 404:
                return f"Deployment {deployment_id} not found."
            resp.raise_for_status()
            d = resp.json()

            status = d["status"]
            lines = [
                f"Deployment '{d['name']}' (id={d['id']})",
                f"  Status: {status}",
                f"  Workflow Run ID: {d['workflow_run_id']}",
            ]

            events = d.get("events", [])
            if events:
                latest = events[-1]
                lines.append(
                    f"  Latest Event: {latest['event_type']} at {latest['created_at']}"
                )
                if latest.get("details"):
                    if "html_url" in latest["details"]:
                        lines.append(f"  Workflow URL: {latest['details']['html_url']}")

            if status in ("stopped", "failed", "destroyed"):
                lines.append(f"\nDeployment has finished with status: {status}")
            else:
                lines.append(
                    f"\nDeployment is still {status}. Poll again to check for updates."
                )

            return "\n".join(lines)
        except httpx.ConnectError:
            return f"Failed to connect to registry at {REGISTRY_URL}. Is the server running?"


if __name__ == "__main__":
    print("Starting Saorsa MCP server...", file=sys.stderr)
    mcp.run(transport="stdio")
