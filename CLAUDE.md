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

## Declarative manifests (`deployments up` / `down` / `plan`)

A deployment can be described by a single declarative YAML **manifest** and run
with one command, instead of a hand-edited bash runbook:

- `saorsa-deploy deployments plan --config <manifest>` — print the computed command sequence (dry-run).
- `saorsa-deploy deployments up --config <manifest>` — run the full deploy sequence.
- `saorsa-deploy deployments down --name <deployment>` (or `--config <manifest>`) — run the destroy sequence. With `--name` alone the teardown is derived from what exists in S3 state (no manifest needed); `--config` mirrors a manifest's components.
- `saorsa-deploy deployments config show|validate --config <manifest>` — inspect/validate a manifest.

One manifest = one deployment's full desired state, discriminated by `kind`
(`testnet`, `testnet-extension`, and the six `prod-*` kinds). The orchestrator
(`deploy/saorsa_deploy/orchestrator/`, schema in `config/`) owns the
ordering/flag-coupling invariants that previously lived in the `deploy-runbook`
skill's prose — it is the single source of truth. `up` executes each step by
re-entering the CLI in-process (`main(argv)`); a failing step halts the run.

`up` is **resumable**: each completed step is recorded in a step journal
(`saorsa-deploy/journals/{name}.json` in the tfstate bucket; logic in
`orchestrator/journal.py`, persistence in `state.py`), so a re-run after a
mid-sequence failure can skip the already-done steps — most valuably the
~10-minute binary builds. A journal hit never silently skips: interactively
`up` prompts [r]esume / [d]elete / [a]bort; non-interactively (agents, the MCP
server) pass **`--resume`** to skip the completed steps — without it a non-TTY
journal hit refuses with guidance rather than hanging. If the journal doesn't
match the computed plan (manifest edited, name reused, kind changed) a diff is
shown and resume means "from the longest matching prefix". A fully successful
`up` — or a successful `down` (either path) — deletes the journal;
`plan`/`up --dry-run` never touch it.

For testnet/extension kinds, **monitoring (InfluxDB + Grafana) defaults ON**
(omit the `monitoring:` block, or `monitoring: {enabled: false}` to opt out) so
client upload/download metrics are captured; Elasticsearch log-forwarding stays
opt-in.

Testnet node VMs can be placed on **DigitalOcean, Hetzner, Vultr, or OVH**
(`nodes.providers.<name>` in a manifest; `--<provider>-vm-count` etc. on
`nodes infra`). OVH (regions like `GRA9`/`UK1`/`DE1`, default flavor `b3-16`,
`size:` → `--ovh-flavor`) reuses the prod-migration node module; it needs
`OVH_APPLICATION_KEY`/`OVH_APPLICATION_SECRET`/`OVH_CONSUMER_KEY`/
`OVH_CLOUD_PROJECT_SERVICE` at both create AND destroy time, and has no
availability preflight (unlike Hetzner/Vultr). Clients support the same four
providers.

Every client kind accepts a **`bootstrap_cache`** option (default **true**) —
per-entry on testnet `clients.uploaders[]`/`clients.downloaders[]` and
`prod_uploaders.uploaders[]` (spec token `bootstrap_cache=true|false`),
spec-level on the downloader/canary/local kinds (CLI flag
`--bootstrap-cache/--no-bootstrap-cache`). When true the service invokes `ant`
**without** `--bootstrap`, which enables the client's peer cache; the peers
come from a `bootstrap_peers.toml` written to ant's config dir
(`/root/.config/ant/` on client VMs — testnet bootstrap addresses for testnet
kinds, the production list for prod kinds; for local-uploads/downloads the
operator's `~/.config/ant/bootstrap_peers.toml` is written only when missing,
never overwritten). `bootstrap_cache: false` restores the legacy explicit
`--bootstrap` invocation, which disables the cache. The cache state per client
is surfaced by `deployments slack-post`, `print-uploads`/`print-downloads`,
and `generate-inventory`.

Testnet client address lists live in the dedicated **public-read**
`saorsa-testnet-addresses` bucket (NOT the tfstate bucket, which keeps Block
Public Access intact; bucket + IAM managed by the `aws-client-s3` Terraform
module). Downloaders list+fetch them with **unsigned HTTPS** and uploaders
flush through **presigned PUT URLs** minted by `clients provision` (7-day
SigV4 max expiry) — both paths avoid the VM signing anything, so clock-skewed
clients (whose own SigV4 requests S3 rejects at ~±15min) keep publishing and
consuming addresses.

