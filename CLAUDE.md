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

For testnet/extension kinds, **monitoring (InfluxDB) defaults ON**
(omit the `monitoring:` block, or `monitoring: {enabled: false}` to opt out) so
client upload/download metrics are captured; Elasticsearch log-forwarding stays
opt-in. The **Grafana** dashboard VM is itself **opt-in and OFF by default** —
add `grafana: true` under `monitoring:` (CLI: `monitoring infra --grafana`) to
also stand up a Grafana VM; without it only InfluxDB is created (metrics stay
queryable via InfluxDB/Flux).

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

Every VM-backed client kind also accepts **`forward_logs`** (default off) and
**`rust_log`** (per-entry on testnet `clients.uploaders[]`/`downloaders[]` and
`prod_uploaders.uploaders[]`; spec-level on `prod_downloaders` /
`prod_continuous_downloader` / `prod_canary`; **not** the local-* kinds).
`forward_logs: true` forwards the client `ant` process's stdout+stderr to
Elasticsearch as raw lines (`message` field, tagged `role`/`stream`) in a
**separate** index `telegraf-ant-clients-<name>`. Testnet/extension kinds reuse
the deployment's **own** ES instance and so **require `elastic.enabled: true`**
(validation error otherwise); prod kinds have no node-tier ES, so the deploy
command stands up a **dedicated** small ES instance and tears it down on destroy
unless `--keep-elastic` / `keep_elastic: true`. `rust_log` sets `RUST_LOG` on the
client service: the client `ant` reads `RUST_LOG` via its tracing `EnvFilter`
(full per-module filters like `a::b=debug` work), but only with logging enabled,
so the service still passes `-v` (installs the subscriber) AND exports `RUST_LOG`
(which overrides the `-v` level). In a colon-delimited spec `rust_log` must be the
**last** token (its value may contain `::`). The client forwarder is a separate
`telegraf-ant-client-elasticsearch` systemd unit (`provisioning/telegraf_client.py`);
the ES `telegraf-ant-clients-*` index template is created by `elastic provision`.
Both options are surfaced by `deployments slack-post`.

Two profiling options were added on the `feat-debug_builds` branch. **Debug
builds**: a binary spec accepts **`debug: true`** (`binaries.<ant|ant_node>` on
testnet/extension kinds, `prod_*.binary` on prod kinds; branch builds only —
release versions ship prebuilt). It builds the binary **without** `--release`
(`build ant`/`build ant-node` gain a `--debug` flag; cargo drops the release
flag and the artifact comes from `target/debug`). The S3 build key is unchanged:
it is already namespaced by deployment `--name` and the builder always
overwrites, so `up` rebuilds when the manifest toggles `debug` (the `--debug`
token in the build step makes the journal re-run it) — no fetch-side flag is
needed. **heaptrack memory profiling**: prod uploaders/downloaders accept
**`memory_profiling: true`** (per-entry `prod_uploaders.uploaders[]` spec token
`memory_profiling=true|false`; spec-level CLI flag
`--memory-profiling/--no-memory-profiling` on `prod downloaders` /
`prod continuous-downloader`). When set the provisioner installs the `heaptrack`
apt package and the Python service wraps the `ant` invocation with
`heaptrack -o /var/log/ant-heaptrack/<upload|download>-<idx>` (helper
`_maybe_wrap_heaptrack` in `resources/scripts/prod_uploader.py` /
`prod_downloader.py`); heaptrack writes `<base>.<pid>.zst` incrementally so even
an OOM/SIGKILL leaves an analysable trace. heaptrack's own output is on stderr,
so the `ant --json` result parse (which walks stdout from the end) is
unaffected. Reuses the same `/var/log/ant-heaptrack/` path as the existing
testnet-only `clients provision --heaptrack` flag. NOT wired to testnet clients
(use `--heaptrack`), the prod-canary kind, or the local-* kinds.

Separately, the testnet/extension **`nodes.log_level`** option (`info`/`debug`/
`trace`) sets ant-node `--log-level` on **every** node-tier service — WAN nodes,
genesis, **and** bootstraps. **ant-node has NO env-filter / per-module logging**:
its `--log-level` is bound to the `RUST_LOG` env var and accepts a *single* level
only (a multi-target `RUST_LOG` makes ant-node exit `INVALIDARGUMENT`), so nodes
get one global level while only the client side gets the full `rust_log`
env-filter — letting one deployment run nodes and clients at different levels.

