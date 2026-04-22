# saorsa-deploy

CLI tool for deploying testnets for [ant-node](https://github.com/jacderida/ant-node) using Terraform and Pyinfra.

## Prerequisites

- Python >= 3.10
- [uv](https://docs.astral.sh/uv/) for package management
- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.0
- Cloud provider accounts (Vultr, Hetzner, AWS)

## Installation

```bash
uv sync
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `AWS_ACCESS_KEY_ID` | AWS credentials for Terraform state backend and deployment state |
| `AWS_SECRET_ACCESS_KEY` | AWS credentials for Terraform state backend and deployment state |
| `ANT_BUILD_AWS_ACCESS_KEY_ID` | AWS credentials for uploading custom-built binaries (only for `build-ant-node-binary`) |
| `ANT_BUILD_AWS_SECRET_ACCESS_KEY` | AWS credentials for uploading custom-built binaries (only for `build-ant-node-binary`) |

Terraform state and deployment metadata are stored in the `maidsafe-org-infra-tfstate` S3 bucket (region `eu-west-2`). AWS credentials are resolved via the standard boto3 credential chain (environment variables, `~/.aws/credentials`, or IAM roles).

The `ANT_BUILD_AWS_*` credentials are for the `ant-build-uploader` IAM user, which has `s3:PutObject` on the `ant-node-builds` bucket. Create the access key manually after applying the Terraform in `saorsa_deploy/resources/aws-build-infra/`.

## Usage

A deployment is built up in phases. Each phase has its own `infra-*` /
`destroy-*` command so partial-failure recovery is independent:

```
infra-bootstraps   →  creates bootstrap VMs (Digital Ocean)
infra-nodes        →  creates node VMs (DO / Hetzner / Vultr) — requires bootstraps
provision-genesis  →  starts the genesis node on bootstrap #1
provision-bootstraps (optional) — starts extra bootstrap nodes #2..N
provision-nodes    →  installs ant-node binary + writes systemd units + enables services (does NOT start them)
start-nodes        →  starts ant-node services across every node VM (parallel systemctl start)

destroy-nodes      →  tears down node VMs, leaves bootstraps alone
destroy-bootstraps →  tears down bootstraps (refuses while nodes are still up)
```

Run `saorsa-deploy <subcommand> --help` for the authoritative argument list
for each subcommand. The per-phase commands are:

### `infra-bootstraps`

Create only the bootstrap VMs. These live on Digital Ocean (one per region,
geographically distributed) and are what the rest of the testnet peers with.

```bash
uv run saorsa-deploy infra-bootstraps --name DEV-01 \
    --bootstrap-count 7 --attached-volume-size 50
```

Writes the deployment's state file to S3 with `bootstrap_nodes` populated
and `regions` empty. Re-running with the same count is a no-op
(idempotent); a different `--bootstrap-count` errors out — destroy the
existing bootstraps first.

### `infra-nodes`

Create the node (main) VMs across one or more providers. Requires
`infra-bootstraps` to have run first (errors out otherwise). Runs
availability preflights against Hetzner (server-type capacity) and Vultr
(plan-region offering) before touching Terraform, and records the resolved
regions in deployment state **before** `terraform apply` — so a partial
failure still leaves something destroyable.

```bash
uv run saorsa-deploy infra-nodes --name DEV-01 \
    --attached-volume-size 50 \
    --do-vm-count 10 --do-regions 3 --do-droplet-size s-8vcpu-16gb \
    --vultr-vm-count 5 --vultr-regions 3 --vultr-plan vhp-8c-16gb-intel \
    --hetzner-vm-count 2 --hetzner-regions 3 --hetzner-server-type cpx42
```

Each provider has a `--<provider>-regions-list` flag that pins exact regions
(comma-separated) instead of relying on the default top-of-list ordering.
Useful when a region has a transient provider quirk (e.g. Vultr's `dfw`
region often lacks block-storage clusters). When a list flag is set, the
corresponding `--<provider>-regions` count flag is ignored and no fallback
substitution happens — you get exactly the regions you asked for.

```bash
uv run saorsa-deploy infra-nodes --name DEV-01 \
    --attached-volume-size 50 \
    --do-vm-count 10 --do-regions-list lon1,nyc1,ams3 --do-droplet-size s-8vcpu-16gb \
    --vultr-vm-count 5 --vultr-regions-list ewr,ord,lax --vultr-plan vhp-8c-16gb-intel
```

### `provision-genesis` / `provision-bootstraps` / `provision-nodes` / `start-nodes`

Start ant-node services. `provision-genesis` runs the genesis node on
bootstrap #1 (it's the one peer everyone dials). `provision-bootstraps`
starts any additional bootstraps (#2..N). `provision-nodes` installs the
binary, writes systemd unit files, and **enables** the services on every
main-VM. `start-nodes` then brings the services up — the split exists so
that optional setup steps (e.g. NAT simulation) can run between unit-write
and service-start.

```bash
uv run saorsa-deploy provision-genesis --name DEV-01 --port 12000
uv run saorsa-deploy provision-bootstraps --name DEV-01 --port 12000   # if --bootstrap-count > 1
uv run saorsa-deploy provision-nodes --name DEV-01 --node-count 25
uv run saorsa-deploy start-nodes --name DEV-01
```

#### Port-restricted NAT simulation

`provision-nodes` can run a configurable percentage of VMs behind a
simulated port-restricted NAT (Linux network namespace + iptables DNAT /
MASQUERADE). Useful for modelling real-world reachability constraints.

```bash
uv run saorsa-deploy provision-nodes --name DEV-01 --node-count 25 --nat-percent 20
uv run saorsa-deploy start-nodes --name DEV-01
```

Selection is deterministic ("one per region first, then round-robin") and
is recorded in deployment state on first run. Subsequent `provision-nodes`
runs honour the locked selection — `--nat-percent` is ignored unless you
pass `--force-nat-reselect`, which recomputes the set and can flip VMs
between NAT and non-NAT.

On a NAT VM you can verify the rules are in place:

```bash
ssh root@<VM> "iptables -t nat -L PREROUTING -n | grep -c DNAT"   # expect 2
ssh root@<VM> "ip netns list"                                      # expect 'ant_nat'
```

#### Binary version selection

By default provisioning uses the latest GitHub release. Override with:

```bash
# Specific release
uv run saorsa-deploy provision-genesis --name DEV-01 --port 12000 --node-version 0.2.0

# Custom branch build (see build-ant-node-binary)
uv run saorsa-deploy provision-nodes --name DEV-01 --node-count 25 \
    --branch-name feature-x --repo-owner myorg
```

`--node-version` and `--branch-name`/`--repo-owner` are mutually exclusive.
`--branch-name` and `--repo-owner` must be used together.

### `destroy-nodes` / `destroy-bootstraps`

Tear down in reverse phase order: nodes first, bootstraps second.

```bash
uv run saorsa-deploy destroy-nodes --name DEV-01 --force
uv run saorsa-deploy destroy-bootstraps --name DEV-01 --force
```

`destroy-bootstraps` refuses while node state is still populated, and
also refuses while any _extension_ testnet is sharing these bootstraps
(see `extend` below). Both commands have a fallback path that scans S3
for orphan tfstate files when the deployment state is missing, so
interrupted runs can still be cleaned up.

### `extend` / `unextend`

Deploy a second testnet that reuses an existing testnet's bootstrap VMs.
Typical use case: binary-compatibility testing — run DEV-02 with a
different `ant-node` binary alongside DEV-01, peering with the same
bootstrap layer.

```bash
# Seed DEV-02's deployment state from DEV-01's bootstraps
uv run saorsa-deploy extend --name DEV-02 --source DEV-01

# From here on, DEV-02 is a normal deployment — run the usual pipeline,
# skipping the bootstrap/genesis stages:
uv run saorsa-deploy infra-nodes --name DEV-02 --do-vm-count 5 --do-regions 2
uv run saorsa-deploy provision-nodes --name DEV-02 \
    --branch-name feature-x --repo-owner myorg
uv run saorsa-deploy start-nodes --name DEV-02

# Teardown — note: no destroy-bootstraps for an extension
uv run saorsa-deploy destroy-nodes --name DEV-02 --force
uv run saorsa-deploy unextend --name DEV-02 --force
```

`extend` copies `bootstrap_nodes`, `bootstrap_ip`, and `bootstrap_port`
from the source's state into the new testnet's state, and records
`shared_bootstrap_from: <source>`. It's idempotent: re-running with the
same source refreshes the bootstrap fields (useful if the source was
re-provisioned). Re-running with a different source requires `--force`.

`unextend` deletes the extension's deployment state. It refuses if the
extension still has nodes (run `destroy-nodes` first) and it never
touches the source's bootstrap VMs.

`destroy-bootstraps` will refuse to destroy a testnet's bootstraps while
any extension references them — `unextend` each dependent first.
`--force` does not override this check.

### `build-ant-node-binary` command

Build ant-node from a Git branch on an ephemeral DO droplet and upload the binary to S3.

```bash
uv run saorsa-deploy build-ant-node-binary --branch-name feature-x --repo-owner myorg
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
3. Uploads the binary to `s3://ant-node-builds/builds/{owner}/{branch}/ant-node`
4. Destroys the droplet (even on failure)

Requires `ANT_BUILD_AWS_ACCESS_KEY_ID` and `ANT_BUILD_AWS_SECRET_ACCESS_KEY`.

#### AWS Build Infrastructure Setup

The S3 bucket and IAM user are managed by Terraform:

```bash
cd saorsa_deploy/resources/aws-build-infra/
terraform init
terraform apply
# Then create the access key manually:
aws iam create-access-key --user-name ant-build-uploader
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
| `--influxdb-org` | string | No | `ant` | InfluxDB organization |
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

The `--name` argument is used as a prefix for all VM names (e.g., `DEV-01-ant-node-lon1-1`).

Before provisioning the main infrastructure, the tool creates a **bootstrap VM** (`{name}-ant-bootstrap`) via the Digital Ocean API. This single VM (s-2vcpu-4gb, Ubuntu 24.04, lon1) with a 35GB attached volume serves as the bootstrap node for the network.

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

## Production Network Deployment

The `bootstrap-production` command deploys 7 hardened bootstrap nodes across 3 cloud providers for the production Autonomi network.

### Architecture

| # | Provider | Region    | Location              | Role    |
|---|----------|-----------|-----------------------|---------|
| 1 | Vultr    | nrt       | Tokyo, JP             | Genesis |
| 2 | Vultr    | syd       | Sydney, AU            | Node    |
| 3 | Vultr    | ewr       | New Jersey, US        | Node    |
| 4 | Vultr    | lax       | Los Angeles, US       | Node    |
| 5 | Hetzner  | fsn1      | Falkenstein, DE       | Node    |
| 6 | Hetzner  | ash       | Ashburn, VA, US       | Node    |
| 7 | AWS      | sa-east-1 | Sao Paulo, BR         | Node    |

Each node runs on a dedicated VM (4 vCPU / 8 GB) with:
- Static IP (pre-allocated, survives VM recreation)
- Striped LVM volume (2x50 GB = 100 GB usable)
- Dedicated SSH key (`~/.ssh/ant-production`)
- Full security hardening (firewall, fail2ban, rate limiting, sysctl tuning)
- Systemd service running as `ant-node` user with resource limits

### Prerequisites

1. **Provider CLI tools**: `hcloud`, `vultr-cli`, `aws`
2. **Environment variables**:
   - `HCLOUD_TOKEN` — Hetzner token (**must be for the `ant-production` project**)
   - `VULTR_API_KEY` — Vultr API key
   - AWS credentials (via env vars or `~/.aws/credentials`)
3. **SSH key**: Generate a dedicated production key:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/ant-production -C "ant-production"
   ```
4. **Static IPs**: Pre-allocated and stored in `deploy/production-ips.json` (already done)

### Commands

#### `bootstrap-production`

Deploy the full production bootstrap network.

```bash
uv run saorsa-deploy bootstrap-production
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--name` | string | `ant-production` | Deployment name |
| `--node-version` | string | latest | Specific ant-node release version |
| `--ssh-key-path` | string | `~/.ssh/ant-production` | Path to SSH private key |
| `--force` | flag | - | Skip confirmation prompts and overwrite stale state |

The command:
1. Validates all provider credentials
2. Prompts to confirm correct Hetzner token (unless `--force`)
3. Creates 7 VMs via Terraform (parallel across all providers)
4. Applies security hardening to all nodes
5. Provisions the genesis node (Vultr nrt, port 10000)
6. Provisions remaining 6 nodes with bootstrap peer address
7. Saves deployment state to S3

#### `destroy-bootstrap-production`

Tear down all production infrastructure. Static IPs are preserved.

```bash
uv run saorsa-deploy destroy-bootstrap-production
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--name` | string | `ant-production` | Deployment name |
| `--ssh-key-path` | string | `~/.ssh/ant-production` | Path to SSH private key |
| `--force` | flag | - | Skip confirmation prompt |

### Static IP Management

Static IPs are allocated outside Terraform and stored in `deploy/production-ips.json`. This ensures IPs survive VM teardown and recreation. The IPs were allocated using provider CLIs:

- **Hetzner**: `hcloud primary-ip create --type ipv4 --name <name> --location <location>`
- **Vultr**: `vultr-cli reserved-ip create --region <region> --type v4`
- **AWS**: `aws ec2 allocate-address --region <region> --domain vpc`

To release IPs (only do this if decommissioning permanently), use the corresponding `delete`/`release` commands for each provider.

### Security Hardening

Each production node receives:

- **Firewall** (provider-level): Only ports 22 (SSH) and 10000 (node) open
- **SSH hardening**: Key-only auth, no password, max 3 auth attempts
- **fail2ban**: Bans IPs after 3 failed SSH attempts for 1 hour
- **Automatic updates**: `unattended-upgrades` for security patches
- **sysctl tuning**: SYN flood protection, ICMP hardening, connection limits
- **Rate limiting**: `iptables hashlimit` on port 10000 (50 conn/sec per source IP)
- **Service isolation**: Runs as `ant-node` user with `LimitNOFILE=65536`, `MemoryMax=6G`, `CPUQuota=350%`

### Extending the Striped Volume

The data storage uses LVM striping across two block devices. This makes it straightforward to expand storage when needed.

#### Adding capacity to existing stripe members

If your provider supports volume resizing (DigitalOcean, Hetzner, AWS):

1. **Resize the volumes** in the provider's console or CLI (e.g., increase both 50 GB volumes to 100 GB)
2. **SSH into the node**:
   ```bash
   ssh -i ~/.ssh/ant-production root@<node-ip>
   ```
3. **Resize the physical volumes** (LVM will detect the new size):
   ```bash
   pvresize /dev/<device1>
   pvresize /dev/<device2>
   ```
4. **Extend the logical volume** to use all available space:
   ```bash
   lvextend -l +100%FREE /dev/ant_storage_vg/ant_storage_lv
   ```
5. **Resize the filesystem**:
   ```bash
   resize2fs /dev/ant_storage_vg/ant_storage_lv
   ```

#### Adding new stripe members

To add a third (or more) volume to the stripe:

1. **Create and attach** a new volume via the provider's console/CLI
2. **SSH into the node** and create a new physical volume:
   ```bash
   pvcreate /dev/<new-device>
   ```
3. **Extend the volume group**:
   ```bash
   vgextend ant_storage_vg /dev/<new-device>
   ```
4. **Extend the logical volume** (note: new stripes won't rebalance existing data):
   ```bash
   lvextend -l +100%FREE /dev/ant_storage_vg/ant_storage_lv
   resize2fs /dev/ant_storage_vg/ant_storage_lv
   ```

> **Note**: Adding a new stripe member extends capacity but new data written after the extension will use the new stripe configuration. Existing data remains on the original stripe layout. This is fine for append-heavy workloads.

#### Checking volume status

```bash
# Show volume group free space
vgs

# Show logical volume details
lvs -o +stripes,stripesize

# Show physical volumes
pvs

# Show filesystem usage
df -h /mnt/ant-node-storage
```

### Running tests

```bash
uv run pytest tests/ -v
```

### Linting and formatting

```bash
uv run ruff format saorsa_deploy/ tests/
uv run ruff check saorsa_deploy/ tests/
```