A top-level `slack_post: true` (valid for **every** kind) appends a final
`deployments slack-post --name <name>` step to `up`, posting a deployment
summary once it's up. `slack-post` has two paths: when
`ANT_DEPLOY_SLACK_BOT_TOKEN` (+ channel via `--channel`/`ANT_DEPLOY_SLACK_CHANNEL`)
is set it posts via the Slack Web API (`chat.postMessage`) and persists the
message's `(channel, ts)` to the deployment (`slack_channel`/`slack_message_ts`
columns); otherwise it falls back to the `ANT_DEPLOY_SLACK_WEBHOOK_URL` incoming
webhook (which returns no message reference). The step is **best-effort** (a
Slack failure warns but never fails the run); `down` is unaffected.

`deployments record-results --update-slack` posts a per-kind results summary as
a **threaded reply** to that original message — it needs the Web API path's
captured `(channel, ts)` plus `ANT_DEPLOY_SLACK_BOT_TOKEN`, and is likewise
best-effort (warns, never fails the recording). The reply is kind-dispatched
(per-host file-op aggregates for testnet/downloader kinds; per-host + per-file
detail for prod-uploaders; alive/pending/confirmed-lost + hours-to-loss
histogram for prod-canary). Web API client lives in
`deploy/saorsa_deploy/slack_api.py`; reply block builders in
`deploy/saorsa_deploy/slack_results.py`.

Reuse: `extends: <path>` (deep-merge a base profile; **lists replace, not
append**), `--set dotted.key=value`, and `--name`. Secrets are never in a
manifest — prod kinds name an env var (`secret_key_env`, default
`ARBITRUM_ONE_SECRET_KEY`). Reusable profiles are committed under
`deploy/manifests/profiles/` (`reference.yaml` = every option, `staging-rc.yaml` =
a base); concrete run manifests live in the git-ignored `deploy/manifests/runs/`
(they churn per run, like `runbooks/`). The
legacy `runbooks/` bash scripts and the primitive `infra`/`provision` commands
remain fully supported and unchanged. See `deploy/README.md` §"Declarative
manifests" for the schema reference.

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

