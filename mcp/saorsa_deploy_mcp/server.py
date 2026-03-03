import functools
import os
import sys

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://localhost:5000")
REGISTRY_API_TOKEN = os.environ.get("REGISTRY_API_TOKEN", "")


class RegistryClient:
    """Thin async wrapper around the testnet registry REST API."""

    def __init__(self, base_url: str = REGISTRY_URL, token: str = REGISTRY_API_TOKEN):
        self.base_url = base_url
        self.token = token

    def _headers(self) -> dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    async def get(self, path: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=60.0, headers=self._headers()) as client:
            resp = await client.get(f"{self.base_url}{path}")
            resp.raise_for_status()
            return resp

    async def post(self, path: str, json: dict) -> httpx.Response:
        async with httpx.AsyncClient(timeout=60.0, headers=self._headers()) as client:
            resp = await client.post(f"{self.base_url}{path}", json=json)
            resp.raise_for_status()
            return resp


registry = RegistryClient()


def handle_api_errors(fn):
    """Decorator that catches httpx errors and returns formatted strings."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            ct = e.response.headers.get("content-type", "")
            if ct.startswith("application/json"):
                body = e.response.json().get("error", str(e))
            else:
                body = e.response.text
            return f"API error ({e.response.status_code}): {body}"
        except httpx.ConnectError:
            return (
                f"Failed to connect to registry at {registry.base_url}. "
                "Is the server running?"
            )

    return wrapper


mcp = FastMCP(
    "saorsa-testnet-registry",
    instructions=(
        "Use this server when the user asks to deploy, launch, or create a testnet. "
        "The create_deployment tool triggers a deployment workflow. "
        "The get_deployment tool checks deployment details and progress. "
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

- **disable_log_forwarding**: Disable Telegraf log forwarding to Elasticsearch (default: false)
  - Log forwarding is enabled by default on all deployments
  - Set to true to skip Telegraf installation and configuration
  - Logs are still written as JSON to disk regardless of this setting

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

After creating a deployment, use the get_deployment tool with the
deployment ID to monitor progress. The background monitor polls GitHub
Actions every 60 seconds and updates the deployment status automatically.

Deployment statuses: pending -> running (success) or failed.

## Destroying Deployments

Use the destroy_deployment tool with the deployment name to tear down
infrastructure. The deployment must be in 'running' state.

The destroy process:
1. Dispatches the destroy-testnet.yml workflow
2. Sets status to 'destroying'
3. Monitor tracks the workflow and sets status to 'destroyed' on success

Destroy statuses: running -> destroying -> destroyed (success) or failed.
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
@handle_api_errors
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
    disable_log_forwarding: bool = False,
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
        disable_log_forwarding: Disable Telegraf log forwarding to Elasticsearch (default: false)
    """
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
    if disable_log_forwarding:
        deploy_args["disable_log_forwarding"] = True

    resp = await registry.post(
        "/api/deployments",
        json={"name": name, "deploy_args": deploy_args, "branch": "main"},
    )
    data = resp.json()
    return (
        f"Created deployment '{data['name']}' (id={data['id']})\n"
        f"  Status: {data['status']}\n"
        f"  Workflow Run ID: {data['workflow_run_id']}"
    )


@mcp.tool()
@handle_api_errors
async def destroy_deployment(name: str) -> str:
    """Destroy a testnet deployment and tear down its infrastructure.

    Dispatches the destroy workflow for the named deployment. The deployment
    must be in 'running' state.

    Args:
        name: Name of the deployment to destroy (e.g. "DEV-01")
    """
    resp = await registry.post("/api/deployments/destroy", json={"name": name})
    data = resp.json()
    return (
        f"Destroy triggered for deployment '{data['name']}' (id={data['id']})\n"
        f"  Status: {data['status']}\n"
        f"  Workflow Run ID: {data['workflow_run_id']}"
    )


@mcp.tool()
@handle_api_errors
async def list_deployments() -> str:
    """List all testnet deployments."""
    resp = await registry.get("/api/deployments")
    deployments = resp.json()
    if not deployments:
        return "No deployments found."
    lines = []
    for d in deployments:
        lines.append(
            f"  {d['id']}: {d['name']} ({d['status']}) - created {d['created_at']}"
        )
    return f"Deployments ({len(deployments)}):\n" + "\n".join(lines)


@mcp.tool()
@handle_api_errors
async def get_deployment(deployment_id: int) -> str:
    """Get details and track progress of a specific deployment.

    Returns status, workflow info, and events. Use this to poll for
    deployment completion — terminal statuses (failed, destroyed) indicate
    the deployment workflow has finished with an error or was torn down.

    Args:
        deployment_id: The deployment ID
    """
    resp = await registry.get(f"/api/deployments/{deployment_id}")
    d = resp.json()
    status = d["status"]

    lines = [
        f"Deployment: {d['name']}",
        f"  ID: {d['id']}",
        f"  Status: {status}",
        f"  Workflow Run ID: {d['workflow_run_id']}",
        f"  Created: {d['created_at']}",
        f"  Updated: {d['updated_at']}",
    ]

    events = d.get("events", [])
    if events:
        lines.append(f"  Events ({len(events)}):")
        for e in events:
            lines.append(f"    [{e['created_at']}] {e['event_type']}")
        latest = events[-1]
        if latest.get("details") and "html_url" in latest["details"]:
            lines.append(f"  Workflow URL: {latest['details']['html_url']}")

    if status in ("failed", "destroyed"):
        lines.append(f"\nDeployment has finished with status: {status}")
    elif status == "running" and any(e["event_type"] == "completed" for e in events):
        lines.append("\nDeployment is running (workflow completed successfully).")
    elif status not in ("pending",):
        lines.append(
            f"\nDeployment is still {status}. Poll again to check for updates."
        )

    return "\n".join(lines)


if __name__ == "__main__":
    print("Starting Saorsa MCP server...", file=sys.stderr)
    mcp.run(transport="stdio")
