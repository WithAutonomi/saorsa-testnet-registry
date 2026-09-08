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
    analysis/                   # `analysis` run-time measurement: snapshots, captures, restarts, ES/Influx extraction, pcap analysers
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

**Keeping the observability stack past teardown.** Testnet/extension manifests
accept **`keep_on_destroy: true`** inside the `elastic:` and/or `monitoring:`
block: `deployments down` then destroys the clients/nodes/bootstraps but leaves
that VM standing, so a run that went wrong stays investigable without paying for
the node fleet. Both teardown paths honour it, because the intent is persisted
into the **component's own S3 state** at create time (`elastic infra` /
`monitoring infra` gain `--keep-on-destroy`, recorded next to `grafana_enabled`) —
so a manifest-less `down --name X` behaves exactly like `down --config`. Default
false; state docs predating the option have no key, which also means destroy. The
key requires the matching `enabled: true` (a keep with no stack is a silent
no-op, so it is a validation error). Two deliberate non-couplings: retention is
untouched (a kept ES index still expires on `elastic.retention_days`, default 7 —
raise it if needed; the testnet Influx bucket is infinite by default), and the
primitive `elastic destroy` / `monitoring destroy` are never gated by the flag
since they are the cleanup route. `down` prints each retained stack's endpoint and
its exact destroy command, and says "torn down (observability stacks retained)"
rather than "has been destroyed". The registry status flip is unaffected —
`DESTROYED` is recorded by whichever command deletes the whole deployment state.
Logic: `_observability_destroy_steps` in `orchestrator/testnet.py`,
`build_destroy_steps_from_state` + `retained_observability` in
`orchestrator/__init__.py`.

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
- `deployments unextend --name DEV-02 --force` — inverse: deletes DEV-02's deployment state. Never touches the source. Refuses if DEV-02 still has nodes or clients (run `nodes destroy` / `clients destroy` first).

The pipeline for an extension skips the bootstrap/genesis stages entirely: `deployments extend` → `nodes infra` → `nodes provision` → `nodes start`. Destroy: `nodes destroy` → `deployments unextend`.

An extension may also carry its **own client tier** (`extension.clients`, same shape as `testnet.clients`, requires `extension.binaries.ant`) — the way to run a second client-version cohort against the same network for mixed-version testing. Extension clients ride the normal `clients infra` → `clients provision` → `clients start` pipeline under the extension's name: they bootstrap off the seeded (source) bootstrap peers, publish/fetch upload addresses under the extension's own S3 prefix (its downloaders consume its uploaders' addresses, not the source's), and report metrics to the extension's own monitoring stack — so each cohort's client metrics stay separable per deployment name. Destroy order becomes `clients destroy` → `nodes destroy` → `deployments unextend`.

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