`prod continuous-downloader deploy` is a higher-level saorsa-deploy command that stands up a single-VM client deployment running a dedicated `prod_downloader.py` Python service against the **production** Autonomi network, with a paired InfluxDB monitoring VM (always in `lon1`). The bootstrap peers list is fetched at runtime from [WithAutonomi/ant-client `resources/bootstrap_peers.toml`](https://raw.githubusercontent.com/WithAutonomi/ant-client/refs/heads/main/resources/bootstrap_peers.toml); a curated list of public file addresses ships at `deploy/saorsa_deploy/resources/prod_addresses.txt` and can be overridden with `--addresses-path`.

- Single VM per deployment; takes one `--spec region[:vm_size]` (download-only — uploaders are rejected).
- Monitoring is mandatory and created inside the same command (reuses `monitoring infra` + `monitoring provision`).
- The service is implemented in Python (`deploy/saorsa_deploy/resources/scripts/prod_downloader.py`) — not the shared `downloader.sh` — because every download (success or failure) keeps a per-op stdout/stderr log on disk under `/var/log/ant-prod-downloader/{idx:04d}-{addr_prefix}.{stdout,stderr}` for after-the-fact analysis. Testnet downloaders deliberately stay on `downloader.sh` so they keep no per-op logs.
- `--sleep-interval-secs` configures a sleep at the **end of each full cycle** through the address list. Default 900 (15 minutes); pass `0` to disable and resume the back-to-back cycling of the legacy bash downloader. Persisted in `deploy_args.sleep_interval_secs` and surfaced by `deployments slack-post`.
- Registers a `Deployment` row with `deploy_args.kind = "prod-continuous-downloader"` so `deployments record-results --name ... --monitoring-name ... --window -1h` works unchanged against it (the new script keeps emitting to the existing `download` Influx measurement).
- `prod continuous-downloader destroy` tears down the monitoring VM alongside the client by default; pass `--keep-monitoring` to preserve the InfluxDB + Grafana stack for after-the-fact analysis.

See `deploy/README.md` §"Running continuous downloaders against production" for the full argument table and sequencing.

### Running prod downloaders (one-shot)

`prod downloaders deploy` is the one-shot sibling of `prod continuous-downloader`. The service walks the address list once and then exits 0; the systemd unit is `Type=simple Restart=no` so the VM can be left alone until destroy. All other behaviour is identical to the continuous variant — same provisioner (`ProdDownloaderProvisioner(one_shot=True)`), same `prod_downloader.py` script (toggled by the `ONE_SHOT=true` env var), same monitoring stack, same `record-results` / `slack-post` paths.

- CLI: `prod downloaders deploy` / `prod downloaders destroy`. Same arguments as `prod continuous-downloader` **except** `--sleep-interval-secs` is omitted.
- Registers a `Deployment` row with `deploy_args.kind = "prod-downloaders"`.
- The persisted `deploy_args["addresses"]` (list of resolved addresses, also written for `prod-continuous-downloader`) is rendered inline by `deployments slack-post` instead of the raw `addresses_file` path.

See `deploy/README.md` §"Running one-shot downloaders against production" for the full argument table.

### Running prod uploaders

`prod uploaders deploy` is the upload-side equivalent of `prod continuous-downloader`: it stands up one or more client VMs that each upload a list of public files to the production Autonomi network with `ant --json file upload --public`, then exit when the list is exhausted. Per-file results (chunks_stored / chunks_failed / address / file size / duration) are emitted to a `prod_upload` Influx measurement on a paired monitoring VM.

- Multi-VM. Repeatable `--uploader REGION:VM_SIZE:concurrency=N:urls=PATH` (all four parts required); each uploader VM downloads its assigned URL list at runtime, uploads each file with `--public`, deletes the local copy, exits.
- **Duplicate-file preflight** (runs before any infrastructure is created, on both `prod uploaders deploy` and `deployments up`): the file name of every upload target (URL basename, as stored in `deployment_prod_upload_results` / shown by `prod files`) is checked against the recorded prod uploads list, and a name collision aborts the deploy. Override with `--allow-duplicate-files` (manifest: `prod_uploaders.allow_duplicate_files: true`) to re-upload missing chunks or test the already-stored path. Skipped with a warning when no `DATABASE_URL` is set.
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

### Running prod downloads from the local machine

`prod local-downloads` is the download-side equivalent of `prod local-uploads`: it walks an address list and runs `ant -v file download <address> -o <output>` sequentially from the host the command is invoked on, recording per-address results directly to the new `deployment_prod_download_results` Postgres table. No Influx, no monitoring VM, no remote infrastructure.

- Single input mode: `--addresses-path FILE` (newline-separated address list; required — no bundled default).
- Default output: per-run temp directory, file deleted as soon as the result row is upserted. Pass `--keep-output-dir DIR` to retain the downloads under a directory you control.
- No `--secret-key`, no `--payment` — downloads are read-only against the network and cost nothing.
- Same `--branch-name` / `--repo-owner` / `--ant-version` resolution as the other prod commands; the resolved binary is cached at `~/.cache/saorsa-deploy/ant-binaries/<key>/ant`.
- Registers a `Deployment` row with `deploy_args.kind = "prod-local-downloads"`. `deployments slack-post` has a dedicated branch that surfaces binary, addresses-path, address count, and output mode (temp vs kept).
- On completion the deployment is marked `stopped` regardless of per-address failures; failure detail lives in the per-row Postgres records. `deployments record-results` refuses this kind; `deployments print-recorded-results` renders per-address Rich tables (`--details` for one full record per download).

See `deploy/README.md` §"Running downloads from the local machine" for the full argument table.

### Running prod canaries

`prod canary deploy` stands up a long-lived loss-measurement deployment against production Autonomi: one upload VM walks a URL list once and exits its uploader service, while N probe VMs run forever, hourly probing every canary they own (per a static `blake3(canary_id)[0] % N` hash partition). All three measurements (`prod_canary_upload`, `prod_canary_chunk`, `prod_canary_probe`) land on a paired InfluxDB+Grafana monitoring stack; `record-results` pulls them into Postgres and re-derives a `prod_canary_loss` table. **The dataset is the product** — `prod canary destroy` defaults to `--keep-monitoring`; pass `--no-keep-monitoring` to also tear down Influx.

- Multi-VM. One `--upload-spec REGION[:VM_SIZE]`, one or more `--probe-spec REGION[:VM_SIZE]:count=N` (sum of `count=N` becomes the probe shard count).
- Single shared `--secret-key` (or `ARBITRUM_ONE_SECRET_KEY`). Funded Arbitrum One mainnet wallet, real ETH.
- Monitoring is mandatory and created inside the same command (reuses `monitoring infra` + `monitoring provision`).
- Loss confirmation: a canary is `lost` when **two consecutive failed probes from the same probe VM report the same `missing_chunk_address`** (the *two-strike rule*). Applied locally on each probe VM (SQLite-backed) and globally in `record-results` (Postgres-derived, authoritative).
- Runtime services are dedicated Python scripts (`prod_canary_upload.py`, `prod_canary_probe.py`), not bash, because per-canary metric emission and asyncio probing were easier in Python. Upload-side systemd unit is `Type=simple Restart=no`; probe-side is `Type=simple Restart=always`.
- Registers a `Deployment` row with `deploy_args.kind = "prod-canary"`.
- Companion commands: `deployments record-results --name <name>` (kind-dispatched — pulls all three Influx measurements, upserts the four `prod_canary_*` Postgres tables, and rederives `prod_canary_loss`; idempotent and cron-safe) and `deployments print-recorded-results` (kind-dispatched — emits an alive / pending / confirmed-lost summary plus an hours-to-loss histogram, per-vantage breakdown, and per-chunk-role breakdown).
- `prod canary destroy` keeps monitoring by default; pass `--no-keep-monitoring` to destroy the Influx + Grafana stack alongside.

See `deploy/README.md` §"Running canaries against production" for the full argument table and the dataset description.

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

Optional per-testnet monitoring stack: InfluxDB 2.x and Grafana on **two
separate VMs**. Standalone — usable by this repo or external setups. Runs on
**DigitalOcean (default) or OVH** (`--provider ovh`).

Grafana is split onto its own small VM so its panel rendering doesn't compete
with InfluxDB for CPU/RAM (the cause of large dashboards like the per-node RSS
panels failing to render on a single shared box). The InfluxDB VM carries the
data volume; Grafana reaches it over the public `:8086` endpoint. The
`monitoring infra`/`provision` flow (and `monitoring destroy`) manage both VMs
in one Terraform state. Legacy single-VM stacks (state without `grafana_ip`)
still re-provision by co-locating Grafana on the InfluxDB VM.

### Commands

- `monitoring infra` — create the InfluxDB VM + Grafana VM via Terraform. `--provider {digitalocean,ovh}` (default digitalocean), `--droplet-size` (InfluxDB VM; default `s-4vcpu-8gb` do / `b3-8` ovh), `--grafana-size` (default `s-1vcpu-2gb` do / `d2-4` ovh), `--volume-size`, `--volume-type` (ovh). OVH needs `OVH_CLOUD_PROJECT_SERVICE` + OVH API creds + an `ant_deploy` SSH key in the project.
- `monitoring provision` — install InfluxDB (InfluxDB VM) + Grafana (Grafana VM), prints connection details. `--retention-days N` caps InfluxDB bucket retention (default `0` = infinite, the dev default; set e.g. 14 for prod). Retention is applied to both buckets at first provision.
- `monitoring provision-telegraf` — install Telegraf on nodes for host metrics (CPU, mem, disk, net) → InfluxDB. Retrofit path (`--inventory-path`/`--host-ips-file`) provisions Tier-1 host+procstat metrics only; the Prometheus app-metrics scrape is only wired on the `--name` path with `node_count > 0`.
- `monitoring info` — print connection details; `--telegraf-config` emits portable Telegraf TOML
- `monitoring destroy` — tear down monitoring infrastructure (both VMs)

The prod client flows (canary / uploaders / downloaders) pin the InfluxDB VM to
`s-2vcpu-4gb` (a single client VM forwards little) but still get the split
Grafana VM.

### Architecture

- Terraform manifests: `deploy/saorsa_deploy/resources/{digitalocean,ovh}-monitoring/` (OVH is OpenStack-backed: it mints an `ovh_cloud_project_user` and uses `openstack_compute_volume_attach_v2` for the InfluxDB data volume, like the `ovh` node module). Provider selection lives in `cmd/obs_provider.py`.
- Provisioners: `deploy/saorsa_deploy/provisioning/monitoring.py` (InfluxDB on `influxdb_ip`, Grafana on `grafana_ip` in two pyinfra sessions), `telegraf_metrics.py` (Telegraf)
- CLI commands: `deploy/saorsa_deploy/cmd/monitoring_infra.py`, `provision_monitoring.py`, `provision_telegraf_metrics.py`, `monitoring_info.py`, `destroy_monitoring.py`
- State: separate S3 key prefix `saorsa-deploy/monitoring/{name}.json` (independent of deployment state); records `provider`, `vm_ip` (InfluxDB) and `grafana_ip`. tfstate key prefix is `do-monitoring`/`ovh-monitoring` per provider.
- Telegraf metrics runs as a separate systemd service (`telegraf-ant-metrics`) alongside the existing log forwarding (`telegraf-ant-elasticsearch`)

### Buckets (node vs client isolation)

Host metrics are split across **two InfluxDB buckets** so the "Node Host
Statistics" and "Client Host Statistics" dashboards are *physically* isolated
rather than relying on a mutable `host_role` dropdown:

- **`testnet-metrics`** — node-tier host metrics (genesis/bootstrap/WAN nodes:
  cpu/mem/disk/net/procstat), the `prometheus` per-node measurement, and all
  application measurements (`download`, `prod_upload`, `prod_canary_*`, …).
- **`client-metrics`** — client-tier host metrics only (uploader / downloader /
  canary client VMs, tagged `host_role=CLIENT`).

`monitoring provision` creates both buckets and mints an **org-scoped** Telegraf
token (read+write on every bucket, so adding a bucket never requires reminting
the immutable token). `monitoring provision-telegraf` routes CLIENT-role hosts
to `client-metrics` and everything else to `testnet-metrics`. The Node Host
Statistics dashboard queries `testnet-metrics`; Client Host Statistics queries
`client-metrics` — neither can render the other's data regardless of the Role
dropdown. Bucket names are constants in `monitoring.py` (`INFLUXDB_BUCKET` /
`INFLUXDB_CLIENT_BUCKET`).

### Telegraf naming

- `elastic provision-telegraf` — Elasticsearch log forwarding
- `monitoring provision-telegraf` — InfluxDB host metrics

## Elasticsearch (Log Forwarding)

Optional per-testnet Elasticsearch instance on a dedicated VM. Enabled via `enable_log_forwarding` in deployment config. Telegraf forwards node JSON logs to the instance.

Runs on **DigitalOcean (default) or OVH** (`--provider ovh`, same OVH env-var /
SSH-key prerequisites as monitoring).

### Commands

- `elastic infra` — create Elasticsearch VM via Terraform. `--provider {digitalocean,ovh}` (default digitalocean), `--droplet-size` (default `s-4vcpu-8gb` do / `b3-8` ovh; bump for large testnets), `--volume-size`, `--volume-type` (ovh)
- `elastic provision` — install Elasticsearch 8.x, create index template, user + API key. `--retention-days` (default 7, ILM) + `--shards`
- `elastic info` — print connection details (URL, user, password, API key)
- `elastic destroy` — tear down Elasticsearch infrastructure
- `elastic provision-telegraf` — install Telegraf on nodes for log forwarding → Elasticsearch (auto-loads ES credentials from state)

### Architecture

- Terraform manifests: `deploy/saorsa_deploy/resources/{digitalocean,ovh}-elastic/` (OVH is OpenStack-backed, like the monitoring/node modules; provider selection in `cmd/obs_provider.py`)
- Provisioner: `deploy/saorsa_deploy/provisioning/elastic.py`
- CLI commands: `deploy/saorsa_deploy/cmd/elastic_infra.py`, `provision_elastic.py`, `elastic_info.py`, `destroy_elastic.py`
- State: separate S3 key prefix `saorsa-deploy/elastic/{name}.json` (records `provider`); tfstate key prefix `do-elastic`/`ovh-elastic` per provider
- Security: user `ant-log-writer` (read/write) + read-only API key for queries
- Index pattern: `telegraf-ant-nodes-{testnet_lower}-%Y.%m.%d`

## Key Commands

- `uv run saorsa-deploy deployments plan --config deploy/manifests/profiles/reference.yaml` - Preview a manifest's command sequence
- `uv run saorsa-deploy deployments up --config <manifest>` / `deployments down --config <manifest>` - Deploy/destroy from a manifest
- `uv run flask --app saorsa_web.app run` - Dev server
- `cd db && uv run alembic revision --autogenerate -m "description"` - New migration
- `cd db && uv run alembic upgrade head` - Apply migrations
- `uv run ruff check . --fix` - Lint and fix
- `uv run pytest deploy/tests/` - Run deploy package tests
- `uv run scripts/bump_version.py <version>` - Bump version across all packages, commit, tag, and push
