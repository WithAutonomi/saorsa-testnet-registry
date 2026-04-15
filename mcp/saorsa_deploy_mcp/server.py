import os
import subprocess
import sys

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from saorsa_db.db import get_session_factory
from saorsa_db.models import DeploymentStatus
from saorsa_db.repositories import SqlAlchemyDeploymentRepository

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://saorsa_testnet:saorsa_testnet@localhost:5432/saorsa_testnet_registry",
)
SAORSA_DEPLOY_CMD = os.environ.get("SAORSA_DEPLOY_CMD", "saorsa-deploy")

_session_factory = get_session_factory(DATABASE_URL)


def _get_repo() -> SqlAlchemyDeploymentRepository:
    session = _session_factory()
    return SqlAlchemyDeploymentRepository(session)


def _run_deploy(args: list[str]) -> tuple[int, str, str]:
    """Run a saorsa-deploy command and return (returncode, stdout, stderr)."""
    cmd = [SAORSA_DEPLOY_CMD] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": DATABASE_URL},
    )
    return result.returncode, result.stdout, result.stderr


mcp = FastMCP(
    "saorsa-testnet-registry",
    instructions=(
        "Use this server when the user asks to deploy, launch, or create a testnet. "
        "The create_deployment tool triggers a deployment by running saorsa-deploy directly. "
        "The get_deployment tool checks deployment details and progress. "
        "Read the testnet://docs/deployment-config resource for parameter reference. "
        "Common requests: 'deploy a testnet', 'launch DEV-01', 'create a network', "
        "'check deployment status', 'wait for deployment to complete'. "
        "The destroy_deployment tool triggers destruction of a testnet."
    ),
)