- CLI: `prod downloaders deploy` / `prod downloaders destroy`. Same arguments as `prod continuous-downloader` **except** `--sleep-interval-secs` and the two retention flags are omitted.
- **Provider selection** (`--provider` for the client VM across all four providers, `--monitoring-provider`/`--monitoring-region`/`--monitoring-droplet-size`/`--monitoring-volume-type` for the monitoring VM) mirrors the continuous variant — the client provider prefix was previously hardcoded to DigitalOcean, so a one-shot run could not share a vantage point with a Hetzner/OVH continuous baseline. Destroy reads both providers back from S3 state.
- **`--download-diagnostics`** (default **off**) passes `ant file download --download-diagnostics <PATH>`, writing one JSONL record per chunk-fetch attempt. Default off because the flag exists only on instrumented `ant` builds — a stock binary rejects it and every download fails. The path is **per-operation** (`/var/log/ant-prod-downloader/<op>.diag.jsonl`, beside that op's `.stdout`/`.stderr`) because `ant` opens it with `truncate`: one shared path across a multi-address run would retain only the last address's records. Threaded via a `DOWNLOAD_DIAGNOSTICS` env-file line, mirroring `MEMORY_PROFILING`. Available on both download kinds and as a manifest key.
- Registers a `Deployment` row with `deploy_args.kind = "prod-downloaders"`.
- The persisted `deploy_args["addresses"]` (list of resolved addresses, also written for `prod-continuous-downloader`) is rendered inline by `deployments slack-post` instead of the raw `addresses_file` path.
- **`deployments generate-inventory` covers both download kinds.** They keep no deployment state (they join production), so the inventory is built from S3 client/monitoring state plus the registry row: `node_vms`/`bootstrap_vms` empty and no devnet manifest (that describes a self-hosted network and its wallet key — meaningless here), but `client_vms` gives the VM IP/provider/region/SSH and a **`client_config`** section (schema **5**) gives the address list, the resolved binary, the settings that change how `ant` is invoked (`bootstrap_cache`, `download_diagnostics`, `memory_profiling`, `rust_log`, `clock_skew`, `network`, `sleep_interval_secs`) and the on-VM `paths` for the artefacts. The **`paths` block is the teardown-critical part**: nothing collects the per-op `.stdout`/`.stderr`, the `.diag.jsonl` or the heaptrack traces, and they die with the VM. The address list lives only in the registry, so `client_config` needs `DATABASE_URL`; without it the VM sections are still written and the command warns. `inventory.py`'s `SUPPORTED_SCHEMA_VERSIONS` accepts 3/4/5 — a client inventory has no `node_vms`, so `load_node_inventory` correctly rejects it. **`deployments up` runs the step automatically** for these two kinds (after the deploy, before `slack-post`), mirroring the final step of the testnet sequence; it is **`best_effort`**, so a snapshot failure warns rather than halting a run whose infrastructure is already up and billing. The other prod kinds emit no such step — `generate-inventory` has no path for them yet and would fail at the end of an otherwise-successful deploy.

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

### The production upload file pool (`prod pool`)

Production uploads must use **real files**, and a file must **never be uploaded
twice** — a canary measuring chunk loss on content already on the network for
months measures nothing. `prod pool` is the catalogue those files come from plus
the ledger of what has been handed out. Three tables in the registry:
`prod_file_pool` (references, never bytes), `prod_file_pool_digests` (one row per
file+algorithm) and `prod_file_pool_claims` (one row per hand-out). Impl:
`db/saorsa_db/{size_class,models/prod_file_pool}.py`,
`deploy/saorsa_deploy/{pool_selection,pool_sources,results_guard}.py`,
`cmd/prod_pool.py`.

Two kinds live in one catalogue — `url` (fetchable by a VM) and `local` (a path
on the operator's machine) — and **the consumer chooses the kind, never a flag**,
so no manifest can ask an uploader VM for a file on the laptop. Selection is by
**size class** derived from bytes (`tiny` <10MiB, `small` <100MiB, `medium`
<1GiB, `large` <5GiB, `xlarge` ≥5GiB; binary, half-open). Deliberately NOT the
`file_size_buckets_mb` in `resources/gates/staging-thresholds.yaml`, which bucket
transfer *durations* for the gates.

A class is sometimes too coarse, so every consumer also accepts
**`pool_min_size`/`pool_max_size`** (manifest key on `prod_uploaders.uploaders[]`
/ `prod_canary` / `prod_local_uploads`; spec tokens `pool_min_size=`/
`pool_max_size=` on `--uploader`; `--pool-min-size`/`--pool-max-size` on the
canary and local-uploads CLIs; `--min-size`/`--max-size` on `pool add
--from-listing` and `pool list`). They **narrow** each requested class without
changing the counts, so a selection's shape survives. The motivating case: the
**chunk floor is 4**, so a 14 KB file buys the same minimum loss observation as
a 5 MB one at the same cost of one permanently-spent entry — "`tiny`, but ≥5 MB"
is not a class. Units are binary and a bare number is bytes; **min inclusive,
max exclusive**, so `10MB`/`100MB` is *exactly* `small`. Bounds that exclude a
requested class are refused at manifest-validate time rather than surfacing as
"0 available", which reads as an empty pool. Impl: `size_class.parse_size` /
`bounded_byte_range`, `pool_selection.parse_size_bounds` / `validate_bounds`.
NB `--pool` and friends live on `prod canary **deploy**`, not the `prod canary`
group parser — a group-parser option must precede the subcommand, so the argv
the orchestrator emits would not parse. `test_pool_size_bounds_plumbing.py`
guards that every pool-kind argv the orchestrator emits actually parses.

A file is `available` (no blocking claim), `claimed` (handed out, unconfirmed),
`uploaded` (confirmed, address recorded) or **`partial`** (an upload stored some
chunks and stopped). `claimed` is **not** evidence anything reached production.
`partial` is the awkward middle: the file goes back in the pool because
completing it is useful work, but chunks of it ARE in production — so it is a
status of its own rather than a `released` claim, it keeps the chunk counts, and
**canary selection excludes it by construction** (not via a flag, so no manifest
can opt into a contaminated loss measurement). A **partial unique index** on
`prod_file_pool_claims` excluding `released` and `partial` makes "at most one
blocking claim per file" a database guarantee — two concurrent deploys racing for the last large file cannot both
win, and an `uploaded` file can never be reclaimed.

**Consumers.** `prod uploaders` (`pool=small:3,medium:2` spec token / per-uploader
`pool:` manifest key, exclusive with `urls`), `prod canary` (`--pool` /
`pool:`, exclusive with `urls_file`), `prod local-uploads` (`--from-pool` /
`pool:`, exclusive with `urls`/`local_dir`), and manual `prod pool checkout` for
the **desktop application** — which has no DB link, so the *checkout* spends the
file, not the upload. Four invariants: selection runs **before any
infrastructure** (a shortfall aborts with no VMs and no mainnet spend, naming
which inventory fell short); it is **all-or-nothing** across classes; the
selection is **persisted to S3** so `up --resume` reuses it rather than drawing a
fresh ~350 files; and claims attach to the **canonical** file so a duplicate
group is spent once.

That persisted draw is keyed on the deployment **name**, which every prod name
reuses each release train — so a new run finds the previous run's draw under its
own key. It is therefore only reused while it is still **live**: every file must
still carry the exact claim the draw recorded, still `CLAIMED`. A claim that
resolved to `UPLOADED`, was released, or was replaced by a different one means an
earlier run finished with that file, and the draw is discarded and taken again
(`selection_is_live` in `pool_selection.py`). Without this, STG-UL-01 on
2026-08-18 was handed the four files the 2026-08-11 run had already uploaded, and
only the duplicate-file gate stopped it re-uploading them for real money.
`prod uploaders destroy` also deletes the persisted draw, since a destroyed
deployment has nothing to resume.

**Claim resolution.** `record-results` closes claims for `prod-uploaders` (from
`deployment_prod_upload_results`) and `prod-canary` (from `prod_canary_uploads` —
different column names for the same concepts), recording address, chunks and
ANT/ETH cost; it upserts, so a re-run can revise. A `partial_upload` row
resolves the claim to `partial` rather than leaving it open — previously such a
file was reported as "no matching upload result", which reads as "the run never
touched this file", the opposite of what happened. A success beats a partial for
the same file. NB `list_all_prod_upload_results` **partitions** on success
(`failed_only=False` returns ONLY successes), so reaching partials needs the
other branch too — `import-history` makes both calls. `prod-local-uploads` resolves
inline per file (a failed upload **releases** rather than spends). Wrapped so a
registry predating the migration cannot cost the operator their results.

**Bulk cataloguing.** `pool add --dir` batches through
`add_pool_files_bulk` — a fixed ~7 statements per 500-file batch instead of ~4
statements plus a COMMIT *per file*. The per-file path measured 163ms/file
against the managed Postgres while reading and hashing the file took 1ms, so
~99% was round-trip latency; 300k files would have been ~13.6 hours. Two traps
found while building it, both caught by a test asserting statement count is flat
in file count rather than by timing: ORM `session.add` per row issues
INSERT..RETURNING per object (use Core `insert()` with a list), and a
`session.refresh()` loop plus a lazy relationship load reintroduced a SELECT per
row after the inserts were already batched. Two further per-row traps found only by running it against a real archive
directory: the duplicate links were written with an UPDATE each (207 links =
5.3s), now one ORM bulk-update-by-primary-key; and the method returned ORM
instances, which `session.commit()` expires — so the caller's summary re-fetched
every row at two statements each, ~46s for 1156 files, *after* the progress bar
had closed. It now returns detached `PoolFileRecord` values that cannot talk to
the database. Re-runs are flat too — an interrupted import skips known files
without a round-trip each. Semantics match
the per-file path exactly, including duplicate-digest linking *within* a batch,
where the pre-flight query cannot see an uncommitted sibling.

**Scale.** Three things narrow in SQL rather than in Python, which matters once
the local dataset lands: `list`'s LIMIT reaches the database (slicing a
materialised list cost 2.28s to return ten rows from a 200k catalogue), tags are
an indexed `prod_file_pool_tags` side table rather than a JSON column (2.11s →
0.00s), and `pool_stats` COUNTs rather than materialising twenty result sets
(4.47s → 0.15s). Tag matching is OR across the given tags, lower-cased on write.

**Scale of demand.** The canary is the dominant consumer: ~350 distinct files burned
permanently per run (382/311/313 measured), small+medium only. `prod pool add
--from-listing internet-archive:<item>` reads IA item metadata — name, size, MD5
and SHA1 per file — so hundreds of entries are catalogued without downloading
anything, and those real checksums are what let an IA URL and a local copy be
recognised as one piece of content (`duplicate_of`).

**Attribution.** `deployment_prod_upload_results` is written by **two** kinds —
remote `prod-uploaders` runs via `record-results`, and `prod-local-uploads`
inline — and the table carries no hint of which. Measured on the live registry:
444 of 583 rows are local uploads, so attributing the table to `prod-uploaders`
mislabels the majority. `import-history` therefore resolves the source from the
deployment's `deploy_args.kind`. `prod_canary_uploads` needs no such lookup —
only a canary writes there, so the table settles it. Because the command is a
re-runnable sync rather than a one-shot, a re-run **corrects** the attribution of
claims a previous import got wrong, but only ones it created itself (identified
by their note) — never a claim a real run took.

**Two honest limits.** `import-history` matches on **file name** — all the
recorded history holds — and errs toward marking spent, since a false match is
undone by `release` while a missed one ruins a canary. And a URL from a source
publishing no checksum that is never downloaded **cannot** be deduplicated before
upload; `verify` reports how many entries are in that state, and the datamap
address (verified content-deterministic: 15/15 repeat uploads produced one
address each) catches it afterwards.

**Teardown guard.** Remote upload results sit in the deployment's InfluxDB until
`record-results` runs, and teardown destroys that VM. `prod uploaders destroy`
(and `prod canary destroy --no-keep-monitoring`) compare recorded rows against
the deployment's pool claims — which say exactly how many files the run took —
and **warn loudly rather than block**: refusing on a non-TTY would strand
billable infrastructure when `down` runs unattended. `--skip-results-check`
silences it. Impl: `results_guard.py`.

**`prod files` now reads both upload tables.** It previously read only
`deployment_prod_upload_results` and so could not see the ~1006 canary files —
two thirds of what production holds.

See `deploy/README.md` §"Managing the production file pool" for the full command
and argument reference.

### The daily production write test (`prod continuous-uploader`)

Nothing else here tests **writes** to production on a schedule (V2-837): the
daily download test re-fetches a fixed pre-June list, so a break in *uploading*
would reach us from a user first. `prod continuous-uploader deploy` stands up
one uploader VM that claims a few pool files a day, uploads them to production
and reads each straight back, plus **N verifier VMs in other regions** that
independently download every address and compare a **SHA-256 against the exact
bytes the uploader uploaded**. Registers `deploy_args.kind =
"prod-continuous-uploader"`; fully declarative (`ProdContinuousUploaderSpec`,
`orchestrator/prod.py` `_continuous_uploader`). Impl:
`cmd/prod_continuous_uploader.py`, `provisioning/prod_continuous_uploader.py`,
`resources/scripts/prod_continuous_uploader.py` + `prod_upload_verifier.py`.
Example: `deploy/manifests/profiles/prod-continuous-uploader.yaml`.

- **It claims from the pool as each upload happens — the only kind that does.**
  Every other consumer draws before any infrastructure exists because it needs
  the whole set up front. A forever-running deployment has no "whole set", so
  the uploader VM holds its own registry connection and calls `claim_pool_files`
  per cycle. Nothing is reserved in advance (the canary's inventory is not
  locked up for months), and a teardown strands nothing. Deploy time only
  **preflights availability** and prints the runway; it deliberately does not
  claim.
- **VM-side database access**, also unique to this kind. Both roles run under a
  venv at `/opt/ant-registry-client` where **`saorsa-db` is installed** (built
  from the working tree, shipped over SSH via `files.put`), so they call the
  *same* tested `claim_pool_files` — canonical-id resolution, the
  partial-unique-index race arbitration and the all-or-nothing pre-check are
  never reimplemented, because a drifting second copy double-spends files.
  `db/` therefore requires **Python ≥3.12**, not 3.13: the VM images are Ubuntu
  24.04. The registry needed no firewall change — the cluster is deliberately
  open from any IP, gated by password over TLS
  (`resources/digitalocean-registry-db/main.tf`).
- **`prod pool client-role`** mints a least-privilege Postgres role for the VMs:
  SELECT on the catalogue + `deployments`; SELECT/INSERT/UPDATE on
  `prod_file_pool_claims` and the two result tables; **no DELETE, no DDL,
  nothing else**. The URL is printed, never stored — the manifest names an env
  var (`pool_database_url_env`), as it does for the wallet key.
- **Content verification, a first for this system.** The hash is taken over the
  bytes handed to `ant`, so it does not depend on the pool entry carrying a
  catalogued digest (many carry only md5/sha1). Verdicts:
  `ok` / `digest_mismatch` / `download_failed` / `inconclusive`.
  **`digest_mismatch` is not a flavour of failure** — a download that fails is
  an availability problem, one that returns *different bytes* is a correctness
  problem — and a recorded mismatch is **never overwritten** by a later success.
  **Only `ok` and `digest_mismatch` are terminal**: a failed or inconclusive
  check is re-queued next cycle, because one transient blip would otherwise
  brand a healthy file unavailable forever. Since a retry overwrites the row,
  `attempts` + `first_failed_at` distinguish a blip from a week-long outage.
- **The verifiers do not shard.** Every verifier checks every address: a second
  region confirming *the same file* is the point, unlike the canary, which
  shards probes for throughput. They also keep **no local state** — the work
  queue is `list_unverified_uploads_for_host`, so a replaced VM resumes and an
  outage of any length is caught up. (The canary probe needs SQLite only because
  it cannot reach Postgres.)
- **Postgres is authoritative, InfluxDB is dashboards.** Both roles write
  results as they happen into `deployment_prod_upload_results` (+ new `sha256`
  column) and **`deployment_prod_upload_verifications`**. Influx gets
  `prod_upload`, `prod_upload_verify` and `prod_upload_pool_shortfall`, but its
  retention is bounded — reading the address list from Influx would make a
  verifier offline >7 days miss those addresses permanently. So
  **`record-results` is a no-op for this kind** (it says so); use
  `print-recorded-results`, which renders per-region verification counts.
- **Pool exhaustion is a signal, not a crash**: the shortfall goes to its own
  measurement (so "the pool ran dry" is never misread as "the network refused")
  and the service sleeps and retries. Topping the pool up heals it with no
  redeploy.
- **Disk must exceed the largest drawable file on every VM** — the uploader
  writes the source to disk before uploading, each verifier downloads it back
  before hashing. The manifest validates this; `xlarge` with no `pool_max_size`
  is refused because the requirement is then unknowable.
- Claims resolve **inline**: success spends, partial marks partial (chunks are
  on the network, so a canary must never get it), and a failure to fetch the
  *source* **releases** — nothing reached the network, so burning it would be a
  lie. `destroy` stops the services first and **warns** about unresolved claims
  without releasing them: a claim that might correspond to a real upload must
  never be freed by a sweep.
- `generate-inventory` covers the kind (all three VMs + a `client_config`); `up`
  emits it automatically as a `best_effort` final step. The address list grows
  forever, so the inventory names the authoritative tables instead of inlining
  it.

See `deploy/README.md` §"The daily production write test" for the full argument
table.

### Running prod uploaders

`prod uploaders deploy` is the upload-side equivalent of `prod continuous-downloader`: it stands up one or more client VMs that each upload a list of public files to the production Autonomi network with `ant --json file upload --public`, then exit when the list is exhausted. Per-file results (chunks_stored / chunks_failed / address / file size / duration) are emitted to a `prod_upload` Influx measurement on a paired monitoring VM.

- Multi-VM. Repeatable `--uploader REGION:VM_SIZE:urls=PATH` (region and vm_size required, then one file source — `urls=` XOR `pool=`); each uploader VM downloads its assigned URL list at runtime, uploads each file with `--public`, deletes the local copy, exits. **There is no `concurrency=` token** — the service is strictly serial, one file at a time (the only `--*concurrency` in the package is `prod canary --probe-concurrency`). Other accepted tokens: `pool_min_size=`, `pool_max_size=`, `exclude_partial=`, `payment=`, `log_level=`, `clock_skew=`, `network=`, `bootstrap_cache=`, `forward_logs=`, `memory_profiling=`, `rust_log=` (last).
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
  - **`--log-retention-days`** (default **2**) installs a daily systemd timer
    (`ant-node-debug-logprune`) pruning rotated ant-node debug logs under
    `/mnt/ant-node-storage/log`. ant-node rotates `--log-dir` output
    daily on its own (one `ant-node.YYYY-MM-DD.log` per service per day) and
    self-prunes to `--log-max-files`/`ANT_LOG_MAX_FILES` (default 7), but at
    DEBUG level that built-in cap alone can still vastly outgrow the disk; this
    is an external backstop. `0` keeps logs forever. The install step also runs
    the prune once immediately (not just enabling the timer) so a disk that's
    already full gets relief right away rather than waiting for the next
    scheduled run.
    **The prune unit restarts the ES log forwarder, and must.** Telegraf's
    `tail` input holds an open fd per matched file with no idle-close, so
    `find -delete` alone unlinks without freeing: files leave `du` while ext4
    keeps the blocks and `df` hits 100%. Measured on ant-prod-debug-01
    (2026-08-02): **694 GiB across 70 fds** on a 968 GB disk showing 0 free with
    271 GB visible; restarting the forwarder released all of it. So the unit has
    a second `ExecStart=-/usr/bin/systemctl try-restart
    telegraf-ant-elasticsearch.service` **after** the `find` — `try-restart` so
    it no-ops under `--no-forward-logs`, `-`-prefixed so a forwarder failure
    can't fail the prune, and unconditional because ant-node's own
    `--log-max-files` self-pruning strands fds identically. This forces
    **`from_beginning=false`** for the kind (threaded via
    `build_node_telegraf_providers(from_beginning=...)`, default `True` so every
    other kind's TOML is byte-identical): telegraf keeps no tail offset across
    restarts, so backfill-from-start would re-ingest every remaining log on
    every prune. Any new prune timer deleting files something else tails needs
    the same treatment.
  - **`--node-count` (default 4) is forwarder-bound, not fleet-matched.** At
    `--log-level debug` each service emits ~758 lines/s (222 B avg) and one
    telegraf tops out ~2 700 lines/s — CPU-bound in the JSON parse + `rename`/
    `starlark`/`strings` chain, NOT blocked on ES (from the node VM, ES took
    ~8 000 docs/s at telegraf's own batch size, 0 write rejections, 13% CPU;
    bigger batches and `force_document_id=false` measured as no help). At 10
    services only **~36%** of lines reached ES and the forwarder had wedged for
    three days (190M docs/day → 5 684). So **Elasticsearch is a sample, not a
    copy**, and the on-disk logs are the authoritative record — which
    `--log-retention-days` discards. Raising one means raising the other.
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

### WebRTC-Direct testnets and the WASM client tier

`ant-cli` **cannot** exercise the WebRTC-Direct browser path — it links native
`ant-core`, so it takes the native transport however it is invoked. A new client
tier (V2-1169) runs a Node.js program importing **`ant-core` compiled to WASM**,
reaching nodes over WebRTC-Direct. Roles `wasm-upload` / `wasm-download`,
manifest keys `clients.wasm_uploaders[]` / `clients.wasm_downloaders[]`,
independent of the native pair (a testnet may run either or both side by side).
Example: `deploy/manifests/profiles/webrtc-wasm.yaml`.

They are **distinct roles, not a flag on the native ones**, because the role names
the VM, its Terraform workspace and its S3 state key — telling the transports
apart in all three is the point of running both. The role-to-unit map is explicit
in `deploy/saorsa_deploy/client_roles.py`; it replaced an `f"ant-{role}er"` string
trick that spells the right thing for exactly the two native roles.

Three pieces beyond an ordinary testnet, all no-ops when no WASM client is
configured (every existing deployment renders byte-identical):

- **A pinned WebRTC port on the bootstrap tier** — `bootstraps.webrtc_port`
  (CLI `--webrtc-port` on `nodes provision-genesis` / `bootstraps provision`;
  ant-node's flag is `--webrtc-direct-port`, alias `--webrtc-port`). Bootstraps
  are the one tier whose browser-facing address must not move: a WASM client
  bootstraps from it. Left unset ant-node **derives** the WebRTC port from the QUIC
  port, so it moves whenever that does. Regular nodes deliberately keep the
  derived port — their address changes propagate over the DHT as QUIC's do.
  **The port is only half of a stable address**: the multiaddr also embeds the
  DTLS certificate hash, which ant-node persists at
  `<root_dir>/webrtc-direct.pem`. It survives restarts, but **wiping a
  bootstrap's data volume rotates it and invalidates every cached browser
  address**. WebRTC-Direct is ON by default in a stock ant-node build (no
  feature flag, and no CLI flag to disable it).
- **`build ant-wasm`** — builds the WASM package plus the TypeScript client to
  `builds/{name}/{owner}/{branch}/ant-wasm-client.tar.gz`. A separate build path
  is unavoidable: `ant-core` declares `default = ["native"]` and a `browser-wasm`
  feature mutually exclusive with it (a wasm32 build without it stops at an
  explicit `compile_error!`), and the artifact comes from `wasm-pack`, not
  `cargo build --bin`. **Branch form only** — no WASM package is published as a
  release, so `binaries.ant_wasm` rejects a version.
- **`deployments webrtc-addresses`** — SSHes the **bootstrap VMs**, reads each
  bootstrap service's `<root_dir>/webrtc-direct.multiaddr`, and saves them as
  `bootstrap_webrtc_multiaddrs`. Cannot run at provision time (the address
  embeds the certhash, which exists only once a node has started) and must run
  before `clients provision` (a WASM client has no other way in). `deployments
  up` sequences it after `nodes start`. Addresses advertising a non-public host
  are rewritten to the VM's real IP.
  **A WASM client gets the SAME bootstraps as a native client, and no more.**
  The sole difference is address format — native takes bare `ip:port`, a browser
  needs the full multiaddr (UDP port + DTLS certhash + peer id) — which is the
  only reason a collection command exists at all: the certhash does not exist
  until the node has run, so the list cannot be written ahead of time.
  The node tier is deliberately **not** sampled, and there is no `--sample-nodes`
  flag. An earlier revision handed each client ~32 ordinary node addresses spread
  across the peer-id keyspace; production cannot supply that shape (a browser
  gets a small bootstrap list), so it masked discovery failures rather than
  exposing them. **If transfers do not work from the bootstraps alone, fix the
  node.** `clients provision` reads only `bootstrap_webrtc_multiaddrs` — the
  older `webrtc_client_bootstrap_multiaddrs` / `webrtc_seed_multiaddrs` keys in
  pre-change state are deliberately not read.

`clients provision` installs Node 22, unpacks the bundle to
`/opt/ant-wasm-client`, and writes `/etc/ant-wasm-client.env` plus the
`ant-wasm-uploader`/`ant-wasm-downloader` unit — targeted per host, so native
client VMs see none of it. The bundle is **always re-fetched** (unlike the
presence-gated `ant` binary install, which is why `deployments
upgrade-ant-binary` had to exist).

**Addresses are one shared pool per deployment** — both tiers publish under the
same prefix and downloaders list the whole prefix (as the native tier always
has), so a WASM downloader may fetch native-uploaded content and vice versa. The
per-uploader *object* is namespaced by role (`upload-` vs `wasm-upload-`)
because `client_index` restarts at 1 per role: without that a native and a WASM
uploader in one region both own `upload-<region>-1.txt`, and each whole-object
PUT flush erases the other's list.

**No `bootstrap_cache` option on either WASM role** — it selects between ant's
peer cache and an explicit `--bootstrap`, and a WASM client does neither. Passing
the token is a hard error, not a silent no-op.

**Payment: no private key ever enters WASM.** `ant-core` verifies each storage
quote and hands the set to a JS callback, which does ERC-20 `approve` then one
`payForQuotes` on the vault and returns the **mined** tx hash; `totalAmount` must
exactly equal the sum of quote amounts or the upload is rejected. The payment
network (chain id / token / vault) is read from a node's own `HELLO` rather than
configured locally, so the client cannot pay on the wrong chain.

**Results are kept separate at every layer, because the two transports are not
the same instrument** — measured on DEV-01-wasm the WASM tier ran ~2.2x slower on
upload and ~4.2x on download, so any blended figure describes neither and moves
with whatever VM mix a run happened to use.
- **Influx:** WASM file operations go to their own **`wasm-client-metrics`**
  bucket (`INFLUXDB_WASM_BUCKET`). They are the same `upload`/`download`
  measurements the native clients write, so one bucket would let any dashboard
  panel or ad-hoc Flux query average the two — the same reasoning that put
  client host metrics in their own bucket rather than behind a mutable tag. A
  WASM VM's *host* metrics still go to `client-metrics` like every other
  client's. `record-results` reads both buckets and deduplicates on
  `(host, start_ns, measurement)`, tolerating a missing WASM bucket so
  pre-feature deployments still record.
- **Postgres:** a nullable **`client_kind`** column (`native`/`wasm`) on
  `deployment_{upload,download}_records`, resolved from each VM's role — the same
  denormalisation pattern as `network_profile`. NULL on every pre-feature row,
  and NULL reads as native rather than unknown.
- **`deployments compare`:** the client-record gates (upload/download success
  floors, success rates, transfer p50/p95 per size bucket) are evaluated **once
  per transport** and suffixed `[native]` / `[wasm]` whenever a run has WASM
  records; with none, the gates are emitted exactly as before. Node-tier gates
  (CPU, RSS, leak, per-GB ratios, log counts) are NOT split — one fleet serves
  both transports.
- **WASM gates are advisory** (`wasm_gates_advisory: true` in
  `resources/gates/staging-thresholds.yaml`): reported in full, excluded from the
  overall verdict and the exit code. They are judged against floors calibrated on
  the native transport which WASM does not yet meet (92.1% upload / 94.9%
  download against 99% / 99.5% on the first measured run), so gating on them
  would hard-fail every run on a tier that is not yet what ships. Flip the flag
  once there is history to calibrate a WASM floor.
- `deployments slack-post` lists the two tiers under separate headings, and only
  when WASM clients exist.

**Profile:** `deploy/manifests/profiles/staging-rc-wasm.yaml` — staging-rc with
three of its ten uploaders and one of its two downloaders moved to the WASM tier
(same VM count and the same spread of sizes, regions, providers, payment modes
and clock skew) and `nat_percent: 0`, since WebRTC has no NAT traversal yet. That
last point makes it non-comparable to a staging-rc run on any node-tier metric
NAT affects.

**Verified against a local `ant-devnet`** (2026-09-07), not a cloud testnet: the
WASM client runs under **plain Node.js** with `node-datachannel` supplying
`globalThis.RTCPeerConnection` — no headless browser. HELLO, `find_node`, a
5 MiB download and a real paid upload with byte-identical round-trip all pass.

Two facts for whoever composes a run manifest:
- **NAT.** Nodes behind the testnet's simulated inbound NAT are unreachable over
  WebRTC-Direct without relayed WebRTC, and each dial burns a 10s DataChannel
  timeout (`ant-node/docs/WEBRTC_DIRECT_TESTNET.md`). Use `nodes.nat_percent: 0`
  — which also makes the run incomparable to a native baseline that used a
  different NAT fraction.
- **A WASM upload needs >= 7 initial PUT peers** before it will pay, so a small
  fleet fails every upload with "Witnessed close group returned only N/7".

See `deploy/README.md` §"WASM client VMs (the browser path)".

### Simulating residential connections on client VMs (`network`)

Client VMs sit on ~gigabit cloud links, which is not how real users reach the
network — and upstream rate, which dominates uploads, is where a home line differs
most. Any client VM can be shaped with `tc` to behave like a residential
connection via a **`network:`** key taking either a built-in profile name or an
inline mapping. Supported on `prod uploaders` (per-entry), `prod downloaders` /
`prod continuous-downloader` (spec-level, CLI `--network`) — **the primary use
case** — and on testnet/extension `clients.uploaders[]`/`downloaders[]`
(secondary; CLI spec token `network=NAME`). NOT wired to `prod canary` or the
`local-*` kinds. Omitting it leaves a deployment byte-identical to before.

Profiles live in `deploy/saorsa_deploy/resources/network/profiles.yaml`,
git-versioned with a `version:` echoed into every recorded run: `home-adsl`
(10/1 Mbit), **`home-calibrated`** (27.4/6.29 Mbit, 12.7 ms — the only *measured*
one, a real TalkTalk line as of 2026-07-30), `home-fttc` (60/18), `home-fttp`
(300/50), `mobile-4g` (30/8, 60 ms, 20 ms jitter, 0.5% loss). Its loss is 0
because 0/300 packets were lost — *below that sample's resolution*, not a proven
lossless line. Impl: `provisioning/network_profile.py` (pure functions, mirroring
the `clock_skew` seam), injected by `provisioning/{client,prod_uploader,
prod_downloader}.py`.

Four invariants, all load-bearing:
- **Monitoring and SSH are exempt** (TCP 22/8086/9200 into an unshaped `htb`
  class). Otherwise the VM's own Telegraf metrics and forwarded client logs
  compete with the transfer for the capped uplink — the measured transfer comes
  out slower than a real user's (no user ships their own logs) and the run's
  results can arrive incomplete, degrading the instrument taking the measurement.
  Keyed on **port, not address**, because the ES VM is created *after* the client
  VMs are provisioned, so its address does not exist when shaping is applied.
- **Delay is applied once, loss per direction.** RTT is a round-trip property, so
  `delay`/`jitter` go on egress only (both paths would double it); loss goes on
  both, since inbound loss is what makes a *download* behave like a home download.
- **Queue depth is not a knob but is always a choice.** A rate cap implies a
  queue; left to chance it gives either tail-drop collapse (throughput far below
  the cap) or bufferbloat (standing delay that swamps `delay`). An explicit AQM is
  pinned — `cake`, falling back to `fq_codel`. Relatedly every htb class sets an
  explicit `quantum`: shaped and unshaped classes are four orders of magnitude
  apart, so no single `r2q` avoids htb's "quantum too big/small" warnings.
- **Ingress needs an `ifb` mirror** — `tc` shapes egress only, so without it the
  downstream cap would not apply to downloads at all.

Applied by a systemd oneshot (`ant-network-impairment.service`) so shaping
survives reboot; the script is **convergent** (re-provisioning replaces rules
rather than stacking — two `netem delay 30ms` would be 60 ms). The profile *name*
is recorded on the four `deployment_*_records`/`_results` tables
(`network_profile`, NULL = unshaped = every pre-feature row); the full resolved
knob set lives once in `deploy_args`. Client egress is ~4x payload (fan-out), so
at `home-adsl`'s 1 Mbit up a 100 MB upload takes ~67 min and 1 GB ~11 hours — size
runs accordingly. **Never enable on a staging gate run**: `deployments compare`
gates on transfer p50/p95 and shaped percentiles are not comparable to an unshaped
baseline. See `deploy/README.md` §"Simulating a residential connection".

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

## The release ledger (`release-candidates` + `releases`)

Two related command groups record the release train in the registry. Both are
CLI-only — not exposed over the REST API or the MCP server — and both **require
`DATABASE_URL`**, exiting non-zero rather than silently no-opping the way the
deployment registry does. Both are driven by skills in the separate
`releases` repo (`/home/chris/dev/releases/.claude/skills/`), not from this one.

**`release-candidates`** (pre-existing) records each RC *cut*: one row per
binary target (`ant-node 0.17.1-rc.1`, `ant 0.3.3-rc.1`), its constituent crates,
and the PRs that landed in each. An `rc.N+1` respin cuts a **new** row — RCs are
not re-edited.

**`releases`** (V2-1015) records the *channel state* of what actually shipped:
`ship` → `promote` | `rolled-past`, plus `current` / `ls` / `show`. It exists
because "what is on stable, what is on beta, how long has it soaked" was
answerable only from GitHub tags and hand-transcribed prose in a Linear issue,
and a beta the channel **rolled past** left no trace anywhere but a closed issue.

Design points that are load-bearing:

- **A separate table, not lifecycle columns on `release_candidates`.** Respins
  produce several RC rows per release; the release is the lineage that shipped.
- **One row per binary target**, matching the RC granularity — a train records
  two releases sharing one `rc_branch`, and some trains ship only one.
- **`releases.version` is the final, suffix-free version** (`0.17.1`). The beta
  suffix lives in `beta_tag`. That is what lets `promote` complete the same row
  the `ship` created.
- **`release_crates` is NOT a reuse of `release_candidate_crates`.** RC crate
  rows carry rc-suffixed versions and RC tags (`evmlib 0.9.1-rc.1`,
  `ant-cli-v0.3.3-rc.1`); a released crate carries the suffix-free version and
  the final tag. Reusing them would record the wrong versions against the release.
- **The FK to `release_candidates` is resolved by the
  `(binary_target, binary_version)` natural key**, not by id — `release-from-rc`
  never holds the RC row id. It is nullable, and a miss warns rather than failing:
  a ship record is worth having without the link.
- **`shipped_to_beta` is the only entry state; `promoted` and `rolled_past` are
  terminal.** The repository refuses to leave a terminal state. A stable version
  that quietly stopped being stable would make every later answer untrustworthy.
- **`--reason` is required on `rolled-past`** — a rolled-past row with no reason
  says nothing the release's absence would not.
- **`releases current` reports the rolled-past betas since the current stable**,
  which is exactly the widened stable version-jump window (N±2) the next
  promotion has to carry.
- **Soak state has three values**: elapsed, not elapsed, and *unknown* (no
  planned promotion date recorded). Unknown is never collapsed into "not yet".

Consumers, all in the `releases` repo: `ship-to-beta` Step 7 (`releases ship`),
`release-from-rc` Step 10.3 (`releases promote --from-file`, replacing its old
"if a recording command exists" hedge), and `beta-health-verdict`, which sources
the beta version, ship date, soak window and the stable **control** version from
`releases current` instead of scraping the Linear issue, and records
`releases rolled-past` on a NO-PROMOTE verdict. Those skills call the **bare**
`saorsa-deploy` binary (their cwd is the `releases` repo, where `uv run` would
resolve the wrong project).

See `deploy/README.md` §"Recording releases and channel state" for the full
argument tables and the promote payload schema.


### Sizing uploader gas funding (`clients estimate-gas`)

Testnet uploader wallets hold a **fixed** amount of native ETH, so runway is inversely
proportional to the gas price — when the price rises the same funding buys fewer
payment transactions and an exhausted client fails **every** subsequent upload, in a
way indistinguishable from a network fault in the gate data. Staging run #566
(2026-09-01) lost six of ten uploaders that way after the Arbitrum Sepolia base fee
rose ~10x in three days, putting whole-run upload success (96.02%) and the
mixed-version phase (79.35%) below their hard-gate floors for a non-product reason.
Throughput was **not** the cause — that run did less work than its baseline.

`clients estimate-gas --config <manifest> --baseline-id <id>` is the read-only
preflight, run as **Step 3a of the `run-staging-testnet` skill**. It reads the current
base fee from the chain, takes per-uploader throughput from a previous run, and prints
the ETH each uploader needs plus whether the funding wallet covers the total.
**Exit 2 = something is short**; the fix is either the profile's `eth:` values (a
profile SHA change, so a re-baselining event needing the RM) or a top-up of the
funding wallet — no profile edit helps the latter.

Three load-bearing details: throughput is taken as each client's **rate over its own
active window**, never its raw count (a client that ran dry stopped early, so its
count would teach the next run to under-fund); scaling is on **base fee**, so the
priority fee and estimator buffer cancel in the ratio; and the funder check compares
against the **full** recommended total, because top-up is per-wallet and sequential
and a short funder leaves a half-provisioned run rather than failing up front.

Model + calibration: `resources/gas/client-gas-model.yaml` (versioned config, like the
gate thresholds — recalibration needs no release). Pure logic in `gas_model.py`, I/O in
`cmd/estimate_client_gas.py`, tests in `deploy/tests/test_gas_model.py` (which assert
the shipped config still reproduces its own six calibration points).

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

- `monitoring infra` — create the InfluxDB VM via Terraform (and the Grafana VM only with `--grafana`). `--provider {digitalocean,ovh}` (default digitalocean), `--droplet-size` (InfluxDB VM; default `s-4vcpu-8gb` do / `b3-8` ovh), `--grafana`/`--no-grafana` (default off), `--grafana-size` (default `s-1vcpu-2gb` do / `d2-4` ovh; only used with `--grafana`), `--volume-size`, `--volume-type` (ovh), `--keep-on-destroy` (record that `deployments down` must leave this stack standing — see "Keeping the observability stack past teardown" above). OVH needs `OVH_CLOUD_PROJECT_SERVICE` + OVH API creds + an `ant_deploy` SSH key in the project.
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

### Per-process memory composition (opt-in, `--node-memory-metrics`)

`procstat.memory_rss` is a single **total** and cannot say what the memory is made
of — the difference between a leak and a growing LMDB chunk-store mmap. Measured
on prod: `ant-node-9` at 880 MB RSS of which 469 MB file-backed, then 3.48 GB of
which 3.07 GB file-backed an hour later. That ambiguity made `memory_rss` misread
as a 20x leak on STG-03 run 475. Collection is **two tiers** (V2-972):

**Tier 1 — native `procstat` mmap, the routine signal.** Telegraf already collects
`memory_pss`, `memory_anonymous`, `memory_private_clean/_dirty`,
`memory_shared_clean/_dirty`, `memory_referenced`, `memory_swap` — they were only
**permission-gated**. Granting `CAP_SYS_PTRACE` unlocks them with **no new plugin
and no script**. The base config now sets `properties` explicitly to EXCLUDE
telegraf's default `mmap`, and the opt-in fragment re-declares the same process
sets with `properties = ["mmap"]` at **60s**. That split is load-bearing: mmap
reads the whole of `/proc/<pid>/smaps` — **6.78%** of a core at the 10s agent
interval vs **1.15%** at 60s vs **0.15%** without — so two tiers keep
`cpu_usage`/`num_fds`/`num_threads`/`memory_rss` at 10s while paying mmap once a
minute. Measured on the canary: telegraf **1.50%** total.

**Tier 2 — a systemd timer** (`ant-node-memory-collector.timer`, cadence
`--node-memory-interval`, default 300s, 60s for an investigation host, jittered by
a tenth). Supplies only what procstat cannot: `rss_anon/file/shmem`, `vm_swap`,
`rollup_pss_anon/file/shmem/dirty`, `rollup_swap_pss`, **`rollup_lazy_free`**,
`rollup_anon_huge_pages`, `rollup_file_pmd_mapped`, `rollup_shmem_pmd_mapped`.
`LazyFree` is the point — MADV_FREE'd pages stay resident until reclaimed, which
IS V2-970's shape — but it is a **lower bound** (the kernel can under-report, so
zero does not exclude it). The three huge-page fields go together: `AnonHugePages`
is anon-only and says nothing about a file-backed mapping.

**Tags are attached by the collector**, since it posts to Influx directly and
Telegraf's `[global_tags]` never apply: `host`, `testnet_name`, `host_role`,
matching procstat so the two measurements join. `host` is load-bearing — without
it the fleet's `ant-node-1` series all merge, AND `record-analysis` drops
host-less points so archival yields nothing. Caught on the canary before rollout.

**NOT a Telegraf `inputs.exec`** — that route measured **~14.9%** of a core,
of which ~12.5% was telegraf overhead that could not be explained (it persisted
with only a trivial input, and with the interval at 600s). The timer costs only
the collector's own work (~732ms per pass over 13 services).

**`CAP_SYS_PTRACE`, not `CAP_PERFMON`.** `/proc/<pid>/smaps*` is gated on
`ptrace_may_access`, NOT file permissions, so the unit's pre-existing
`CAP_DAC_READ_SEARCH` does not satisfy it. `CAP_PERFMON` (the narrower grant) was
tested on kernel 6.8.0-86: **0 of 13 processes readable**, same as nothing, with
and without NNP. Granted only on opt-in hosts, with a matching
`CapabilityBoundingSet` and `NoNewPrivileges=yes`; verify via `CapEff`
(`0x80004` = both, `0x4` = base only). The failure mode is **silent and partial** —
the cheap sources report normally while composition returns nothing — which cost
one canary cycle because all benchmarking had been done as root over SSH.

**Units:** telegraf reports `memory_rss` in **bytes** but smaps fields in **kB**;
corrected once at capture so everything stored is bytes.

**Archival is rollup-only** — `memory_composition_by_service` in
`deployment_host_results.host_metrics` (JSON column, no migration), excluded from
`deployment_host_metric_buckets` via `ROLLUP_ONLY_METRICS` (~164k rows / ~35 MB
per 990-node run against a table already 76% of the DB; see V2-973).

**`memory_rss − memory_anonymous` is non-anonymous/file-or-shared resident** — NOT
"file-backed", NOT "LMDB". Proving LMDB attribution needs a pathname-level `smaps`
snapshot, **not yet implemented**.

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

### Bucketed host metrics + per-service node RSS

`deployments record-analysis` writes host metrics **twice**: the unchanged whole-run
rollup in `deployment_host_results`, plus fixed-width time buckets in
**`deployment_host_metric_buckets`** (one row per `(host, bucket_start, metric,
device, service)` holding `avg`/`min`/`max`/`samples`). The buckets exist so an
arbitrary sub-window can be re-derived **after the monitoring stack is destroyed** —
previously `--window` fixed the window at capture and InfluxDB was then gone, which
is why the rc.1 staging comparison could only be reported directionally (3.37h vs
5.45h stored windows, the latter including the mixed-version phase).

`--bucket-secs` (default **300**; `0` disables bucket capture). **Width does not
affect accuracy**: for a boundary-aligned window, `sum(avg*samples)/sum(samples)` is
exactly the mean of the underlying 10s samples and `MIN(min)`/`MAX(max)`/
`SUM(samples)` are exact, at any width — each bucket carries its own min/max, so a
brief spike still surfaces. Width trades only window-edge precision (≤5 min) and
trend-plot resolution. 300s keeps a staging-sized run near 100k rows (~18 MB)
instead of ~500k; the registry is on DO's smallest managed Postgres (1 vCPU, ~1 GB
RAM → `shared_buffers=190MB`, 10 GB disk), where table size *is* query time.

