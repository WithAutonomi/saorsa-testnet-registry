# Saorsa Testnet Registry

## Architecture

A single-user system for managing testnet deployments, structured as a uv workspace with four packages:

- **`db/`** (`saorsa-db`) - Shared database models, repositories, and Alembic migrations
- **`web/`** (`saorsa-web`) - Flask app serving a read-only REST API (`/api/...`) and web UI (`/deployments`) with Jinja2 + HTMX
- **`deploy/`** (`saorsa-deploy`) - CLI tool that provisions testnets via Terraform and Pyinfra, records deployments in PostgreSQL. **This is the interface** — everything below is a `saorsa-deploy` command.
- **`mcp/`** (`saorsa-deploy-mcp`) - **LEGACY, unused.** An MCP server wrapping a subset of `saorsa-deploy`. Not maintained: its `create_deployment`/`destroy_deployment` tools still shell out to the `infra`/`destroy` commands that `d3a56c5` (2026-04-17) split up, so they fail outright. Do not route work through it and do not extend it; drive the CLI.
- **Database** - PostgreSQL, managed via Alembic migrations (inside `db/`)
- **Deployment** - Digital Ocean App Platform (`app.yaml`)

### Data Flow

```
Claude / operator → saorsa-deploy (CLI) → infrastructure
                          ↕
                      PostgreSQL (via saorsa-db)
                          ↑
                   Read-only web UI (via saorsa-db)
```

`saorsa-deploy` records deployment lifecycle events directly in PostgreSQL via the `saorsa-db` repository interfaces. The web app is a read-only viewer.

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
    server.py                   # MCP server — legacy, unused (see Architecture)
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

A deployment is described by one declarative YAML **manifest** and run with one
command. This is the normal way to deploy; the legacy `runbooks/` bash scripts
and the primitive `infra`/`provision` commands remain supported.

- `deployments plan --config <m>` — print the computed command sequence (dry-run).
- `deployments up --config <m>` — run the full sequence. `down --config <m>` or
  `down --name <n>` to tear down (with `--name` alone the teardown is derived
  from what exists in S3 state).
- `deployments config show|validate --config <m>` — inspect/validate.

One manifest = one deployment's full desired state, discriminated by `kind`
(`testnet`, `testnet-extension`, `elasticsearch`, and the `prod-*` kinds). The
orchestrator (`deploy/saorsa_deploy/orchestrator/`, schema in `config/`) owns the
ordering and flag-coupling invariants and is the single source of truth. `up`
executes each step by re-entering the CLI in-process (`main(argv)`); a failing
step halts the run.

What will bite you:

- **`up` is resumable and a journal hit never silently skips.** Completed steps
  are journalled to the tfstate bucket, so a re-run can skip the ~10-minute
  binary builds. Interactively it prompts [r]esume / [d]elete / [a]bort;
  **non-interactively you must pass `--resume`** or a journal hit refuses rather
  than hanging. A successful `up` or `down` deletes the journal; `plan` and
  `up --dry-run` never touch it.
- **Monitoring (InfluxDB) defaults ON** for testnet/extension kinds; the
  **Grafana VM is opt-in and OFF** (`monitoring: {grafana: true}`). Elasticsearch
  log-forwarding is opt-in.
- **`extends: <path>` deep-merges a base profile, but lists REPLACE, not
  append.** Also `--set dotted.key=value` and `--name`.
- **Secrets are never in a manifest** — prod kinds name an env var
  (`secret_key_env`, default `ARBITRUM_ONE_SECRET_KEY`).
- **Never run two `up`/`down` for one name concurrently** (S3 + Terraform races).
- **`keep_on_destroy: true`** inside `elastic:`/`monitoring:` leaves that stack
  standing after `down`, so a run that went wrong stays investigable without
  paying for the node fleet. The intent is persisted into the component's own S3
  state at create time, so a manifest-less `down --name X` honours it too. A keep
  with no stack is a validation error. Retention is untouched — a kept ES index
  still expires on `elastic.retention_days` (default 7).

Reusable profiles are committed under `deploy/manifests/profiles/`
(`reference.yaml` = every option, `staging.yaml` = THE staging profile every
release train extends). Concrete run manifests live in the git-ignored
`deploy/manifests/runs/`.

`slack_post: true` (valid for every kind) appends a `deployments slack-post`
step; it is best-effort and never fails the run. `record-results --update-slack`
posts a per-kind results summary as a **threaded reply**, which needs the Web API
path's captured `(channel, ts)`.

Testnet node VMs can be placed on **DigitalOcean, Hetzner, Vultr or OVH**
(`nodes.providers.<name>`). OVH needs four `OVH_*` vars at both create AND
destroy time and has no availability preflight (unlike Hetzner/Vultr).

See `deploy/README.md` §"Declarative manifests" for the schema reference, and
§"Resumable runs (the step journal)" for the journal.

## Deploying & Destroying Testnets

Deploy and destroy by driving the **`saorsa-deploy` CLI** — normally a manifest
through `deployments up` / `down` (see "Declarative manifests" above). The MCP
server is legacy and partly broken; do not route deployments through it.

`saorsa-deploy` runs locally. It needs cloud credentials in the local environment: `DO_TOKEN`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, SSH keys. When `DATABASE_URL` is set, it automatically records deployments in PostgreSQL.

The full testnet sequence is `bootstraps infra` → `nodes infra` → `nodes
provision-genesis` → `bootstraps provision` → `nodes provision` → `nodes start`;
teardown is `nodes destroy` → `bootstraps destroy`. `deployments up`/`down`
sequence these from a manifest.

`nodes provision` installs the ant-node binary, writes systemd unit files, and **enables** the services; it does not start them. `nodes start` is a separate step that runs `systemctl start ant-node-{1..N}` in parallel across every node VM. The split exists so that optional setup (notably port-restricted NAT simulation configured via `--nat-percent`) can run between unit-write and service-start.

### Extending a testnet (shared bootstraps)

A testnet can be deployed as an **extension** of another — it reuses the source testnet's bootstrap VMs and peers with them, but has its own fresh node VMs (typically running a different `ant-node` binary for compatibility testing). Commands:

- `deployments extend --name DEV-02 --source DEV-01` — seeds DEV-02's state with a copy of DEV-01's `bootstrap_nodes` / `bootstrap_ip` / `bootstrap_port` and records `shared_bootstrap_from: "DEV-01"`. No infrastructure is created. Idempotent: re-running refreshes the bootstrap fields from the source.
- `deployments unextend --name DEV-02 --force` — inverse: deletes DEV-02's deployment state. Never touches the source. Refuses if DEV-02 still has nodes or clients (run `nodes destroy` / `clients destroy` first).

The pipeline for an extension skips the bootstrap/genesis stages entirely: `deployments extend` → `nodes infra` → `nodes provision` → `nodes start`. Destroy: `nodes destroy` → `deployments unextend`.

An extension may also carry its **own client tier** (`extension.clients`, same shape as `testnet.clients`, requires `extension.binaries.ant`) — the way to run a second client-version cohort against the same network for mixed-version testing. Extension clients ride the normal `clients infra` → `clients provision` → `clients start` pipeline under the extension's name: they bootstrap off the seeded (source) bootstrap peers, publish/fetch upload addresses under the extension's own S3 prefix (its downloaders consume its uploaders' addresses, not the source's), and report metrics to the extension's own monitoring stack — so each cohort's client metrics stay separable per deployment name. Destroy order becomes `clients destroy` → `nodes destroy` → `deployments unextend`.

`bootstraps destroy` now scans for any deployment whose `shared_bootstrap_from` matches the name being destroyed and refuses if dependents exist. `--force` does not override this — the user must `deployments unextend` each dependent first.

### Running a prod continuous-downloader

`prod continuous-downloader deploy` — one client VM running `prod_downloader.py`
against the **production** Autonomi network forever, with a paired mandatory
InfluxDB VM created in the same command. `deploy_args.kind =
"prod-continuous-downloader"`. Bootstrap peers are fetched at runtime from
WithAutonomi/ant-client; the curated address list is
`deploy/saorsa_deploy/resources/prod_addresses.txt` (`--addresses-path` to
override).

- **Single VM, download only** — one `--spec region[:vm_size]`; uploaders are
  rejected.
- Keeps a per-op stdout/stderr log under `/var/log/ant-prod-downloader/` (this is
  why it is Python, not the shared `downloader.sh`, which keeps none).
- `--sleep-interval-secs` sleeps at the end of each full cycle (default 900;
  `0` = back-to-back; `--daily` = 86400).
- Client and monitoring VM providers are **independent** (`--provider` across all
  four; `--monitoring-provider` DO/OVH only). Destroy reads both back from state.
- **Retention is bounded, so archive to Postgres**: `--log-retention-days`
  (default 7) and `--influx-retention-days` (default 7, first provision only).
  Run `deployments archive --name <n>` with `DATABASE_URL` at least as often as
  the Influx window — `deployment_download_records` is the authoritative history.
- `destroy` tears down monitoring too; `--keep-monitoring` to preserve it.

Fully declarative (`kind: prod-continuous-downloader`). See `deploy/README.md`
§"Running continuous downloaders against production".

### Running prod downloaders (one-shot)

`prod downloaders deploy` — the one-shot sibling of `prod continuous-downloader`:
walks the address list once and exits 0, with `Type=simple Restart=no` so the VM
can be left alone until destroy. Same provisioner and script, toggled by
`ONE_SHOT=true`; same monitoring, `record-results` and `slack-post` paths.
`deploy_args.kind = "prod-downloaders"`.

- Same arguments as the continuous variant **except** `--sleep-interval-secs` and
  the two retention flags.