`nodes.log_level` also **lifts the node→ES Telegraf drop filter** so verbose node
logs actually reach Elasticsearch. The forwarder drops any line whose level is in
the node-tier `--drop-log-levels` flag (folded telegraf default `DEBUG,TRACE`); left
alone that silently strips the very trace/debug the operator asked for (nodes
write it to disk, ES shows only INFO/WARN). The orchestrator therefore derives the
drop set from `nodes.log_level` (drop only levels strictly *more verbose* than the
configured one) and passes it on the `nodes provision`/`provision-genesis`/`bootstraps
provision` steps alongside `--with-telegraf`: `trace` → `--drop-log-levels ""` (keep
everything), `debug` → `--drop-log-levels TRACE`, `info`/unset → omit the flag (folded
telegraf default `DEBUG,TRACE`).
An explicit **`elastic.drop_log_levels`** in the manifest always wins: set it to
`"DEBUG,TRACE"` to keep verbose logs on disk but out of ES (volume), or `""` to
force-keep everything regardless of node level. The **client** forwarder has no
level filter (it ships raw stderr lines unconditionally), so `rust_log` trace
already reaches ES; its only knob is buffer size — **`elastic.buffer_limit`** is
now threaded to the client forwarder too (via `clients provision
--forward-buffer-limit`) so a high-volume trace uploader doesn't overflow the
client forwarder's small default buffer and drop forwarded lines. Logic lives in
`orchestrator/testnet.py` (`_resolve_elastic_drop_log_levels`).

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

