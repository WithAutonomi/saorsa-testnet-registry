# Saorsa Testnet Registry

## Architecture

A single-user system for managing testnet deployments, structured as a uv workspace with four packages:

- **`db/`** (`saorsa-db`) - Shared database models, repositories, and Alembic migrations
- **`web/`** (`saorsa-web`) - Flask app serving a read-only REST API (`/api/...`) and web UI (`/deployments`) with Jinja2 + HTMX
- **`mcp/`** (`saorsa-deploy-mcp`) - MCP server that runs saorsa-deploy as subprocesses and reads deployment state from the database
- **`deploy/`** (`saorsa-deploy`) - CLI tool that provisions testnets via Terraform and Pyinfra, records deployments in PostgreSQL
- **Database** - PostgreSQL, managed via Alembic migrations (inside `db/`)
- **Deployment** - Digital Ocean App Platform (`app.yaml`)

### Data Flow

```
Claude → MCP Server → saorsa-deploy (subprocess) → infrastructure
                          ↕
                      PostgreSQL (via saorsa-db)
                          ↑
                   Read-only web UI (via saorsa-db)
```

The MCP server invokes `saorsa-deploy` CLI commands as blocking subprocesses. `saorsa-deploy` records deployment lifecycle events directly in PostgreSQL via the `saorsa-db` repository interfaces. The web app is a read-only viewer.

## Project Structure

```
pyproject.toml                  # workspace root (not a package)
app.yaml                        # DO App Platform spec
db/
  pyproject.toml
  alembic.ini
  alembic/                      # database migrations
  saorsa_db/
    db.py                       # SQLAlchemy engine/session factory
    models/deployment.py        # Deployment, DeploymentEvent models
    repositories.py             # DeploymentRepository ABC + SqlAlchemy impl
web/
  pyproject.toml
  wsgi.py                       # gunicorn entry point
  templates/                    # Jinja2 templates
  saorsa_web/
    app.py                      # Flask app factory
    config.py                   # env var configuration
    db.py                       # Flask-specific DB wrapper (scoped session)
    models/__init__.py           # re-exports from saorsa_db
    api/deployments.py          # Read-only REST API endpoints
    web/deployments.py          # Web UI views
mcp/
  pyproject.toml
  saorsa_deploy_mcp/
    server.py                   # MCP server — subprocess + DB reads
deploy/
  pyproject.toml
  README.md                     # Command reference, argument tables, architecture
  saorsa_deploy/
    main.py                     # CLI entry point
    registry.py                 # Optional DB recording (no-op if DATABASE_URL unset)
    binary_source.py            # GitHub release / custom branch binary resolution
    bootstrap.py                # Bootstrap VM (genesis node) setup
    build_droplet.py            # Build VM provisioning
    executor.py                 # Command execution wrapper
    providers.py                # Provider configuration (Digital Ocean)
    resources.py                # Resource management
    ssh.py                      # SSH operations
    state.py                    # Deployment state management (S3)
    terraform.py                # Terraform orchestration
    cmd/                        # CLI subcommands (bootstraps infra, nodes infra, nodes provision, nodes destroy, monitoring, etc.)
    provisioning/               # Pyinfra provisioning logic (genesis, node, build, monitoring)
    resources/                  # Terraform manifests (digitalocean/, digitalocean-monitoring/, aws-build-infra/)
  tests/                        # pytest test suite
```

## Database

### Repository Pattern

`saorsa-db` provides abstract `DeploymentRepository` and concrete `SqlAlchemyDeploymentRepository`:

```python
from saorsa_db.repositories import DeploymentRepository, SqlAlchemyDeploymentRepository
from saorsa_db.db import get_session_factory

factory = get_session_factory()
repo = SqlAlchemyDeploymentRepository(factory())
repo.create(name="DEV-01", deploy_args={...})
```

Use the abstract interface for unit testing with fakes.

### Models

- `Deployment` — id, name, status (enum), workflow_run_id (nullable), deploy_args (JSON), timestamps
- `DeploymentEvent` — id, deployment_id (FK), event_type, details (JSON), timestamp
- `DeploymentStatus` — pending, running, stopped, failed, destroying, destroyed

## Development