- **`--download-diagnostics` is off by default**: the flag exists only on
  instrumented `ant` builds, and a stock binary rejects it and fails every
  download. The path is **per-operation**, because `ant` opens it with
  `truncate` — one shared path would retain only the last address's records.
- **Nothing collects the per-op logs, the `.diag.jsonl` or the heaptrack traces;
  they die with the VM.** `up` therefore runs `generate-inventory` automatically
  (inventory schema 7, `client_config.paths`) as a **`best_effort`** final step.
  It needs `DATABASE_URL` for the address list, and warns without one.

See `deploy/README.md` §"Running one-shot downloaders against production".

### The browser path on the production download kinds (WASM)

Both remote download kinds take **`client_kind: native|wasm`** (CLI
`--client-kind`, default native). `wasm` runs the transfers over WebRTC-Direct
through ant-core compiled to WASM instead of the native `ant`, measuring what a
browser client experiences against the real production network — which nothing
else does, since `ant-cli` links native ant-core whatever you pass it.
**One transport per deployment**, persisted in `deploy_args.client_kind` (WASM
measured ~4.2x slower on download, so a blended result describes neither).

`ant-wasm` is a single-operation CLI and a **drop-in for `ant`** at
`/usr/local/bin/ant-wasm`: it prints a final JSON line in the `ant --json` shape,
so the prod Python services, their units, their per-op logs and their restart
policies are unchanged by the transport.

What will bite you:

- **Over-cap is a SKIP, not a failure.** ant-core refuses a file above
  `WASM_MAX_FILE_BYTES` (1,000,000,000) — a property of the transport, not the
  network. Wire value `2` (`OpRecordStatus.SKIPPED`); stored, but excluded from
  **every** success-rate denominator. A **bare address line is never skipped** —
  unknown is not "too big".
- **Refused, not ignored**: `bootstrap_cache`, `memory_profiling`,
  `download_diagnostics` (all act on the `ant` process). `rust_log`, `network`
  and `clock_skew` work normally.
- **`record-results` takes `client_kind` from `deploy_args`, not S3 client
  state** — `clients destroy` deletes that state, and a re-run after teardown
  would relabel every WASM op as native and push its failures into the native
  hard floors.
- **8 GB minimum**, refused outright on a DO slug below it (a browser-path
  download holds ~4x the file in WASM memory); other providers only warn.
- Bootstraps are **ICE-probed before any infrastructure exists** (zero answering
  refuses, partial warns). `--skip-bootstrap-probe` exists for the 2–15 min
  listener delay after an ant-node restart.
- Results land in the **`wasm-client-metrics`** Influx bucket.

`wasm_binary: {branch, repo_owner}` takes a branch **or a tag** (`version:` is
refused — no ant-core WASM package is published as a release). `build ant-wasm`
runs `npm ci` against the committed lockfile and writes a `build-info.json` plus
an S3 sidecar holding the ant-client **commit** — the commit is what identifies
the code, where a moving branch name does not.

See `deploy/README.md` §"The browser path on the production download kinds".

### Upgrading the `ant` binary in place on a running prod downloader

`deployments upgrade-ant-binary --name <n>` force-installs `ant` on an
already-running `prod-downloaders` / `prod-continuous-downloader` client VM over
SSH and restarts the service. No infrastructure is touched.

It exists because the deploy-time install op is **presence-gated**
(`test -f /root/.local/bin/ant && SKIP`), so a plain re-`deploy` never picks up a
new release on an existing deployment. VM IPs come from S3 client state.

- **`--no-restart` matters for a completed `prod-downloaders` run**: the service
  has no checkpoint, so restarting re-walks the whole address list from scratch
  and duplicates the metrics.

See `deploy/README.md` §"Upgrading the `ant` binary in place".

### Upgrading the WASM client in place, and `client_build`

`deployments upgrade-wasm-client --name <n> [--name <n>...] --branch-name <ref>`
replaces the WASM bundle on a running **`prod-continuous-uploader`** or
**`prod-continuous-downloader`** VM over SSH and restarts its unit. The permanent
prod WASM deployments are a *series*, so iterating on the client must not break
it — `upgrade-ant-binary` covers only native `ant`, and re-running `up` would
call `record_deployment_created` again and start a fresh registry row.

- **Everything is refused before anything is built** (wrong kind, `client_kind:
  native`, unknown name, no client state), all problems reported in one
  invocation, because a build is ~10 minutes on a billing VM. `DATABASE_URL` is
  required.
- **One build, all names** — built under the FIRST name and the same bundle URL
  installed everywhere; identical bytes are what keep the deployments comparable.
- **The continuous uploader's verifier VMs are deliberately untouched** (they are
  native, so a `digest_mismatch` stays attributable to the upload rather than to
  a browser read path).
- A failure on one deployment still upgrades the rest, exiting non-zero at the end.
- On success `deploy_args.wasm_build` is replaced and a `wasm_client_upgraded`
  event records old/new commit; on failure `deploy_args` is left alone, so the
  registry never claims a build a VM did not take.
- A run manifest still pinning a different `wasm_binary.branch` is warned about —
  the next `up` would roll the upgrade back.

**`client_build` — which build performed each transfer.** `deploy_args.wasm_build`
holds only the CURRENT build and is overwritten, so the commit travels with the
operation instead: `ant-wasm` reads its own bundle's `build-info.json` and adds
`client_build` to its result line, and a nullable `client_build` column on the
three result tables receives it. **NULL means unknown build** and the upsert
**COALESCEs** (as `client_kind` does), so a re-record from a source without a
build never erases one. No past row can be backfilled.

See `deploy/README.md` §"Upgrading the WASM client in place".

### The production upload file pool (`prod pool`)

Production uploads must use **real files**, and a file must **never be uploaded
twice** — a canary measuring chunk loss on content that has been on the network
for months measures nothing. `prod pool` is the catalogue those files come from
plus the ledger of what has been handed out: `prod_file_pool`,
`prod_file_pool_digests`, `prod_file_pool_claims`.