Three invariants, all deliberate:
- **Rates are computed before bucketing, never after.** net/diskio are per-second
  deltas taken *within* an interface/device; bucketing raw counters and
  differencing afterwards is nonsense. `influx.py` is split into
  `normalise_host_metric_points` (delta/normalise) and `bucket_host_metric_samples`
  (windowing); `aggregate_host_metrics` keeps its signature and is now the
  degenerate "one bucket spanning everything" case, so the two paths cannot drift.
  A rate sample is timestamped at the **end** of its interval.
- **Re-aggregating `avg` must be sample-weighted**, never a mean of means — buckets
  hold unequal sample counts at run boundaries and missed scrapes. Done server-side
  in `repo.get_host_metric_window(deployment_id, start, end)`, which returns
  `host_metrics`-shaped entries so a window and the rollup are interchangeable.
  Half-open on `bucket_start`: a bucket is in or out whole.
- **Bucketed diskio is restricted to the `node-storage`-tagged device.** On a
  measured staging run 348 of 421 device series were root-disk boot/ESP partitions
  (`vda1`/`vda14`/`vda15`/`vda16`) duplicating `vda` — the majority of rows for no
  signal. The rollup still keeps every device.

Historical deployments have no bucket rows; consumers **must** degrade to the
rollup. **No backfill is possible.** `deployments prune-host-buckets --older-than N
[--name X] [--dry-run]` drops bucket rows only, leaving the rollup intact, so a
pruned run degrades to exactly its pre-bucketing view.