```bash
# Install all workspace packages
uv sync --all-packages

# Set up environment
cp .env.example .env
# Edit .env with DATABASE_URL and cloud credentials (DO_TOKEN, AWS keys)

# Run migrations (from db/ directory)
cd db && uv run alembic upgrade head

# Start dev server
uv run flask --app saorsa_web.app run

# Run the MCP server (stdio transport)
uv run python -m saorsa_deploy_mcp.server

# Run tests
uv run pytest

# Run deploy package tests
uv run pytest deploy/tests/

# Show saorsa-deploy CLI help
uv run saorsa-deploy --help

# Lint
uv run ruff check .
```

## Deploying & Destroying Testnets

The data flow is: **Claude → MCP → saorsa-deploy (subprocess) → infrastructure**

`saorsa-deploy` runs locally (or wherever the MCP server runs). It needs cloud credentials in the local environment: `DO_TOKEN`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, SSH keys. When `DATABASE_URL` is set, it automatically records deployments in PostgreSQL.

When asked to deploy, launch, create, or destroy a testnet, **use the `saorsa-testnet-registry` MCP tools** — do not search the codebase. The MCP server provides:

- `create_deployment` — runs the full deployment sequence (bootstraps infra → nodes infra → nodes provision-genesis → bootstraps provision → nodes provision → nodes start)
- `destroy_deployment` — runs saorsa-deploy nodes destroy then bootstraps destroy to tear down infrastructure

NOTE: the MCP server currently still shells out to the pre-split `infra`/`destroy` commands and needs an update before Claude can use these tools end-to-end. Until then, run the saorsa-deploy CLI directly.

`nodes provision` installs the ant-node binary, writes systemd unit files, and **enables** the services; it does not start them. `nodes start` is a separate step that runs `systemctl start ant-node-{1..N}` in parallel across every node VM. The split exists so that optional setup (notably port-restricted NAT simulation configured via `--nat-percent`) can run between unit-write and service-start.
- `list_deployments` — lists all deployments from the database
- `get_deployment` — gets deployment details, events, and status
- `testnet://docs/deployment-config` — resource with full parameter documentation

### Extending a testnet (shared bootstraps)

A testnet can be deployed as an **extension** of another — it reuses the source testnet's bootstrap VMs and peers with them, but has its own fresh node VMs (typically running a different `ant-node` binary for compatibility testing). Commands:

- `deployments extend --name DEV-02 --source DEV-01` — seeds DEV-02's state with a copy of DEV-01's `bootstrap_nodes` / `bootstrap_ip` / `bootstrap_port` and records `shared_bootstrap_from: "DEV-01"`. No infrastructure is created. Idempotent: re-running refreshes the bootstrap fields from the source.
- `deployments unextend --name DEV-02 --force` — inverse: deletes DEV-02's deployment state. Never touches the source. Refuses if DEV-02 still has nodes (run `nodes destroy` first).

The pipeline for an extension skips the bootstrap/genesis stages entirely: `deployments extend` → `nodes infra` → `nodes provision` → `nodes start`. Destroy: `nodes destroy` → `deployments unextend`.

`bootstraps destroy` now scans for any deployment whose `shared_bootstrap_from` matches the name being destroyed and refuses if dependents exist. `--force` does not override this — the user must `deployments unextend` each dependent first.

### Running a prod continuous-downloader