Two kinds live in one catalogue — `url` (fetchable by a VM) and `local` (a path
on the operator's machine) — and **the consumer chooses the kind, never a flag**,
so no manifest can ask an uploader VM for a file on the laptop. Selection is by
**size class** derived from bytes (`tiny` <10MiB, `small` <100MiB, `medium`
<1GiB, `large` <5GiB, `xlarge` ≥5GiB; binary, half-open) — deliberately NOT the
`file_size_buckets_mb` in the gate thresholds, which bucket transfer *durations*.
Every consumer also accepts **`pool_min_size`/`pool_max_size`**, which *narrow* a
requested class without changing the counts (binary units, **min inclusive, max
exclusive**); bounds that exclude a requested class are refused at
manifest-validate time rather than surfacing as "0 available".

States: `available`, `claimed` (handed out, unconfirmed — **not** evidence
anything reached production), `uploaded`, and **`partial`** (an upload stored
some chunks and stopped, so completing it is useful work but chunks of it ARE in
production). A **partial unique index** excluding `released` and `partial` makes
"at most one blocking claim per file" a database guarantee — two concurrent
deploys racing for the last large file cannot both win.

What will bite you:

- **Canary selection excludes `partial` by construction** — not via a flag, so no
  manifest can opt into a contaminated loss measurement.
- **Selection runs before any infrastructure** (a shortfall aborts with no VMs
  and no mainnet spend, naming which inventory fell short), is
  **all-or-nothing** across classes, and is **persisted to S3** so `up --resume`
  reuses it. It is reused only while **live** — every file still carrying the
  exact claim the draw recorded, still `CLAIMED`.
- **A claim attaches to the canonical file**, so a duplicate group is spent once.
- **`--pool` and friends live on `prod canary deploy`, not the `prod canary`
  group parser** — a group-parser option must precede the subcommand, so the argv
  the orchestrator emits would not parse.
- For the **desktop application** (no DB link) `prod pool checkout` *spends* the
  file, not the upload.
- `record-results` closes claims for `prod-uploaders` and `prod-canary`;
  `prod-local-uploads` resolves inline (a failed upload **releases**). NB
  `list_all_prod_upload_results` **partitions** on success, so reaching partials
  needs the other branch too.
- **Teardown guard**: `prod uploaders destroy` compares recorded rows against
  claims and **warns loudly rather than blocking** — refusing on a non-TTY would
  strand billable infrastructure when `down` runs unattended.
  `--skip-results-check` silences it.
- **`import-history` matches on file name** and errs toward marking spent: a
  false match is undone by `release`, a missed one ruins a canary. It resolves
  the source from `deploy_args.kind`, since `deployment_prod_upload_results` is
  written by two kinds and carries no hint of which.
- A URL from a source publishing no checksum that is never downloaded **cannot**
  be deduplicated before upload; `verify` reports how many entries are in that
  state.

Scale: the canary is the dominant consumer, burning ~350 distinct files
permanently per run, small+medium only. `pool add --from-listing
internet-archive:<item>` catalogues hundreds from item metadata without
downloading anything, and those real checksums are what let an IA URL and a local
copy be recognised as one piece of content. `pool add --dir` batches its database
work; `list`, tags and `stats` all narrow in SQL rather than in Python.

See `deploy/README.md` §"Managing the production file pool".

### The daily production write test (`prod continuous-uploader`)

Nothing else tests **writes** to production on a schedule (the daily download
test re-fetches a fixed pre-June list), so a break in uploading would reach us
from a user first. `prod continuous-uploader deploy` stands up one uploader VM
that claims a few pool files a day, uploads them and reads each straight back,
plus **N verifier VMs in other regions** that independently download every
address and compare a **SHA-256 against the exact bytes the uploader uploaded**.
`deploy_args.kind = "prod-continuous-uploader"`.

What makes this kind unlike every other:

- **It claims from the pool as each upload happens.** Every other consumer draws
  before any infrastructure exists because it needs the whole set up front; a
  forever-running deployment has no "whole set", so the VM holds its own registry
  connection and calls `claim_pool_files` per cycle. Nothing is reserved in
  advance and a teardown strands nothing. Deploy time only **preflights
  availability** and prints the runway — it deliberately does not claim.
- **VM-side database access.** Both roles run a venv at
  `/opt/ant-registry-client` with `saorsa-db` installed, so they call the *same*
  tested `claim_pool_files` — canonical-id resolution, the index race arbitration
  and the all-or-nothing pre-check are never reimplemented, because a drifting
  second copy double-spends files. `db/` therefore requires **Python ≥3.12**, not
  3.13 (the VM images are Ubuntu 24.04). `prod pool client-role` mints a
  least-privilege Postgres role (no DELETE, no DDL); the URL is printed, never
  stored (manifest names `pool_database_url_env`).
- **Content verification, a first for this system.** The hash is taken over the
  bytes handed to `ant`, so it does not depend on the pool entry carrying a
  catalogued digest. Verdicts `ok` / `digest_mismatch` / `download_failed` /
  `inconclusive`. **`digest_mismatch` is not a flavour of failure** — a download
  that fails is availability, one returning *different bytes* is correctness —
  and a recorded mismatch is **never overwritten** by a later success. **Only
  `ok` and `digest_mismatch` are terminal**; a failed or inconclusive check is
  re-queued next cycle, so one transient blip cannot brand a healthy file
  unavailable forever. Since a retry overwrites the row, `attempts` +
  `first_failed_at` distinguish a blip from a week-long outage.
- **The verifiers do not shard** — every verifier checks every address, because a
  second region confirming *the same file* is the point (unlike the canary, which
  shards for throughput). They keep **no local state**, so a replaced VM resumes
  and an outage of any length is caught up.
- **Postgres is authoritative, InfluxDB is dashboards.** Influx retention is
  bounded, so reading the address list from it would make a verifier offline >7
  days miss those addresses permanently. **`record-results` is a no-op for this
  kind** (it says so) — use `print-recorded-results`.
- **Pool exhaustion is a signal, not a crash**: its own measurement, so "the pool
  ran dry" is never misread as "the network refused", then sleep and retry.
  Topping up the pool heals it with no redeploy.
- **Disk must exceed the largest drawable file on every VM** (the uploader writes
  the source to disk, each verifier downloads it back before hashing); `xlarge`
  with no `pool_max_size` is refused because the requirement is then unknowable.
- Claims resolve inline: success spends, partial marks partial, and a failure to
  fetch the **source releases** — nothing reached the network, so burning it
  would be a lie. `destroy` stops the services first and **warns** about
  unresolved claims without releasing them.

`generate-inventory` covers the kind; `up` emits it as a `best_effort` final
step. The address list grows forever, so the inventory names the authoritative
tables instead of inlining it.

See `deploy/README.md` §"The daily production write test".

#### The browser path on the daily write test (WASM)

`client_kind: native|wasm` (CLI `--client-kind`, default native) makes the
**uploader** service spawn `ant-wasm`, so the daily test answers whether a
*browser* client can still write to production. **One transport per deployment**
(the browser path measured several times slower), persisted in
`deploy_args.client_kind`; the WASM series is its own deployment alongside the
native one. Everything around the spawn is unchanged — the per-cycle
`claim_pool_files`, the SHA-256, the Postgres writes, the replacement logic, the
unit names and restart policies.

- **The read-back runs over the same transport** (the VM's own view as a browser
  user), but **the independent verifier VMs stay NATIVE**, so a
  `digest_mismatch` remains attributable to the upload rather than to the browser
  read path.
- **A failed upload is classified by whether a payment went out** — partial if it
  did (chunks may be in production, so a canary must never draw the file),
  released if it did not (nothing reached the network, so burning the entry would
  be a lie). The result JSON's `payment_sent` **or** the `--payment-marker` file
  decides it, the latter being the only evidence left when an OOM kills the WASM
  child after the money went out. A signal exit records `error_kind=crash`. The
  native "no result document + non-zero exit → release" rule is untouched.
- **The success check is on the `error` key's VALUE, not its presence.**
  `ant-wasm` always emits `error` (null on success) where `ant --json` omits it —
  a presence check reported every healthy browser-path upload as a partial, which
  SPENDS the file's claim.
- **The pool draw is narrowed to ant-core's browser cap**: `large`/`xlarge`
  refused at validation, `medium` clamped to 1,000,000,000 in `--pool-max-size`
  so `plan` shows it. An over-cap file is therefore never claimed.
- **Refused, not ignored**: `bootstrap_cache` (a browser client neither uses
  ant's peer cache nor an explicit `--bootstrap`) and `log_level` (the `ant`
  verbosity flag — use `rust_log`, which `ant-wasm` reads from its environment).
- **8 GB minimum**, exact on DO slugs. Nothing on a WASM uploader runs `ant`, so
  the binary is not installed there; the verifiers keep it.
- Rows carry `client_kind='wasm'` and Influx points go to `wasm-client-metrics`.

See `deploy/README.md` §"The daily production write test".

### Running prod uploaders

`prod uploaders deploy` — one or more client VMs that each upload a list of
public files to production with `ant --json file upload --public`, then exit when
the list is exhausted. Per-file results go to a `prod_upload` Influx measurement
on a paired mandatory monitoring VM. `deploy_args.kind = "prod-uploaders"`.

- Multi-VM, repeatable `--uploader REGION:VM_SIZE:urls=PATH` (region and vm_size
  required, then one file source — `urls=` XOR `pool=`). Other tokens:
  `pool_min_size=`, `pool_max_size=`, `exclude_partial=`, `payment=`,
  `log_level=`, `clock_skew=`, `network=`, `bootstrap_cache=`, `forward_logs=`,
  `memory_profiling=`, `rust_log=` (**last**).
- **The service is strictly serial, one file at a time. There is no
  `concurrency=` token.**
- **Duplicate-file preflight runs before any infrastructure** (on both `deploy`
  and `up`): the file name of every target is checked against the recorded prod
  uploads list and a collision **aborts** the deploy. Override with
  `--allow-duplicate-files` to re-upload missing chunks or test the
  already-stored path. Skipped with a warning when no `DATABASE_URL` is set.
- **Single shared `--secret-key` / `ARBITRUM_ONE_SECRET_KEY`** — a funded
  Arbitrum One **mainnet** wallet, real ETH. Deliberately distinct from the
  testnet `ANT_DEPLOY_SECRET_KEY`.
- Companions: `deployments print-uploads --name <n>` (`--details` for one full
  record per upload) and `record-results` (`--window` is ignored for this kind —
  uploads are a bounded run).
- `destroy` tears down monitoring too; `--keep-monitoring` to preserve it.

See `deploy/README.md` §"Running uploaders against production".

#### The browser path on `prod uploaders` (`--wasm-uploader`)

`--wasm-uploader` / manifest `prod_uploaders.wasm_uploaders[]` (+ `wasm_binary:
{branch, repo_owner}`) makes an uploader's `ant-prod-uploader` service spawn
`ant-wasm`. Unlike the download kinds the transport is **per uploader, not per
deployment**: a native and a browser-path uploader in one run share the wallet,
the monitoring stack and the prod bootstrap list, so both transports are measured
against the same network at the same time. `uploaders` may be empty for a
WASM-only run.

Each WASM entry gets the `wasm-upload` **role** from `clients infra` — the role
names the VM, its Terraform workspace and its S3 state key, and telling the tiers
apart in all three is the point of running both.

- **The browser file cap shapes the pool draw.** `large`/`xlarge` are **refused
  at validation** — every file in them is over the cap, and a drawn one is a
  permanently-spent entry that could never be uploaded — while `medium` is
  implicitly narrowed to `pool_max_size=1000000000`, which `plan` SHOWS.
  `tiny`/`small` gain no bound, because a token that narrows nothing is not free:
  the argv is what the step journal compares.
- **Claim outcomes are the load-bearing part.** Success → `uploaded`. A failure
  **after** a payment was submitted → `partial_upload` → `partial` (chunks ARE in
  production, so uploaders may complete the file but a canary must never draw
  it). A failure **before** any payment → `failed` → **released** (nothing
  reached the network, so burning the entry would be a lie). An over-cap file →
  released. A source-fetch failure → released too. "After payment" means
  `payment_sent: true` **or** the `--payment-marker` file exists — the marker
  covers a child killed after the money went out but before it printed anything,
  and trusting only the JSON would return a paid-for file to the pool.
- **Refused, not ignored**: `bootstrap_cache`, `memory_profiling`, `log_level`.
- Results land in `wasm-client-metrics` (`fetch_all_prod_upload_points` reads
  both buckets and dedupes on `(host, start_ns)`), and a nullable `client_kind`
  column labels each row — resolved per host from the **`deploy_args`-persisted
  uploader list**, NOT S3 client state, which `clients destroy` deletes; the
  upsert also COALESCEs. Over-cap files are excluded from every denominator.
- The duplicate-file preflight and the destroy results guard cover WASM entries
  exactly as they do native ones. `generate-inventory` still has no
  `prod-uploaders` path.

See `deploy/README.md` §"The browser path on `prod uploaders`".

### Running prod uploads from the local machine

`prod local-uploads` — the local-machine variant of `prod uploaders deploy`: runs
`ant --json file upload --public` sequentially against production from the host
the command is invoked on, instead of provisioning remote VMs. Results are
written **directly into `deployment_prod_upload_results`**; there is no Influx,
no monitoring VM and no destroy command. The intended use case is testing uploads
from a real home internet connection. `deploy_args.kind = "prod-local-uploads"`.

- Two input modes: `--urls FILE` (download each URL to a temp file, upload,
  delete only the temp copy) and `--local-dir-path DIR` (walk recursively,
  identifying each file by its path relative to the root; **source files are
  never modified or deleted**).
- Sequential, single-file-at-a-time. No `--concurrency` flag.
- Single `--secret-key` / `ARBITRUM_ONE_SECRET_KEY` — funded Arbitrum One wallet,
  real ETH.
- The resolved binary is cached at
  `~/.cache/saorsa-deploy/ant-binaries/<key>/ant` (pinned releases are reused;
  `latest` and branch builds are re-fetched).
- On completion the deployment is marked `stopped` **regardless of per-file
  failures**; failure detail lives in the per-row Postgres records.

See `deploy/README.md` §"Running uploads from the local machine".

### Running prod downloads from the local machine

`prod local-downloads` — the download-side equivalent: walks an address list
running `ant -v file download <address> -o <output>` sequentially from the
invoking host, recording per-address results to
`deployment_prod_download_results`. No Influx, no monitoring VM, no remote
infrastructure. `deploy_args.kind = "prod-local-downloads"`.

- `--addresses-path FILE` is **required** — there is no bundled default.
- Default output is a per-run temp directory, with each file deleted as soon as
  its result row is upserted; `--keep-output-dir DIR` retains them.
- No `--secret-key`, no `--payment` — downloads are read-only and cost nothing.
- On completion the deployment is marked `stopped` regardless of per-address
  failures.
- **`record-results` refuses this kind**; use `print-recorded-results`
  (`--details` for one full record per download).

See `deploy/README.md` §"Running downloads from the local machine".

### Running prod canaries

`prod canary deploy` — a long-lived loss-measurement deployment against
production: one upload VM walks a URL list once and exits its uploader service,
while N probe VMs run forever, hourly probing every canary they own (per a static
`blake3(canary_id)[0] % N` hash partition). All three measurements
(`prod_canary_upload`, `prod_canary_chunk`, `prod_canary_probe`) land on a
mandatory paired InfluxDB+Grafana stack. `deploy_args.kind = "prod-canary"`.

- One `--upload-spec REGION[:VM_SIZE]`, one or more
  `--probe-spec REGION[:VM_SIZE]:count=N` (the counts sum to the probe shard
  count).
- Single shared `--secret-key` / `ARBITRUM_ONE_SECRET_KEY` — funded Arbitrum One
  mainnet wallet, real ETH.
- **Loss confirmation is the two-strike rule**: a canary is `lost` when **two
  consecutive failed probes from the same probe VM report the same
  `missing_chunk_address`**. Applied locally on each probe VM (SQLite-backed, the
  only reason it needs SQLite — it cannot reach Postgres) and globally in
  `record-results` (Postgres-derived, authoritative).
- Upload-side unit is `Type=simple Restart=no`; probe-side is `Restart=always`.
- **The dataset is the product**, so `destroy` defaults to `--keep-monitoring`;
  pass `--no-keep-monitoring` to tear the Influx + Grafana stack down too.
- `record-results` pulls all three measurements, upserts the four
  `prod_canary_*` tables and rederives `prod_canary_loss` — idempotent and
  cron-safe. `print-recorded-results` emits alive / pending / confirmed-lost plus
  an hours-to-loss histogram, per-vantage and per-chunk-role breakdowns.

See `deploy/README.md` §"Running canaries against production".

### Running prod profiling nodes

`prod nodes deploy` — node VM(s), each running `--node-count` ant-node services
(default 15, matching the prod fleet), joined to the **production** network with
every service wrapped in **heaptrack** and running a **debug build**, to chase
the intermittent ant-node memory leak under real prod load. The node-tier sibling
of the prod client commands. `deploy_args.kind = "prod-nodes"`.

- **The vehicle is neither `extend` nor `migration`.** `deployments extend` only
  reuses another *testnet's* bootstraps, and the `migration` subsystem is
  modelled around migrating an existing source fleet (drain / destroy-source /
  inventory-splice) — wrong for a throwaway profiling VM. Instead it **seeds the
  deployment's S3 state with the production bootstrap peers**
  (`fetch_prod_bootstraps()`) and reuses the normal `nodes infra` → prod-mode
  `nodes provision` → `nodes start` lifecycle. Prod-mode settings match
  `migration provision` (no `--evm-network` → arbitrum-one, `Restart=always`,
  `RestartSec=10`, `LimitNOFILE=65536`); `--stop-on-upgrade` is always emitted
  and no upgrade channel is set, so the pinned debug build keeps running and
  never auto-upgrades.
- **Node-side heaptrack is the one genuinely new mechanism** (heaptrack was
  client-only before): it composes **inside** the NAT `ip netns exec` prefix, so
  it profiles the node rather than netns, and writes to the **attached data
  volume**, NOT the root disk — 15 debug nodes generate large, growing traces.
  Default **ON** here (`--no-heaptrack` to opt out).
- `--log-retention-days` (default 7; `0` = keep forever) installs a daily timer
  pruning old `.zst` traces.
- Optional `--monitoring` (off by default, `--grafana` opt-in) forwards host and
  per-service RSS metrics to InfluxDB via the `--with-telegraf` seam.
- **`scp` the `.zst` traces off each VM before destroy.** `destroy` tears down
  the paired monitoring stack by default (`--keep-monitoring` to retain it).

Declarative (`kind: prod-nodes`); `binary.debug: true` emits the
`build ant-node --debug` step, and `up` is resumable so a retry skips it.

See `deploy/README.md` §"Running profiling nodes against production".

### Running prod crash-debug nodes

`prod debug-nodes deploy` — the panic/crash-debugging sibling of `prod nodes`
(which is heaptrack *memory* profiling): a small, isolated, **permanent** set of
ant-node services joining the **production** network, configured to capture crash
information over time, with its OWN dedicated small InfluxDB + short-retention
Elasticsearch (not the main prod stack). `deploy_args.kind =
"prod-debug-nodes"`. Same vehicle as `prod nodes` — seed the production bootstrap
peers into S3 state, then the normal node lifecycle.

All crash-debug config is default-guarded in `provisioning/node.py`, so every
other kind's systemd unit stays byte-identical.

- **`--rust-backtrace`** (default `full`; `1` or `""` to disable) plus an
  unconditional `COLORBT_SHOW_HIDDEN=1` — without it ant-node's color-backtrace
  filters every frame and prints `<empty backtrace>`. Both **must** be systemd
  `Environment=` lines, not inlined on ExecStart, so they propagate through the
  NAT `ip netns exec` wrapper.
- **`--coredumps`** (default on) → `LimitCORE=infinity` on the unit plus a global
  `kernel.core_pattern` writing cores to the data volume (or the root disk on
  volume-less providers), so even an OOM/SIGKILL leaves an inspectable core.
  Installs `gdb` + `elfutils`; `systemd-coredump` is deliberately NOT installed
  (it would be inert against a file-based `core_pattern`, or force cores off the
  data volume). The release binary is stripped, so symbols are limited without an
  unstripped build and the live COLORBT backtrace is the higher-value signal.
  `--coredump-retention-days` (default 7) prunes.
- **`--log-retention-days` (default 2), and its prune unit restarts the ES log
  forwarder — it must.** Telegraf's `tail` input holds an open fd per matched
  file with no idle-close, so `find -delete` alone unlinks without freeing: files
  leave `du` while ext4 keeps the blocks and `df` hits 100%. Measured **694 GiB
  across 70 fds** on a 968 GB disk showing 0 free. The unit therefore runs
  `systemctl try-restart telegraf-ant-elasticsearch` **after** the `find`
  (`try-restart` so it no-ops under `--no-forward-logs`, `-`-prefixed so a
  forwarder failure cannot fail the prune). That forces
  **`from_beginning=false`** for this kind, since telegraf keeps no tail offset
  across restarts and backfill-from-start would re-ingest every remaining log on
  every prune. **Any new prune timer deleting files something else tails needs
  the same treatment.**
- **`--node-count` (default 4) is forwarder-bound, not fleet-matched.** One
  telegraf tops out ~2,700 lines/s — CPU-bound in the JSON parse chain, not
  blocked on ES — while a debug-level service emits ~758, so at 10 services only
  ~36% of lines reached ES and the forwarder had wedged for three days.
  **Elasticsearch is a sample, not a copy**; the on-disk logs are the
  authoritative record — which `--log-retention-days` discards, so raising one
  means raising the other.
- **`--log-level` (default `debug`): ant-node has NO per-module env filter**, so
  a single global level is the only option and per-module `RUST_LOG` targeting is
  not achievable at the deploy layer. `debug` resolves the node→ES drop filter to
  `TRACE` so the debug logs actually reach ES.
- heaptrack is OFF by default here (use `prod nodes` for that kind).

Node VMs pick their provider via `--provider` (all five); the monitoring and ES
VMs pick theirs independently via `--monitoring-provider` / `--elastic-provider`
(DigitalOcean/OVH/**Contabo** only — those are the observability TF modules).
Torn down on `destroy` unless `--keep-monitoring` / `--keep-elastic`.

Declarative (`kind: prod-debug-nodes`); `binary.debug: true` emits the
`build ant-node --debug` step and `up` is resumable.

See `deploy/README.md` §"Running crash-debug nodes against production".

### Watching a production rollout (`prod rollout-watch`)

`prod rollout-watch --to-version <v> [--name ant-prod-01] [--window 1h]
[--json-out FILE]` reports a staged rollout from **Elasticsearch alone**: fleet
adoption by node version, the auto-upgrade lifecycle messages, restarts, per-arm
health while the fleet is mixed, and a version-telemetry check. It replaced the
SQLite-backed upgrade tracker, which is gone.

- **Stateless, and that is the point.** Every run queries ES afresh — no
  database, no cursor, no `init`. The tracker needed durable per-node state
  because it had to *infer* a running version from the sequence of upgrade log
  messages, and a cursor seeded after the rollout had started blinded it
  permanently. ant-node now stamps `node_version`/`node_commit` on every JSON log
  line and both are Telegraf tags on all 73 ant-prod-01 hosts, so a service's
  version is just the tag on its most recent document.
- **The denominator is "logged in the last 15 minutes"**, keyed on distinct
  `(tag.host.keyword, tag.service_number.keyword)` — which includes the 7
  bootstraps. Shorter and a quiet service drops out; longer and a service that
  died an hour ago still counts as running.
- **The `unknown` bucket is printed as `unknown (unstamped,
  pre-<to-version>)`**, and that note applies **only** to the literal `unknown`
  — every future old arm is a real version appearing under its own value, so the
  note must not follow it.
- **The 5% suspension rule applies to whichever arm is short**, not just the old
  one: below 5% of running services an arm's rates are dominated by whichever
  host happens to be sick, and an arm of zero produces no rate at all. The
  per-arm table is omitted and the four health queries are **not issued**, which
  is most of the report's cost. It is the **new** arm that is short before the
  rollout starts and the **old** one by the end.
- **`wire_tx_bytes`, never `udp_tx_bytes`** — saorsa-transport #169 changed what
  the latter counts, so across a mixed fleet the two arms would not be measuring
  the same thing.
- **§2 counts single-fire messages, so a document count IS an event count** — but
  the obvious matchers do not have that property. A start logs two lines back to
  back, so only `Starting auto-apply upgrade from` is matched; their common
  prefix counted every attempt twice. All those lines log under
  `tag.target = ant_node::node`, which the target filter did not match — the root
  cause of the 0.20.0 rollout's under-counted successes.
- **`Error during upgrade process:` is split transient vs failure**, and **only
  the failures table breaches the rollout gate**. The check-phase sites
  (`TRANSIENT_ERROR_MARKERS`) attempted nothing and go to an informational table;
  everything else, **including a variant a future ant-node adds**, is a failure —
  fail loud, and downgrading one is an explicit list addition. A `retry backoff`
  count against an empty failures table is a failure the matchers missed, which
  is the only reason that row exists.

See `deploy/README.md` §"Watching a production rollout".

### Upgrading the production bootstraps ahead of the fleet

The seven production bootstraps are every client's way into the network.
`prod bootstrap upgrade --node-version <v>` walks them one at a time, ahead of
the fleet's own 24h staged rollout, and in the same restart pins their
WebRTC-Direct listener with `--webrtc-direct-port` (default **10001**) —
unpinned, ant-node derives that port afresh on every start, so a published
browser address goes stale on every restart.

Their inventory is `~/.autonomi/testnets/ant-prod-01/bootstrap-inventory.json` —
a **separate** file from the fleet's `inventory.json`, and one with **no
`provider_id`**, so `load_node_inventory` needs `require_provider_id=False`.

- **The binary path is read from the live unit, never assumed.** Every live host
  runs `/opt/ant-node/ant-node`; installing over a constant would leave a second,
  unused binary on disk while the service kept running the old one — an upgrade
  that reports success and changes nothing. Root dir, log dir and service user
  come from the same `ExecStart`. A unit missing `--stop-on-upgrade` (the node's
  own auto-upgrader is then live and would undo the install) or `--root-dir` is
  refused.
- **The unit edit is append-only.** One argument line is appended and every other
  line stays byte-identical, because these seven units carry hand-maintained
  arguments that a re-render from `build_systemd_unit` would silently discard.
  Idempotent, and exactly reversible for the rollback path.
- **One archive, verified locally, uploaded to all seven** — checksummed against
  the release's own `SHA256SUMS.txt` and run with `--version` on this machine
  before anything is touched, so every host demonstrably installs identical bytes
  rather than each fetching the release and being trusted.
- **Two gates per host, then a 60s settle.** The health gate reads only what the
  restart wrote (a log cursor is taken first) and requires `ant-node starting` at
  the **requested version** — `Restart=always` brings a host back on the *old*
  binary when the new one will not run, and that writes the same line — plus the
  WebRTC listening line on the pinned port and `is-active`. Then an **ICE probe**
  from the operator's machine. Any failure restores the backup binary and the
  original unit, prints `RESTORED`, and **stops the run**; hosts after it are
  untouched.
- **The probe must be a real ICE connectivity check, not a STUN binding** —
  saorsa-transport's admission filter returns `Decision::Ignore` on every
  failure, so an unauthenticated probe cannot tell a firewalled port from a
  healthy one. DTLS is never completed. **Standard library only**, so the
  rollout-watch scripts bundle can import it.
- **Below ant-node 0.20.0 the WebRTC half is `not expected`, not failed, and the
  unit is left untouched.** 0.19.0 has no `--webrtc-direct-port` at all and clap
  exits on an unknown argument, so appending it would leave a bootstrap that
  cannot start. That exemption is what makes a same-version rehearsal
  (`--limit-to <host> -y`) exercise the whole path safely.
- **Firewalls are opened for the whole tier first** (`--skip-firewall` to opt
  out): Hetzner via `hcloud`, AWS via `authorize-security-group-ingress`
  (`InvalidPermission.Duplicate` = success), Vultr not at all — its group exists
  in Terraform but is attached to nothing live.

`prod bootstrap webrtc-addresses` then reads each bootstrap's
`<root_dir>/webrtc-direct.multiaddr` and writes
`resources/prod_bootstrap_webrtc_multiaddrs.txt`, the published list the prod
WASM client kinds consume. It is **all-or-nothing** and rewrites each address to
the **inventory** IP (the two Hetzner hosts carry a second public IPv4 on the
same interface, and only the inventory one is in the client's
`bootstrap_peers.toml`). A missing file, malformed address or failed probe on any
host exits non-zero and leaves the existing file in place — a short list
published as the production bootstrap set silently narrows every browser client's
way in.

**`prod bootstrap set-peers` rewrites the tier's `--bootstrap` peer lists.** Each
unit named the peers that host dials on a cold start, written by hand and never
revisited; nothing looks wrong while every host has a populated routing snapshot
and peer cache, because the cost lands only on a host that lost its data
directory. `set-peers` gives each host **the other six inventory IPs on
udp/10000, never itself**, one host at a time: rewrite, `daemon-reload`,
`restart`, wait for that host's own start line plus `is-active`, settle 60s,
next. A failure restores the unit it found and stops the run. Two properties make
it safe: **only the `--bootstrap` lines move**, and it is **idempotent** — a host
already listing exactly those peers is not restarted, so a re-run after a
mid-tier failure touches only what is left. `upgrade` deliberately leaves these
lines alone. `prod bootstrap deploy` now applies the same rule at provision time.

See `deploy/README.md` §"Upgrading the production bootstraps ahead of the fleet".

### WebRTC-Direct testnets and the WASM client tier

`ant-cli` **cannot** exercise the WebRTC-Direct browser path — it links native
`ant-core`, so it takes the native transport however it is invoked. This tier
runs a Node.js program importing **`ant-core` compiled to WASM**, reaching nodes
over WebRTC-Direct. Roles `wasm-upload` / `wasm-download`, manifest keys
`clients.wasm_uploaders[]` / `clients.wasm_downloaders[]`, independent of the
native pair (a testnet may run either or both side by side). Example:
`deploy/manifests/profiles/webrtc-wasm.yaml`.

They are **distinct roles, not a flag on the native ones**, because the role
names the VM, its Terraform workspace and its S3 state key. The role-to-unit map
is explicit in `client_roles.py`.

Three pieces beyond an ordinary testnet, all no-ops when no WASM client is
configured (every existing deployment renders byte-identical):

- **A pinned WebRTC port on the bootstrap tier** — `bootstraps.webrtc_port`
  (ant-node's flag is `--webrtc-direct-port`). Bootstraps are the one tier whose
  browser-facing address must not move; left unset ant-node **derives** the
  WebRTC port from the QUIC port, so it moves whenever that does. Regular nodes
  deliberately keep the derived port. **The port is only half of a stable
  address**: the multiaddr also embeds the DTLS certificate hash, persisted at
  `<root_dir>/webrtc-direct.pem` — it survives restarts, but **wiping a
  bootstrap's data volume rotates it and invalidates every cached browser
  address**. WebRTC-Direct is ON by default in a stock ant-node build.
- **`build ant-wasm`** — `ant-core` declares `default = ["native"]` and a
  `browser-wasm` feature mutually exclusive with it (a wasm32 build without it
  stops at an explicit `compile_error!`), and the artifact comes from `wasm-pack`
  rather than `cargo build --bin`, so a separate build path is unavoidable.
  **Branch form only** — no WASM package is published as a release.
- **`deployments webrtc-addresses`** — SSHes the **bootstrap VMs** and reads each
  bootstrap service's `<root_dir>/webrtc-direct.multiaddr`. **Cannot run at
  provision time** (the address embeds the certhash, which exists only once a
  node has started) and **must run before `clients provision`** (a WASM client
  has no other way in). `up` sequences it after `nodes start`. Addresses
  advertising a non-public host are rewritten to the VM's real IP.

What will bite you:

- **A WASM client gets the SAME bootstraps as a native client, and no more.** The
  sole difference is address format. The node tier is deliberately **not**
  sampled and there is no `--sample-nodes` flag: an earlier revision handed each
  client ~32 node addresses spread across the keyspace, which production cannot
  supply (a browser gets a small bootstrap list), so it masked discovery failures
  rather than exposing them. **If transfers do not work from the bootstraps
  alone, fix the node.** `clients provision` reads only
  `bootstrap_webrtc_multiaddrs`.
- **`nodes.nat_percent: 0`** — nodes behind the simulated inbound NAT are
  unreachable over WebRTC-Direct without relayed WebRTC, and each dial burns a
  10s DataChannel timeout. This also makes the run incomparable to a native
  baseline that used a different NAT fraction.
- **A WASM upload needs ≥7 initial PUT peers** before it will pay; a small fleet
  fails every upload with "Witnessed close group returned only N/7".
- **No `bootstrap_cache` on either WASM role** — it selects between ant's peer
  cache and an explicit `--bootstrap`, and a WASM client does neither. Passing
  the token is a hard error, not a silent no-op.
- **Every published address line is `<address> <bytes>`.** ant-core refuses a
  browser-path file above 1,000,000,000 only *after* resolving its data map, so
  with bare lines the WASM downloader drew the native 1000 MiB uploads from the
  shared pool and failed every one. Both downloaders parse the first field, and
  one with a bound skips over-cap entries before fetching anything. A bare line
  is never excluded — unknown is not "too big" — so pre-change lists keep
  working.
- **Addresses are one shared pool per deployment, read two ways.** The WASM
  downloader lists the whole prefix (so it fetches native uploads too); the
  native `downloader.sh` filters keys to `upload-*` and never sees WASM uploads.
  The per-uploader *object* is namespaced by role, because `client_index`
  restarts at 1 per role — without that a native and a WASM uploader in one
  region would each erase the other's list on every whole-object flush.
- **Payment: no private key ever enters WASM.** ant-core verifies each storage
  quote and hands the set to a JS callback, which does ERC-20 `approve` then one
  `payForQuotes` and returns the **mined** tx hash; `totalAmount` must exactly
  equal the sum of quote amounts or the upload is rejected. The payment network
  is read from a node's own `HELLO` rather than configured locally, so the client
  cannot pay on the wrong chain.

**Results are kept separate at every layer, because the two transports are not
the same instrument** — measured ~2.2x slower on upload and ~4.2x on download,
so any blended figure describes neither and moves with whatever VM mix a run
happened to use:

- **Influx**: WASM file operations go to their own **`wasm-client-metrics`**
  bucket. They are the same `upload`/`download` measurements the native clients
  write, so one bucket would let any panel or ad-hoc query average the two. A
  WASM VM's *host* metrics still go to `client-metrics`. `record-results` reads
  both and deduplicates on `(host, start_ns, measurement)`.
- **Postgres**: a nullable **`client_kind`** column on
  `deployment_{upload,download}_records`, resolved from each VM's role. NULL on
  every pre-feature row, and NULL reads as native rather than unknown.
- **`record-results` never erases `client_kind`.** The transport is resolved from
  S3 client state and `clients destroy` deletes it, so a re-run after teardown
  would resolve NULL for every host — and an unconditional write would relabel
  every WASM op as native, pushing WASM failures into the NATIVE hard floors. The
  upsert therefore COALESCEs, and warns when it cannot classify a WASM host.
- **`deployments compare`** evaluates the client-record gates **once per
  transport**, suffixed `[native]`/`[wasm]` whenever a run has WASM records; with
  none, the gates are emitted exactly as before. Node-tier gates are NOT split —
  one fleet serves both transports.
- **The two KINDS of WASM gate get opposite answers**, and both keys are
  **required**: **`wasm_floor_gates_advisory: true`** (the success floors are
  absolute numbers calibrated on native which WASM does not meet, and the gap is
  NAT, not the release under test, so gating would hard-fail every run) and
  **`wasm_relative_gates_advisory: false`** (the baseline-relative gates need no
  absolute calibration — they ask only whether this release made the WASM tier
  worse than the last one did — so they **COUNT**, as soft gates). **No WASM gate
  ever reaches the exit code**; that stays the native hard floors'.
- **`clients estimate-gas` sizes the WASM tier too**, labelled `[wasm]` and
  counted in the funder total, with `fixed_eth_per_upload` **doubled** (a
  browser-path payment is two transactions where a native one is one) while
  `eth_per_gb` is unchanged. Throughput comes from the baseline's own WASM
  records; failing that, from the NEAREST native uploader by size, labelled
  `approx: native twin <host>` (a native rate over-funds, which is the safe
  side). Native uploaders are sized from native records only.
- `slack-post` and the `record-results --update-slack` reply render the two tiers
  separately, and only when WASM clients exist.

**Profile:** the WASM tier lives in `staging.yaml` itself — 3 WASM uploaders
(900/300/20 MB; 900 not 1000 because of the cap) and 1 WASM downloader on an
8 GB VM — **added on top of** the unchanged native tier, not swapped into it. The
profile's `nat_percent` dropped 30 → 10 for the same reason WASM's floors are
advisory, and both changes made the first run on it a full **REBASELINE** (what
`--new-baseline` is for). The WASM binary is built from an ant-client **git
tag**. `build ant-wasm-check --branch <ref>` runs that same build **locally** in
a temp dir and deploys nothing, so an ant-client API drift is caught before a
build VM and ~10 minutes are spent on it.

See `deploy/README.md` §"WASM client VMs (the browser path)".

### The pointer client tier (`clients.pointers[]`)

A **pointer** is a paid, owner-signed mutable reference (ADR-0016 in `ant-node`,
V2-1277): one ML-DSA-65 owner key owns exactly one pointer at
`BLAKE3::derive_key(owner)` forever, and every write — create AND update — is one
paid single-node quote. The pointer client tier (V2-1339) exercises them for the
life of a testnet, so the behaviour is measured before it reaches production, the
way the WASM tier did for WebRTC. Impl: `client_roles.py` (role `pointer`, unit
`ant-pointer`), `resources/scripts/pointer_client.py` (the workload),
`pointer_results.py` (the pure arithmetic), `cmd/print_pointers.py`,
`db/saorsa_db/models/pointer_record.py`. Example:
`deploy/manifests/profiles/pointers.yaml`.

**A role of its own, and an upload-type role.** Its own because the role names the
VM, its Terraform workspace and its S3 state key; upload-type because it carries a
funded per-VM wallet, is topped up from the funding wallet and drained on destroy
exactly as an uploader is. `is_upload(ROLE_POINTER)` is therefore **True** —
callers that mean "uploads files" must test the role, since a pointer client has
no `file_size_mb` (it is persisted as 0, which is what lets every shared wallet
path treat it as an uploader without special-casing). The sites that dispatch on
role and silently skip an unknown one are guarded by
`test_pointer_client_plumbing.py`: `clients clean` (now stops EVERY role's unit,
derived from `ALL_CLIENT_ROLES`), the destroy-time service stop (hard-coded
`ant-uploader`, so a pointer client was drained while still spending — the exact
nonce race that stop exists to prevent), `_FORWARD_ROLE_FILES`, the `clients infra`
summary label (a ternary that spelled every non-native VM "Downloader"), and the
`role` validation list in all four `*-client/variables.tf`.

**The workload is one serial scheduler, not three threads.** Create every
`create_interval_secs` (60), update a random owned pointer every
`update_interval_secs` (30) and read it back, sweep every known pointer — own AND
every other client's published ones — no more often than every
`sweep_interval_secs` (600). All three share one wallet and every write is an
on-chain payment, so two concurrent writes would race the same nonce. Defaults are
**180 paid writes per client per hour**, one Arbitrum Sepolia transaction every
20s. Creation runs for the WHOLE run rather than seeding a pool up front, because
decay with age needs pointers of every age present at every moment;
`pointer_max` (0) is a safety cap, not the design.

**The ledger is durable and load-bearing.** `/var/lib/ant-pointer/` holds the
owner keys and a JSON ledger of the expected `(counter, target)` per owned pointer
plus the last-seen counter per foreign one. `Restart=always`, and **ownership is
immutable** — a lost key is a pointer that can never be updated again — so a
restart that forgot the keys would abandon everything it had paid for. The key is
written to the ledger BEFORE the create that pays for it. `clients clean` removes
the directory deliberately: a surviving ledger would have the client sweeping
pointers whose keys are gone and reporting every one as a failure.

**Seven outcome classes, because four of the six failures are ordinary events.**
`ok`; `stale` (a state older than the one written, or a foreign counter going
backwards) — **EXPECTED at a non-zero rate**, since ADR-0016 says an eclipsed
reader can be served an older validly-signed state and cannot tell; `lost_write`
(refused as "moved while in flight", or fell short of its quorum — paid for, not
landed); `not_found` (the close group agreed there is nothing there — an ANSWER,
which is why `ant pointer get`'s exit-1-on-absent maps here and NOT to `error`);
`read_shortfall` (too few answered, or no state had two peers behind it);
`anomaly` (a counter ABOVE the one written, which only a foreign write to an owned
pointer could cause); `error` (the only class that indicates a fault).
Classification is by matching strings `ant` prints, read off the branch — note
`Error::CloseGroupShortfall` renders as "close-group **PUT** shortfall" for READS
too, so matching on the word PUT would put every read shortfall in the write
bucket.

**Cost is measured as PAID, never as quoted.** `ant pointer create --json` prints
only `{"address": ...}` — there is no cost in its output. So ANT comes from `ant
wallet balance` and native gas from a plain `eth_getBalance` against the same RPC
the payment settles on, read either side of each write. That is also the only way
to see the ADR's warning that a write which times out and is retried **pays
twice**: `paid_attempts > 1` is a double payment, and a write that spent and did
not land is reported separately as "paid but not stored".

**Targets and publishing.** `chunk` targets are drawn from the shared upload
address pool so `resolve` checks end to end against a real data map, with a random
32-byte fallback so the role never depends on the uploader tier (a node stores 33
opaque bytes and never reads the target). A `chain_fraction` (0.10) of updates
re-point at another OWNED pointer and are followed with `resolve`; chain
resolution, its depth cap of 16 and its cycle check are entirely client-side. Each
client publishes its addresses to `pointer-{region}-{index}.txt` under the
deployment's shared prefix via the presigned PUT the uploaders use. `downloader.sh`
filters the listing to `^upload-` and is unaffected; **the WASM downloader lists
the whole prefix and had to be taught to skip `pointer-` objects**
(`isNonFileKey` in `resources/wasm-client/src/addresses.ts`) or it would fetch
pointer addresses as files and the failures would land in the download success
rate as lost content.

**Results.** Influx measurement `pointer` in `testnet-metrics` (tags `host`,
`region`, `op`; no client-supplied timestamp, for the usual clock-skew reason);
`record-results` records every op into **`deployment_pointer_records`** in the SAME
pass as the file ops, because a pointer client rides an ordinary testnet and a
command an operator has to remember separately does not get run. Its own table,
not a third `deployment_*_records` sibling: there is no file, so `file_size_bytes`
is meaningless, and folding `stale` into `OpRecordStatus.FAILURE` would put the
expected stale-read rate into the upload/download hard floors.
`deployments print-pointers --name` reads Influx (live, the only view during a
run), `--id` reads Postgres (post-run, the only view that survives teardown), both
through `pointer_results.py` so they cannot disagree; `--by-age` buckets **sweep**
outcomes by age at read (a `get` is the confirming read, always ~0 age), own and
foreign separately. `slack-post` and `record-results --update-slack` render the
tier only when it exists.

**Node-side signal.** `record-log-counts` gains a `pointer` group in the log
catalogue (version **3**; the connection groups are untouched, so v2 and v3 counts
of them stay comparable) covering nine node WARN/INFO lines, and additionally
counts `pointer_put_rpc` / `pointer_get_rpc` per outcome with latency p50/p95 into
the same table keyed `<event>:<outcome>` (no migration). Those two are the only
quantitative node-side pointer signal that is not DEBUG — stale refusals, quorum
shortfalls and unpaid offers are all DEBUG and the node forwarder drops DEBUG at
`log_level: info`, which is what staging runs at, so they are deliberately NOT
catalogued (they would read zero for a reason unrelated to pointers). Promoting
them is a request on V2-1277.

**Gates.** Eight `[pointer]` gates in `deployments compare`: a write and a read
success floor (absolute, single-run, and therefore surviving `--new-baseline`),
plus stale-read rate, lost-write rate, write/read latency p50/p95 and cost per
write, all relative. **NOT ASSESSED, never omitted** when the baseline has no
pointer records — which is every baseline on the first pointer-carrying train.
**No pointer gate reaches the exit code**; every one is soft, so the worst is a
`SOFT FAIL`. The two floors are additionally advisory
(`pointer_floor_gates_advisory: true`) because no run has measured a pointer tier
and the ADR does not say how large the expected stale rate is — a floor set today
is a guess. `clients estimate-gas` sizes a pointer client from the **manifest
rates** (no baseline needed — they are deterministic) against a new
`fixed_eth_per_pointer_write`, seeded from the per-upload fixed cost and awaiting
recalibration from the first run's measured spend.

`staging.yaml` carries **two** pointer entries, DigitalOcean `lon1` and `syd1` —
geographically separated so cross-client sweeps span a real distance. **The train's
RC must carry pointers**; if it predates them the run operator strips the two
entries for that train. Not in scope: a `wasm-pointer` role (V2-1340) and pointers
against production.

### Simulating residential connections on client VMs (`network`)

Client VMs sit on ~gigabit cloud links, which is not how real users reach the
network — and upstream rate, which dominates uploads, is where a home line
differs most. Any client VM can be shaped with `tc` via a **`network:`** key
taking either a built-in profile name or an inline mapping. Supported on
`prod uploaders` (per-entry), `prod downloaders` / `prod continuous-downloader`
(spec-level, `--network`) — **the primary use case** — and on testnet/extension
`clients.uploaders[]`/`downloaders[]` (secondary). NOT wired to `prod canary` or
the `local-*` kinds. Omitting it leaves a deployment byte-identical to before.

Profiles are git-versioned in `resources/network/profiles.yaml` with a
`version:` echoed into every recorded run: `home-adsl` (10/1 Mbit),
**`home-calibrated`** (27.4/6.29 Mbit, 12.7 ms — the only *measured* one, a real
line as of 2026-07-30, whose loss is 0 because 0/300 packets were lost, i.e.
*below that sample's resolution*, not a proven lossless line), `home-fttc`
(60/18), `home-fttp` (300/50) and `mobile-4g` (30/8, 60 ms, 20 ms jitter, 0.5%
loss).

Four invariants, all load-bearing:

- **Monitoring and SSH are exempt** (TCP 22/8086/9200 into an unshaped `htb`
  class). Otherwise the VM's own Telegraf metrics and forwarded client logs
  compete with the transfer for the capped uplink — the measured transfer comes
  out slower than a real user's (no user ships their own logs) and the run's
  results can arrive incomplete, degrading the instrument taking the
  measurement. Keyed on **port, not address**, because the ES VM is created
  *after* the client VMs are provisioned, so its address does not exist when
  shaping is applied.
- **Delay is applied once, loss per direction.** RTT is a round-trip property, so
  `delay`/`jitter` go on egress only (both paths would double it); loss goes on
  both, since inbound loss is what makes a *download* behave like a home
  download.
- **Queue depth is not a knob but is always a choice.** A rate cap implies a
  queue, and left to chance it gives either tail-drop collapse (throughput far
  below the cap) or bufferbloat (standing delay that swamps `delay`), so an
  explicit AQM is pinned — `cake`, falling back to `fq_codel`. Relatedly every
  htb class sets an explicit `quantum`: shaped and unshaped classes are four
  orders of magnitude apart, so no single `r2q` avoids htb's warnings.
- **Ingress needs an `ifb` mirror** — `tc` shapes egress only, so without it the
  downstream cap would not apply to downloads at all.

Applied by a systemd oneshot (`ant-network-impairment.service`) so shaping
survives reboot; the script is **convergent** (re-provisioning replaces rules
rather than stacking — two `netem delay 30ms` would be 60 ms).

- **Never enable on a staging gate run**: `deployments compare` gates on transfer
  p50/p95, and shaped percentiles are not comparable to an unshaped baseline.
- Client egress is ~4x payload (fan-out), so at `home-adsl`'s 1 Mbit up a 100 MB
  upload takes ~67 min and 1 GB ~11 hours. Size runs accordingly.

The profile *name* is recorded on the four `deployment_*_records`/`_results`
tables (`network_profile`, NULL = unshaped = every pre-feature row); the full
resolved knob set lives once in `deploy_args`.

See `deploy/README.md` §"Simulating a residential connection".

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

Two command groups record the release train in the registry. Both are
**CLI-only** (not exposed over the REST API) and both **require `DATABASE_URL`**,
exiting non-zero rather than silently no-opping the way the deployment registry
does. Both are driven by skills in the separate `releases` repo
(`/home/chris/dev/releases/.claude/skills/`), not from here — and those skills
call the **bare** `saorsa-deploy` binary, since their cwd would make `uv run`
resolve the wrong project.

**`release-candidates`** records each RC *cut*: one row per binary target
(`ant-node 0.17.1-rc.1`, `ant 0.3.3-rc.1`), its constituent crates, and the PRs
that landed in each. An `rc.N+1` respin cuts a **new** row — RCs are not
re-edited.

**`releases`** records the *channel state* of what actually shipped: `ship` →
`promote` | `rolled-past`, plus `current` / `ls` / `show`. It exists because
"what is on stable, what is on beta, how long has it soaked" was answerable only
from GitHub tags and hand-transcribed prose in a Linear issue, and a beta the
channel **rolled past** left no trace anywhere but a closed issue.

Load-bearing design points:

- **A separate table, not lifecycle columns on `release_candidates`** — respins
  produce several RC rows per release; the release is the lineage that shipped.
- **One row per binary target**, matching the RC granularity: a train records two
  releases sharing one `rc_branch`, and some trains ship only one.
- **`releases.version` is the final, suffix-free version** (`0.17.1`); the beta
  suffix lives in `beta_tag`. That is what lets `promote` complete the same row
  the `ship` created.
- **`release_crates` is NOT a reuse of `release_candidate_crates`.** RC crate
  rows carry rc-suffixed versions and RC tags; a released crate carries the
  suffix-free version and the final tag, so reusing them would record the wrong
  versions against the release.
- **The FK to `release_candidates` is resolved by the `(binary_target,
  binary_version)` natural key**, not by id — `release-from-rc` never holds the
  RC row id. It is nullable, and a miss warns rather than failing: a ship record
  is worth having without the link.
- **`shipped_to_beta` is the only entry state; `promoted` and `rolled_past` are
  terminal.** The repository refuses to leave a terminal state — a stable version
  that quietly stopped being stable would make every later answer untrustworthy.
- **`--reason` is required on `rolled-past`** — a rolled-past row with no reason
  says nothing the release's absence would not.
- **`releases current` reports the rolled-past betas since the current stable**,
  which is exactly the widened stable version-jump window (N±2) the next
  promotion has to carry.
- **Soak state has three values**: elapsed, not elapsed, and *unknown* (no
  planned promotion date recorded). Unknown is never collapsed into "not yet".

Consumers, all in the `releases` repo: `ship-to-beta` Step 7, `release-from-rc`
Step 10.3, and `beta-health-verdict`, which sources the beta version, ship date,
soak window and the stable **control** version from `releases current` instead of
scraping the Linear issue, and records `releases rolled-past` on a NO-PROMOTE
verdict.

See `deploy/README.md` §"Recording releases and channel state".

## Sizing uploader gas funding (`clients estimate-gas`)

Testnet uploader wallets hold a **fixed** amount of native ETH, so runway is inversely
proportional to the gas price — when the price rises the same funding buys fewer
payment transactions and an exhausted client fails **every** subsequent upload, in a
way indistinguishable from a network fault in the gate data. Staging run #566
(2026-09-01) lost six of ten uploaders that way after the Arbitrum Sepolia base fee
rose ~10x in three days, putting whole-run upload success (96.02%) and the
mixed-version phase (79.35%) below their hard-gate floors for a non-product reason.
Throughput was **not** the cause — that run did less work than its baseline.

`clients estimate-gas --config <manifest> --baseline-id <id>` is the read-only
preflight, run as **Step 3b of the `run-staging-testnet` skill**. It reads the current
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

### Traffic-accounting counters in InfluxDB + the Egress accounting dashboard (V2-1225)

An 18-hour bandwidth episode on a prod node (2026-09-14) was invisible in every
dashboard although the node's own traffic-accounting counters (V2-623) recorded
it — they sat in Elasticsearch, while the `net.bytes_sent` they must be divided
by lives in InfluxDB, and labels differ (`tag.host.keyword` upper-cased vs
lowercase `host`). So the ES forwarder (`provisioning/telegraf.py`) now renders a
**second output** whenever a deployment has both elastic and monitoring state
(every kind via `telegraf_fold`; retrofit via `provision-telegraf`, where the
Influx creds are resolved even under `--skip-metrics`): `[[outputs.influxdb_v2]]`
with `namepass=["tail"]` and `tagpass target = ["*::traffic",
"saorsa_transport::masque::relay_server"]` (`INFLUX_TRAFFIC_TARGET_PATTERNS`).
The tagpass is **mandatory** (else every log line hits Influx); it is a **glob,
not the issue's literal 5-list**, because ant-node#230 (V2-834) adds
`ant_node::{payment,storage,upgrade}::traffic` and a literal list would drop them
silently — a parametrised test asserts all eight targets match. Creds are
duplicated into `/etc/default/telegraf-ant` as `INFLUXDB_*`; without monitoring
the ES config/env are byte-identical (tested). The ES output is untouched.

Points land in `tail` in `testnet-metrics` with the **upper-cased `host`** (the
uppercase processor runs before every output; changing it would alter ES docs).
Dashboard "Egress accounting" (uid `ant-egress-accounting`,
`provisioning/egress_dashboard.py`, written by every `monitoring provision` with
Grafana): coverage % (numerator = `udp_tx_bytes` + `relay_carrier_udp_tx_bytes`
+ `bytes_relayed` under BOTH relay targets; `wire_tx_bytes`/`fetch_response_tx_bytes`
excluded as double counts; red <80%; legend mean = the figure for the range),
unaccounted top-15 table, **socket ground truth egress/ingress** (`sock_tx_bytes`,
`sock_rx_bytes` — V2-834's own 2% gate; empty on older builds; an earlier branch
revision left relay-socket ingress in a separate `raw_rx_bytes`, bootstraps read
21–33% without it — fixed upstream and renamed `relay_target_rx_bytes` at this
dashboard's request; verified on DEV-02 2026-09-22 at 90–98% from `sock_rx` alone), a stacked egress decomposition, relay throughput/occupancy over both
relay servers, replication serving, numerator note. Facts the Flux is shaped by,
all MEASURED (prod ES 2026-09-17; DEV-01 on the V2-834 branch 2026-09-18):
summaries are **cumulative per process, every 5 min** → `aggregateWindow(10m,
last)` → `difference(nonNegative)` → per-`(host,_time)` `sum()` (NOT a second
`aggregateWindow`: re-stamps one window late + bogus zero row); **one series can
carry several lines per tick** (V2-834 emits the endpoint summary once per
endpoint, one with `udp_tx_bytes=0`; `relay::traffic` interleaves per-session
lines with the summary) — differenced raw it flaps `0,X,0,X` and reads 25× the
NIC, so every series is windowed with `max`, never `last`; **`bytes_relayed` exists under two
targets** (stream relay `relay::traffic`, dominant on V2-834 builds; MASQUE
`masque::relay_server`, dominant on the shipped build and emitted only from the
CONNECT path — an idle relay has no line); **host case**: `net` keeps the
deployment name's case on a testnet (`DEV-01-ant-node-…`), so BOTH sides are
lower-cased and `$host` matched `(?i)`, and the Host variable is pinned to `cpu`
so tail points don't duplicate the dropdown; **NAT VMs**: `veth-nat` is the
busiest interface and its `bytes_sent` is traffic INTO the netns → NIC side
excludes `lo|all|veth*`; **performance**: range→filters→`aggregateWindow` with
NOTHING in between (pushdown: one row per series per window; a 24h view went
18s→2s), window scales with range (10m/30m/1h), `union`+`pivot` not `join()`;
**the partial window at the range end is dropped on both sides** (5-min ticks vs
10-s samples → measured 5× in that window, legend mean 95→116%). With all that,
DEV-01 read 82–97% on every host on all three reconciliations at 2h and 24h.
Companion dashboard **"Node traffic by service"** (uid
`ant-node-traffic-by-service`, same module): prod has 700+ services, so
fleet-wide it RANKS (four top-20 `(host, service)` tables by mean rate with peak
window — `sort|>limit`, flat cost at any fleet size) and draws per-service lines
only for ONE host (single-select Host variable, `single_host=True` in
`_build_host_dashboard`). Found while building it: the ES forwarder's path
parser handled `ant-node-N`, `ant-genesis-*` and bare `ant-bootstrap` but not
the testnet bootstrap provisioner's `ant-bootstrap-{index}` dir, so every
testnet bootstrap's `service_number` (ES and Influx) was the raw log path —
fixed in the starlark; a test executes the parser against all four shapes. Verified on that live run AND against a local docker
InfluxDB with synthetic points (restart, double endpoint line, per-session line,
mixed-case host, veth-nat). Prod rollout: retrofit with `provision-telegraf
--name ant-prod-01 --host-ips-file <fleet ips> --host-role WAN_NODE
--skip-metrics --only-new-logs` (the last flag or the restart re-ingests every
on-disk log into ES); the dashboard JSON can be dropped into
`/var/lib/grafana/dashboards/` on the Grafana VM — the file provider picks it
up without a restart.

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

**Covers**: 3 hard gates (upload floor 99%, download floor 99.5%, data
integrity) + 8 soft gates (success rate, transfer p50/p95 **per file-size
bucket**, node CPU, node RSS, leak slope, outbound bytes/GB, disk bytes/GB).
Transfer percentiles are exact — from the per-op
`deployment_{upload,download}_records`. **`deployment_file_results` is
unusable**: an aggregate with no per-op timestamps, so it cannot be windowed.

**Deliberately does NOT cover** three hard gates — the report is silent on them
entirely: crashes (panics → stderr/journald, not the JSON logs telegraf tails;
ES dies at teardown), mixed-version chunk storage (SSH `chunks.mdb` check), and
canaries (separate deployment). The **`evaluate-staging-run`** skill supplies
those observationally and assembles the release-issue report.

**Data integrity (V2-757).** The fourth used to be on that list, because nothing
anywhere verified downloaded content: a transfer that "succeeded" while silently
returning corrupt bytes passed every check the system made. Now `uploader.sh`
hashes each generated file before invoking `ant` and publishes
`<address> <bytes> <sha256>`; `downloader.sh` carries the hash through
`eligible_addresses` (an address alone cannot say what was uploaded under it),
hashes the output **before** the existing `rm`, and emits an integer
`verify_status` on the `download` point — `0` ok, `1` mismatch, `2` unavailable
— **omitted entirely on a failed download**, with `expected_sha256`/
`actual_sha256` on a mismatch only. One- and two-field lines still parse and
verify as `unavailable`, so a WASM uploader publishing into the same shared pool
keeps working. Columns: `deployment_upload_records.sha256`,
`deployment_download_records.{verify_status,expected_sha256,actual_sha256}`, all
nullable — **NULL is "not applicable", never a failure**, and no past run can be
backfilled. The `compare` gate is **hard, candidate-only** (so it survives
`--new-baseline`), fails above `hard_gates.download_verify_mismatch_max: 0`
(thresholds config **v5**; a COUNT, and the one legitimately-zero threshold in
that file), and reads NOT ASSESSED with the reason `no verified downloads
recorded` when nothing was checked. Its denominator is **ok + mismatch only** —
`unavailable` was never checked and NULL was never checkable, so counting either
would report an unverified run as a verified one; the reported **coverage**
(verified ÷ successful downloads) is what says how much of the run was covered.
**A mismatch never touches the availability floors**: a download that fails is an
availability problem, one that returns different bytes is a correctness problem.
Native testnet tier only — the WASM tier is V2-1360, the prod client kinds
V2-1361. Ride-along: both scripts emitted `status=$exit_code` raw, which
`record-results` reads as an enum and DROPS above 2, so an `ant` exit of 3 was
silently discarded rather than recorded as a failure; every non-zero exit is now
clamped to 1 at the emit site.

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