**Per-service node RSS** (`rss_by_service` in the rollup, `rss_bytes` as a bucket
series) is captured from the `procstat`/`memory_rss` field via the `node_service`
tag — Telegraf always collected it, nothing ever queried it, so process-level memory
was absent from the registry entirely. `mem_percent` is no substitute: whole-host,
includes page cache, cannot attribute anything to one of 15 services on a VM. Keyed
by `node_service` not PID (services restart, PIDs churn) and **never pooled into a
host total** — one leaking service is invisible in a 15-service mean. The
`record-analysis` table shows only the peak service as a scalar;
`print-recorded-results` renders one row per (host, service). Client-tier RSS is a
separate existing path in the `client-metrics` bucket and is unaffected.

### Normalised per-GB metrics (`deployment_derived_metrics`)

Raw measurements aren't comparable across runs — a longer run moves more bytes —
which is why the rc.1 gate verdict recorded **outbound bytes per GB of user
traffic** (directly the bandwidth bill) and **disk consumed per GB stored** as
NOT ASSESSED. `record-analysis` now derives both at extraction time into
**`deployment_derived_metrics`**, one row per `(deployment, metric,
window_label)` carrying value + numerator + denominator + unit + `source` +
`details` JSON. Ratios: `outbound_bytes_per_gb_uploaded`,
`outbound_bytes_per_gb_traffic` (÷ uploaded + downloaded),
`disk_bytes_per_gb_stored`. Components stored in their own right
(`node_outbound_bytes_total`, `node_inbound_bytes_total`,
`node_storage_growth_bytes`, `gb_uploaded`, `gb_downloaded`) so a ratio is
debuggable and a later gate can renormalise — impossible once the run's InfluxDB
is destroyed. Impl: `deploy/saorsa_deploy/derived_metrics.py` (pure functions),
consumed by V2-721's comparison-report command.

