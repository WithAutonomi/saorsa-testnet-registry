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

## Deployment Statuses

`pending` → `running` → `stopped` (success) or `failed`

When destroying: `running`/`stopped` → `destroying` → `destroyed` (success) or `failed`

## API Endpoints (Read-Only)

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/deployments` | List all deployments |
| GET | `/api/deployments/<id>` | Get deployment with events |
| GET | `/api/deployments/<id>/events` | List deployment events |

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

Unattended runs are driven from a Linear issue: `scripts/run-testnet-issues.sh`
/ `run-testnet-chain.sh` → the `run-testnet-from-issue` skill, and
`scripts/run-maintenance-issues.sh` → the **`run-prod-maintenance-from-issue`**
skill for production maintenance. Both sit on `scripts/lib/issue-runs.sh` (one
loop, one `RUN COMPLETE <issue> <verdict> <field>` parser) and differ in their
**interrupt policy**: a testnet Ctrl-C kills the `deployments up` it started,
while a maintenance Ctrl-C **never signals a running `saorsa-deploy prod …`** —
it waits for that command and stops the session only afterwards (the command is
a grandchild of the session, so the other order could kill it by proxy), because
only the live command can run its own rollback and a host left between the
binary swap and the restart has none. The maintenance skill treats the issue as a
**goal** (`## Purpose` + `## Validation`) with the Procedure as the intended path:
when a step fails or cannot run as written it diagnoses, fixes and carries on,
inside hard **boundaries** — only the named targets, no destruction or spend the
issue did not sanction, no secrets printed, repo fixes via a branch and PR, and
`retry: never` steps (the one-host-at-a-time commands with built-in rollback)
never re-run or signalled. Every fix is reported under `### Fixes applied` and a
fixed run is `WARN` at best. It starts nothing new at the hard ceiling. The
testnet skill's ceiling bounds the measurement phases only: once recording has
started it runs to completion, and no tier a recording reads from (ES,
monitoring, the S3 client state) is destroyed before that recording exits.

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

**Every node-tier and client host has `ufw` disabled at provision time** (op
`Disable host firewall (ufw)`, early in the pass, before the unit-file write and
before NAT setup). **Vultr's Ubuntu 24.04 image boots with `ufw` active,
default-deny incoming, allowing only `22/tcp`** — so SSH, provisioning and
node-to-node traffic (nodes hole-punch outbound) all work while nothing can dial
*in*. That cost run DEV-03 (V2-1410, id 632) its upload rate: of 6,821 direct
client QUIC attempts to the seven Vultr node VMs, zero connected, against tens
of thousands succeeding on the DigitalOcean VMs, and 0 of 105 Vultr nodes ever
became `Direct`. Disable rather than allow, because node services (and MASQUE relay
servers) take OS-assigned high UDP ports an allow-list cannot name. The command
is idempotent, persists across reboot, and is a no-op where `ufw` is absent —
every other provider — so it is unconditional. One definition
(`provisioning/firewall.py`) shared with the NAT script, which has run the same
line since June 2026. **Not** applied to the monitoring, Elasticsearch, build or
`prod uploaders`/`downloaders`/`canary` client VMs — nothing dials in to those.

### Extending a testnet (shared bootstraps)

A testnet can be deployed as an **extension** of another: it reuses the source
testnet's bootstrap VMs and peers with them, but has its own fresh node VMs,
typically running a different `ant-node` binary for compatibility testing.

- `deployments extend --name DEV-02 --source DEV-01` copies DEV-01's
  `bootstrap_nodes`/`bootstrap_ip`/`bootstrap_port` into DEV-02's state and
  records `shared_bootstrap_from`. **No infrastructure is created.** Idempotent.
- `deployments unextend --name DEV-02 --force` deletes DEV-02's deployment state.
  Never touches the source. Refuses if DEV-02 still has nodes or clients.

The pipeline **skips the bootstrap/genesis stages entirely**: `extend` →
`nodes infra` → `nodes provision` → `nodes start`. Destroy is `nodes destroy` →
`unextend`.

- **`bootstraps destroy` refuses if any deployment's `shared_bootstrap_from`
  matches the name being destroyed, and `--force` does NOT override it** — you
  must `unextend` each dependent first.
- An extension may carry its **own client tier** (`extension.clients`, requires
  `extension.binaries.ant`) — the way to run a second client-version cohort
  against the same network. Its clients bootstrap off the seeded peers and
  publish/fetch addresses under the **extension's own** S3 prefix, so each
  cohort's metrics stay separable per deployment name. Destroy order becomes
  `clients destroy` → `nodes destroy` → `unextend`.

See `deploy/README.md` §"`deployments extend` / `deployments unextend`".

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

### The browser path (WASM): rules common to every kind

`ant-cli` **cannot** exercise the WebRTC-Direct browser path — it links native
`ant-core`, so it takes the native transport however it is invoked. Several kinds
therefore have a WASM variant that runs the transfers through ant-core compiled
to WASM. The rules below hold for all of them; each kind's own section states
only what differs.