`prod continuous-downloader deploy` is a higher-level saorsa-deploy command that stands up a single-VM client deployment running a dedicated `prod_downloader.py` Python service against the **production** Autonomi network, with a paired InfluxDB monitoring VM. The bootstrap peers list is fetched at runtime from [WithAutonomi/ant-client `resources/bootstrap_peers.toml`](https://raw.githubusercontent.com/WithAutonomi/ant-client/refs/heads/main/resources/bootstrap_peers.toml); a curated list of public file addresses ships at `deploy/saorsa_deploy/resources/prod_addresses.txt` and can be overridden with `--addresses-path`.

- Single VM per deployment; takes one `--spec region[:vm_size]` (download-only — uploaders are rejected).
- Monitoring is mandatory and created inside the same command (reuses `monitoring infra` + `monitoring provision`).
- The service is implemented in Python (`deploy/saorsa_deploy/resources/scripts/prod_downloader.py`) — not the shared `downloader.sh` — because every download (success or failure) keeps a per-op stdout/stderr log on disk under `/var/log/ant-prod-downloader/{idx:04d}-{addr_prefix}.{stdout,stderr}` for after-the-fact analysis. Testnet downloaders deliberately stay on `downloader.sh` so they keep no per-op logs.
- `--sleep-interval-secs` configures a sleep at the **end of each full cycle** through the address list. Default 900 (15 minutes); pass `0` to disable and resume the back-to-back cycling of the legacy bash downloader. Persisted in `deploy_args.sleep_interval_secs` and surfaced by `deployments slack-post`. `--daily` is a convenience that sets it to `86400` for a permanent once-a-day cadence.
- **Selectable providers** (added for permanent runs). `--provider {digitalocean,hetzner,vultr,ovh}` (default DO) chooses the **client** VM provider — non-DO is cheaper for a permanent run and cuts pooled DO egress; it reuses the existing `*-client` TF modules (un-hardcoded the old `digital_ocean:` prefix in `_ensure_client_infra`). `--monitoring-provider {digitalocean,ovh}` (default DO) chooses the **monitoring** VM provider — only DO and OVH have monitoring TF modules. Client and monitoring providers are independent; OVH needs the four `OVH_*` vars, Hetzner `HCLOUD_TOKEN`, Vultr `VULTR_API_KEY`. Optional `--monitoring-region` / `--monitoring-droplet-size` / `--monitoring-volume-type` override the per-provider monitoring defaults (DO still defaults to `lon1` / `s-2vcpu-4gb`).
- **Bounded retention for permanent operation.** `--log-retention-days` (default 7) installs a daily `ant-prod-downloader-logprune.timer` systemd timer that `find -mtime +N -delete`s the per-op logs (`0` = keep forever). `--influx-retention-days` (default 7) is threaded into `monitoring provision` so the InfluxDB bucket expires old points (`0` = infinite); applied at first provision only.
- **Long-history archival.** Because Influx retention is bounded, `deployments archive --name <n>` periodically copies results into Postgres (thin wrapper over `record-results`; one-shot by default for cron/`/schedule`, `--loop --interval-secs N` for a local long-runner). Run it from the box with `DATABASE_URL` at least as often as the Influx retention window; the per-op `deployment_download_records` table is the authoritative long-term history. Impl: `deploy/saorsa_deploy/cmd/archive_results.py`.
- Registers a `Deployment` row with `deploy_args.kind = "prod-continuous-downloader"` (now also persisting `provider`, `monitoring_provider`, `influx_retention_days`, `log_retention_days`) so `deployments record-results --name ... --monitoring-name ... --window -1h` works unchanged against it (the new script keeps emitting to the existing `download` Influx measurement).
- **Manifest support.** This kind is fully declarative via `deployments up/plan/down --config <manifest>` (`kind: prod-continuous-downloader`). The `ProdContinuousDownloaderSpec` schema (`config/models.py`) exposes all the permanent-operation options — `provider`, `monitoring_provider` (+ `monitoring_region`/`monitoring_droplet_size`/`monitoring_volume_type`), `influx_retention_days`, `log_retention_days`, plus the pre-existing `spec`/`binary`/`sleep_interval_secs`/`addresses_path`/etc. — and `orchestrator/prod.py` `_continuous_downloader` maps them to the deploy argv. Once-a-day = `sleep_interval_secs: 86400` (the `--daily` sugar has no manifest field). Example: `deploy/manifests/profiles/prod-continuous-downloader.yaml`.
- `prod continuous-downloader destroy` tears down the monitoring VM alongside the client by default; pass `--keep-monitoring` to preserve the InfluxDB + Grafana stack for after-the-fact analysis. (Destroy reads the client/monitoring provider back from S3 state, so no provider flags are needed on teardown.)

See `deploy/README.md` §"Running continuous downloaders against production" for the full argument table and sequencing.

### Running prod downloaders (one-shot)

`prod downloaders deploy` is the one-shot sibling of `prod continuous-downloader`. The service walks the address list once and then exits 0; the systemd unit is `Type=simple Restart=no` so the VM can be left alone until destroy. All other behaviour is identical to the continuous variant — same provisioner (`ProdDownloaderProvisioner(one_shot=True)`), same `prod_downloader.py` script (toggled by the `ONE_SHOT=true` env var), same monitoring stack, same `record-results` / `slack-post` paths.

- CLI: `prod downloaders deploy` / `prod downloaders destroy`. Same arguments as `prod continuous-downloader` **except** `--sleep-interval-secs` is omitted.
- Registers a `Deployment` row with `deploy_args.kind = "prod-downloaders"`.
- The persisted `deploy_args["addresses"]` (list of resolved addresses, also written for `prod-continuous-downloader`) is rendered inline by `deployments slack-post` instead of the raw `addresses_file` path.

See `deploy/README.md` §"Running one-shot downloaders against production" for the full argument table.

### Upgrading the `ant` binary in place on a running prod downloader

`deployments upgrade-ant-binary --name <name>` force-installs the latest (or a
pinned/branch) `ant` binary on an already-running `prod-downloaders` or
`prod-continuous-downloader` client VM over SSH and restarts the service —
no infrastructure is touched. It exists because the `deploy`-time binary
install op is presence-gated (`test -f /root/.local/bin/ant && SKIP`), so a
plain re-`deploy` never picks up a new release on an existing deployment.
Minimal flags (`--name` only needed; `--branch-name`/`--repo-owner`/
`--ant-version` to override latest, `--ssh-key-path`,
`--restart`/`--no-restart`, default on) — VM IP(s) come from the deployment's
S3 client state, same as `deployments record-results`. `--no-restart` matters
for a completed `prod-downloaders` run: the service has no checkpoint, so
restarting it re-walks the whole address list from scratch, duplicating
metrics. Impl: `deploy/saorsa_deploy/cmd/upgrade_ant_binary.py`,
`provisioning/prod_downloader.py` `build_ant_binary_upgrade_command`.

See `deploy/README.md` §"Upgrading the `ant` binary in place" for the full argument table.

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

### Running prod profiling nodes

`prod nodes deploy` stands up node VM(s) — each running `--node-count` ant-node
services (default 15, matching the prod fleet) — that join the **production**
Autonomi network with every service wrapped in **heaptrack**, to chase the
intermittent ant-node memory leak under real prod load with a **debug build**.
This is the node-tier sibling of the prod client commands (uploaders/downloaders/
canary).

- **Vehicle (not `extend`, not `migration`).** `deployments extend` only reuses
  another *testnet's* bootstraps; the `migration` subsystem is the established
  prod-node provisioner but is modelled around migrating an existing source fleet
  (drain/destroy-source/inventory-splice) — wrong for a throwaway profiling VM.
  Instead `prod nodes` **seeds the deployment's S3 state with the production
  bootstrap peers** (`fetch_prod_bootstraps()` → WithAutonomi `bootstrap_peers.toml`)
  and then reuses the normal node lifecycle: `nodes infra` (all four providers'
  TF modules) → a prod-mode `nodes provision` → `nodes start`. Prod-mode node
  settings match `migration provision` (no `--evm-network` → arbitrum-one,
  `Restart=always`, `RestartSec=10`, `LimitNOFILE=65536`); `--stop-on-upgrade` is
  always emitted and no `--upgrade-channel` is set, so the pinned debug build
  keeps running (never auto-upgrades).
- **Node-side heaptrack** is the one genuinely new mechanism (heaptrack was
  client-only before): `provisioning/node.py` wraps each service's `ExecStart`
  `heaptrack -o <dir>/ant-node-<i> <ant-node …>`, composing **inside** the NAT
  `ip netns exec` prefix (so heaptrack profiles the node, not netns), plus an
  idempotent heaptrack apt-install op and a trace-dir mkdir. Traces go to the
  **attached data volume** (`/mnt/ant-node-storage/heaptrack/ant-node-<i>.<pid>.zst`),
  NOT the root disk — 15 debug nodes generate large, growing traces. heaptrack is
  default **ON** for this command (`--no-heaptrack` to opt out).
- **Debug build** reuses the existing `build ant-node --debug` (no migration/build
  change). Manifest `binary.debug: true` → orchestrator emits the `build ant-node
  --debug` step before the deploy; from the CLI run `build ant-node --debug` first
  against the same `--name`.
- **Optional monitoring** (`--monitoring`, off by default; `--grafana` opt-in):
  reuses `monitoring infra/provision` + the node provision `--with-telegraf` seam
  to forward host/per-service RSS metrics to InfluxDB (`testnet-metrics` bucket).
- **Bounded trace retention**: `--log-retention-days` (default 7; `0` = keep
  forever) installs a daily `ant-node-heaptrack-logprune.timer` that
  `find -mtime +N -delete`s old `.zst` traces.
- Registers a `Deployment` row with `deploy_args.kind = "prod-nodes"`. Fully
  declarative via `deployments up/plan/down --config` (`kind: prod-nodes`,
  `ProdNodesSpec` in `config/models.py`, `orchestrator/prod.py` `_nodes`); `up` is
  resumable so a retry skips the ~10-minute debug build. Example:
  `deploy/manifests/profiles/prod-nodes.yaml`. Impl: `cmd/prod_nodes.py`.
- `prod nodes destroy` tears down the node VMs and (by default) the paired
  monitoring stack; pass `--keep-monitoring` to retain it. `scp` the `.zst` traces
  off each VM before destroy.

See `deploy/README.md` §"Running profiling nodes against production" for the full
argument table.

### Running prod crash-debug nodes

`prod debug-nodes deploy` is the panic/crash-debugging sibling of `prod nodes`
(which is heaptrack *memory* profiling). It stands up a small, isolated,
**permanent** set of ant-node services joining the **production** network,
configured to capture crash information over time, with its OWN dedicated small
InfluxDB + short-retention Elasticsearch (not the main prod stack). Registers a
`Deployment` row with `deploy_args.kind = "prod-debug-nodes"`; fully declarative
via `deployments up/plan/down --config` (`ProdDebugNodesSpec` in `config/models.py`,
`orchestrator/prod.py` `_debug_nodes`). Impl: `cmd/prod_debug_nodes.py`. Example:
`deploy/manifests/profiles/prod-debug-nodes.yaml`.

- **Crash-debug node config** (all default-guarded in `provisioning/node.py`, so
  every other kind's systemd unit stays byte-identical):
  - **`--rust-backtrace`** (default `full`; `1` or `""` to disable) →
    `Environment=RUST_BACKTRACE=` on each service, PLUS an unconditional
    `Environment=COLORBT_SHOW_HIDDEN=1` (ant-node's color-backtrace filters every
    frame otherwise, printing `<empty backtrace>`). Both MUST be systemd
    `Environment=` lines (not inlined on ExecStart) so they propagate through the
    NAT `ip netns exec` wrapper.
  - **`--coredumps`** (default on) → `LimitCORE=infinity` on the unit + a global
    `kernel.core_pattern` sysctl writing cores to
    `/mnt/ant-node-storage/coredumps/` (on the data volume, or the root disk on
    volume-less providers), so even an OOM/SIGKILL leaves an inspectable core.
    Also installs `gdb` + `elfutils` (`eu-stack`) on the VM to symbolise those
    file cores in place — `systemd-coredump` is deliberately NOT installed (it
    would be inert against the file-based `core_pattern`, or force cores off the
    data volume). NB the release binary is stripped, so symbols are limited
    without an unstripped build; the live COLORBT backtrace is the higher-value
    signal. `--coredump-retention-days` (default 7) installs a daily prune timer.
  - **`--log-retention-days`** (default **1**) installs a daily systemd timer
    (`ant-node-debug-logprune`) pruning rotated ant-node debug logs under
    `/mnt/ant-node-storage/log` — deliberately much shorter than the coredump
    default, since logs (unlike core dumps) are already durably forwarded to
    Elasticsearch via `--forward-logs`. ant-node rotates `--log-dir` output
    daily on its own (one `ant-node.YYYY-MM-DD.log` per service per day) and
    self-prunes to `--log-max-files`/`ANT_LOG_MAX_FILES` (default 7), but at
    DEBUG level that built-in cap alone can still vastly outgrow the disk; this
    is an external backstop. `0` keeps logs forever. The install step also runs
    the prune once immediately (not just enabling the timer) so a disk that's
    already full gets relief right away rather than waiting for the next
    scheduled run.
  - **`--log-level`** (default `debug`). **ant-node has NO per-module env filter**
    — a single global level is the only option, so the requested per-module
    `RUST_LOG` targeting (`saorsa_transport`, `saorsa_core::*`,
    `ant_node::replication`) is NOT achievable at the deploy layer. `--log-level
    debug` resolves the node→ES drop filter to `TRACE` (keep DEBUG, drop TRACE)
    so the debug logs actually reach ES.
  - heaptrack is OFF by default here (use `prod nodes` for the heaptrack kind).
- **Dedicated monitoring/logging, selectable provider+region.** `--monitoring`
  (default on) stands up a small InfluxDB; `--forward-logs` (default on) stands
  up a dedicated Elasticsearch with a short ILM (`--elastic-retention-days`,
  default 3). Node VMs pick their provider via `--provider` (all five providers);
  the monitoring/ES VMs pick theirs independently via `--monitoring-provider` /
  `--elastic-provider` (DigitalOcean/OVH/**Contabo** only — those are the
  observability TF modules). Torn down on `destroy` unless `--keep-monitoring` /
  `--keep-elastic`.
- Vehicle is the same as `prod nodes`: seed the production bootstrap peers into
  S3 state, then `nodes infra` → prod-mode `nodes provision` → `nodes start`
  (arbitrum-one, `Restart=always`, `LimitNOFILE=65536`, `--stop-on-upgrade`, no
  upgrade channel so the pinned debug build keeps running). `binary.debug: true`
  emits a `build ant-node --debug` step; `up` is resumable.

See `deploy/README.md` §"Running crash-debug nodes against production" for the
full argument table.

### Cloud provider: Contabo (nodes + monitoring/logging)

**Contabo** is supported as a node provider (alongside DigitalOcean/Hetzner/
Vultr/OVH) and as a monitoring/Elasticsearch provider (alongside DigitalOcean/
OVH). It authenticates via an OAuth2 password flow reading four **`CNTB_OAUTH2_*`**
env vars (`CLIENT_ID`/`CLIENT_SECRET`/`USER`=account email/`PASS`=API password),
which the `contabo/contabo` Terraform provider reads directly from the
environment — so, like OVH, there is no `TF_VAR` credential mapping, just a
`_check_contabo_env` preflight (`deploy/saorsa_deploy/contabo.py`). TF modules:
`resources/contabo{,-monitoring,-elastic}`.

Two Contabo constraints shape the design:
- **No attachable block volume.** Contabo's provider has no standalone
  attachable/growable block-storage resource (extra disk is only a buggy
  in-contract `extraStorage` add-on), so Contabo VMs are **root-disk only** —
  reusing the existing `ovh_3az` root-disk machinery (`root_disk_ips` /
  `build_root_disk_dir_command`; `NO_VOLUME_PROVIDERS` in `cmd/infra_nodes.py`;
  a `root_disk` flag on `MonitoringProvisioner`/`ElasticProvisioner`). Size the
  root disk (a VDS) up front — it can't be grown online later without a
  disruptive rebuild. `--volume-size` etc. are ignored for Contabo.
- **Committed monthly billing.** Contabo instances are prepaid monthly contracts
  (no hourly billing, no proration on early destroy), which suits the *permanent*
  debug-node use case but not ephemeral churn.

Contabo images are referenced by UUID (not resolvable by name via Terraform):
pass **`--contabo-image-id`** (resolve via `cntb get images`) wherever a Contabo
tier is used; the operator public key is registered as a `contabo_secret` from
`--contabo-ssh-public-key-path` (default `~/.ssh/id_rsa.pub`) and `default_user=root`
gives root SSH. Contabo is currently wired for the single-`--provider` node path
(prod kinds / `nodes infra`) and the observability stacks; it is not yet a
`nodes.providers.<name>` option in testnet/extension manifests.

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

Optional per-testnet monitoring stack: InfluxDB 2.x (always) plus an **optional**
Grafana VM. Standalone — usable by this repo or external setups. Runs on
**DigitalOcean (default) or OVH** (`--provider ovh`).

**Grafana is opt-in and OFF by default**: `monitoring infra` creates only the
InfluxDB VM unless `--grafana` is passed (manifest `monitoring: { grafana: true }`).
The choice is recorded as `grafana_enabled` in the monitoring S3 state. The
Terraform `grafana` resources are `count`-gated on an `enable_grafana` variable
(with `moved` blocks so pre-existing 2-VM stacks migrate to index `[0]` in place
rather than being recreated), and `monitoring destroy` passes `enable_grafana`
back from state so the destroy plan matches what was created.

When enabled, Grafana is split onto its own small VM so its panel rendering
doesn't compete with InfluxDB for CPU/RAM (the cause of large dashboards like the
per-node RSS panels failing to render on a single shared box). The InfluxDB VM
carries the data volume; Grafana reaches it over the public `:8086` endpoint. The
`monitoring infra`/`provision` flow (and `monitoring destroy`) manage both VMs in
one Terraform state. Three provision-time cases, keyed off `grafana_enabled`:
disabled (default) → InfluxDB only; enabled with its own `grafana_ip` → Grafana on
that VM; legacy single-VM stacks (no `grafana_enabled` key, no `grafana_ip`) →
co-locate Grafana on the InfluxDB VM. Prod client flows (canary/uploaders/
downloaders) likewise default Grafana off; pass `--grafana` to those deploy
commands to opt in.

### Commands

- `monitoring infra` — create the InfluxDB VM via Terraform (and the Grafana VM only with `--grafana`). `--provider {digitalocean,ovh}` (default digitalocean), `--droplet-size` (InfluxDB VM; default `s-4vcpu-8gb` do / `b3-8` ovh), `--grafana`/`--no-grafana` (default off), `--grafana-size` (default `s-1vcpu-2gb` do / `d2-4` ovh; only used with `--grafana`), `--volume-size`, `--volume-type` (ovh). OVH needs `OVH_CLOUD_PROJECT_SERVICE` + OVH API creds + an `ant_deploy` SSH key in the project.
- `monitoring provision` — install InfluxDB (InfluxDB VM) and, when Grafana was enabled, Grafana (Grafana VM); prints connection details. `--retention-days N` caps InfluxDB bucket retention (default `0` = infinite, the dev default; set e.g. 14 for prod). Retention is applied to both buckets at first provision.
- `deployments provision-telegraf` — install Telegraf for host metrics (CPU, mem, disk, net) → InfluxDB **and** node log forwarding → Elasticsearch in one pass (see "Telegraf provisioning" below). Metrics-only when only monitoring state exists. Retrofit path (`--inventory-path`/`--host-ips-file`) provisions Tier-1 host+procstat metrics only; the Prometheus app-metrics scrape is only wired on the `--name` path with `node_count > 0`.
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
  canary client VMs, tagged `host_role=CLIENT`), **plus** the client `ant`
  service's memory (see below).

On CLIENT-role hosts, `deployments provision-telegraf` appends two extra inputs
(from `resources/monitoring/telegraf_metrics_client.conf`, gated on
`host_role=="CLIENT"` so node configs stay byte-identical and the cgroup input's
"path missing" error never fires on the node fleet): a `procstat` keyed on
`system.slice/ant-*.service` (per-process RSS, tagged `metric_source=ant-client`,
2s interval) and a `cgroup` input reading `memory.current` + `memory.peak`
(whole-service live + high-water peak, 2s) for the same glob. The single `ant-*`
glob matches whichever client unit a VM runs (`ant-uploader`, `ant-downloader`,
`ant-prod-uploader`, `ant-prod-downloader`, `ant-prod-canary-upload`,
`ant-prod-canary-probe`) and nothing else. This covers testnet and prod
uploads/downloads/canary; the local-* prod kinds run no systemd unit and are not
covered. `memory.peak` is the reliable high-water companion when a sub-2s
terminal spike clips the discretely-sampled procstat RSS.

`monitoring provision` creates both buckets and mints an **org-scoped** Telegraf
token (read+write on every bucket, so adding a bucket never requires reminting
the immutable token). `deployments provision-telegraf` routes CLIENT-role hosts
to `client-metrics` and everything else to `testnet-metrics`. The Node Host
Statistics dashboard queries `testnet-metrics`; Client Host Statistics queries
`client-metrics` — neither can render the other's data regardless of the Role
dropdown. Bucket names are constants in `monitoring.py` (`INFLUXDB_BUCKET` /
`INFLUXDB_CLIENT_BUCKET`).

### Per-device disk I/O

**Both tiers** collect per-device disk I/O via a `[[inputs.diskio]]` stanza in the
shared base config (`resources/monitoring/telegraf_metrics.conf`), **on by default,
no flag**. Because the client config is an *append to the node base*, that one
stanza covers node-tier and client-tier hosts and reaches every install path
(`nodes provision`, `provision-genesis`, `bootstraps provision`, `clients provision`,
the standalone `deployments provision-telegraf`, and `monitoring info
--telegraf-config`). Distinct from `[[inputs.disk]]`, which is capacity only.
The `diskio` measurement emits the monotonic counters `read_bytes`/`write_bytes`,
`reads`/`writes`, `read_time`/`write_time`, `io_time`, `weighted_io_time` plus the
gauge `iops_in_progress`, tagged with the device `name` — throughput, IOPS, await
and `%util` all derive from those.

The **`devices` list is include-only** (the plugin has no exclude option), so the
glob `["sd*","vd*","nvme*","xvd*","dm-*"]` must name every wanted pattern — it spans
all providers' naming plus LVM `dm-*`, and deliberately does not match `loop*`
(Ubuntu's ~10 snapd loop devices would be that many dead series per host).

**Identifying the node storage volume.** diskio knows only kernel device names
(`sdb`, `dm-0`), never mountpoints, and the device backing `/mnt/ant-node-storage`
varies by provider and by raw-ext4 vs LVM-striped. So it is resolved **on the host at
provision time** (`findmnt -nro SOURCE` → `lsblk -nro KNAME`, the same idiom as
`set_reserved_blocks.py`; `build_data_device_env_command` in
`provisioning/telegraf_metrics.py`), written to `ANT_DATA_DEVICE` in
`/etc/default/telegraf-ant-metrics`, and matched by a `[[processors.regex]]` scoped
to `namepass=["diskio"]` that tags that device **`volume_role = "node-storage"`**.
The tag lands in InfluxDB, so Grafana and `record-analysis` filter on it directly —
no join against `disk`, no LVM breakage. On **root-disk providers** (Contabo, OVH
3-AZ) node data lives on the OS root filesystem, so the **root device** is tagged
(that series then also carries OS I/O — unavoidable when they share a disk).
**Client VMs** never fall back to root; they get the sentinel `__none__`, which
matches no device. Always test `volume_role == "node-storage"`, never mere presence.

**Archival.** `deployments record-analysis` now runs a fourth Flux query (`diskio`
alongside cpu/mem/net) and persists **`disk_io_by_device`** into
`deployment_host_results.host_metrics` — a dict keyed by device name, each holding
`volume_role` plus `read_bytes_per_sec` / `write_bytes_per_sec` / `reads_per_sec` /
`writes_per_sec` as `{avg,min,max,samples}` stats. Rates are computed *within* a
device (per-second deltas, negative deltas dropped as reboot counter resets).
Devices are deliberately **NOT** averaged together the way net pools across
interfaces — a root disk and a node storage volume are not interchangeable, and
averaging an idle root disk against a busy data volume describes neither. The
`record-analysis` summary table shows the node storage volume's figures (naming the
device); `print-recorded-results` renders one row per (host, device) with the
storage volume flagged. `host_metrics` is a JSON column, so this needed **no Alembic
migration** and pre-existing rows simply lack the key (the section is skipped).
Retrofit a running deployment by re-running `deployments provision-telegraf --name
<name>`, which regenerates the config and restarts `telegraf-ant-metrics`.

### Telegraf provisioning (folded into the provision passes)

The two Telegraf forwarders — Elasticsearch log forwarding (systemd
`telegraf-ant-elasticsearch`) and InfluxDB host metrics (systemd
`telegraf-ant-metrics`) — are installed **inside the existing provisioning
passes**, not as a separate step. In a `deployments up` run there is **no
standalone telegraf step**: the node-tier provision commands install Telegraf in
the same SSH pass they use to provision the ant-node services, so the node VM
list is walked **once**, not twice (the old separate pass over the large node
list was the deploy bottleneck).

- **`nodes provision` / `nodes provision-genesis` / `bootstraps provision`** take
  a **`--with-telegraf`** flag (plus `--drop-log-levels` / `--buffer-limit` for
  the ES side). When set, the command resolves whatever elastic / monitoring
  state the deployment has and folds the matching forwarders into its pyinfra
  pass — the first installs the Telegraf deb, the rest reuse it. A backend with
  no state is skipped; if neither resolves it warns and installs nothing (never
  fails node provisioning). The orchestrator adds `--with-telegraf` to all three
  node-tier steps whenever elastic and/or monitoring is enabled, deriving
  `--drop-log-levels` from `nodes.log_level` exactly as before.
- **Client host metrics** are installed by **`clients provision`** (best-effort,
  gated on `--monitoring-name`), alongside the client→ES log forwarder it already
  installs. So the client tier is also covered without a separate step.

Because Telegraf is installed during provisioning (which runs before
`nodes start`), host metrics still cover the node's lifetime from the first
second.

The mechanism: each node/bootstrap provisioner accepts `extra_op_providers` — a
list of objects exposing `add_ops(state, install=...)` — and folds their ops into
its state before `run_ops`. `cmd/telegraf_fold.py` `build_node_telegraf_providers`
resolves the ES + InfluxDB forwarders from state; the per-forwarder logic lives in
`provisioning/telegraf.py` (ES) and `provisioning/telegraf_metrics.py` (InfluxDB),
each exposing the `add_ops` seam; the shared install op is in
`provisioning/telegraf_install.py`.

The standalone **`deployments provision-telegraf`** command (combined ES +
InfluxDB in one pass over a host group; `provisioning/telegraf_combined.py`,
`cmd/provision_telegraf_combined.py`) is **retained for retrofit / manual re-runs**
against an existing deployment — it's just no longer part of the `up` sequence.

## Elasticsearch (Log Forwarding)

Optional per-testnet Elasticsearch instance on a dedicated VM. Enabled via `enable_log_forwarding` in deployment config. Telegraf forwards node JSON logs to the instance.

Runs on **DigitalOcean (default) or OVH** (`--provider ovh`, same OVH env-var /
SSH-key prerequisites as monitoring).

### Commands

- `elastic infra` — create Elasticsearch VM via Terraform. `--provider {digitalocean,ovh}` (default digitalocean), `--droplet-size` (default `s-4vcpu-8gb` do / `b3-8` ovh; bump for large testnets), `--volume-size`, `--volume-type` (ovh)
- `elastic provision` — install Elasticsearch 8.x, create index template, user + API key. `--retention-days` (default 7, ILM) + `--shards`
- `elastic info` — print connection details (URL, user, password, API key)
- `elastic destroy` — tear down Elasticsearch infrastructure
- Node log forwarding is installed by `deployments provision-telegraf` (auto-loads ES credentials from state), alongside InfluxDB host metrics in one pass — see "Telegraf provisioning" under Monitoring.

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