Invariants, all load-bearing:
- **Node tier only, by construction** — the numerator covers the queried Influx
  bucket, which defaults to `testnet-metrics`; client VMs report to
  `client-metrics`, so the workload generator's egress is excluded with no
  hostname heuristic.
- **Outbound summed from raw counter deltas, never integrated from the stored
  averages** — rate samples are emitted per interface and pooled by *averaging*,
  so integrating them reports a 2-NIC host's mean, not its sum.
- **Growth is `last - first`, not `max - min`** — a pruned host hasn't consumed
  its peak; the peak goes in `details.peak_growth_bytes`.
- **Denominator = successful ops whose `finished_at` is in the window**, from the
  per-op `deployment_{upload,download}_records`. `deployment_file_results` is an
  aggregate with no per-op timestamps and cannot be windowed. **Run
  `record-results` BEFORE `record-analysis`** or the ratios have no denominator.
- **Unmeasurable ⇒ omitted, never estimated** (empty denominator, or no
  node-storage series). `--no-derived` skips the whole computation.

This needed **filesystem occupancy**, which Telegraf collected (`[[inputs.disk]]`)
but nothing ever queried — so "chunk-store size" was never in Postgres for any
run, and `diskio` can't substitute (it counts I/O incl. rewrites/compaction, not
what's resident). The `disk` measurement is tagged `volume_role=node-storage` by
**mountpoint** (`path`), NOT device: `disk`'s `device` tag is the fstab-style
source (`mapper/vg-lv`), never the kernel name `diskio` reports, so on LVM the two
never match. `ANT_DATA_MOUNT` is resolved on the host beside `ANT_DATA_DEVICE`
(`build_data_location_env_command`). Lands as `disk_usage_by_mount` in the rollup
(with `first`/`last`/`growth_bytes`) and the `disk_used_bytes` bucket series
(node-storage mount only). Retrofit a live deployment with `deployments
provision-telegraf --name X`; **no past run can be backfilled**.