DEPLOYMENT_CONFIG_DOCS = """# Testnet Deployment Configuration Reference

## Required Parameters

- **name**: Deployment name used as a prefix in VM names
  - Use DEV-01 through DEV-08 for development testnets
  - Must be unique across all active deployments

- **node_count**: Number of node services to run per VM
  - Typical values: 10-50 depending on VM size

- **genesis_port**: Port the genesis node will listen on
  - This port is used to construct the bootstrap address for other nodes
  - Typical value: 12000

## Provider Configuration

At least one provider must have vm_count > 0. Each provider has two
parameters: vm_count (VMs per region) and regions (number of regions).

- **do_vm_count**: Number of VMs per region on Digital Ocean (default: 0)
- **do_regions**: Number of Digital Ocean regions to use (default: 3)
  - Available: lon1, nyc1, ams3, sfo3, sgp1, blr1, fra1, tor1

- **hetzner_vm_count**: Number of VMs per region on Hetzner (default: 0)
  - Note: Hetzner accounts typically have a max of 50 VMs
- **hetzner_regions**: Number of Hetzner regions to use (default: 3)
  - Available: fsn1, nbg1, hel1, ash, hil

- **vultr_vm_count**: Number of VMs per region on Vultr (default: 0)
- **vultr_regions**: Number of Vultr regions to use (default: 3)
  - Available: ewr, ord, dfw, lax, ams, lhr, fra, sjc, syd, nrt, cdg, sgp

## Optional Parameters

- **testnet**: Run nodes with --network-mode testnet (default: true)
  - Applies to provision and provision-genesis commands

- **attached_volume_size**: Size of attached storage volume in GB (default: 20)
  - Increase for testnets that will store significant data

- **log_level**: Logging level for all nodes
  - Common values: "trace", "debug", "info", "warn", "error"

- **port**: Beginning of a port range for non-genesis nodes
  - If omitted, random ports are assigned
  - When set, nodes use ports from PORT to PORT+N

- **enable_log_forwarding**: Enable Telegraf log forwarding to Elasticsearch (default: false)
  - When true, an ephemeral Elasticsearch instance is provisioned and Telegraf is configured to forward logs
  - Logs are always written as JSON to disk regardless of this setting

- **upgrade_channel**: Upgrade channel for ant-node
  - Allowed values: "beta", "stable"
  - If omitted, no upgrade channel is specified and the node uses its default behaviour
  - When set, all nodes are started with `--upgrade-channel <value>`

- **rewards_address**: Arbitrum wallet address for node rewards
  - Default: "0x03B770D9cD32077cC0bF330c13C114a87643B124"
  - All nodes are started with `--rewards-address <value>`

## Node Binary Options

These options control which ant-node binary is deployed. They are
**mutually exclusive** — you may specify `node_version` OR
`branch_name`/`repo_owner`, but not both. If neither is set, the latest
released binary is used.

- **node_version**: Deploy a specific released version of ant-node
  - Example: "0.3.3"
  - The binary is fetched from the GitHub release for this version

- **branch_name**: Git branch of the ant-node repo to build from source
  - Example: "feat/my-change"
  - Triggers a build step that compiles the binary on an ephemeral VM
  - Must NOT be combined with node_version

- **repo_owner**: Repository owner for custom branch builds (default: "maidsafe")
  - Only used when branch_name is set
  - Example: "maidsafe" or a fork owner

## Examples

### Minimal development testnet (Digital Ocean only)
name=DEV-01, do_vm_count=1, do_regions=1, node_count=10, genesis_port=12000

### Multi-provider testnet
name=DEV-02, do_vm_count=5, do_regions=3, hetzner_vm_count=3, hetzner_regions=2,
vultr_vm_count=4, vultr_regions=3, node_count=25, genesis_port=12000

### Hetzner-only testnet (respecting 50 VM limit)
name=DEV-03, hetzner_vm_count=10, hetzner_regions=5, node_count=20, genesis_port=12000

## Tracking Deployments

After creating a deployment, use the get_deployment tool with the
deployment ID to monitor progress. The deployment status is updated
directly by saorsa-deploy as each step completes.

Deployment statuses: pending -> running (success) or failed.

## Destroying Deployments

Use the destroy_deployment tool with the deployment name to tear down
infrastructure. The deployment must be in 'running' or 'stopped' state.

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
def create_deployment(
    name: str,
    node_count: int,
    genesis_port: int,
    do_vm_count: int = 0,
    do_regions: int = 3,
    hetzner_vm_count: int = 0,
    hetzner_regions: int = 3,
    vultr_vm_count: int = 0,
    vultr_regions: int = 3,
    testnet: bool = True,
    attached_volume_size: int | None = None,
    log_level: str | None = None,
    port: int | None = None,
    node_version: str | None = None,
    branch_name: str | None = None,
    repo_owner: str | None = None,
    enable_log_forwarding: bool = False,
    upgrade_channel: str | None = None,
    rewards_address: str | None = None,
) -> str:
    """Create a new testnet deployment by running saorsa-deploy commands.

    This runs the full deployment sequence: infra -> provision-genesis -> provision.
    The deployment is recorded in the database automatically by saorsa-deploy.

    Read the testnet://docs/deployment-config resource for full parameter
    documentation, defaults, and usage examples.

    At least one provider must have vm_count > 0.

    Args:
        name: Unique deployment name (e.g. "DEV-01")
        node_count: Number of node services per VM
        genesis_port: Port the genesis node will run on
        do_vm_count: Number of VMs per region on Digital Ocean (0 = don't use)
        do_regions: Number of Digital Ocean regions (default: 3)
        hetzner_vm_count: Number of VMs per region on Hetzner (0 = don't use)
        hetzner_regions: Number of Hetzner regions (default: 3)
        vultr_vm_count: Number of VMs per region on Vultr (0 = don't use)
        vultr_regions: Number of Vultr regions (default: 3)
        testnet: Run nodes with --network-mode testnet (default: true)
        attached_volume_size: Size of attached volume in GB
        log_level: Logging level the nodes will run with
        port: Beginning of a port range for non-genesis nodes
        node_version: Specific released version of ant-node to deploy (e.g. "0.3.3")
        branch_name: Git branch of the ant-node repo to build from source
        repo_owner: Repository owner for custom branch builds (default: "maidsafe")
        enable_log_forwarding: Enable Telegraf log forwarding to Elasticsearch (default: false)
        upgrade_channel: Upgrade channel for ant-node ("beta" or "stable")
        rewards_address: Arbitrum wallet address for node rewards
    """
    if do_vm_count <= 0 and hetzner_vm_count <= 0 and vultr_vm_count <= 0:
        return (
            "Error: At least one provider must have vm_count > 0. "
            "Set do_vm_count, hetzner_vm_count, or vultr_vm_count."
        )

    output_lines = []

    # Step 1: Build binary if branch_name specified
    if branch_name:
        owner = repo_owner or "maidsafe"
        build_args = [
            "build-ant-node-binary",
            "--name", name,
            "--branch-name", branch_name,
            "--repo-owner", owner,
        ]
        output_lines.append(f"Building ant-node from {owner}/{branch_name}...")
        rc, stdout, stderr = _run_deploy(build_args)
        if rc != 0:
            return f"Build failed (exit code {rc}):\n{stderr}\n{stdout}"
        output_lines.append("Build completed.")

    # Step 2: Infrastructure
    infra_args = ["infra", "--name", name]
    if do_vm_count > 0:
        infra_args.extend(["--do-vm-count", str(do_vm_count)])
        infra_args.extend(["--do-regions", str(do_regions)])
    if hetzner_vm_count > 0:
        infra_args.extend(["--hetzner-vm-count", str(hetzner_vm_count)])
        infra_args.extend(["--hetzner-regions", str(hetzner_regions)])
    if vultr_vm_count > 0:
        infra_args.extend(["--vultr-vm-count", str(vultr_vm_count)])
        infra_args.extend(["--vultr-regions", str(vultr_regions)])
    if attached_volume_size is not None:
        infra_args.extend(["--attached-volume-size", str(attached_volume_size)])

    output_lines.append("Provisioning infrastructure...")
    rc, stdout, stderr = _run_deploy(infra_args)
    if rc != 0:
        return f"Infrastructure provisioning failed (exit code {rc}):\n{stderr}\n{stdout}"
    output_lines.append("Infrastructure ready.")

    # Step 3: Provision genesis
    genesis_args = [
        "provision-genesis",
        "--name", name,
        "--port", str(genesis_port),
    ]
    if testnet:
        genesis_args.append("--testnet")
    if log_level:
        genesis_args.extend(["--log-level", log_level])
    if node_version:
        genesis_args.extend(["--node-version", node_version])
    if branch_name:
        genesis_args.extend(["--branch-name", branch_name])
        genesis_args.extend(["--repo-owner", repo_owner or "maidsafe"])
    if upgrade_channel:
        genesis_args.extend(["--upgrade-channel", upgrade_channel])
    if rewards_address:
        genesis_args.extend(["--rewards-address", rewards_address])

    output_lines.append("Provisioning genesis node...")
    rc, stdout, stderr = _run_deploy(genesis_args)
    if rc != 0:
        return f"Genesis provisioning failed (exit code {rc}):\n{stderr}\n{stdout}"
    output_lines.append("Genesis node ready.")

    # Step 4: Provision remaining nodes
    provision_args = [
        "provision",
        "--name", name,
        "--node-count", str(node_count),
    ]
    if testnet:
        provision_args.append("--testnet")
    if log_level:
        provision_args.extend(["--log-level", log_level])
    if port:
        provision_args.extend(["--port", str(port)])
    if node_version:
        provision_args.extend(["--node-version", node_version])
    if branch_name:
        provision_args.extend(["--branch-name", branch_name])
        provision_args.extend(["--repo-owner", repo_owner or "maidsafe"])
    if upgrade_channel:
        provision_args.extend(["--upgrade-channel", upgrade_channel])
    if rewards_address:
        provision_args.extend(["--rewards-address", rewards_address])

    output_lines.append("Provisioning nodes...")
    rc, stdout, stderr = _run_deploy(provision_args)
    if rc != 0:
        return f"Node provisioning failed (exit code {rc}):\n{stderr}\n{stdout}"
    output_lines.append("All nodes provisioned.")

    # Step 5: Optional log forwarding
    if enable_log_forwarding:
        for step_name, step_args in [
            ("elastic-infra", ["elastic-infra", "--name", name]),
            ("provision-elastic", ["provision-elastic", "--name", name]),
            ("provision-telegraf-logs", ["provision-telegraf-logs", "--name", name]),
        ]:
            output_lines.append(f"Running {step_name}...")
            rc, stdout, stderr = _run_deploy(step_args)
            if rc != 0:
                output_lines.append(
                    f"Warning: {step_name} failed (exit code {rc}), "
                    "but deployment is running."
                )
                break
            output_lines.append(f"{step_name} completed.")

    # Read final state from DB
    repo = _get_repo()
    deployment = repo.get_by_name(name)
    if deployment:
        output_lines.append(
            f"\nDeployment '{deployment.name}' (id={deployment.id}) "
            f"is {deployment.status.value}."
        )

    return "\n".join(output_lines)


@mcp.tool()
def destroy_deployment(name: str) -> str:
    """Destroy a testnet deployment and tear down its infrastructure.

    Runs saorsa-deploy destroy for the named deployment. The deployment
    status is updated in the database automatically.

    Args:
        name: Name of the deployment to destroy (e.g. "DEV-01")
    """
    rc, stdout, stderr = _run_deploy(["destroy", "--name", name, "--force"])
    if rc != 0:
        return f"Destroy failed (exit code {rc}):\n{stderr}\n{stdout}"

    return f"Deployment '{name}' destroyed successfully."


@mcp.tool()
def list_deployments() -> str:
    """List all testnet deployments."""
    repo = _get_repo()
    deployments = repo.list_all()
    if not deployments:
        return "No deployments found."
    lines = []
    for d in deployments:
        lines.append(
            f"  {d.id}: {d.name} ({d.status.value}) - created {d.created_at.isoformat()}"
        )
    return f"Deployments ({len(deployments)}):\n" + "\n".join(lines)


@mcp.tool()
def get_deployment(deployment_id: int) -> str:
    """Get details and track progress of a specific deployment.

    Returns status, configuration, and event history.

    Args:
        deployment_id: The deployment ID
    """
    repo = _get_repo()
    d = repo.get(deployment_id)
    if d is None:
        return f"Deployment {deployment_id} not found."

    lines = [
        f"Deployment: {d.name}",
        f"  ID: {d.id}",
        f"  Status: {d.status.value}",
        f"  Created: {d.created_at.isoformat()}",
        f"  Updated: {d.updated_at.isoformat()}",
    ]

    if d.deploy_args:
        lines.append("  Config:")
        for key, value in d.deploy_args.items():
            lines.append(f"    {key}: {value}")

    events = d.events
    if events:
        lines.append(f"  Events ({len(events)}):")
        for e in events:
            lines.append(f"    [{e.created_at.isoformat()}] {e.event_type}")

    if d.status == DeploymentStatus.FAILED:
        lines.append("\nDeployment has failed.")
    elif d.status == DeploymentStatus.DESTROYED:
        lines.append("\nDeployment has been destroyed.")
    elif d.status == DeploymentStatus.RUNNING:
        lines.append("\nDeployment is running.")
    elif d.status == DeploymentStatus.PENDING:
        lines.append("\nDeployment is still being provisioned.")

    return "\n".join(lines)


if __name__ == "__main__":
    print("Starting Saorsa MCP server...", file=sys.stderr)
    mcp.run(transport="stdio")