`prod continuous-downloader deploy` is a higher-level saorsa-deploy command that stands up a single-VM client deployment running a dedicated `prod_downloader.py` Python service against the **production** Autonomi network, with a paired InfluxDB monitoring VM (always in `lon1`). The bootstrap peers list is fetched at runtime from [WithAutonomi/ant-client `resources/bootstrap_peers.toml`](https://raw.githubusercontent.com/WithAutonomi/ant-client/refs/heads/main/resources/bootstrap_peers.toml); a curated list of public file addresses ships at `deploy/saorsa_deploy/resources/prod_addresses.txt` and can be overridden with `--addresses-file`.

- Single VM per deployment; takes one `--spec region[:vm_size]` (download-only — uploaders are rejected).
- Monitoring is mandatory and created inside the same command (reuses `monitoring infra` + `monitoring provision`).
- The service is implemented in Python (`deploy/saorsa_deploy/resources/scripts/prod_downloader.py`) — not the shared `downloader.sh` — because every download (success or failure) keeps a per-op stdout/stderr log on disk under `/var/log/ant-prod-downloader/{idx:04d}-{addr_prefix}.{stdout,stderr}` for after-the-fact analysis. Testnet downloaders deliberately stay on `downloader.sh` so they keep no per-op logs.
- `--sleep-interval-secs` configures a sleep at the **end of each full cycle** through the address list. Default 900 (15 minutes); pass `0` to disable and resume the back-to-back cycling of the legacy bash downloader. Persisted in `deploy_args.sleep_interval_secs` and surfaced by `deployments slack-post`.
- Registers a `Deployment` row with `deploy_args.kind = "prod-continuous-downloader"` so `deployments record-results --name ... --monitoring-name ... --window -1h` works unchanged against it (the new script keeps emitting to the existing `download` Influx measurement).
- `prod continuous-downloader destroy` tears down the monitoring VM alongside the client by default; pass `--keep-monitoring` to preserve the InfluxDB + Grafana stack for after-the-fact analysis.

See `deploy/README.md` §"Running continuous downloaders against production" for the full argument table and sequencing.

### Running prod uploaders

`prod uploaders deploy` is the upload-side equivalent of `prod continuous-downloader`: it stands up one or more client VMs that each upload a list of public files to the production Autonomi network with `ant --json file upload --public`, then exit when the list is exhausted. Per-file results (chunks_stored / chunks_failed / address / file size / duration) are emitted to a `prod_upload` Influx measurement on a paired monitoring VM.

- Multi-VM. Repeatable `--uploader REGION:VM_SIZE:concurrency=N:urls=PATH` (all four parts required); each uploader VM downloads its assigned URL list at runtime, uploads each file with `--public`, deletes the local copy, exits.
- Single shared `--secret-key` across all uploaders (or `ARBITRUM_ONE_SECRET_KEY` env var — deliberately distinct from the testnet `ANT_DEPLOY_SECRET_KEY` because prod uploads need a funded Arbitrum One mainnet wallet and cost real ETH).
- Monitoring is mandatory and created inside the same command (reuses `monitoring infra` + `monitoring provision`).
- Service is implemented in Python (`deploy/saorsa_deploy/resources/scripts/prod_uploader.py`) — not bash — because per-upload JSON parsing and metric emission was easier that way. The systemd unit is `Type=simple, Restart=no` so the service stops on its own.
- Registers a `Deployment` row with `deploy_args.kind = "prod-uploaders"`.
- Companion commands: `deployments print-uploads --name <name>` (dispatches on kind — for prod-uploaders it pulls the `prod_upload` Influx measurement and prints per-host Rich tables; pass `--details` for one full record per upload) and `deployments record-results --name <name>` (also kind-dispatched — for prod-uploaders it upserts every Influx record into the `deployment_prod_upload_results` Postgres table; `--window` is ignored for this kind because uploads are a bounded run).
- `prod uploaders destroy` tears down the monitoring VM alongside the uploader VMs by default; pass `--keep-monitoring` to preserve the InfluxDB + Grafana stack.

See `deploy/README.md` §"Running uploaders against production" for the full argument table and sequencing.

### Running prod uploads from the local machine

`prod local-uploads` is the local-machine variant of `prod uploaders deploy`: it runs `ant --json file upload --public` sequentially against the production Autonomi network from the host the command is invoked on, instead of provisioning remote VMs. Per-file results are written directly into the `deployment_prod_upload_results` Postgres table — there is no Influx, no monitoring VM, and no destroy command. The intended use case is testing uploads from a real home internet connection.

- Two input modes: `--urls FILE` (download each URL to a temp file, upload, delete only the temp copy) and `--local-dir-path DIR` (recursively walk the directory and upload every regular file underneath, identifying each by its path relative to the root; source files are never modified or deleted).
- Sequential, single-file-at-a-time. No `--concurrency` flag.
- Same `--branch-name`/`--repo-owner`/`--ant-version` resolution as the other prod commands; the resolved binary is cached at `~/.cache/saorsa-deploy/ant-binaries/<key>/ant` (pinned releases are reused; `latest` and branch builds are re-fetched).
- Single `--secret-key` (or `ARBITRUM_ONE_SECRET_KEY`) — funded Arbitrum One wallet, real ETH.
- Registers a `Deployment` row with `deploy_args.kind = "prod-local-uploads"`. `deployments slack-post` has a dedicated branch for this kind that surfaces hostname, input mode + path, file count, and payment mode.
- On completion the deployment is marked `stopped` regardless of per-file failures; failure detail lives in the per-row Postgres records.

See `deploy/README.md` §"Running uploads from the local machine" for the full argument table.

### Deployment Statuses

`pending` → `running` → `stopped` (success) or `failed`

When destroying: `running`/`stopped` → `destroying` → `destroyed` (success) or `failed`

### API Endpoints (Read-Only)

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/deployments` | List all deployments |
| GET | `/api/deployments/<id>` | Get deployment with events |
| GET | `/api/deployments/<id>/events` | List deployment events |

## Monitoring

Optional per-testnet monitoring stack (InfluxDB 2.x + Grafana) on a single VM. Standalone — usable by this repo or external setups.

### Commands

- `monitoring infra` — create monitoring VM via Terraform
- `monitoring provision` — install InfluxDB + Grafana, prints connection details
- `monitoring provision-telegraf` — install Telegraf on nodes for host metrics (CPU, mem, disk, net) → InfluxDB
- `monitoring info` — print connection details; `--telegraf-config` emits portable Telegraf TOML
- `monitoring destroy` — tear down monitoring infrastructure

### Architecture

- Terraform manifests: `deploy/saorsa_deploy/resources/digitalocean-monitoring/`
- Provisioners: `deploy/saorsa_deploy/provisioning/monitoring.py` (InfluxDB+Grafana), `telegraf_metrics.py` (Telegraf)
- CLI commands: `deploy/saorsa_deploy/cmd/monitoring_infra.py`, `provision_monitoring.py`, `provision_telegraf_metrics.py`, `monitoring_info.py`, `destroy_monitoring.py`
- State: separate S3 key prefix `saorsa-deploy/monitoring/{name}.json` (independent of deployment state)
- Telegraf metrics runs as a separate systemd service (`telegraf-ant-metrics`) alongside the existing log forwarding (`telegraf-ant-elasticsearch`)

### Telegraf naming

- `elastic provision-telegraf` — Elasticsearch log forwarding
- `monitoring provision-telegraf` — InfluxDB host metrics

## Elasticsearch (Log Forwarding)

Optional per-testnet Elasticsearch instance on a dedicated VM. Enabled via `enable_log_forwarding` in deployment config. Telegraf forwards node JSON logs to the instance.

### Commands

- `elastic infra` — create Elasticsearch VM via Terraform; `--droplet-size` for large testnets
- `elastic provision` — install Elasticsearch 8.x, create index template, user + API key
- `elastic info` — print connection details (URL, user, password, API key)
- `elastic destroy` — tear down Elasticsearch infrastructure
- `elastic provision-telegraf` — install Telegraf on nodes for log forwarding → Elasticsearch (auto-loads ES credentials from state)

### Architecture

- Terraform manifests: `deploy/saorsa_deploy/resources/digitalocean-elastic/`
- Provisioner: `deploy/saorsa_deploy/provisioning/elastic.py`
- CLI commands: `deploy/saorsa_deploy/cmd/elastic_infra.py`, `provision_elastic.py`, `elastic_info.py`, `destroy_elastic.py`
- State: separate S3 key prefix `saorsa-deploy/elastic/{name}.json`
- Security: user `ant-log-writer` (read/write) + read-only API key for queries
- Index pattern: `telegraf-ant-nodes-{testnet_lower}-%Y.%m.%d`

## Key Commands

- `uv run flask --app saorsa_web.app run` - Dev server
- `cd db && uv run alembic revision --autogenerate -m "description"` - New migration
- `cd db && uv run alembic upgrade head` - Apply migrations
- `uv run ruff check . --fix` - Lint and fix
- `uv run pytest deploy/tests/` - Run deploy package tests
- `uv run scripts/bump_version.py <version>` - Bump version across all packages, commit, tag, and push