`deployments derive-metrics --name X [--window-start/-end ISO] [--label L]`
re-derives a window from Postgres alone after teardown. It is marked
`source=buckets` (outbound integrated as `avg × width`, growth between first/last
bucket average — approximate) versus `record-analysis`'s exact `source=capture`;
never compare the two, and `--label full-run` is refused outright. Bucket width is
**inferred** from the stored buckets (modal gap, so a collection gap can't move
it) because it isn't recorded on the rows and a wrong width scales every byte
total by exactly that factor.

### Deterministic staging gate comparison (`deployments compare`)

`deployments compare --id <candidate> --baseline-id <baseline>` compares two
recorded runs on their **stored metrics alone**, applies versioned thresholds,
and emits per-gate PASS / FAIL / GRAY / **NOT ASSESSED** as JSON plus a
fixed-format markdown report — upserted into **`deployment_gate_verdicts`**
keyed `(candidate, baseline, window_label)` so a re-run replaces rather than
accumulates. Same inputs ⇒ byte-identical report (the renderer never reads the
clock; `computed_at` is passed in). Exits `2` on a hard-gate fail only.

Both runs are identified by **deployment id, never name** — staging names are
reused every train. There is deliberately **no run-identity plumbing**: no
`staging_runs` table, no RC↔deployment FK, no `profile_version` column, no
"last shipped release" resolution. The baseline is the operator's judgment,
supplied as an id.