- **`ant-wasm` is a drop-in for `ant`.** Installed at `/usr/local/bin/ant-wasm`
  from the bundle's `dist/cli.js`, it supports `file upload --public` and `file
  download`, reads `SECRET_KEY`/`BOOTSTRAP_MULTIADDRS`/`RUST_LOG` from the
  environment (no `--evm-network`/`--bootstrap` — the payment identity comes from
  a node's HELLO), and prints a **final JSON line in the `ant --json` shape**
  plus `payment_sent`, `error_kind`, `file_size`, `client_build`. That shape is
  what leaves the services, their units, their per-op logs and their restart
  policies **unchanged by the transport**. The `Install Node.js 22` op fails the
  provisioning pass unless the installed major is 22 afterwards. A failed
  NodeSource setup used to fall through to Ubuntu's own `nodejs` 18.19.1, on which
  every browser-path transfer panics in `getrandom` (V2-1557).
- **The transport is a property of the DEPLOYMENT** (`client_kind: native|wasm`,
  CLI `--client-kind`, persisted in `deploy_args.client_kind`), because a blended
  result describes neither instrument — WASM measured ~2.2x slower on upload and
  ~4.2x on download. **The one exception is `prod uploaders`**, where it is per
  uploader on purpose, so both transports meet the same network at the same time.
- **Results stay separate at every layer.** WASM operations land in the
  **`wasm-client-metrics`** Influx bucket (they are the same `upload`/`download`
  measurements, so one bucket would let any panel average the two), and a
  nullable **`client_kind`** column labels each Postgres row. **NULL reads as
  native.**
- **`record-results` never erases `client_kind`, and resolves it from
  `deploy_args`, not S3 client state** — `clients destroy` deletes that state, so
  a re-run after teardown would otherwise relabel every WASM op as native and
  push its failures into the NATIVE hard floors. The upsert **COALESCEs**: a NULL
  never overwrites a known transport.
- **The file cap is ant-core's, not the network's**: it refuses a browser-path
  **upload** above `WASM_MAX_FILE_BYTES` (1,000,000,000). Downloads were capped
  too until ant-client#214 (in `ant-cli` 0.3.11-rc.1), which removed the download
  cap; a 0.3.11-rc.1 bundle fetched 2.6–3.1 GB files against production
  (STG-WDL-01, run 670). The tooling's download-side skip (a declared over-cap
  size, `MAX_DOWNLOAD_BYTES`, `error_kind=over_cap`) is still in place and still
  fires whatever the bundle, so it is now needed only for older bundles and is
  more conservative than a current client: a testnet WASM downloader still skips
  entries over 1 GB that the client could fetch. What
  each kind does about the cap differs — a download **skips**, a pool draw is
  **refused or clamped** — but an over-cap file is never counted in a
  success-rate denominator, and a **bare address line is never treated as
  over-cap** (unknown is not "too big").
- **An upload that failed is classified by whether a payment went out**:
  `payment_sent: true` in the result JSON **or** the existence of the
  `--payment-marker` file. The marker is the only evidence left when an OOM
  kills the WASM child (the largest RSS on the VM) after the money went out but
  before it printed anything, and trusting only the JSON would return a paid-for
  file to the pool.
- **`bootstrap_cache` is refused on every WASM role** — it selects between ant's
  peer cache and an explicit `--bootstrap`, and a browser client does neither.
  Passing it is a hard error, not a silent no-op. Each kind refuses further
  options that act on the `ant` process; a silently-dropped option leaves the
  operator reading their own manifest as a description of the run.
- **8 GB minimum on the prod kinds**, validated exactly on DigitalOcean slugs
  (memory is encoded in the slug, so it is a fact) and only warned about
  elsewhere. A browser-path transfer holds ~4x the file in WASM memory, so an
  under-sized box makes every transfer an OOM kill, which reads as a network
  fault.
- **`wasm_binary: {branch, repo_owner}` takes a branch or a TAG** (that is how an
  RC is pinned); `version:` is refused, since no ant-core WASM package is
  published as a release. **`build ant-wasm` runs `npm ci`** against the
  committed lockfile — `npm install` could resolve a different
  `node-datachannel`, the WebRTC implementation the whole path runs on, for the
  same ant-client ref — and writes a `build-info.json` plus an S3 sidecar holding
  the ant-client **commit**. The commit is what identifies the code; a moving
  branch name does not.
- **`client_build` travels with each operation**, because `deploy_args.wasm_build`
  holds only the current build and is overwritten. `ant-wasm` reads its own
  bundle's `build-info.json` and puts the commit on every result row.
  **NULL means unknown build** and the upsert COALESCEs, as `client_kind` does.
  No past row can be backfilled — the Influx points never held the field.
- **The prod bootstraps are ICE-probed before any infrastructure exists** on the
  prod kinds, from the committed
  `resources/prod_bootstrap_webrtc_multiaddrs.txt` (there is deliberately no URL
  flag — nothing publishes these addresses at a URL). **Zero answering refuses**
  (the alternative is a VM billing from creation that fails every op); a partial
  answer warns, names the dead entries and proceeds, since ant-core cools a
  failed endpoint down for 30 minutes and a live subset is a working deployment.
  `--skip-bootstrap-probe` exists because ant-node opens the listener 2–15 min
  after a restart.

Shared implementation: `prod_wasm.py` + `cmd/prod_wasm_cli.py` (the prod-WASM
foundation), `provisioning/wasm_client.py`, `resources/wasm-client/src/cli.ts`.

### The browser path on the production download kinds (WASM)

Both remote download kinds take **`client_kind: native|wasm`**, measuring what a
browser client experiences against the REAL production network. See "rules common
to every kind" above for the transport, results, cap, sizing and probe rules.
Example: `deploy/manifests/profiles/prod-wasm-downloaders.yaml`.

What is specific to the download kinds:

- **Over-cap is a SKIP, not a failure.** The content is stored and healthy, so a
  refusal is a property of the transport. Address lines are `<address>[ <bytes>]`,
  so a declared over-cap size is skipped **up front** with nothing spawned, and a
  child's `error_kind=over_cap` is recorded as a skip too. `status` is an integer
  Influx field (a string would be a type conflict and the point would be
  dropped), so the wire value is **`2`** → `OpRecordStatus.SKIPPED`. Skipped rows
  are STORED but excluded from every success-rate denominator — with a six-address
  list holding two over-cap entries, counting them would cap the gate at 66.7%
  and fail every run. Since ant-client#214 a current bundle no longer refuses
  large downloads, so on an address list with bare lines (STG-WDL-01's) no skip
  is expected at all.
- **Crash accounting comes free**: the WASM process is the largest RSS on the VM,
  so an OOM lands on the **child**, the parent sees a signal exit, records
  `error_kind=crash` and continues — no checkpoint, no `inflight.json`, no
  restart-policy change.
- **Refused, not ignored**: `memory_profiling` and `download_diagnostics` (both
  act on the `ant` process). `rust_log`, `network` and `clock_skew` work normally
  — and `network` is now actually threaded through `prod continuous-downloader`'s
  infra step, where it had been dropped for native too.
- A nullable **`address`** column on `deployment_download_records` records what
  each op fetched (both transports emit it now; nothing else maps an operation to
  its target). `print-downloads`, `print-recorded-results`, `slack-post` and
  `record-results --update-slack` render the per-transport split with skip and
  crash counts; the Slack post names the bundle commit and the probe result.
- The testnet `ROLE` loop (`dist/main.js`) is untouched.

See `deploy/README.md` §"The browser path on the production download kinds".

### Upgrading the native `ant` binary in place on a running prod client

`deployments upgrade-ant-binary --name <n>` force-installs `ant` on an
already-running `prod-downloaders` / `prod-continuous-downloader` /
`prod-continuous-uploader` client VM over SSH and restarts the service. No
infrastructure is touched.

It exists because the deploy-time install ops are **presence-gated**
(`test -f /root/.local/bin/ant && SKIP`), so a plain re-`deploy` never picks up a
new release on an existing deployment — and for `prod-continuous-uploader` a
`down`/`up` is not a way round it either, since `deploy` deliberately reuses the
existing registry row and a fresh one would break the deployment's continuous
history. VM IPs come from S3 client state.

- **Hosts are grouped by the unit they actually run, and each group restarts its
  own.** `prod-continuous-uploader` is the only multi-role kind:
  `role: upload` → `ant-prod-continuous-uploader`, every other role (the
  verifiers) → `ant-prod-upload-verifier`. **Both roles execute the native `ant`
  binary**, so both want the upgrade; one hard-coded unit name restarted a
  service that exists on neither.
- **A WASM uploader VM is skipped entirely** — `--name PROD-WUL-01` installs on
  its NATIVE verifier VMs only. The transport is read from `deploy_args.client_kind`
  (absent = native, as the provisioner defaults), never probed on the host.
  `PROD-WDL-01` is wholly WASM, so the command warns and names
  `upgrade-wasm-client` instead of installing a binary nothing there executes.
- **One unreachable host does not stop the others** — groups are upgraded
  independently, failures accumulate, the summary reports per role, and the
  `ant_binary_upgrade_failed` event names the hosts.
- **`--no-restart` matters for a completed `prod-downloaders` run**: the service
  has no checkpoint, so restarting re-walks the whole address list from scratch
  and duplicates the metrics.

See `deploy/README.md` §"Upgrading the `ant` binary in place".

### Upgrading the WASM client in place, and `client_build`

`deployments upgrade-wasm-client --name <n> [--name <n>...] --branch-name <ref>`
replaces the WASM bundle on a running **`prod-continuous-uploader`** or
**`prod-continuous-downloader`** VM over SSH and restarts its unit. The permanent
prod WASM deployments are a *series*, and the point of them is the series, so
iterating on the client must not break it — `upgrade-ant-binary` covers only
native `ant`, and re-running `up` would call `record_deployment_created` again
and start a fresh registry row. (`client_build`, which records *which* build
performed each transfer and is what makes an in-place upgrade analysable, is in
the common WASM rules above.)

- **Everything is refused before anything is built** — another kind,
  `client_kind: native`, an unknown name, or no client state — with all problems
  in one invocation, because a build is ~10 minutes on a VM that bills.
  `DATABASE_URL` is required (the kind, the transport and the old commit all come
  from the registry). The one-shot kinds are rebuilt by every `up`, and a
  testnet's WASM tier dies with its testnet, so neither needs this.
- **One build, all names.** Built once under the FIRST named deployment and the
  same bundle URL installed everywhere — installing identical bytes is what keeps
  the deployments comparable, where a build per deployment could give each a
  different commit of a branch that moved in between.
- **The continuous uploader's verifier VMs are deliberately untouched** (they are
  native, so a `digest_mismatch` stays attributable to the upload rather than to
  a browser read path). Their `ant` binary is `upgrade-ant-binary`'s job.
- **The host-side service script is redeployed with the bundle.** The bundle is
  the half that *performs* a transfer; `prod_downloader.py` /
  `prod_continuous_uploader.py` is the half that *records* it, so upgrading one
  alone leaves the recording half behind. PROD-WUL-01 proved it: provisioned
  2026-09-26, `client_build` plumbing landed 2026-09-27 17:43, bundle upgraded
  78 minutes later — and from then on its per-op log carried `client_build` while
  **every** `deployment_prod_upload_results` row had the column empty. One
  definition (`provisioning/service_script.py`) shared with both provisioners.
- Timing is the operator's — run it outside the daily window. A failure on one
  deployment is reported and the rest still upgrade, exiting non-zero at the end.
- On success `deploy_args.wasm_build` is replaced and a `wasm_client_upgraded`
  event records `{old_commit, new_commit, ref, repo_owner, upgraded_at}`; a
  failure records `wasm_client_upgrade_failed` and leaves `deploy_args` alone, so
  the registry never claims a build a VM did not take.
- A run manifest under `deploy/manifests/runs/` still pinning a different
  `wasm_binary.branch` is warned about — the next `up` would roll the upgrade
  back.
- `print-recorded-results` splits its tables and totals footers by `client_build`
  whenever a deployment shows more than one; with one (or none) the output is
  unchanged.

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

`client_kind: native|wasm` makes the **uploader** service spawn `ant-wasm`, so
the daily test answers whether a *browser* client can still write to production.
The WASM series is its own deployment alongside the native one. See "rules common
to every kind" above. Everything around the spawn is unchanged — the per-cycle
`claim_pool_files`, the SHA-256, the Postgres writes, the replacement logic, the
unit names and restart policies. Example:
`deploy/manifests/profiles/prod-wasm-continuous-uploader.yaml`.

What is specific to this kind:

- **The read-back runs over the same transport** (the VM's own view as a browser
  user), but **the independent verifier VMs stay NATIVE**, so a
  `digest_mismatch` remains attributable to the upload rather than to the browser
  read path. The browser read path is covered by the read-back and by
  `prod downloaders --client-kind wasm`.
- A failed upload is partial if a payment went out, released if not (see the
  common rules). **The native "no result document + non-zero exit → release" rule
  is untouched.**
- **The success check is on the `error` key's VALUE, not its presence.**
  `ant-wasm` always emits `error`, set to null on success, where `ant --json`
  omits it — a presence check reported every healthy browser-path upload as a
  partial, which SPENDS the file's claim.
- **The pool draw is narrowed to the cap**: `large`/`xlarge` refused at
  validation, `medium` clamped to 1,000,000,000 in `--pool-max-size` so `plan`
  shows it. So an over-cap file is never claimed, and the runtime `over_cap`
  status the one-shot uploader needs is unreachable here.
- **`log_level` is refused too** (the `ant` verbosity flag — use `rust_log`). The
  CLI's `--log-level` default moved from `"info"` to `None` so an explicit one
  can be told apart; every consumer already resolved None to `info`.
- Nothing on a WASM uploader runs `ant`, so the binary is not installed there;
  the verifiers keep it.

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

`--wasm-uploader` / manifest `prod_uploaders.wasm_uploaders[]` makes an
uploader's `ant-prod-uploader` service spawn `ant-wasm`. See "rules common to
every kind" above. Example: `deploy/manifests/profiles/prod-wasm-uploaders.yaml`.

- **The transport is per uploader, not per deployment** — the one kind where that
  is true. A native and a browser-path uploader in one run share the wallet, the
  monitoring stack and the prod bootstrap list, so both transports are measured
  against the same network at the same time. `uploaders` may be empty for a
  WASM-only run.
- Each WASM entry gets the `wasm-upload` **role** from `clients infra` — the role
  names the VM, its Terraform workspace and its S3 state key, and telling the
  tiers apart in all three is the point of running both. Same shared
  `ARBITRUM_ONE_SECRET_KEY` as native (deploy prints the paying address).
- **The cap shapes the pool draw.** `large`/`xlarge` are **refused at
  validation** — every file in them is over the cap, and a drawn one is a
  permanently-spent entry that could never be uploaded — while `medium` is
  implicitly narrowed to `pool_max_size=1000000000`, which `plan` SHOWS.
  `tiny`/`small` gain no bound, because a token that narrows nothing is not free:
  the argv is what the step journal compares.
- **Claim outcomes are the load-bearing part**, and two `ProdUploadStatus` values
  exist to carry them. Success → `uploaded`. A failure **after** payment →
  `partial_upload` → `partial` (chunks ARE in production, so uploaders may
  complete the file but a canary must never draw it). A failure **before** any
  payment → `failed` → **released**. Over-cap → released. A source-fetch failure
  → released too, where it was previously left open — which read as "the run
  never touched this file" while withholding it forever.
- **Refused, not ignored**: `memory_profiling` and `log_level`. The manifest has
  no such fields (so `extra="forbid"` names them) and the spec parser rejects the
  tokens by name.
- `fetch_all_prod_upload_points` reads both buckets and dedupes on
  `(host, start_ns)`. `print-uploads`, `print-recorded-results`, `slack-post`
  (two headings, the WASM one only when WASM entries exist) and
  `record-results --update-slack` all split per transport. The duplicate-file
  preflight and the destroy results guard cover WASM entries exactly as native
  ones. `generate-inventory` still has no `prod-uploaders` path.

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
- **Its probe pull is INCREMENTAL and its writes are batched**, because the
  probe series only grows: PROD-CANARY-04's first publisher run took 5,062s for
  423,108 points, 3,164s of it one `INSERT … ON CONFLICT` and one `commit()` per
  row. The window now opens **6h before the newest `probed_at` in Postgres**
  (the margin covers probe-VM clock skew and a late point; re-reading the
  overlap is a no-op write), and 1,000 rows share one statement and one commit.
  A deployment with no probes recorded still pulls everything. The upload and
  chunk queries are deliberately NOT windowed — they are the canary set, written
  once. `--since <ISO>` / `--since 0` / `--full` override it.
- **Loss and the episodes are re-derived from POSTGRES, scoped to the canaries
  this run saw a probe for** — the two-strike rule needs both strikes and an
  episode can open before the window, so the window is never the derivation's
  input; and a canary with no new probe keeps the verdict it had, so its loss
  row is also exempt from the stale-row deletion. `--full` is therefore the
  rebuild after a change to how a past probe should be classified. An
  already-`uploaded` pool claim is skipped for this kind too (a canary's upload
  rows never change, so re-resolving one rewrote what it already held).
- **`prod canary export` publishes the public dataset** — two JSON documents
  (`<prefix>/index.json` plus `<prefix>/<series id>.json`) to an S3 bucket or a
  local dir, which is what **`canary.autonomi.com`** renders. It **reads
  Postgres only** (so export *after* `record-results`, and a destroyed canary
  stays exportable as history) and its **schema is a contract with that site**,
  versioned `schema_version` and closed: the key set is written out explicitly,
  so a probe hostname, an IP or a new column cannot leak by default — a vantage
  is a region code with a label, never a host. The index is **merged**, keeping
  every other series' entry, and `--as-of` makes the bytes reproducible.
- **`prod canary provision-publisher` is what feeds `canary.autonomi.com`
  unattended**: a daily timer on the **monitoring VM** running `record-results`
  then `prod canary export`. That host because `destroy` keeps it, it already
  carries the Slack reporter, and InfluxDB is local to it. It needs **Python
  3.13 via a pinned `uv` on the VM** (the `deploy` package requires ≥3.13 and
  Ubuntu 24.04 ships 3.12), installed from two wheels built at provision time
  and pushed over SSH — the VM has no GitHub credential. `run.sh` passes the
  **Influx URL and token explicitly**, so `record-results` never falls back to
  `load_monitoring_state`: the VM has no credential for the tfstate bucket and
  the fallback would fail the timer nightly with a boto3 error. It is
  `set -uo pipefail`, not `-e`, so `last-run.json` can name the failing step.
  Re-running the command IS the upgrade path (hence `uv venv --clear`);
  `unprovision-publisher [--purge]` removes it and nothing else on the VM.
  The env file is **uploaded with `files.put`, never heredoc'd** — it carries a
  DB password, an AWS secret and the Influx token, which a shell op would put
  in the VM's `/proc/*/cmdline` — and its values are **double-quoted**, since
  systemd reads `\` in an unquoted `EnvironmentFile` value as an escape.
- **Both workspace packages need an explicit hatch wheel `exclude`**, because
  **hatchling honours only the ROOT `.gitignore`**: `resources/wasm-client/
  node_modules/` is ignored by a nested one alone, so it went into the
  `saorsa-deploy` wheel on any tree that had run `build ant-wasm-check` —
  23.7 MB and 12,478 of 12,888 entries, against 1.3 MB clean. Anything else
  that installs a wheel built from this tree inherits that trap;
  `build_publisher_wheels` refuses one over 8 MB as the tripwire.

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
- **The command leaves the whole binary directory owned by the unit's `User=`**
  (`chown -R` on both the install and the rollback, then `find <dir> ! -user
  <svc>` — any output rolls the host back and stops the run). ant-node's own
  upgrader runs as that user and writes `<binary>.backup` with `fs::copy`, which
  cannot overwrite a file it does not own; six hosts log in as root and `cp`
  without `-p` takes the invoking user, so this command used to leave a
  `root:root` backup that defeated the upgrader **permanently**. NRT and SYD sat
  retrying hourly on 0.20.0 through the whole 0.21.0 rollout — alive, serving and
  a release behind, with nothing alerting on it. `prod bootstrap deploy` chowns
  the directory at provision time for the same reason.
- **`--limit-to` never shrinks the published address list**: the collection after
  the last host reads the WHOLE inventory. The file is every browser client's way
  in, and the all-or-nothing guard catches an unreachable host, not a short input
  list.
- **`prod bootstrap status` is the read-only view** — per host: on-disk vs running
  version, `active`, the pinned WebRTC port, `stop_on_upgrade`, the backup
  binary's owner and size, and the `find`-derived `foreign_owned` list. It
  **never refuses a host** (a unit `upgrade` would reject still yields a row with
  `unit_ok: false`), `--json` puts the array alone on stdout, and the exit code
  says only whether the report is complete: `0` all reachable, `1` otherwise.
  `running_version` comes from the start line in the log **of the day systemd
  says the running process started** — ant-node rotates daily and keeps 14, so
  the newest file holds no start line on a host that has been up for days, and
  walking back from it reads 4.7 GB off nrt's disk for one line.
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

The testnet-side browser tier: roles `wasm-upload` / `wasm-download`, manifest
keys `clients.wasm_uploaders[]` / `clients.wasm_downloaders[]`, independent of
the native pair (a testnet may run either or both side by side). See "rules
common to every kind" above for transport, results, cap, build and `client_build`.
Example: `deploy/manifests/profiles/webrtc-wasm.yaml`.

They are **distinct roles, not a flag on the native ones**, because the role
names the VM, its Terraform workspace and its S3 state key. The role-to-unit map
is explicit in `client_roles.py`; it replaced an `f"ant-{role}er"` string trick
that spells the right thing for exactly the two native roles.

Three pieces beyond an ordinary testnet, all no-ops when no WASM client is
configured (every existing deployment renders byte-identical):

- **A pinned WebRTC port on the bootstrap tier** — `bootstraps.webrtc_port` (CLI
  `--webrtc-port` on `nodes provision-genesis` / `bootstraps provision`).
  Bootstraps are the one tier whose browser-facing address must not move; left
  unset ant-node **derives** the WebRTC port from the QUIC port, so it moves
  whenever that does. Regular nodes deliberately keep the derived port — their
  address changes propagate over the DHT as QUIC's do. **The port is only half of
  a stable address**: the multiaddr also embeds the DTLS certhash, persisted at
  `<root_dir>/webrtc-direct.pem`. It survives restarts, but **wiping a
  bootstrap's data volume rotates it and invalidates every cached browser
  address**. WebRTC-Direct is ON by default in a stock ant-node build.
- **`build ant-wasm`** — a separate build path is unavoidable: `ant-core`
  declares `default = ["native"]` and a `browser-wasm` feature mutually exclusive
  with it (a wasm32 build without it stops at an explicit `compile_error!`), and
  the artifact comes from `wasm-pack`, not `cargo build --bin`.
- **`deployments webrtc-addresses`** — SSHes the **bootstrap VMs** and reads each
  bootstrap service's `webrtc-direct.multiaddr`, saving them as
  `bootstrap_webrtc_multiaddrs`. **Cannot run at provision time** (the address
  embeds the certhash, which exists only once a node has started) and **must run
  before `clients provision`** (a WASM client has no other way in). `up`
  sequences it after `nodes start`. Addresses advertising a non-public host are
  rewritten to the VM's real IP.

What will bite you:

- **A WASM client gets the SAME bootstraps as a native client, and no more.** The
  sole difference is address format — native takes bare `ip:port`, a browser
  needs the full multiaddr — which is the only reason a collection command exists
  at all. The node tier is deliberately **not** sampled and there is no
  `--sample-nodes` flag: an earlier revision handed each client ~32 node
  addresses spread across the peer-id keyspace, which production cannot supply (a
  browser gets a small bootstrap list), so it masked discovery failures rather
  than exposing them. **If transfers do not work from the bootstraps alone, fix
  the node.** `clients provision` reads only `bootstrap_webrtc_multiaddrs`.
- **`nodes.nat_percent: 0`** — nodes behind the simulated inbound NAT are
  unreachable over WebRTC-Direct without relayed WebRTC, and each dial burns a
  10s DataChannel timeout. This also makes the run incomparable to a native
  baseline that used a different NAT fraction.
- **A WASM upload needs ≥7 initial PUT peers** before it will pay; a small fleet
  fails every upload with "Witnessed close group returned only N/7".
- **Every published address line is `<address> <bytes>`.** ant-core refuses an
  over-cap file only *after* resolving its data map, so with bare lines the WASM
  downloader drew the native 1000 MiB uploads from the shared pool and failed
  every one. Both downloaders parse the first field, and one with a bound
  (`MAX_DOWNLOAD_BYTES`, `0` = none) skips over-cap entries before fetching
  anything. `downloader.sh` tries up to five keys because one uploader's entire
  list can be over the cap. The constant lives in `client_roles.py` (the
  provisioner must not import the schema).
- **Addresses are one shared pool per deployment, read two ways.** The WASM
  downloader lists the whole prefix (so it fetches native uploads too); the
  native `downloader.sh` filters keys to `upload-*` and never sees WASM uploads.
  The per-uploader *object* is namespaced by role (`upload-` vs `wasm-upload-`)
  because `client_index` restarts at 1 per role — without that a native and a
  WASM uploader in one region both own `upload-<region>-1.txt`, and each
  whole-object PUT flush would erase the other's list.
- **Payment: no private key ever enters WASM.** ant-core verifies each storage
  quote and hands the set to a JS callback, which does ERC-20 `approve` then one
  `payForQuotes` on the vault and returns the **mined** tx hash; `totalAmount`
  must exactly equal the sum of quote amounts or the upload is rejected. The
  payment network (chain id / token / vault) is read from a node's own `HELLO`
  rather than configured locally, so the client cannot pay on the wrong chain.

**Gates — the two KINDS get opposite answers**, and both keys in
`staging-thresholds.yaml` are **required** (the retired single
`wasm_gates_advisory` fails validation naming its replacements):

- **`wasm_floor_gates_advisory: true`** — the success FLOORS are absolute numbers
  calibrated on the native transport which WASM does not meet (92.1% upload /
  94.9% download against 99% / 99.5% on the first measured run), and the gap is
  **NAT** — a peer behind the simulated inbound NAT is unreachable over
  WebRTC-Direct until relayed WebRTC exists — not the release under test, so
  gating on it would hard-fail every run.
- **`wasm_relative_gates_advisory: false`** — the success-rate and per-bucket
  p50/p95 gates against the BASELINE need no absolute calibration, since they ask
  only whether this release made the WASM tier worse than the last one did, so
  they **COUNT**, as SOFT gates like their native twins.
- **No WASM gate ever reaches the exit code**; that stays the native hard floors'.
- `deployments compare` evaluates the client-record gates **once per transport**,
  suffixed `[native]`/`[wasm]` whenever a run has WASM records; with none, the
  gates are emitted exactly as before. **Node-tier gates are NOT split** — one
  fleet serves both transports.
- **`clients estimate-gas` sizes the WASM tier too**, labelled `[wasm]` and
  counted in the funder total, with `fixed_eth_per_upload` **doubled** via
  `wasm_fixed_cost_multiplier: 2.0` (a browser-path payment is two transactions,
  `approve` then `payForQuotes`, where a native one is one) while `eth_per_gb` is
  unchanged. Throughput comes from the baseline's own `client_kind = 'wasm'`
  records; failing that, from the NEAREST native uploader by size, labelled
  `approx: native twin <host>` (WASM measured ~6–8x slower, so a native rate
  over-funds, which is the safe side). Native uploaders are sized from native
  records only, so a future baseline carrying both tiers cannot pollute native
  sizing.
- `slack-post` and the `record-results --update-slack` reply render the two tiers
  separately, and only when WASM clients exist (NULL counts as native).

**Profile:** the WASM tier lives in `staging.yaml` itself — 3 WASM uploaders
(900/300/20 MB; 900 not 1000 because of the cap) and 1 WASM downloader on an
8 GB VM — **added on top of** the unchanged native tier, not swapped into it. The
profile's `nat_percent` dropped 30 → 10 for the same reason WASM's floors are
advisory, and both changes made the first run on it a full **REBASELINE** (what
`--new-baseline` is for). The binary is built from an ant-client **git tag**.
`build ant-wasm-check --branch <ref> --repo-owner WithAutonomi` runs that same
build **locally** in a temp dir and deploys nothing, so an ant-client API drift
is caught before a build VM and ~10 minutes are spent on it.

**Verified against a local `ant-devnet`** (2026-09-07), not a cloud testnet: the
WASM client runs under **plain Node.js** with `node-datachannel` supplying
`globalThis.RTCPeerConnection` — no headless browser. HELLO, `find_node`, a
5 MiB download and a real paid upload with byte-identical round-trip all pass.

See `deploy/README.md` §"WASM client VMs (the browser path)".

### The pointer client tier (`clients.pointers[]`)

A **pointer** is a paid, owner-signed mutable reference (ADR-0016 in `ant-node`):
one ML-DSA-65 owner key owns exactly one pointer at `BLAKE3::derive_key(owner)`
forever, and every write — create AND update — is one paid single-node quote. This
tier exercises them for the life of a testnet, so the behaviour is measured before
it reaches production, the way the WASM tier did for WebRTC. Role `pointer`, unit
`ant-pointer`, manifest `clients.pointers[]`. Example:
`deploy/manifests/profiles/pointers.yaml`.

**A role of its own, and an upload-type role.** Its own because the role names the
VM, its Terraform workspace and its S3 state key; upload-type because it carries a
funded per-VM wallet, topped up and drained exactly as an uploader's.
`is_upload(ROLE_POINTER)` is therefore **True**, so callers meaning "uploads
files" must test the role — a pointer client has no `file_size_mb` (persisted as
0, which is what lets every shared wallet path treat it as an uploader).
`test_pointer_client_plumbing.py` guards the sites that dispatch on role and would
silently skip a new one, including the destroy-time service stop that was
hard-coded to `ant-uploader` — a pointer client was being drained while still
spending, the exact nonce race that stop exists to prevent.

What will bite you:

- **The workload is one serial scheduler, not four threads.** All four activities
  share one wallet and every write is an on-chain payment, so two concurrent
  writes would race the same nonce. Defaults are **180 paid writes per client per
  hour**, plus 24 more when the handover loop is on. Creation runs for the WHOLE
  run rather than seeding a pool up front, because decay with age needs pointers
  of every age present at every moment; `pointer_max` is a safety cap, not the
  design.
- **The ledger is durable and load-bearing.** `/var/lib/ant-pointer/` holds the
  owner keys and the expected `(counter, target)` per pointer. `Restart=always`,
  and **ownership is immutable** — a lost key is a pointer that can never be
  updated again — so the key is written to the ledger BEFORE the create that pays
  for it. `clients clean` removes the directory deliberately: a surviving ledger
  would have the client sweeping pointers whose keys are gone.
- **Ten outcome classes, because most of the failures are ordinary events.**
  `ok`; `stale` — **EXPECTED at a non-zero rate**, since an eclipsed reader can be
  served an older validly-signed state and cannot tell; `lost_write` (paid for,
  not landed); `not_found` (the close group agreed there is nothing there — an
  ANSWER, which is why `ant pointer get`'s exit-1 maps here and NOT to `error`);
  `read_shortfall`; `anomaly` (a counter ABOVE the one written); and `error`, the
  only class indicating a fault. The last three come from the handover's finality
  check: `settling` (the group has the final state but has not converged),
  `unconfirmed` (final on a MAJORITY with a peer that did not answer — the state a
  silent peer leaves behind, and exactly what `transfer` refuses to write into)
  and `forked` (two rival final states, the one alarming reading). NB
  `Error::CloseGroupShortfall` renders as "close-group **PUT** shortfall" for
  READS too, so matching on the word PUT would put every read shortfall in the
  write bucket.
- **Cost is measured as PAID, never as quoted.** `ant pointer create --json`
  prints no cost, so ANT comes from `ant wallet balance` and native gas from
  `eth_getBalance` either side of each write. That is also the only way to see the
  ADR's warning that a write which times out and is retried **pays twice**:
  `paid_attempts > 1` is a double payment.
- **The WASM downloader had to be taught to skip `pointer-` objects.** It lists
  the whole shared prefix, so it would otherwise fetch pointer addresses as files
  and the failures would land in the download success rate as lost content.
  `downloader.sh` filters to `^upload-` and is unaffected. The handover list
  (`pointer-transfers-*`) is covered by the same prefix, so neither needed a
  change for it.
- **Results go in their own table** (`deployment_pointer_records`), recorded in the
  SAME `record-results` pass as the file ops — a command an operator has to
  remember separately does not get run. Not a third `deployment_*_records` sibling:
  there is no file, and folding `stale` into `OpRecordStatus.FAILURE` would put the
  expected stale-read rate into the upload/download hard floors.
- **Eleven `[pointer]` gates, none of which reaches the exit code** (every one
  soft, so the worst is a SOFT FAIL): four absolute floors and seven relative
  comparisons. **NOT ASSESSED, never omitted** when the baseline has no pointer
  records — which is every baseline on the first pointer-carrying train. The four
  floors are additionally advisory (`pointer_floor_gates_advisory: true`) because
  no run has measured a pointer tier and the ADR does not say how large the
  expected stale rate is.

`deployments print-pointers --name` reads Influx (live, the only view during a
run); `--id` reads Postgres (post-run, the only view that survives teardown);
both go through `pointer_results.py` so they cannot disagree. `--by-age` buckets
**sweep** outcomes by age at read, own and foreign separately.

#### The handover loop (`transfer_interval_secs`, V2-1354)

`clients.pointers[].transfer_interval_secs` — integer, **default 0 = no handover
loop** — adds a fourth loop that hands pointers over for good: `ant pointer
transfer` signs one last state at counter `u64::MAX` whose target is a recipient
pointer's address, and `finality` / `controller` then read the result. One cycle
is: pick an owned, untransferred pointer → create a recipient (an ordinary
`op=create`) → `transfer` → `finality` → a redirect `get` → `controller` →
`resolve` → an `update` take-back → a `transfer` take-back. Nine steps, nine
points, each with its own `start_ns`.

It is OFF by default because `ant pointer transfer` does not exist on a build
predating V2-1354 — on such a train the operator strips the KEY, not the entry,
and the two handover gates then read NOT ASSESSED. `staging.yaml` and
`pointers.yaml` both carry `transfer_interval_secs: 300`.

What will bite you:

- **A take-back's success is a REFUSAL, and the wallet is half the assertion.**
  `ok` needs exit ≠ 0, `pointer is final` on stderr, AND both balances read with
  zero spend: a refusal that still cost money is not a refusal, and the ADR's
  claim is that a handover cannot be taken back at all. A balance that could not
  be READ gives `error`, never `ok` — unknown is not zero.
- **The silent-peer refusal is excluded from the completion rate.** `transfer`
  refuses before paying when any close-group peer stays silent ("a transfer cannot
  be taken back, so try again when all of them can be asked"). That is the guard
  working, so it is reported as its own rate over all attempts while
  `completion = ok / (ok + lost_write + error + anomaly)`. A **rival** final state
  is `anomaly` and DOES count against completion — nothing of ours finalized the
  pointer, so something else ended its life.
- **`transfer`, `finality`, `controller` and `takeback` are four new ops, and only
  `transfer` is a write.** `takeback` is in neither `WRITE_OPS` nor `READ_OPS`:
  averaging an op whose success is a refusal with writes that are supposed to land
  would invert its contribution.
- **`u64::MAX` fits neither an Influx integer field nor a Postgres `BIGINT`.** It
  travels as a STRING field under its own name (`counter_expected_u64`), the same
  route the atto balances already take in this measurement — a string in
  `counter_expected` would be a type conflict and Influx would drop the whole
  point — and `counter_expected`/`counter_observed` widen to `NUMERIC(20)`.
- **Handovers are published to a SECOND S3 object**, `pointer-transfers-{region}-
  {index}.txt`, one `<old> <recipient>` line each. Not onto the address list: its
  reader takes the first field of each line, so a pair written there would publish
  the old address with the recipient silently dropped, leaving every other client
  with an address whose expected state it does not know.
- **A handed-over pointer is excluded from `update` and from the chain targets**,
  and the ledger moves its expected state to `(u64::MAX, recipient)` so every
  later read — own sweeps and the other client's — is checked against the final
  state. The RECIPIENT is an ordinary owned pointer and stays updatable.
- **`finality` on foreign handovers is SAMPLED** (at most 20 a sweep, a module
  constant). It asks the whole close group and never stops early, so it is the
  most expensive read here while the handover population only grows.
- Two soft floors, `handover completion [pointer]` (95%) and `handover final on
  sweep [pointer]` (99%, over finality checks at least 600s old — a check seconds
  after the transfer legitimately reads `settling`). Both advisory, both NOT
  ASSESSED when the candidate has no `transfer` records.
- **Catalogue v6 adds `pointer_finality`** — the node side: two refusals of a
  final state (fresh and replicated) and the fork WARN. All three are
  event-only, so a zero means "no handover reached this fleet" as much as "none
  was refused"; it is only readable beside the client-side `transfer` counts. The
  happy-path refusal of a take-back logs NOTHING.

`record-log-counts` gains `pointer` groups (catalogue version **3**; the
connection groups are untouched, so v2 and v3 counts of them stay comparable).
The stale refusals, quorum shortfalls and unpaid offers are all DEBUG and the node
forwarder drops DEBUG at `log_level: info` — which is what staging runs at — so
they are deliberately NOT catalogued; they would read zero for a reason unrelated
to pointers.

**Catalogue v4 (V2-1413) adds the subtree storage-audit counts**, for the
V2-1363 A/B. `subtree_audit_failure` carries one pattern per
`AuditFailureReason` variant at ant-node v0.21.0 (`Timeout`,
`MalformedResponse`, `DigestMismatch`, `KeyAbsent`, `Rejected`) off the single
auditor WARN, so the group sums to the **true** total — `tail.message` is
analysed, so the parentheses are discarded and it is the reason *token* that
discriminates, which it does uniquely. ant-node #238's two new strings are
deliberately absent (one shares its message with the pre-existing heavy-pool
drops; the other is `debug!`); both its regression modes show up as
`subtree_audit_fail_timeout` rising. Every pre-existing key is untouched, so
only the NEW keys read NOT ASSESSED against runs 612/614/615/627.

**A clean audit-failure count is unfalsifiable on its own**, because v0.21.0 has
no info-level line for a successful round-2 pointer opening: zero failures reads
identically to audits that never opened a pointer leaf. The proxy is
`analysis pointer-commitments --name N --start … --end …`, which splits the
`Storage commitment rotated` line by whether it reports `pointers=0`. That needs
a **wildcard on `tail.message.keyword`** — the whole line is one unparsed
`tail.message`, `pointers=` is not a field, and `match_phrase` cannot express a
wildcard — which is why it is a command and not a seventh pattern. Query shape
reused verbatim from `evaluate_commitment` in
`scripts/rollout-watch/pointer_signals.py`; the only differences are the index
and no version arm (a testnet run is single-version). Exits **0 whatever it
finds** — a measurement, not a gate.

**Its verdict separates composition from sufficiency** (V2-1450). The line is
emitted at most hourly per node and only when the responsible key set changed,
so a short window legitimately holds one rotation — which is well formed, and
used to print **PASS**: run 647 did exactly that against its own criterion of
≥20 rotations on ≥5 hosts. Pass the run's own `--min-rotations` / `--min-hosts`
and a shortfall reads **INSUFFICIENT**; with neither supplied the detail line
says volume and spread were **not assessed**. A rotation *missing* `pointers=`
still FAILs at any volume (one line proves a non-pointer-capable service), while
a shortfall outranks the all-`pointers=0` FAIL (absence needs volume).

**`--json-out -` / `/dev/stdout` is how an unattended runner reads an
`analysis` command** (every one but `flowstat`, which must stay stdlib-only for
the VM): the JSON goes to stdout exactly once and every human line
— the `wrote …` trailer, the table, the preamble — is suppressed. These commands
used to print the object as their own output *and* write it to the destination,
so `/dev/stdout` emitted it twice with the trailer on top; `jq` aborted and the
`// 0` default produced a confident **0**, which is how run 647's `reach` table
came out as thirty zeros for a fleet at 100% upload and download success. The
`tail -1` mitigation in the older run issues does not cover a parse abort. Use
`-`; a real path keeps its trailer.

`staging.yaml` carries **two** pointer entries, geographically separated so
cross-client sweeps span a real distance. **The train's RC must carry pointers**;
if it predates them, strip the two entries for that train.

See `deploy/README.md` §"Pointer client VMs".

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
OVH). It authenticates via an OAuth2 password flow reading four
**`CNTB_OAUTH2_*`** env vars (`CLIENT_ID`/`CLIENT_SECRET`/`USER`=account email/
`PASS`=API password), which the `contabo/contabo` Terraform provider reads
directly from the environment — so, like OVH, there is no `TF_VAR` credential
mapping, just a `_check_contabo_env` preflight. TF modules:
`resources/contabo{,-monitoring,-elastic}`.

Two Contabo constraints shape the design:

- **No attachable block volume.** Contabo's provider has no standalone
  attachable/growable block-storage resource, so Contabo VMs are **root-disk
  only**, reusing the existing OVH 3-AZ root-disk machinery. **Size the root disk
  up front** — it cannot be grown online later without a disruptive rebuild.
  `--volume-size` and friends are ignored.
- **Committed monthly billing.** Instances are prepaid monthly contracts (no
  hourly billing, no proration on early destroy), which suits the *permanent*
  debug-node use case but not ephemeral churn.

Images are referenced by UUID (not resolvable by name via Terraform): pass
**`--contabo-image-id`** (resolve via `cntb get images`) wherever a Contabo tier
is used. The operator public key is registered as a `contabo_secret` from
`--contabo-ssh-public-key-path`, and `default_user=root` gives root SSH.

Contabo is wired for the single-`--provider` node path (prod kinds /
`nodes infra`) and the observability stacks; it is **not** yet a
`nodes.providers.<name>` option in testnet/extension manifests.

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

Testnet uploader wallets hold a **fixed** amount of native ETH, so runway is
inversely proportional to the gas price — when the price rises the same funding
buys fewer payment transactions, and an exhausted client fails **every**
subsequent upload in a way indistinguishable from a network fault in the gate
data. Staging run #566 lost six of ten uploaders that way after the Arbitrum
Sepolia base fee rose ~10x in three days, putting whole-run upload success
(96.02%) and the mixed-version phase (79.35%) below their hard-gate floors for a
non-product reason. Throughput was **not** the cause — that run did less work
than its baseline.

`clients estimate-gas --config <manifest> --baseline-id <id>` is the read-only
preflight, run as **Step 3b of the `run-staging-testnet` skill**: it reads the
current base fee from the chain, takes per-uploader throughput from a previous
run, and prints the ETH each uploader needs plus whether the funding wallet
covers the total. **Exit 2 = something is short.** The fix is either the
profile's `eth:` values (a profile SHA change, so a re-baselining event needing
the RM) or a top-up of the funding wallet — no profile edit helps the latter.

Three load-bearing details:

- Throughput is taken as each client's **rate over its own active window**, never
  its raw count — a client that ran dry stopped early, so its count would teach
  the next run to under-fund.
- Scaling is on **base fee**, so the priority fee and estimator buffer cancel in
  the ratio.
- The funder check compares against the **full** recommended total, because
  top-up is per-wallet and sequential, and a short funder leaves a
  half-provisioned run rather than failing up front.

Model and calibration live in `resources/gas/client-gas-model.yaml` (versioned
config, like the gate thresholds, so recalibration needs no release). Pure logic
in `gas_model.py`; `deploy/tests/test_gas_model.py` asserts the shipped config
still reproduces its own six calibration points.

See `deploy/README.md` §"Sizing uploader gas funding".

## Monitoring

Optional per-testnet monitoring stack: InfluxDB 2.x (always) plus an **optional**
Grafana VM. Standalone — usable by this repo or external setups. Runs on
**DigitalOcean (default) or OVH** (`--provider ovh`).

**Grafana is opt-in and OFF by default**: `monitoring infra` creates only the
InfluxDB VM unless `--grafana` is passed (manifest `monitoring: {grafana:
true}`). The choice is recorded as `grafana_enabled` in the monitoring S3 state,
the Terraform `grafana` resources are `count`-gated on `enable_grafana` (with
`moved` blocks so pre-existing 2-VM stacks migrate to index `[0]` in place
rather than being recreated), and `monitoring destroy` passes `enable_grafana`
back from state so the destroy plan matches what was created.

When enabled, Grafana is split onto its own small VM so its panel rendering does
not compete with InfluxDB for CPU/RAM — the cause of large dashboards failing to
render on a single shared box. The InfluxDB VM carries the data volume; Grafana
reaches it over the public `:8086` endpoint. Three provision-time cases, keyed
off `grafana_enabled`: disabled (default) → InfluxDB only; enabled with its own
`grafana_ip` → Grafana on that VM; legacy single-VM stacks (no `grafana_enabled`
key, no `grafana_ip`) → co-locate Grafana on the InfluxDB VM. Prod client flows
likewise default Grafana off and pin the InfluxDB VM to `s-2vcpu-4gb` (a single
client VM forwards little).

Commands: `monitoring infra` / `provision` / `info` / `destroy`, plus
`deployments provision-telegraf` (see "Telegraf provisioning" below).
`provision` takes `--retention-days N` (default `0` = infinite, the dev default;
set e.g. 14 for prod), applied to both buckets at first provision.
`monitoring info --telegraf-config` emits portable Telegraf TOML. State lives
under its own S3 key prefix `saorsa-deploy/monitoring/{name}.json`, independent
of deployment state. Provider selection is in `cmd/obs_provider.py`; OVH is
OpenStack-backed and needs `OVH_CLOUD_PROJECT_SERVICE` + OVH API creds + an
`ant_deploy` SSH key in the project.

See `deploy/README.md` §"Monitoring" for the full argument tables.

### Traffic-accounting counters in InfluxDB + the Egress accounting dashboard (V2-1225)

An 18-hour bandwidth episode on a prod node (2026-09-14) was invisible in every
dashboard although the node's own traffic-accounting counters (V2-623) recorded
it — they sat in Elasticsearch, while the `net.bytes_sent` they must be divided
by lives in InfluxDB, and the labels differ (`tag.host.keyword` upper-cased vs
lowercase `host`). So the ES forwarder renders a **second output** whenever a
deployment has both elastic and monitoring state: an `[[outputs.influxdb_v2]]`
with `namepass=["tail"]` and a `tagpass` on `target`.

- **The tagpass is mandatory** (else every log line hits Influx) and is a
  **glob, not a literal list**, because ant-node#230 adds more
  `*::traffic` targets and a literal list would drop them silently. Without
  monitoring the ES config and env are byte-identical.
- Points land in `tail` in `testnet-metrics` with the **upper-cased `host`** (the
  uppercase processor runs before every output; changing it would alter ES docs).

Dashboard **"Egress accounting"** (uid `ant-egress-accounting`,
`provisioning/egress_dashboard.py`, written by every `monitoring provision` with
Grafana) plus a companion **"Node traffic by service"**. Facts the Flux is shaped
by, all MEASURED:

- Summaries are **cumulative per process, every 5 min** → `aggregateWindow(10m,
  last)` → `difference(nonNegative)` → per-`(host,_time)` `sum()`. **Not** a
  second `aggregateWindow` (it re-stamps one window late and adds a bogus zero
  row).
- **One series can carry several lines per tick**, so every series is windowed
  with **`max`, never `last`** — differenced raw it flaps `0,X,0,X` and reads 25x
  the NIC.
- **`bytes_relayed` exists under two targets** (stream relay and MASQUE, the
  latter emitted only from the CONNECT path, so an idle relay has no line).
- **Host case**: `net` keeps the deployment name's case on a testnet, so BOTH
  sides are lower-cased and `$host` matched `(?i)`; the Host variable is pinned to
  `cpu` so tail points do not duplicate the dropdown.
- **NAT VMs**: `veth-nat` is the busiest interface and its `bytes_sent` is traffic
  INTO the netns, so the NIC side excludes `lo|all|veth*`.
- **Performance**: range → filters → `aggregateWindow` with NOTHING in between
  (pushdown; a 24h view went 18s → 2s), window scales with range,
  `union`+`pivot` not `join()`.
- **The partial window at the range end is dropped on both sides** (5-min ticks
  vs 10-s samples measured 5x in that window).
- `wire_tx_bytes`/`fetch_response_tx_bytes` are **excluded from the coverage
  numerator** as double counts.
- Prod has 700+ services, so fleet-wide the companion dashboard **RANKS** (top-20
  tables, flat cost at any fleet size) and draws per-service lines for ONE host
  only.

Found while building it: the ES forwarder's path parser handled `ant-node-N`,
`ant-genesis-*` and bare `ant-bootstrap` but **not** the testnet bootstrap
provisioner's `ant-bootstrap-{index}` dir, so every testnet bootstrap's
`service_number` was the raw log path.

Prod rollout is a retrofit with `provision-telegraf --name ant-prod-01
--host-ips-file <ips> --host-role WAN_NODE --skip-metrics --only-new-logs` —
**the last flag matters**, or the restart re-ingests every on-disk log into ES.
The dashboard JSON can be dropped into `/var/lib/grafana/dashboards/`; the file
provider picks it up without a restart.

See `deploy/README.md` §"Traffic-accounting counters → InfluxDB".

### Buckets (node vs client isolation)

Host metrics are split across **two InfluxDB buckets** so the "Node Host
Statistics" and "Client Host Statistics" dashboards are *physically* isolated
rather than relying on a mutable `host_role` dropdown:

- **`testnet-metrics`** — node-tier host metrics, the `prometheus` per-node
  measurement, and all application measurements (`download`, `prod_upload`,
  `prod_canary_*`, `pointer`, …).
- **`client-metrics`** — client-tier host metrics only (tagged
  `host_role=CLIENT`), **plus** the client `ant` service's memory.

On CLIENT-role hosts `provision-telegraf` appends two extra inputs, **gated on
`host_role=="CLIENT"`** so node configs stay byte-identical and the cgroup
input's "path missing" error never fires on the node fleet: a `procstat` keyed on
`system.slice/ant-*.service` (per-process RSS, 2s) and a `cgroup` input reading
`memory.current` + `memory.peak` (2s). The single `ant-*` glob matches whichever
client unit a VM runs and nothing else. `memory.peak` is the reliable high-water
companion when a sub-2s terminal spike clips the discretely-sampled procstat RSS.
The `local-*` prod kinds run no systemd unit and are not covered.

`monitoring provision` creates both buckets and mints an **org-scoped** Telegraf
token (read+write on every bucket, so adding a bucket never requires reminting
the immutable token). Bucket names are constants in `monitoring.py`.

### Per-process memory composition (opt-in, `--node-memory-metrics`)

`procstat.memory_rss` is a single **total** and cannot say what the memory is
made of — the difference between a leak and a growing LMDB chunk-store mmap.
That ambiguity made `memory_rss` misread as a 20x leak on STG-03 run 475.
Collection is **two tiers**:

- **Tier 1 — native `procstat` mmap, the routine signal.** Telegraf already
  collects `memory_pss`, `memory_anonymous`, `memory_private_*`,
  `memory_shared_*`, `memory_referenced` and `memory_swap` — they were only
  **permission-gated**, so granting `CAP_SYS_PTRACE` unlocks them with **no new
  plugin and no script**. The base config sets `properties` explicitly to EXCLUDE
  telegraf's default `mmap`, and the opt-in fragment re-declares the same process
  sets with `properties = ["mmap"]` at **60s**. That split is load-bearing: mmap
  reads the whole of `/proc/<pid>/smaps` — 6.78% of a core at the 10s agent
  interval vs 1.15% at 60s vs 0.15% without.
- **Tier 2 — a systemd timer** (`--node-memory-interval`, default 300s, jittered)
  supplying only what procstat cannot: `rss_anon/file/shmem`, `vm_swap`, the
  `rollup_*` fields, and **`rollup_lazy_free`**. LazyFree is the point —
  MADV_FREE'd pages stay resident until reclaimed — but it is a **lower bound**,
  so zero does not exclude it. The three huge-page fields go together:
  `AnonHugePages` is anon-only and says nothing about a file-backed mapping.

- **NOT a Telegraf `inputs.exec`** — that route measured ~14.9% of a core, of
  which ~12.5% was telegraf overhead that persisted even with a trivial input and
  a 600s interval. The timer costs only the collector's own work.
- **`CAP_SYS_PTRACE`, not `CAP_PERFMON`.** `/proc/<pid>/smaps*` is gated on
  `ptrace_may_access`, NOT file permissions, so the pre-existing
  `CAP_DAC_READ_SEARCH` does not satisfy it. `CAP_PERFMON` was tested on kernel
  6.8.0-86: **0 of 13 processes readable**. Verify via `CapEff` (`0x80004` =
  both, `0x4` = base only). **The failure mode is silent and partial** — the
  cheap sources report normally while composition returns nothing — which cost a
  canary cycle because all benchmarking had been done as root over SSH.
- **Tags are attached by the collector**, since it posts to Influx directly and
  `[global_tags]` never apply. `host` is load-bearing: without it the fleet's
  `ant-node-1` series all merge, AND `record-analysis` drops host-less points.
- **Units**: telegraf reports `memory_rss` in **bytes** but smaps fields in
  **kB**; corrected once at capture so everything stored is bytes.
- **Archival is rollup-only** (`memory_composition_by_service` in
  `deployment_host_results.host_metrics`), excluded from the bucket table via
  `ROLLUP_ONLY_METRICS` — ~164k rows / ~35 MB per 990-node run against a table
  already 76% of the DB.
- **`memory_rss − memory_anonymous` is non-anonymous/file-or-shared resident** —
  NOT "file-backed", NOT "LMDB". Proving LMDB attribution needs a pathname-level
  `smaps` snapshot, **not yet implemented**.

See `deploy/README.md` §"Per-process memory composition".

### Per-device disk I/O

**Both tiers** collect per-device disk I/O via a `[[inputs.diskio]]` stanza in
the shared base config, **on by default, no flag**. Because the client config is
an *append to the node base*, that one stanza covers every install path.
Distinct from `[[inputs.disk]]`, which is capacity only.

- **The `devices` list is include-only** (the plugin has no exclude option), so
  the glob must name every wanted pattern. It spans all providers' naming plus
  LVM `dm-*`, and deliberately does not match `loop*` — Ubuntu's ~10 snapd loop
  devices would be that many dead series per host.
- **Identifying the node storage volume.** diskio knows only kernel device names,
  never mountpoints, and the backing device varies by provider and by raw-ext4 vs
  LVM-striped. So it is resolved **on the host at provision time**
  (`findmnt` → `lsblk`), written to `ANT_DATA_DEVICE`, and matched by a
  `[[processors.regex]]` that tags that device **`volume_role =
  "node-storage"`** — so Grafana and `record-analysis` filter on it directly, no
  join against `disk`, no LVM breakage. On **root-disk providers** the root
  device is tagged (that series then also carries OS I/O — unavoidable when they
  share a disk). **Client VMs never fall back to root**; they get the sentinel
  `__none__`. **Always test `volume_role == "node-storage"`, never mere
  presence.**
- **Archival**: `record-analysis` persists **`disk_io_by_device`** into
  `deployment_host_results.host_metrics`, keyed by device, each holding
  `volume_role` plus per-second rate stats computed *within* a device (negative
  deltas dropped as reboot counter resets). Devices are deliberately **NOT**
  averaged together the way net pools across interfaces — a root disk and a node
  storage volume are not interchangeable, and averaging an idle root against a
  busy data volume describes neither.
- `host_metrics` is a JSON column, so this needed **no Alembic migration**;
  pre-existing rows simply lack the key. Retrofit a running deployment by
  re-running `deployments provision-telegraf --name <name>`.

See `deploy/README.md` §"Per-device disk I/O".

### Bucketed host metrics + per-service node RSS

`record-analysis` writes host metrics **twice**: the unchanged whole-run rollup
in `deployment_host_results`, plus fixed-width time buckets in
**`deployment_host_metric_buckets`** (one row per `(host, bucket_start, metric,
device, service)` holding `avg`/`min`/`max`/`samples`). The buckets exist so an
arbitrary sub-window can be re-derived **after the monitoring stack is
destroyed** — previously `--window` fixed the window at capture and InfluxDB was
then gone.

`--bucket-secs` (default **300**; `0` disables capture). **Width does not affect
accuracy** — for a boundary-aligned window `sum(avg*samples)/sum(samples)` is
exactly the mean of the underlying 10s samples, and `MIN`/`MAX`/`SUM` are exact,
at any width; each bucket carries its own min/max, so a brief spike still
surfaces. It trades only window-edge precision (≤5 min) and trend resolution.
What it buys is table size, and the registry is on DO's smallest managed Postgres
where table size *is* query time.

Three invariants, all deliberate:

- **Rates are computed before bucketing, never after.** net/diskio are
  per-second deltas taken *within* an interface/device; bucketing raw counters
  and differencing afterwards is nonsense. `aggregate_host_metrics` is now the
  degenerate "one bucket spanning everything" case of the same code, so the two
  paths cannot drift. A rate sample is timestamped at the **end** of its interval.
- **Re-aggregating `avg` must be sample-weighted**, never a mean of means —
  buckets hold unequal sample counts at run boundaries and missed scrapes. Done
  server-side in `repo.get_host_metric_window`, which returns `host_metrics`-shaped
  entries so a window and the rollup are interchangeable. Half-open on
  `bucket_start`: a bucket is in or out whole.
- **Bucketed diskio is restricted to the `node-storage`-tagged device.** On a
  measured staging run 348 of 421 device series were root-disk boot/ESP
  partitions duplicating the root device — the majority of rows for no signal.
  The rollup still keeps every device.

Historical deployments have no bucket rows, so consumers **must** degrade to the
rollup. **No backfill is possible.** `deployments prune-host-buckets --older-than
N [--name X] [--dry-run]` drops bucket rows only, so a pruned run degrades to
exactly its pre-bucketing view.

**Per-service node RSS** (`rss_by_service` in the rollup, `rss_bytes` as a bucket
series) is captured from `procstat`/`memory_rss` via the `node_service` tag —
Telegraf always collected it and nothing ever queried it, so process-level memory
was absent from the registry entirely. `mem_percent` is no substitute:
whole-host, includes page cache, cannot attribute anything to one of 15 services.
Keyed by `node_service` not PID (services restart, PIDs churn) and **never pooled
into a host total** — one leaking service is invisible in a 15-service mean.

See `deploy/README.md` §"Bucketed host metrics".

### Normalised per-GB metrics (`deployment_derived_metrics`)

Raw measurements are not comparable across runs — a longer run moves more bytes
— which is why the rc.1 gate verdict recorded **outbound bytes per GB of user
traffic** (directly the bandwidth bill) and **disk consumed per GB stored** as
NOT ASSESSED. `record-analysis` derives both at extraction time into
**`deployment_derived_metrics`**, one row per `(deployment, metric,
window_label)` carrying value + numerator + denominator + unit + `source` +
`details`. Components are stored in their own right so a ratio is debuggable and
a later gate can renormalise — impossible once the run's InfluxDB is destroyed.

Invariants, all load-bearing:

- **Node tier only, by construction** — the numerator covers the queried Influx
  bucket, which defaults to `testnet-metrics`, and client VMs report to
  `client-metrics`, so the workload generator's egress is excluded with no
  hostname heuristic.
- **Outbound is summed from raw counter deltas, never integrated from the stored
  averages** — rate samples are emitted per interface and pooled by *averaging*,
  so integrating them reports a 2-NIC host's mean, not its sum.
- **Growth is `last - first`, not `max - min`** — a pruned host has not consumed
  its peak; the peak goes in `details.peak_growth_bytes`.
- **Denominator = successful ops whose `finished_at` is in the window**, from the
  per-op records. `deployment_file_results` is an aggregate with no per-op
  timestamps and cannot be windowed. **Run `record-results` BEFORE
  `record-analysis`** or the ratios have no denominator.
- **Unmeasurable ⇒ omitted, never estimated.** `--no-derived` skips it entirely.

This needed **filesystem occupancy**, which Telegraf collected
(`[[inputs.disk]]`) but nothing ever queried — so chunk-store size was never in
Postgres for any run, and `diskio` cannot substitute (it counts I/O including
rewrites and compaction, not what is resident). The `disk` measurement is tagged
`volume_role=node-storage` by **mountpoint**, NOT device: `disk`'s `device` tag
is the fstab-style source (`mapper/vg-lv`), never the kernel name `diskio`
reports, so on LVM the two never match. **No past run can be backfilled.**

`deployments derive-metrics --name X [--window-start/-end ISO] [--label L]`
re-derives a window from Postgres alone after teardown. It is marked
`source=buckets` (outbound integrated as `avg × width` — approximate) versus
`record-analysis`'s exact `source=capture`; **never compare the two**, and
`--label full-run` is refused outright. Bucket width is **inferred** (modal gap,
so a collection gap cannot move it) because it is not recorded on the rows and a
wrong width scales every byte total by exactly that factor.

See `deploy/README.md` §"Normalised per-GB metrics".

### Deterministic staging gate comparison (`deployments compare`)

`deployments compare --id <candidate> --baseline-id <baseline>` compares two
recorded runs on their **stored metrics alone**, applies versioned thresholds,
and emits per-gate PASS / FAIL / GRAY / **NOT ASSESSED** as JSON plus a
fixed-format markdown report — upserted into **`deployment_gate_verdicts`** keyed
`(candidate, baseline, window_label)` so a re-run replaces rather than
accumulates. Same inputs ⇒ byte-identical report (the renderer never reads the
clock). **Exits `2` on a hard-gate fail only.**

Both runs are identified by **deployment id, never name** — staging names are
reused every train. There is deliberately **no run-identity plumbing**: no
`staging_runs` table, no RC↔deployment FK, no `profile_version` column. The
baseline is the operator's judgment, supplied as an id.

**Covers** 3 hard gates (upload floor 99%, download floor 99.5%, data integrity)
and 8 soft gates (success rate, transfer p50/p95 **per file-size bucket**, node
CPU, node RSS, leak slope, outbound bytes/GB, disk bytes/GB), plus one soft gate
per connection-error group. Transfer percentiles are exact, from the per-op
records — **`deployment_file_results` is unusable**, being an aggregate with no
per-op timestamps.

**Deliberately does NOT cover** three hard gates, and the report is silent on
them entirely: crashes (panics go to stderr/journald, not the JSON logs telegraf
tails, and ES dies at teardown), mixed-version chunk storage (an SSH check), and
canaries (a separate deployment). The **`evaluate-staging-run`** skill supplies
those observationally.

- **Data integrity** is **hard, candidate-only** (so it survives
  `--new-baseline`) and fails above `download_verify_mismatch_max: 0` — a COUNT,
  and the one legitimately-zero threshold in the file. Its denominator is **ok +
  mismatch only**: `unavailable` was never checked and NULL was never checkable,
  so counting either would report an unverified run as verified. **A mismatch
  never touches the availability floors** — a download that fails is
  availability, one that returns different bytes is correctness. NULL is "not
  applicable, never a failure", and no past run can be backfilled.
- **`--new-baseline`** (mutually exclusive with `--baseline-id`, exactly one
  required) evaluates only the single-run floors; every relative gate is
  **EMITTED** as NOT ASSESSED with the reason `new baseline` — emitted, not
  omitted, because a gate missing from the report reads as one nobody thought
  about. `deployment_gate_verdicts.baseline_deployment_id` is nullable for it,
  with a **partial unique index** WHERE it IS NULL, since Postgres treats NULLs
  as distinct and a re-run would otherwise accumulate rows.
- **Refuses** on mismatched `window_label`, mismatched derived-metric `source`
  (`capture` is exact, `buckets` approximate — comparing them measures the
  method), or window lengths differing >1.25x. **Warns** on activity coverage
  differing >1.3x: the per-GB numerators accumulate through idle time while the
  denominators do not. A window-*length* check cannot catch this.
- **Prefer a labelled phase window over `full-run`** — see the README; both
  guards can pass on a comparison that is still wrong.
- **Statistics**: reports mark `[p50]` (exact whole-run), `[p50~]` (window
  *estimated* from per-bucket percentiles, which do not compose like avg/min/max)
  or `[mean]`. **Comparing two different statistics is NOT ASSESSED, never
  silently blended.** Full-run comparisons read the rollup; windowed ones read
  buckets.
- **Node RSS compares a percentile across services** (default p95), not the fleet
  max — with ~1000 services the max is an extreme order statistic (one run moved
  max +64% while the median moved +3.8% and p95 +1.8%). The worst service is
  always in the detail.
- **The leak check is absolute** (a leaking baseline should not excuse the
  candidate) and uses the **median** service slope, not a tail: fitting a line
  through 2h of jittery RSS gives slopes symmetric about zero, so a p95 threshold
  would measure how noisy RSS is, not whether it leaks. The 5%/h threshold is
  calibrated against the **noise floor**, **not** against a known leak — a PASS
  means "no fleet-wide RSS trend".
- **Connection-error counts** come from `deployments record-log-counts`, which
  must run **before teardown** (ES is per-run scratch), so every pre-existing run
  reads NOT ASSESSED — never zero, which would show the candidate's counts
  against nothing. Its own table, NOT `deployment_derived_metrics`, because
  `compare` refuses when the two runs' `source` sets differ. The catalogue groups
  patterns and says how each combines — `Connection attempt failed` and
  `connect_happy_eyeballs failed` are one group taking the **max**, not the sum,
  being one failure logged at two layers. Phrases are **stable substrings**,
  never the paraphrase in an issue. An **empty index aborts** rather than
  recording zeros.

Thresholds live in `resources/gates/staging-thresholds.yaml` (git-versioned,
`version:` echoed into every report and stored on the verdict) so the quarterly
recalibration needs no tool release. Validation fails loudly — a threshold
silently defaulting to zero would be a rubber stamp printing PASS. `--history
<ids>` supplies prior **accepted** runs for the 2σ criterion; *selecting* them is
judgment and belongs to the operator, only the arithmetic is in the tool.

See `deploy/README.md` §"Staging gate comparison".

### Clock-skewed hosts must not vanish from Influx capture

**Every Flux `range()` in the query paths carries an explicit `stop`**
(`FLUX_FAR_FUTURE` in `influx.py`, also applied in `influx_canary.py`). Flux
defaults `stop` to `now()`, and `provisioning/clock_skew.py` jumps a VM's
**system** clock (nodes ±3h in a per-host random direction, clients by an
explicit signed duration), so a *forward*-skewed host's Telegraf writes every
point hours ahead of real time — and those points were silently discarded.

Shape-dependent, which is why it hid for so long: a long run lost only a
forward-skewed host's final skew's-worth of data, a short run lost the host
entirely. Found on DEV-03: a +3h node VM contributed **0 of 27** hosts to
`record-analysis` while holding 460 `memory_rss` points. The same defect applied
to the file-op, prod-upload and canary queries — all fixed together, since it is
one mechanism.

Skewed hosts are now captured, but their timestamps stay displaced, so a
sub-window re-derived from buckets covers a *different* stretch of their run.
`record-analysis` prints a **`Clock offset detected`** warning listing hosts
deviating from the fleet **median** `metrics_ended_at` by >15 min, with the
signed offset. Median, not local clock: no assumption about the invoking machine,
and a few skewed hosts cannot drag it. Negative offsets may instead mean a host
stopped reporting early — also worth knowing.

Regression guard: `test_clock_skewed_metrics.py` asserts no bare
`range(start: {start})` survives in either query module.

See `deploy/README.md` §"Clock-skewed hosts".

### Telegraf provisioning (folded into the provision passes)

The two Telegraf forwarders — Elasticsearch log forwarding
(`telegraf-ant-elasticsearch`) and InfluxDB host metrics
(`telegraf-ant-metrics`) — are installed **inside the existing provisioning
passes**, not as a separate step. In a `deployments up` run there is **no
standalone telegraf step**: the node VM list is walked **once**, not twice (the
old separate pass over a large node list was the deploy bottleneck).

- **`nodes provision` / `nodes provision-genesis` / `bootstraps provision`** take
  **`--with-telegraf`** (plus `--drop-log-levels` / `--buffer-limit` for the ES
  side). The command resolves whatever elastic/monitoring state the deployment
  has and folds the matching forwarders into its pyinfra pass — the first
  installs the deb, the rest reuse it. A backend with no state is skipped; if
  neither resolves it warns and installs nothing, **never failing node
  provisioning**. The orchestrator adds the flag to all three node-tier steps
  whenever elastic and/or monitoring is enabled.
- **Client host metrics** are installed by **`clients provision`** (best-effort,
  gated on `--monitoring-name`), alongside the client→ES log forwarder.

Because Telegraf is installed during provisioning, which runs before
`nodes start`, host metrics cover the node's lifetime from the first second.

The mechanism: each provisioner accepts `extra_op_providers` — objects exposing
`add_ops(state, install=...)` — and folds their ops into its state before
`run_ops`. `cmd/telegraf_fold.py` resolves the forwarders from state; the
per-forwarder logic lives in `provisioning/telegraf.py` (ES) and
`telegraf_metrics.py` (InfluxDB).

The standalone **`deployments provision-telegraf`** is **retained for retrofit /
manual re-runs** against an existing deployment — it is just no longer part of
`up`. Metrics-only when only monitoring state exists. The retrofit path
(`--inventory-path`/`--host-ips-file`) provisions Tier-1 host+procstat metrics
only; the Prometheus app-metrics scrape is wired only on the `--name` path with
`node_count > 0`. **Always pass `--only-new-logs`** on a retrofit, or the restart
re-ingests every on-disk log into ES.

See `deploy/README.md` §"`deployments provision-telegraf` command".

### Standalone Elasticsearch (`kind: elasticsearch`) — the beta-channel instance

A manifest kind whose *whole* deployment is one Elasticsearch instance — no
nodes, no clients, no binary. It exists so **community beta-channel node
operators**, on machines we do not run, have somewhere to forward logs; consumed
by the `beta-health-verdict` skill and the beta rollout watch. It reuses the
existing `elastic infra`/`provision`/`destroy` commands and the same state key;
the kind adds `--standalone` (which registers the `Deployment` row, since no
`bootstraps infra` runs to do it) and the public-ingest shape. Example:
`deploy/manifests/profiles/elasticsearch.yaml`.

- **`public_ingest: true` is a different posture, not a flag.** A testnet's ES is
  reachable only by our own VMs for hours, so it runs **plaintext on 9200 with
  anonymous access enabled** — unacceptable for an endpoint on the open internet.
  Public ingest binds ES to **loopback**, disables anonymous access, closes 9200,
  and fronts it with **Caddy on 443**. Off by default, so every existing stack
  renders **byte-identical** config.
- **The proxy allowlists writes** (`_bulk`/`_doc`/`_create`, POST/PUT) and 404s
  everything else, so an endpoint added by a future ES version is unreachable by
  default rather than newly exposed. It also forces a `filter_path`: ES otherwise
  answers a bulk request with a result object *per document*, making the response
  roughly the size of the request — pure egress on every batch, forever.
- **DigitalOcean is not a preference, it is the only option that satisfies
  "extendable disk".** DO's volume grows **online**; **OVH's is ForceNew** and
  would wipe the data; Contabo has no attachable volume; Vultr/Hetzner have no
  `*-elastic` module. Egress is a non-issue — ingest is **inbound** (free on DO),
  and the disk caps egress structurally at `volume_size / retention_days`.
- **The DNS record is the one manual step, and the deploy is two steps because of
  it.** The certificate is issued *during provisioning*, but the address does not
  exist until `elastic infra` has run: infra prints the **reserved IP** →
  operator sets the A record (**DNS only, grey cloud** — a proxied record
  terminates TLS at Cloudflare and issuance fails) → `up --resume` runs provision,
  which **preflights the record** and names the exact fix if it is wrong. Two
  journal entries mean the resume retries provisioning alone instead of
  recreating the VM. It is one-off: the **reserved IP survives a rebuild**.
- **Port 80 is never opened** — TLS-ALPN-01 on 443 issues the certificate, so
  there is no HTTP-01 challenge to serve and no cleartext listener.
- **Per-user credentials, not a shared key** (`elastic mint-key` / `list-keys` /
  `revoke-key`): each participant gets `create_doc` + `auto_configure` on
  `beta-nodes-*` and **no `read`**, so a leaked key can add logs and nothing
  else. A shared key would mean a leak re-keys *everyone* and nothing is
  attributable. The pre-existing `ant_log_writer` role is deliberately **not**
  reused (it grants `read` and `manage`). Keys **do not expire** by default —
  revocation is the real control — and an expiry **cannot be cleared** once set.
  The credential is shown **once**.
- **Log level is enforced server-side, because a participant's forwarder config
  is theirs to change.** `min_log_level` (default `info`) drops anything more
  verbose at ingest via an **`index.final_pipeline`**. That slot is load-bearing
  and verified against ES 8.17.4: a client passing **`?pipeline=_none` does NOT
  bypass `final_pipeline`**, but the same request **DOES bypass
  `default_pipeline`** entirely — so never use `default_pipeline` for anything an
  untrusted client must not opt out of. The proxy also strips a client-supplied
  `pipeline` param. WARN/ERROR are never dropped; a document with **no** `level`
  is **kept**. A dropped doc returns `200 / "noop"` with `errors: false` so
  forwarders do not retry — deliberate, since a `fail` processor would make them
  retry forever. The cost: drops are **invisible** and participants are not
  notified. `elastic set-log-level` changes it at runtime with no redeploy,
  restart or reindex. NB the **`reroute` processor is rejected in a
  final_pipeline**, so quarantining verbose logs to a cheap index only works in
  `default_pipeline`. This bounds **disk**, not bandwidth — the document already
  crossed the wire.
- **The ingest contract**: `POST https://<host>/_bulk`, `Authorization: ApiKey
  <encoded>` (a real ES API key, **not** a bearer token),
  `application/x-ndjson`, index `beta-nodes-YYYY.MM.DD`, time field
  **`@timestamp`**, body ≤ **50 MB**. **The action must be `create`; `index`
  returns a per-item 403**, since `create_doc` cannot overwrite and granting
  `index` would let one participant overwrite another's document by `_id`.
  **`_bulk` returns HTTP 200 even when items fail**, so a client MUST read
  `items[].status`: `201` indexed, `200` dropped by the level filter (expected),
  `403`/`429`/`400` real failures. The forced filter keeps `items.*.status`, and
  that part is load-bearing: `items.*.error` alone is smaller but ES **collapses
  the items array to only the failures**, destroying the position correlation, so
  a client could not tell which document a per-item 429 refers to and would have
  to retry the whole batch. Reads are refused.
- **One shared daily index, attribution by `beta_user` field** — not an index per
  user: ~100 users × daily × 14 days would exceed 1000 shards on one small node.
- `letsencrypt_staging: true` while iterating: production issuance allows only
  **5 duplicate certificates per week** and the lockout outlasts the debugging
  session that caused it.
- **`generate-inventory` covers this kind** (schema **6**). Two deliberately
  separated sections: **`ingest`** (the public write URL and rules) and
  **`read`** — which is **NOT** the public URL, since ES binds to loopback and
  the read key against the public host returns **404 at the proxy**,
  indistinguishable from a missing index. The read section gives
  `http://localhost:9200` plus a ready-made `ssh -L` command and says why. The
  **read-only key is included** (the file is meant to be shared); the **admin
  password is not**, because the provisioner sets the superuser and the
  log-writer from one secret.

The log-forwarder tooling that points community machines at this instance is a
**separate issue** — this kind stands up the endpoint and mints the credentials
only.

See `deploy/README.md` §"A standalone Elasticsearch instance".

## Elasticsearch (Log Forwarding)

Optional per-testnet Elasticsearch instance on a dedicated VM, enabled via
`enable_log_forwarding`. Telegraf forwards node JSON logs to it. Runs on
**DigitalOcean (default) or OVH** (`--provider ovh`, same env-var / SSH-key
prerequisites as monitoring).

Commands: `elastic infra` / `provision` / `info` / `destroy`, plus
`mint-key` / `list-keys` / `revoke-key` / `set-log-level` for the standalone
beta-channel kind (see above). `infra` takes `--droplet-size` (bump for large
testnets), `--volume-size`, `--volume-type` (ovh) and `--keep-on-destroy`;
`provision` takes `--retention-days` (default 7, ILM) and `--shards`. Node log
forwarding itself is installed by the provision passes — see "Telegraf
provisioning" above.

- **A kept index still expires on the ILM policy**, so raise `--retention-days`
  if the logs are needed for longer than a `keep_on_destroy` stack's lifetime.

Terraform manifests are `resources/{digitalocean,ovh}-elastic/`; the provisioner
is `provisioning/elastic.py`; state lives under its own S3 key prefix
`saorsa-deploy/elastic/{name}.json`. Security: user `ant-log-writer` (read/write)
plus a read-only API key for queries. Index pattern
`telegraf-ant-nodes-{testnet_lower}-%Y.%m.%d`.

See `deploy/README.md` §"Elasticsearch" for the full argument tables.

## Key Commands

- `uv run saorsa-deploy deployments plan --config deploy/manifests/profiles/reference.yaml` - Preview a manifest's command sequence
- `uv run saorsa-deploy deployments up --config <manifest>` / `deployments down --config <manifest>` - Deploy/destroy from a manifest
- `uv run flask --app saorsa_web.app run` - Dev server
- `cd db && uv run alembic revision --autogenerate -m "description"` - New migration
- `cd db && uv run alembic upgrade head` - Apply migrations
- `uv run ruff check . --fix` - Lint and fix
- `uv run pytest deploy/tests/` - Run deploy package tests
- `uv run scripts/bump_version.py <version>` - Bump version across all packages, commit, tag, and push
