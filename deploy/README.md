# saorsa-deploy

CLI tool for deploying testnets for [saorsa-node](https://github.com/jacderida/saorsa-node) using Terraform and Pyinfra.

## Prerequisites

- Python >= 3.10
- [uv](https://docs.astral.sh/uv/) for package management
- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.0
- A Digital Ocean account with API token

## Installation

```bash
uv sync
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `DO_TOKEN` | Digital Ocean API token (required) |
| `AWS_ACCESS_KEY_ID` | AWS credentials for Terraform state backend and deployment state |
| `AWS_SECRET_ACCESS_KEY` | AWS credentials for Terraform state backend and deployment state |
| `SAORSA_BUILD_AWS_ACCESS_KEY_ID` | AWS credentials for uploading custom-built binaries (only for `build-saorsa-node-binary`) |
| `SAORSA_BUILD_AWS_SECRET_ACCESS_KEY` | AWS credentials for uploading custom-built binaries (only for `build-saorsa-node-binary`) |

Terraform state and deployment metadata are stored in the `maidsafe-org-infra-tfstate` S3 bucket (region `eu-west-2`). AWS credentials are resolved via the standard boto3 credential chain (environment variables, `~/.aws/credentials`, or IAM roles).

The `SAORSA_BUILD_AWS_*` credentials are for the `saorsa-build-uploader` IAM user, which has `s3:PutObject` on the `saorsa-node-builds` bucket. Create the access key manually after applying the Terraform in `saorsa_deploy/resources/aws-build-infra/`.

## Usage

### `infra` command

Provision VMs with attached storage volumes using Terraform.

```bash
uv run saorsa-deploy infra --name DEV-01 --node-count 10 --vm-count 3 --testnet
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--attached-volume-size` | int | No | 20 | Size of attached volume in GB |
| `--name` | string | Yes | - | Deployment name (used as prefix in VM names) |
| `--node-count` | int | Yes | - | Number of nodes per VM |
| `--region-counts` | string | No | 3 | Comma-separated region counts per provider |
| `--testnet` | flag | No | - | Testnet mode: Digital Ocean only, lon1 region |
| `--vm-count` | int | Yes | - | Number of VMs per provider per region |

#### Examples

Deploy a testnet (single provider, single region):
```bash
uv run saorsa-deploy infra --name DEV-01 --node-count 10 --vm-count 3 --testnet
```

Deploy across 2 regions with larger volumes:
```bash
uv run saorsa-deploy infra --name PERF-05 --node-count 10 --vm-count 5 --region-counts 2 --attached-volume-size 50
```

### `provision-genesis` command

Provision the genesis node on the bootstrap VM.

```bash
uv run saorsa-deploy provision-genesis --name DEV-01 --port 12000
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--branch-name` | string | No | - | Use custom-built binary from this branch (requires `--repo-owner`) |
| `--ip-version` | string | No | - | IP version: `v4` or `v6` |
| `--log-level` | string | No | - | Logging level for the node |
| `--name` | string | Yes | - | Deployment name (must match `infra`) |
| `--node-version` | string | No | - | Specific release version (e.g., `0.2.0`) |
| `--port` | int | Yes | - | Port for the genesis node |
| `--repo-owner` | string | No | - | GitHub repo owner (requires `--branch-name`) |
| `--ssh-key-path` | string | No | `~/.ssh/id_rsa` | SSH key for provisioning |
| `--testnet` | flag | No | - | Run with `--testnet` flag |
| `--upgrade-channel` | string | No | - | Upgrade channel: `beta` or `stable` |

### `provision` command

Provision node services on all VMs (or a specific region).

```bash
uv run saorsa-deploy provision --name DEV-01 --node-count 10
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--branch-name` | string | No | - | Use custom-built binary from this branch (requires `--repo-owner`) |
| `--ip-version` | string | No | - | IP version: `v4` or `v6` |
| `--log-level` | string | No | - | Logging level for the nodes |
| `--name` | string | Yes | - | Deployment name (must match `infra`) |
| `--node-count` | int | Yes | - | Number of node services per VM |
| `--node-version` | string | No | - | Specific release version (e.g., `0.2.0`) |
| `--port` | int | No | - | Beginning of port range (omit for random) |
| `--region` | string | No | - | Provision only VMs in this region |
| `--repo-owner` | string | No | - | GitHub repo owner (requires `--branch-name`) |
| `--ssh-key-path` | string | No | `~/.ssh/id_rsa` | SSH key for provisioning |
| `--testnet` | flag | No | - | Run with `--testnet` flag |
| `--upgrade-channel` | string | No | - | Upgrade channel: `beta` or `stable` |

#### Binary version selection

By default, provisioning uses the latest GitHub release. You can override this:

```bash
# Use a specific release version
uv run saorsa-deploy provision-genesis --name DEV-01 --port 12000 --node-version 0.2.0

# Use a custom-built binary (must run build-saorsa-node-binary first)
uv run saorsa-deploy provision --name DEV-01 --node-count 10 --branch-name feature-x --repo-owner myorg
```

`--node-version` and `--branch-name`/`--repo-owner` are mutually exclusive. `--branch-name` and `--repo-owner` must be used together.

### `build-saorsa-node-binary` command

Build saorsa-node from a Git branch on an ephemeral DO droplet and upload the binary to S3.

```bash
uv run saorsa-deploy build-saorsa-node-binary --branch-name feature-x --repo-owner myorg
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--branch-name` | string | Yes | - | Git branch to build from |
| `--repo-owner` | string | Yes | - | GitHub repository owner |
| `--ssh-key-path` | string | No | `~/.ssh/id_rsa` | SSH key for provisioning the build VM |

This command:
1. Creates a `c-16` (16 vCPU, 32GB RAM) DO droplet in lon1
2. Installs Rust, clones the repo, builds in release mode
3. Uploads the binary to `s3://saorsa-node-builds/builds/{owner}/{branch}/saorsa-node`
4. Destroys the droplet (even on failure)

Requires `SAORSA_BUILD_AWS_ACCESS_KEY_ID` and `SAORSA_BUILD_AWS_SECRET_ACCESS_KEY`.

#### AWS Build Infrastructure Setup

The S3 bucket and IAM user are managed by Terraform:

```bash
cd saorsa_deploy/resources/aws-build-infra/
terraform init
terraform apply
# Then create the access key manually:
aws iam create-access-key --user-name saorsa-build-uploader
```

### `destroy` command

Tear down all infrastructure for a named deployment. Reads deployment metadata from S3 (saved automatically by the `infra` command), so you only need to specify the deployment name.

```bash
uv run saorsa-deploy destroy --name DEV-01
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--force` | flag | No | - | Skip the confirmation prompt |
| `--name` | string | Yes | - | Deployment name to destroy |

#### Examples

Destroy a deployment (with confirmation prompt):
```bash
uv run saorsa-deploy destroy --name DEV-01
```

Destroy without confirmation (for CI/CD pipelines):
```bash
uv run saorsa-deploy destroy --name DEV-01 --force
```

### `provision-telegraf-logs` command

Install and configure Telegraf for Elasticsearch log forwarding on all testnet VMs. Formerly named `provision-telegraf`.

```bash
uv run saorsa-deploy provision-telegraf-logs --name DEV-01 \
  --elasticsearch-url https://es.example.com \
  --elasticsearch-username user \
  --elasticsearch-password pass
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--name` | string | Yes | - | Deployment name |
| `--elasticsearch-url` | string | Yes | - | Elasticsearch endpoint URL |
| `--elasticsearch-username` | string | Yes | - | Elasticsearch username |
| `--elasticsearch-password` | string | Yes | - | Elasticsearch password |
| `--node-version` | string | No | - | Version of the deployed binary |
| `--branch-commit` | string | No | - | Git branch or commit |
| `--repo-owner` | string | No | `maidsafe` | Repository owner |
| `--ssh-key-path` | string | No | `~/.ssh/id_rsa` | SSH key for provisioning |

## Monitoring

Optional per-testnet monitoring stack: InfluxDB 2.x + Grafana on a single VM, with Telegraf forwarding host-level metrics (CPU, memory, disk, network) from testnet nodes.

The monitoring stack is standalone — it can be used by deployments from this repository or external setups.

### `monitoring-infra` command

Create a monitoring VM with attached storage using Terraform.

```bash
uv run saorsa-deploy monitoring-infra --name DEV-01
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--force` | flag | No | - | Delete stale Terraform state and proceed |
| `--name` | string | Yes | - | Monitoring stack name |
| `--region` | string | No | `lon1` | Region for the monitoring VM |
| `--volume-size` | int | No | 50 | Attached volume size in GB (for InfluxDB data) |

### `provision-monitoring` command

Install and configure InfluxDB 2.x and Grafana on the monitoring VM.

```bash
uv run saorsa-deploy provision-monitoring --name DEV-01
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--name` | string | Yes | - | Monitoring stack name (must match `monitoring-infra`) |
| `--ssh-key-path` | string | No | `~/.ssh/id_rsa` | SSH key for provisioning |

On completion, prints connection details including InfluxDB URL, API token, Grafana URL, and a ready-to-paste `provision-telegraf-metrics` command.

### `provision-telegraf-metrics` command

Install Telegraf for InfluxDB metrics forwarding on testnet VMs. Collects host-level metrics: CPU, memory, disk space (all mount points), and network bandwidth.

```bash
# Using deployment state (loads IPs from S3)
uv run saorsa-deploy provision-telegraf-metrics --name DEV-01 \
  --influxdb-url http://1.2.3.4:8086 \
  --influxdb-token TOKEN

# Using explicit host IPs (for external setups)
uv run saorsa-deploy provision-telegraf-metrics \
  --host-ips 5.6.7.8,9.10.11.12 \
  --influxdb-url http://1.2.3.4:8086 \
  --influxdb-token TOKEN
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--name` | string | No | - | Deployment name (loads VM IPs from deployment state) |
| `--host-ips` | string | No | - | Comma-separated host IPs (alternative to `--name`) |
| `--influxdb-url` | string | Yes | - | InfluxDB URL |
| `--influxdb-token` | string | Yes | - | InfluxDB API token |
| `--influxdb-org` | string | No | `saorsa` | InfluxDB organization |
| `--influxdb-bucket` | string | No | `testnet-metrics` | InfluxDB bucket |
| `--ssh-key-path` | string | No | `~/.ssh/id_rsa` | SSH key for provisioning |

Either `--name` or `--host-ips` must be provided. The `--host-ips` flag allows provisioning machines from external testnet setups without any deployment state in this repository.

### `monitoring-info` command

Print monitoring stack connection details from S3 state. Useful for retrieving credentials after provisioning, or for getting a ready-to-use Telegraf config for external setups.

```bash
# Print connection summary
uv run saorsa-deploy monitoring-info --name DEV-01

# Emit a complete Telegraf TOML config to stdout
uv run saorsa-deploy monitoring-info --name DEV-01 --telegraf-config > telegraf.conf
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--name` | string | Yes | - | Monitoring stack name |
| `--telegraf-config` | flag | No | - | Emit complete Telegraf TOML config to stdout |

### `destroy-monitoring` command

Tear down monitoring infrastructure.

```bash
uv run saorsa-deploy destroy-monitoring --name DEV-01
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--force` | flag | No | - | Skip confirmation prompt |
| `--name` | string | Yes | - | Monitoring stack name to destroy |

### Monitoring workflow example

```bash
# 1. Create monitoring VM
uv run saorsa-deploy monitoring-infra --name DEV-01

# 2. Install InfluxDB + Grafana (prints connection details)
uv run saorsa-deploy provision-monitoring --name DEV-01

# 3. Install Telegraf on testnet nodes
uv run saorsa-deploy provision-telegraf-metrics --name DEV-01 \
  --influxdb-url http://1.2.3.4:8086 \
  --influxdb-token TOKEN

# 4. View Grafana dashboards at http://1.2.3.4:3000

# 5. Get connection details later
uv run saorsa-deploy monitoring-info --name DEV-01

# 6. Get a portable Telegraf config for external use
uv run saorsa-deploy monitoring-info --name DEV-01 --telegraf-config

# 7. Tear down when done
uv run saorsa-deploy destroy-monitoring --name DEV-01
```

### How it works

The `--name` argument is used as a prefix for all VM names (e.g., `DEV-01-saorsa-node-lon1-1`).

Before provisioning the main infrastructure, the tool creates a **bootstrap VM** (`{name}-saorsa-bootstrap`) via the Digital Ocean API. This single VM (s-2vcpu-4gb, Ubuntu 24.04, lon1) with a 35GB attached volume serves as the bootstrap node for the network.

The tool then uses Terraform to create Digital Ocean droplets (Ubuntu 24.04, s-2vcpu-4gb) with attached block storage volumes. Each VM gets one volume of the specified size, formatted as ext4.

For each provider/region combination, the tool:

1. Copies the Terraform manifests to an isolated workspace directory
2. Runs `terraform init` with a per-region state key
3. Runs `terraform apply` with the appropriate variables

All regions are provisioned in parallel (up to 5 concurrent Terraform runs). A live progress table shows the status of each region with elapsed time. On completion, a summary of total resources created is printed. If any region fails, the full Terraform error output is displayed.

### Supported Providers

Currently only Digital Ocean is supported. The architecture is designed for multiple providers -- adding a new provider involves creating a Terraform manifest directory and registering it in the provider config.

**Digital Ocean regions**: lon1, nyc1, ams3, sfo3, sgp1, blr1, fra1, tor1

## Releasing

Releases use semantic versioning with git tags. A release script handles the version bump, commit, tag, and push:

```bash
uv run scripts/release.py 0.2.0
```

This will:
1. Update the version in `pyproject.toml`
2. Create a `chore(release): v0.2.0` commit
3. Tag the commit as `v0.2.0`
4. Push the commit and tag to the maidsafe remote (`origin` or `upstream`, auto-detected)

The script auto-detects which remote points to `maidsafe/saorsa-deploy`, so it works whether you're running from a fork (where `upstream` = maidsafe) or a direct clone (where `origin` = maidsafe).

To check the current version:
```bash
uv run saorsa-deploy --version
```

## Development

### Running tests

```bash
uv run pytest tests/ -v
```

### Linting and formatting

```bash
uv run ruff format saorsa_deploy/ tests/
uv run ruff check saorsa_deploy/ tests/
```