**Covers**: 2 hard gates (upload floor 99%, download floor 99.5%) + 8 soft gates
(success rate, transfer p50/p95 **per file-size bucket**, node CPU, node RSS,
leak slope, outbound bytes/GB, disk bytes/GB). Transfer percentiles are exact —
from the per-op `deployment_{upload,download}_records`. **`deployment_file_results`
is unusable**: an aggregate with no per-op timestamps, so it cannot be windowed.

**Deliberately does NOT cover** four hard gates — the report is silent on them
entirely: data integrity (no content verification exists
anywhere), crashes (panics → stderr/journald, not the JSON logs telegraf tails;
ES dies at teardown), mixed-version chunk storage (SSH `chunks.mdb` check), and
canaries (separate deployment). The **`evaluate-staging-run`** skill supplies
those observationally and assembles the release-issue report.

**Prefer a labelled phase window over `full-run`.** A full-run window is the span
of observed *metrics*, not the run's shape: the −3h node clock skew drags a host's
first points well before T0, and any post-measurement phase (the mixed-version
hour) is swept in even when the baseline never ran one. Both guards passed on
STG-01 #474 vs #470 (length 1.13x, coverage 1.12x) while the candidate carried
3h05m of pre-run data plus that extra phase — they check size and idleness, not
phase composition, and nothing records phase boundaries so it cannot be detected.
`compare` therefore *warns* on `--window-label full-run`. Moving that pair to
steady-state took CPU +19.28%→+15.29% and disk/GB −2.35%→−1.07%, with both runs
landing at ~8.7 GB/GB. Slice with `derive-metrics --id <id> --window-start/-end
--label steady-state` on **both** runs — **`--id`, not `--name`**: a name resolves
to the newest run bearing it, so a reused-name baseline is unreachable otherwise,
and `compare --window-label` needs the label on both sides.

**Connection-error counts (V2-842).** `deployments record-log-counts --name X`
counts a versioned set of named log strings in the run's Elasticsearch and stores
the **counts** in `deployment_log_event_counts`; `compare` then adds one soft gate
per group, failing above `connection_error_increase_percent` (10%, arbitrary —
there is no run history behind it). Must run **before teardown**: ES is per-run
scratch and nothing can be backfilled, so every pre-existing run reads NOT
ASSESSED (never zero — zero would show the candidate's counts against nothing).
Its own table, NOT `deployment_derived_metrics`, because `compare` refuses when
the two runs' derived `source` sets differ, which an `elasticsearch` row would
trigger against every existing baseline. The catalogue
(`resources/log_patterns/connection-errors.yaml`, version stored on every count)
groups patterns and says how each combines: `Connection attempt failed` and
`connect_happy_eyeballs failed` are one group taking the **max**, not the sum —
they are one failure logged at two layers (1,415/1,415 then 71,309/71,160 across
the DEV-02 comparison). Phrases are **stable substrings**, never the paraphrase
in an issue (`dial failed for all`, because the emit interpolates a count).
Node-tier index only; each count stores host cardinality, level breakdown,
observed span and total forwarded lines, because a bare count cannot tell a
fleet-wide regression from one sick node, nor more errors from more logging. An
**empty index aborts** rather than recording zeros; a zero against a populated
index records but warns (fixed / renamed / emitted at a dropped level are
indistinguishable from ES alone). Impl: `log_patterns.py`, `es_log_counts.py`
(pure), `cmd/record_log_counts.py`, `_soft_log_event_counts` in
`gate_comparison.py`.

**Refuses** on mismatched `window_label`, mismatched derived-metric `source`
(`capture` is exact, `buckets` approximate — comparing them measures the method),
or window lengths differing >1.25x. **Warns** on **activity coverage** (op span ÷
window) differing >1.3x: the per-GB numerators accumulate through idle time while
the denominators don't. Measured — 39.2% coverage → 14.86 GB/GB vs 52–61% → 9.1–10.1.
A window-*length* check cannot catch this; the length can be perfectly ordinary.

**Statistics.** The spec names CPU *median* / RSS *p50*; buckets historically held
only `{avg,min,max,samples}`. `_stats` in `influx.py` now captures `p50`/`p95` —
exact in the rollup, per-bucket in `deployment_host_metric_buckets` (nullable, **no
backfill possible**). Reports mark `[p50]` (exact whole-run), `[p50~]` (window
*estimated* from per-bucket percentiles — they don't compose like avg/min/max), or
`[mean]` (no percentile captured). Comparing two different statistics is NOT
ASSESSED, never silently blended. Full-run comparisons read the **rollup** (exact);
windowed ones read buckets.

Node RSS compares a **percentile across services** (default p95), not the fleet
max — with ~1000 services the max is an extreme order statistic (470→474: max
+64%, median +3.8%, p95 +1.8%). Worst service always in the detail.

The **leak check is absolute** (a leaking baseline shouldn't excuse the candidate)
and uses the **median** service slope, not a tail. Fitting a line through 2h of
jittery RSS gives slopes **symmetric about zero**: p5 −40…−60 %/h vs p95 +37…+60
%/h, ~50% of services negative, positive tail ≈ negative tail. A p95 threshold
would measure how noisy RSS is, not whether it leaks. Threshold 5%/h on the median
is calibrated against the **noise floor** (observed medians +2.44/−2.06/−0.66/−1.54),
**not** against a known leak — a PASS means "no fleet-wide RSS trend". A **tail
asymmetry** figure is reported (the negative tail is a free self-calibrating noise
reference for a subset leak) but not gated on.

The leak window is computed **per host**, from that host's own last bucket: the
profile skews node clocks ±3h and telegraf stamps by the host's clock, so a
fleet-wide `max(bucket_start)` assessed **15 of 997 services** on run 470.

Thresholds live in `resources/gates/staging-thresholds.yaml` (git-versioned,
`version:` echoed into every report and stored on the verdict) — the process
schedules a quarterly recalibration that must not need a tool release. Override
with `--thresholds`. Validation fails loudly; a threshold silently defaulting to
zero would be a rubber stamp printing PASS. `--history <ids>` supplies prior
**accepted** runs for the 2σ criterion — *selecting* them is judgment and belongs
to the operator/skill, only the arithmetic is in the tool; below the minimum the
percentage threshold stands alone. Impl: `gate_config.py`, `gate_comparison.py`
(pure), `gate_report.py`, `cmd/compare_runs.py`.

### Clock-skewed hosts must not vanish from Influx capture

**Every Flux `range()` in the query paths carries an explicit `stop`**
(`FLUX_FAR_FUTURE` in `influx.py`; also applied in `influx_canary.py`). Flux
defaults `stop` to `now()`, and `provisioning/clock_skew.py` jumps a VM's
**system** clock (nodes ±`SKEW_HOURS`=3 in a per-host random direction, clients
by an explicit signed duration), so a *forward*-skewed host's Telegraf writes
every point hours ahead of real time — and those points were silently discarded.

Shape-dependent, which is why it hid for so long: a long run lost only a
forward-skewed host's final skew's-worth of data, a short run lost the host
entirely. Found on DEV-03 (2026-07-25): a +3h node VM contributed **0 of 27**
hosts to `record-analysis` while holding 460 `memory_rss` points. The same
defect applied to the file-op (`record-results`), prod-upload and canary
queries — all fixed together, since it is one mechanism.

Skewed hosts are now captured, but their timestamps stay displaced, so a
sub-window re-derived from buckets covers a *different* stretch of their run.
`record-analysis` prints a **`Clock offset detected`** warning
(`detect_clock_offsets`) listing hosts deviating from the fleet **median**
`metrics_ended_at` by >15 min, with the signed offset. Median, not local clock:
no assumption about the invoking machine, and a few skewed hosts can't drag it.
Negative offsets may instead mean a host stopped reporting early — also worth
knowing. Regression guard: `test_clock_skewed_metrics.py` asserts no bare
`range(start: {start})` survives in either query module.

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

### Standalone Elasticsearch (`kind: elasticsearch`) — the beta-channel instance

A manifest kind whose *whole* deployment is one Elasticsearch instance — no
nodes, no clients, no binary (V2-1016). It exists so **community beta-channel
node operators**, on machines we do not run, have somewhere to forward logs;
consumed by the `beta-health-verdict` skill and the beta rollout watch. It
reuses the existing `elastic infra`/`provision`/`destroy` commands and the same
`saorsa-deploy/elastic/{name}.json` state; the kind adds `--standalone` (which
registers the `Deployment` row, since no `bootstraps infra` runs to do it) and
the public-ingest shape. Impl: `orchestrator/elastic.py`,
`provisioning/es_ingest_proxy.py`, `cmd/elastic_keys.py`. Example:
`deploy/manifests/profiles/elasticsearch.yaml`.

- **`public_ingest: true` is a different posture, not a flag.** A testnet's ES
  is reachable only by our own VMs for hours, so it runs **plaintext on 9200
  with anonymous access enabled** — fine there, unacceptable for an endpoint on
  the open internet. Public ingest binds ES to **loopback**, disables anonymous
  access, closes 9200, and fronts it with **Caddy on 443**. Off by default, so
  every existing testnet/prod stack renders **byte-identical** config (asserted
  by a test that diffs against the pre-change provisioner).
- **The proxy allowlists writes** (`_bulk`/`_doc`/`_create`, POST/PUT) and 404s
  everything else — so an endpoint added by a future ES version is unreachable
  by default rather than newly exposed. It also forces
  **`filter_path=errors,items.*.error`**: ES otherwise answers a bulk request
  with a result object *per document* (~200+ B each), making the response
  roughly the size of the request — pure egress on every batch, forever.
  Forcing it at the proxy means it holds regardless of the community
  forwarder's config.
- **DigitalOcean is not a preference, it is the only option that satisfies
  "extendable disk".** DO's `digitalocean_volume.size` grows **online**
  (`0 add / 1 change / 0 destroy` + `resize2fs`); **OVH's is ForceNew** and
  would wipe the data; Contabo has no attachable volume; Vultr/Hetzner have no
  `*-elastic` TF module. Egress is a non-issue here — ingest is **inbound**
  (free on DO), and the disk caps egress structurally at `volume_size /
  retention_days`, ~$2–5/month at 100 GB / 14 d.
- **The DNS record is the one manual step, and the deploy is two steps because
  of it.** The certificate is issued *during provisioning*, but the address does
  not exist until `elastic infra` has run. So: infra prints the **reserved IP** →
  operator sets the A record (**Cloudflare grey cloud / DNS only** — a proxied
  record terminates TLS at Cloudflare and issuance fails) → `up --resume` runs
  provision, which **preflights the record** and names the exact fix if it is
  wrong. Two journal entries mean the resume retries provisioning alone instead
  of recreating the VM. It is one-off: the **reserved IP survives a rebuild**
  (record it in `reserved_ip`), which is the whole reason for using one.
- **Port 80 is never opened** — TLS-ALPN-01 on 443 issues the certificate, so
  there is no HTTP-01 challenge to serve and no cleartext listener to redirect
  from.
- **Per-user credentials, not a shared key** (`elastic mint-key` / `list-keys` /
  `revoke-key`). Each participant gets their own ES API key with
  `create_doc` + `auto_configure` on `beta-nodes-*` and **no `read`** — a leaked
  key can add logs and nothing else: it cannot read another participant's logs,
  overwrite a document, or touch another index. A shared key would mean a leak
  re-keys *everyone* and nothing is attributable. The pre-existing
  `ant_log_writer` role is deliberately **not** reused (it grants `read` and
  `manage`). Keys **do not expire** by default — a participant runs their node
  indefinitely, so a lifetime means re-keying everyone on a schedule and a
  lapsed forwarder fails silently; revocation is the real control. An expiry
  **cannot be cleared** once set (the update API rejects a null, but accepts a
  longer value), so it is set at mint time or not at all. The encoded credential
  is shown **once**.
- **Log level is enforced server-side, because a participant's forwarder config
  is theirs to change.** `min_log_level` (default `info`) drops anything more
  verbose at ingest, via an ingest pipeline attached as the index's
  **`index.final_pipeline`**. That slot is load-bearing and verified against ES
  8.17.4: a client passing **`?pipeline=_none` does NOT bypass `final_pipeline`**,
  but the same request **DOES bypass `default_pipeline`** entirely — so never use
  `default_pipeline` for anything an untrusted client must not opt out of. The
  proxy also strips a client-supplied `pipeline` param (`uri query -pipeline`).
  Drop set derives from the minimum exactly like the node telegraf
  `--drop-log-levels` (`info` → DEBUG,TRACE; `debug` → TRACE; `trace` → none);
  WARN/ERROR never dropped; a document with **no** `level` is **kept**.
  A dropped doc returns `status: 200, result: "noop"` with `errors: false`, so
  forwarders treat it as accepted and do **not** retry — deliberate, since a
  `fail` processor would make them retry the batch forever. The cost: drops are
  **invisible** (nothing counts them) and participants are not notified.
  **`elastic set-log-level --name X --min-level debug` changes it at runtime** —
  it rewrites only the pipeline body, so no redeploy, no restart, no reindex;
  applies to the next write, and already-indexed logs are untouched.
  NB the **`reroute` processor is rejected in a final_pipeline** ("can't change
  the target index"), so quarantining verbose logs to a cheap index only works
  in `default_pipeline` — viable solely because the proxy strips `pipeline`.
  This bounds **disk**, not bandwidth: the document already crossed the wire.
- **`generate-inventory` covers this kind** (schema **6**, adding
  `elasticsearch_config` beside v5's `client_config`); `up` emits it
  automatically as a `best_effort` final step. `node_vms`/`client_vms` are
  empty, so `load_node_inventory` rejects it like a client inventory. Two
  deliberately separated sections: **`ingest`** (the public write URL, index
  naming, `create`-only rule, body cap, current `min_log_level`) and **`read`**
  — which is **NOT** the public URL. ES binds to loopback and 9200 is
  firewalled, so the read key against `https://logs.autonomi.com` returns
  **404 at the proxy**, indistinguishable from a missing index; the section
  gives `http://localhost:9200` plus a ready-made `ssh -L` tunnel command and
  says why. The **read-only key is included** (the file is meant to be shared);
  the **admin password is not** — the provisioner sets the `elastic` superuser
  and the log-writer from one secret, so the stored password is effectively
  superuser. The command warns about what the file contains.
- **The ingest contract (what V2-1021's forwarding daemon writes).** Verified
  against ES 8.17.4 with security on, using a key minted by `elastic mint-key`:
  `POST https://<host>/_bulk`, `Authorization: ApiKey <encoded>` (a real ES API
  key, **not** a bearer token), `application/x-ndjson`, index
  `beta-nodes-YYYY.MM.DD`, time field **`@timestamp`**, body ≤ **50 MB** (proxy
  cap; ES's own is 100 MB). The proxy is **transparent** — it is the
  Elasticsearch API, not a translation layer.
  **The action must be `create`; `index` returns a per-item 403** — the key
  grants `create_doc`, which cannot overwrite, so granting `index` would let one
  participant overwrite another's document by `_id`.
  **`_bulk` returns HTTP 200 even when items fail**, so a client MUST read
  `items[].status`: `201` indexed, `200` dropped by the level filter (expected,
  not an error), `403`/`429`/`400` real failures.
  The forced filter is **`errors,items.*.status,items.*.error`** and the
  `status` part is load-bearing: `items.*.error` alone is smaller (16 B vs 13 KB
  per 500-doc batch, against 100 KB unfiltered) but ES **collapses the items
  array to only the failures**, destroying the position correlation — a client
  then cannot tell which document a per-item 429 refers to and must retry the
  whole batch, duplicating everything that already succeeded (auto-ID `create`
  writes).
  Reads are refused (`_search` and `GET /` both 403).
- **One shared daily index, attribution by `beta_user` field** — not an index
  per user: ~100 users x daily x 14 days would exceed 1000 shards on a single
  small node.
- `letsencrypt_staging: true` while iterating: production issuance allows only
  **5 duplicate certificates per week** and the lockout outlasts the debugging
  session that caused it.
- The log-forwarder tooling that points community machines at this instance is a
  **separate issue** — this kind stands up the endpoint and mints the
  credentials only.

## Elasticsearch (Log Forwarding)

Optional per-testnet Elasticsearch instance on a dedicated VM. Enabled via `enable_log_forwarding` in deployment config. Telegraf forwards node JSON logs to the instance.

Runs on **DigitalOcean (default) or OVH** (`--provider ovh`, same OVH env-var /
SSH-key prerequisites as monitoring).

### Commands

- `elastic infra` — create Elasticsearch VM via Terraform. `--provider {digitalocean,ovh}` (default digitalocean), `--droplet-size` (default `s-4vcpu-8gb` do / `b3-8` ovh; bump for large testnets), `--volume-size`, `--volume-type` (ovh), `--keep-on-destroy` (record that `deployments down` must leave this stack standing — see "Keeping the observability stack past teardown" under Declarative manifests). NB a kept index still expires on the ILM policy, so raise `--retention-days` if the logs are needed for longer.
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
