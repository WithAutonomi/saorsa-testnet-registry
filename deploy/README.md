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
| `ANT_BUILD_AWS_ACCESS_KEY_ID` | AWS credentials for uploading custom-built binaries (only for `build ant-node`) |
| `ANT_BUILD_AWS_SECRET_ACCESS_KEY` | AWS credentials for uploading custom-built binaries (only for `build ant-node`) |

Terraform state and deployment metadata are stored in the `maidsafe-org-infra-tfstate` S3 bucket (region `eu-west-2`). AWS credentials are resolved via the standard boto3 credential chain (environment variables, `~/.aws/credentials`, or IAM roles).

The `ANT_BUILD_AWS_*` credentials are for the `ant-build-uploader` IAM user, which has `s3:PutObject` on the `ant-node-builds` bucket. Create the access key manually after applying the Terraform in `saorsa_deploy/resources/aws-build-infra/`.

## Declarative manifests (`deployments up` / `down` / `plan`)

The per-phase commands below are the primitives. For day-to-day work, prefer a
**declarative YAML manifest** plus the orchestrator, which computes the correct
ordered command sequence for you — replacing the hand-edited bash runbooks.

```bash
saorsa-deploy deployments plan          --config deploy/manifests/runs/STG-01.yaml   # preview the sequence (no execution)
saorsa-deploy deployments config show   --config deploy/manifests/runs/STG-01.yaml   # merged + validated manifest as YAML
saorsa-deploy deployments up            --config deploy/manifests/runs/STG-01.yaml   # run the full deploy sequence
saorsa-deploy deployments down          --name STG-01                                # destroy (teardown derived from state)
```

`down` is name/state-driven, so a manifest is optional: `down --name <deployment>`
tears down whatever exists in S3 state for that deployment (clients → nodes →
bootstraps → elastic → monitoring), while `down --config <manifest>` mirrors a
specific manifest's components. (prod-* kinds are single-command deployments —
tear them down with their own `prod <kind> destroy --name`, or `down --config`.)

Pass `--skip-drain-failures` to forward the same flag to the clients-destroy
step, so teardown proceeds even when draining a per-uploader wallet keeps
failing (e.g. a stuck nonce or too little ETH to cover the drain). The
abandoned dust is reported but no longer blocks the destroy. Without it, a
drain failure aborts `down` at step 1 so funds aren't silently lost.

Reusable profiles are committed under `deploy/manifests/profiles/`
(`reference.yaml` shows every option); copy-me starting points for the prod
kinds are committed under `deploy/manifests/examples/`. Your concrete run
manifests live in the git-ignored `deploy/manifests/runs/` — they pin
versions/branches and churn per run, so like the bash `runbooks/` they stay
local. (Profiles are deep-merge bases for `extends:`; examples are standalone
templates to copy into `runs/`.)

One manifest describes one deployment's full desired state, tagged by `kind`
(`testnet`, `testnet-extension`, `prod-uploaders`, `prod-canary`,
`prod-continuous-downloader`, `prod-downloaders`, `prod-local-uploads`,
`prod-local-downloads`). The orchestrator owns the ordering and flag-coupling
invariants (e.g. `bootstraps provision` only when bootstrap count > 1, `--testnet`
on all three node-provision commands, telegraf after `nodes start`, destroy in
reverse) so they can't be got wrong by hand. `up` executes each step by
re-entering the CLI in-process, so a failing step halts the run.

### Resumable runs (the step journal)

`up` records every completed step in a **journal** at
`s3://maidsafe-org-infra-tfstate/saorsa-deploy/journals/<name>.json`. When a
run fails partway, re-running `up` with the same manifest can skip the steps
that already succeeded — most valuably the 8–10 minute `build ant-node` /
`build ant` steps — and resume at the step that failed.

A journal hit **never silently skips**: journals can outlive their deployment
(a failed run torn down via the primitive destroy commands, or simply
abandoned) and names are reused, so resuming against stale history would fake
a deploy. Instead, when `up` finds a journal it prints what the journal
recorded (creation time, completed steps) and asks:

```
Journal found for DEV-01 (created 2026-06-04T14:02:11+00:00, 8/19 steps completed):
   1. ✓ Build ant-node from branch
   ...
   8. ✓ Bootstraps provision
[r]esume (skip completed steps) / [d]elete journal and run all steps / [a]bort?
```

If the journal doesn't match the freshly computed plan (manifest edited, name
reused with a different config, saorsa-deploy itself changed the step list), a
diff at the first divergence is shown and "resume" means *resume from the
longest matching prefix*. A manifest `kind` change invalidates the journal
outright.

For unattended runs (AI agents, the MCP server, cron) there is no prompt: pass
`--resume` to pre-answer it. With a journal present, no TTY, and no `--resume`,
`up` refuses with guidance rather than hanging — the operator (or agent)
re-runs with `--resume`, or a human runs `up` interactively to delete the
journal.

Lifecycle: a fully successful `up` deletes the journal, as does a successful
`down` (either path). Best-effort steps that fail-but-continue (e.g. the Slack
post) are journaled as done so a resume doesn't re-run them. `plan` and
`up --dry-run` never read or write the journal.

See [`manifests/README.md`](manifests/) for worked examples. Schema sketch
(`testnet` kind):

```yaml
name: STG-01
kind: testnet
purpose: "RC sanity check"
ssh_key_path: ~/.ssh/ant_deploy
slack_post: true                       # optional (any kind): append a final `deployments slack-post` step to `up`
extends: profiles/staging-rc.yaml      # optional: deep-merge a reusable base (lists REPLACE)
testnet:
  mode: true                            # --testnet
  upgrade_channel: beta                 # beta | stable | omit
  binaries:
    ant_node: { version: 0.11.5-rc.4 }  # XOR { branch: x, repo_owner: y, validation: z }
    ant:      { version: 0.2.5-rc.4 }   # required when clients are present
  bootstraps: { count: 7, port: 10000 }
  nodes:
    per_vm: 15
    nat_percent: 30
    attached_volume_size: 100
    providers:                          # vm_count is PER REGION; total = vm_count × regions
      digital_ocean: { vm_count: 5, regions_list: [ams3, lon1, nyc1], size: s-8vcpu-16gb }
      vultr:         { vm_count: 5, regions: 3, size: vhp-8c-16gb-intel }
      ovh:           { vm_count: 2, regions_list: [GRA9, UK1], size: b3-16 }  # needs OVH env vars (see `nodes infra`)
    log_level: debug         # optional: single ant-node --log-level for EVERY node-tier service (nodes+genesis+bootstraps); info|debug|trace (ant-node has no per-module logging)
  clients:                              # need ≥1 uploader AND ≥1 downloader
    uploaders:
      - { provider: vultr, region: syd, file_size_mb: 1000, vm_size: vc2-2c-4gb, payment: merkle, ant: 1000, eth: 0.1, forward_logs: true, rust_log: "ant_cli=info,saorsa_core::x=debug" }  # forward_logs needs elastic.enabled; rust_log overrides -v
    downloaders:
      - { provider: digital_ocean, region: tor1, bootstrap_cache: false }  # per-client ant peer cache (default true)
  elastic:    { enabled: true, droplet_size: s-8vcpu-16gb, volume_size: 500, shards: 8 }  # opt-in (default off); REQUIRED when any client sets forward_logs
  monitoring: { enabled: true }   # InfluxDB + Grafana — ON by default; set `enabled: false` to opt out
```

**Monitoring defaults ON** for testnet/extension kinds (the `monitoring:` block can be
omitted entirely). Client upload/download metrics need an InfluxDB to land in, and
`deployments print-uploads` / `record-results` read from it — so it's opt-out, not
opt-in. Elasticsearch log-forwarding stays opt-in.

**Bootstrap cache:** every client kind accepts `bootstrap_cache` (default **true**) —
per-entry on testnet `clients.uploaders[]` / `clients.downloaders[]` and
`prod_uploaders.uploaders[]`, spec-level on `prod_downloaders` /
`prod_continuous_downloader` / `prod_canary` / `prod_local_uploads` /
`prod_local_downloads`. When true the service invokes `ant` **without**
`--bootstrap`, which enables the client's peer cache; the peers come from a
`bootstrap_peers.toml` placed in ant's config dir (the testnet's bootstrap
addresses for testnet kinds, the production list for prod kinds; on the
operator's machine the file is only written when missing). `bootstrap_cache:
false` restores the legacy explicit `--bootstrap` invocation, which disables
the cache.

**Client log forwarding & RUST_LOG:** every VM-backed client kind also accepts
`forward_logs` and `rust_log` (testnet `clients.uploaders[]`/`downloaders[]` and
`prod_uploaders.uploaders[]` per-entry; `prod_downloaders` /
`prod_continuous_downloader` / `prod_canary` spec-level; **not** the local-* kinds).
`forward_logs: true` forwards the client `ant` process's **stdout + stderr** to
Elasticsearch as raw lines (a `message` field, tagged `role`/`stream`) in a
**separate** index `telegraf-ant-clients-<name>` so client logs never mingle with
node logs. For testnet/extension kinds it reuses the deployment's **own** ES
instance and therefore **requires `elastic.enabled: true`** (a validation error
otherwise). The prod kinds have no node-tier ES, so the deploy command stands up a
small **dedicated** ES instance for the client logs and tears it down on destroy
unless `--keep-elastic` (manifest `keep_elastic: true`) is given. `rust_log:
"<spec>"` sets `RUST_LOG` on the client service: the client `ant` binary reads
`RUST_LOG` via its tracing `EnvFilter` (so a full per-module filter like
`a::b=debug` works), but only once logging is enabled — the service therefore
still passes `-v` (which installs the subscriber) and exports `RUST_LOG`
alongside it, where `RUST_LOG` overrides the `-v` level. In a colon-delimited
CLI/spec form `rust_log` must be the **last** token because its value may contain
`:` (RUST_LOG module paths use `::`).

**Node-tier log level:** the testnet/extension `nodes` block accepts a
**`log_level`** (`info`/`debug`/`trace`) applied to **every** node-tier service —
the WAN nodes, the genesis node, *and* the bootstraps — as ant-node `--log-level`.
Unlike the client `ant`, **ant-node has no env-filter / per-module logging**: its
`--log-level` is bound to the `RUST_LOG` env var and accepts a *single* level
only (setting a multi-target `RUST_LOG` makes ant-node exit `INVALIDARGUMENT`).
So nodes get one global level (`debug` captures everything, including the modules
you might want), while the client side keeps the full `rust_log` env-filter —
letting a deployment run nodes and clients at different levels.

**Reuse:** `extends:` (deep-merge a base; lists replace wholesale), `--set
dotted.key=value` (override one value, e.g. `--set testnet.nodes.nat_percent=0`),
and `--name NEW`. **Secrets** are never in a manifest — prod kinds name an env var
(`secret_key_env:`, default `ARBITRUM_ONE_SECRET_KEY`) and `up` fails fast if it's
unset. The legacy bash runbooks and the primitive commands below remain fully
supported.

**Slack:** `slack_post: true` (top-level, valid for every kind) appends a final
`deployments slack-post --name <name>` step to `up`, posting a summary of the
deployment once it is up. If `ANT_DEPLOY_SLACK_BOT_TOKEN` (+ a channel via
`ANT_DEPLOY_SLACK_CHANNEL`) is set the post goes via the Slack Web API and its
message reference is recorded so a later `deployments record-results
--update-slack` can thread a results summary onto it; otherwise it falls back to
the `ANT_DEPLOY_SLACK_WEBHOOK_URL` incoming webhook (no threading). This step is
**best-effort**: if the post fails (no token/webhook, Slack rejects it, network
error), the run logs a warning and still exits 0 — the deployment itself is
already complete by then. `down` is unaffected.

## Usage

A deployment is built up in phases. Each phase has its own `infra-*` /
`destroy-*` command so partial-failure recovery is independent:

```
bootstraps infra   →  creates bootstrap VMs (Digital Ocean)
nodes infra        →  creates node VMs (DO / Hetzner / Vultr / OVH) — requires bootstraps
nodes provision-genesis  →  starts the genesis node on bootstrap #1
bootstraps provision (optional) — starts extra bootstrap nodes #2..N
nodes provision    →  installs ant-node binary + writes systemd units + enables services (does NOT start them)
nodes start        →  starts ant-node services across every node VM (parallel systemctl start)

nodes destroy      →  tears down node VMs, leaves bootstraps alone
bootstraps destroy →  tears down bootstraps (refuses while nodes are still up)
```

Run `saorsa-deploy <subcommand> --help` for the authoritative argument list
for each subcommand. The per-phase commands are:

### `bootstraps infra`

Create only the bootstrap VMs. These live on Digital Ocean (one per region,
geographically distributed) and are what the rest of the testnet peers with.

```bash
uv run saorsa-deploy bootstraps infra --name DEV-01 \
    --bootstrap-count 7 --attached-volume-size 50
```

Writes the deployment's state file to S3 with `bootstrap_nodes` populated
and `regions` empty. Re-running with the same count is a no-op
(idempotent); a different `--bootstrap-count` errors out — destroy the
existing bootstraps first.

To pin the exact regions instead of relying on the default ordering — useful
when a region is suffering availability issues — pass
`--do-regions-list` (comma-separated). The list length becomes the
bootstrap count, so `--bootstrap-count` is ignored. On rerun, the list
must match the existing bootstrap regions (same values in the same order)
or the command errors out.

```bash
uv run saorsa-deploy bootstraps infra --name DEV-01 \
    --do-regions-list lon1,nyc1,fra1 --attached-volume-size 50
```

### `nodes infra`

Create the node (main) VMs across one or more providers (DigitalOcean,
Hetzner, Vultr, OVH). Requires `bootstraps infra` to have run first (errors
out otherwise). Runs availability preflights against Hetzner (server-type
capacity) and Vultr (plan-region offering) before touching Terraform, and
records the resolved regions in deployment state **before** `terraform
apply` — so a partial failure still leaves something destroyable.

```bash
uv run saorsa-deploy nodes infra --name DEV-01 \
    --attached-volume-size 50 \
    --do-vm-count 10 --do-regions 3 --do-droplet-size s-8vcpu-16gb \
    --vultr-vm-count 5 --vultr-regions 3 --vultr-plan vhp-8c-16gb-intel \
    --hetzner-vm-count 2 --hetzner-regions 3 --hetzner-server-type cpx42 \
    --ovh-vm-count 2 --ovh-regions 3 --ovh-flavor b3-16
```

OVH (regions like `GRA9`, `UK1`, `DE1`; default flavor `b3-16`) reuses the
node Terraform module proven by the prod migration flow and has **no
availability preflight** (the OVH API doesn't pre-validate flavor/region
offerings). It needs `OVH_CLOUD_PROJECT_SERVICE` (the Public Cloud project
ID) plus the `OVH_APPLICATION_KEY` / `OVH_APPLICATION_SECRET` /
`OVH_CONSUMER_KEY` env vars and an `ant_deploy` SSH key registered in the
OVH project — the same prerequisites as `clients infra` for OVH. The env
vars are also required at `nodes destroy` time (Terraform re-reads the
module's data sources on destroy).

Each provider has a `--<provider>-regions-list` flag that pins exact regions
(comma-separated) instead of relying on the default top-of-list ordering.
Useful when a region has a transient provider quirk (e.g. Vultr's `dfw`
region often lacks block-storage clusters). When a list flag is set, the
corresponding `--<provider>-regions` count flag is ignored and no fallback
substitution happens — you get exactly the regions you asked for.

```bash
uv run saorsa-deploy nodes infra --name DEV-01 \
    --attached-volume-size 50 \
    --do-vm-count 10 --do-regions-list lon1,nyc1,ams3 --do-droplet-size s-8vcpu-16gb \
    --vultr-vm-count 5 --vultr-regions-list ewr,ord,lax --vultr-plan vhp-8c-16gb-intel
```

### `nodes provision-genesis` / `bootstraps provision` / `nodes provision` / `nodes start`

Start ant-node services. `nodes provision-genesis` runs the genesis node on
bootstrap #1 (it's the one peer everyone dials). `bootstraps provision`
starts any additional bootstraps (#2..N). `nodes provision` installs the
binary, writes systemd unit files, and **enables** the services on every
main-VM. `nodes start` then brings the services up — the split exists so
that optional setup steps (e.g. NAT simulation) can run between unit-write
and service-start.

```bash
uv run saorsa-deploy nodes provision-genesis --name DEV-01 --port 12000
uv run saorsa-deploy bootstraps provision --name DEV-01 --port 12000   # if --bootstrap-count > 1
uv run saorsa-deploy nodes provision --name DEV-01 --node-count 25
uv run saorsa-deploy nodes start --name DEV-01
```

#### Port-restricted NAT simulation

`nodes provision` can run a configurable percentage of VMs behind a
simulated port-restricted NAT (Linux network namespace + iptables DNAT /
MASQUERADE). Useful for modelling real-world reachability constraints.

```bash
uv run saorsa-deploy nodes provision --name DEV-01 --node-count 25 --nat-percent 20
uv run saorsa-deploy nodes start --name DEV-01
```

Selection is deterministic ("one per region first, then round-robin") and
is recorded in deployment state on first run. Subsequent `nodes provision`
runs honour the locked selection — `--nat-percent` is ignored unless you
pass `--force-nat-reselect`, which recomputes the set and can flip VMs
between NAT and non-NAT.

On a NAT VM you can verify the rules are in place:

```bash
ssh root@<VM> "iptables -t nat -L PREROUTING -n | grep -c DNAT"   # expect 2
ssh root@<VM> "ip netns list"                                      # expect 'ant_nat'
```

#### Clock skew (testing clock-independence)

`nodes provision` can inject deliberate clock skew into a subset of node VMs.
On a selected host, provisioning disables the OS time-sync service
(`systemd-timesyncd` / chrony / ntp) and jumps the system clock by a fixed
magnitude (a few hours) in a per-host direction (some VMs run fast, some slow).
This exists to verify the network no longer depends on client/node clocks being
on UTC.

```bash
uv run saorsa-deploy nodes provision --name DEV-01 --node-count 25 --clock-skew-count 2
```

`--clock-skew-count N` skews `N` VMs, chosen with the same deterministic,
region-distributed strategy as `--nat-percent` and recorded in deployment state
on first run. Subsequent runs honour the locked selection — `--clock-skew-count`
is ignored unless you pass `--force-clock-skew-reselect`. The clock change is
idempotent (guarded by a `/etc/ant-clock-skew-applied` marker), so re-running
`nodes provision` never compounds the offset. The skew persists until the VM is
destroyed.

Verify on a skewed VM:

```bash
ssh root@<VM> "cat /etc/ant-clock-skew-applied"   # e.g. '+3h'
ssh root@<VM> "timedatectl"                        # NTP synchronized: no
```

The same mechanism is available per-VM on clients, but with an **explicit signed
magnitude** instead of the node-side fixed-3h/random-direction behaviour — see
the `clock_skew=DURATION` spec token under "Deploying clients" and the prod
client commands below.

#### Storage-full simulation

`nodes provision` can make a configurable percentage of VMs behave as
storage-full by pre-filling their data volume. On a selected VM the provisioner
writes a single filler file (`/mnt/ant-node-storage/.ant_full_filler`, created
with `fallocate`, `dd` fallback) sized to consume all free space minus a small
headroom, so every ant-node sharing that volume sees no space and rejects new
records. Useful for modelling a network with full nodes.

```bash
uv run saorsa-deploy nodes provision --name DEV-01 --node-count 25 --full-percent 20
uv run saorsa-deploy nodes start --name DEV-01
```

`--full-percent` is taken as a percentage of the **non-NAT** VMs — the selection
is always disjoint from `--nat-percent`, so a NAT VM is never also filled.
Selection is **uniform random** (seeded by deployment name), *not*
region-distributed like `--nat-percent`: a chunk PUT only exercises a full node
when that node is among the closest nodes to the chunk's keyspace address, so
full nodes must be scattered rather than clustered. The draw is recorded in
deployment state on first run; subsequent runs honour the locked selection
unless you pass `--force-full-reselect`. `--full-headroom-mb` (default 100)
tunes how much free space to leave. The fill is idempotent (guarded by the
filler file existing), so re-running `nodes provision` never double-fills.

Verify on a full VM:

```bash
ssh root@<VM> "df -h /mnt/ant-node-storage"        # Avail near zero
ssh root@<VM> "ls -lh /mnt/ant-node-storage/.ant_full_filler"
```

#### Binary version selection

By default provisioning uses the latest GitHub release. Override with:

```bash
# Specific release
uv run saorsa-deploy nodes provision-genesis --name DEV-01 --port 12000 --node-version 0.2.0

# Custom branch build (see build ant-node)
uv run saorsa-deploy nodes provision --name DEV-01 --node-count 25 \
    --branch-name feature-x --repo-owner myorg
```

`--node-version` and `--branch-name`/`--repo-owner` are mutually exclusive.
`--branch-name` and `--repo-owner` must be used together.

### `nodes destroy` / `bootstraps destroy`

Tear down in reverse phase order: nodes first, bootstraps second.

```bash
uv run saorsa-deploy nodes destroy --name DEV-01 --force
uv run saorsa-deploy bootstraps destroy --name DEV-01 --force
```

`bootstraps destroy` refuses while node state is still populated, and
also refuses while any _extension_ testnet is sharing these bootstraps
(see `deployments extend` below). Both commands have a fallback path that scans S3
for orphan tfstate files when the deployment state is missing, so
interrupted runs can still be cleaned up.

### `deployments extend` / `deployments unextend`

Deploy a second testnet that reuses an existing testnet's bootstrap VMs.
Typical use case: binary-compatibility testing — run DEV-02 with a
different `ant-node` binary alongside DEV-01, peering with the same
bootstrap layer.

```bash
# Seed DEV-02's deployment state from DEV-01's bootstraps
uv run saorsa-deploy deployments extend --name DEV-02 --source DEV-01

# From here on, DEV-02 is a normal deployment — run the usual pipeline,
# skipping the bootstrap/genesis stages:
uv run saorsa-deploy nodes infra --name DEV-02 --do-vm-count 5 --do-regions 2
uv run saorsa-deploy nodes provision --name DEV-02 \
    --branch-name feature-x --repo-owner myorg
uv run saorsa-deploy nodes start --name DEV-02

# Teardown — note: no bootstraps destroy for an extension
uv run saorsa-deploy nodes destroy --name DEV-02 --force
uv run saorsa-deploy deployments unextend --name DEV-02 --force
```

`deployments extend` copies `bootstrap_nodes`, `bootstrap_ip`, and `bootstrap_port`
from the source's state into the new testnet's state, and records
`shared_bootstrap_from: <source>`. It's idempotent: re-running with the
same source refreshes the bootstrap fields (useful if the source was
re-provisioned). Re-running with a different source requires `--force`.

`deployments unextend` deletes the extension's deployment state. It refuses if the
extension still has nodes (run `nodes destroy` first) and it never
touches the source's bootstrap VMs.

`bootstraps destroy` will refuse to destroy a testnet's bootstraps while
any extension references them — `deployments unextend` each dependent first.
`--force` does not override this check.

### `deployments slack-post`

Post a formatted summary of a deployment to Slack. There are two posting paths:

- **Web API (preferred for threading).** When `ANT_DEPLOY_SLACK_BOT_TOKEN` is
  set, the command posts via `chat.postMessage` and records the message's
  `(channel, ts)` on the deployment row (`slack_channel` / `slack_message_ts`).
  That reference is what `deployments record-results --update-slack` later
  needs to post a results summary as a **threaded reply**. The channel comes
  from `--channel` or `ANT_DEPLOY_SLACK_CHANNEL` (a channel ID or name); the bot
  must be a member of it and have `chat:write`.
- **Incoming webhook (legacy fallback).** When no bot token is set, the command
  posts to `ANT_DEPLOY_SLACK_WEBHOOK_URL`. Incoming webhooks return no message
  reference, so **threaded result updates are unavailable** on this path.

```bash
# Web API path — enables threaded result updates:
export ANT_DEPLOY_SLACK_BOT_TOKEN='xoxb-...'
export ANT_DEPLOY_SLACK_CHANNEL='C0123456789'   # or pass --channel
uv run saorsa-deploy deployments slack-post --name DEV-01

# Legacy webhook path (no threading):
export ANT_DEPLOY_SLACK_WEBHOOK_URL='https://hooks.slack.com/services/T.../B.../...'
uv run saorsa-deploy deployments slack-post --name DEV-01

# Preview the payload without posting (neither var required):
uv run saorsa-deploy deployments slack-post --name DEV-01 --dry-run
```

The post includes the deployment name, kind, purpose, status, total node
count (= `vm_count` × `nodes_per_vm`), NAT-simulated node count, the
provider/region distribution, and any uploader/downloader client VMs
recorded for the deployment. For `prod-uploaders` and
`prod-continuous-downloader` deployments the testnet-specific fields are
replaced with the relevant prod-side details.

Purpose is captured at deploy time, not at post time: pass `--purpose
"<text>"` to `bootstraps infra`, `deployments extend`, `prod
continuous-downloader deploy`, or `prod uploaders deploy`. The Slack
post renders `_(none recorded)_` if a deployment was created without the
flag.

#### `deployments record-results --update-slack`

After recording results, `--update-slack` posts a per-kind results summary as a
threaded reply to the original `slack-post` message. It requires that the
original post was made on the Web API path (so its `(channel, ts)` was captured)
and that `ANT_DEPLOY_SLACK_BOT_TOKEN` is still set. The reply content is
kind-dispatched: per-host upload/download aggregates for testnet and downloader
kinds; per-host plus per-file detail for `prod-uploaders`; an
alive/pending/confirmed-lost summary with an hours-to-loss histogram for
`prod-canary`. The reply is **best-effort** — a missing thread reference,
missing token, or Slack error warns but never fails the recording.

```bash
uv run saorsa-deploy deployments record-results --name PROD-UP-01 --update-slack
```

### Client VMs (uploaders / downloaders)

Client VMs run the ant binary as long-running upload-or-download workers against a testnet. Each VM runs *either* an uploader *or* a downloader — not both — and the downloader VMs pull addresses produced by uploaders through a per-testnet S3 folder.

Commands:

- `clients infra --name <testnet> --uploader provider:region:file_size_mb[:vm_size][:payment=MODE][:ant=N][:eth=N][:clock_skew=DURATION][:bootstrap_cache=BOOL][:forward_logs=BOOL][:rust_log=SPEC] [--uploader ...] --downloader provider:region[:vm_size][:clock_skew=DURATION][:bootstrap_cache=BOOL][:forward_logs=BOOL][:rust_log=SPEC] [--downloader ...]` — create the VMs. The optional `forward_logs=true` key forwards that VM's `ant` stdout+stderr to the deployment's Elasticsearch (index `telegraf-ant-clients-<name>`; needs `elastic provision`); `rust_log=SPEC` sets `RUST_LOG` on the service (overriding `-v`) and **must be the last token** in the spec (its value may contain `:`). Each flag is repeatable; at least one of either is required. `provider` is **mandatory** and must be one of `digital_ocean`, `vultr`, `hetzner`, `ovh` — different uploaders/downloaders may target different providers (e.g. cheap-egress providers for the upload-heavy VMs). **Region names and VM sizes vary per provider and are passed through verbatim** — supply valid strings for the chosen provider (DO `s-2vcpu-4gb` / Vultr `vc2-2c-4gb` / Hetzner `cpx22` / OVH `b3-8`, regions like DO `lon1` / Vultr `lhr` / Hetzner `fsn1` / OVH `GRA9`); there is no per-provider validation. The optional `ant=` / `eth=` keys set per-uploader funding amounts (defaults: 100 ANT, 0.5 ETH). The optional `payment=` key sets the storage-payment mode (`merkle` or `no-merkle`). The optional `clock_skew=DURATION` key makes `clients provision` disable time sync and shift that VM's clock by a **signed Go-style duration** (e.g. `2h`, `-2h`, `30m`; a leading `-` runs the clock slow) — for testing the network's clock-independence; idempotent via a `/etc/ant-clock-skew-applied` marker. The optional `bootstrap_cache=true|false` key (default **true**) controls the ant peer cache per VM: when true the service invokes `ant` **without** `--bootstrap` (the cache is enabled; peers come from a `bootstrap_peers.toml` written to `/root/.config/ant/` by `clients provision`, containing the testnet's bootstrap addresses), and `bootstrap_cache=false` restores the legacy explicit `--bootstrap` invocation, which disables the cache. OVH requires `OVH_CLOUD_PROJECT_SERVICE` (the Public Cloud project ID) plus the `OVH_APPLICATION_KEY` / `OVH_APPLICATION_SECRET` / `OVH_CONSUMER_KEY` env vars; Vultr requires `VULTR_API_KEY`; Hetzner requires `HCLOUD_TOKEN`; DigitalOcean requires `DO_TOKEN`.
- `clients provision` — install the ant binary, `awscli`, and the relevant role's systemd unit (only one of `ant-uploader` / `ant-downloader` per VM). For each uploader, generates a fresh Arbitrum Sepolia EOA wallet, transfers the configured ANT + ETH from the funding wallet (`--wallet-secret-key` / `ANT_DEPLOY_SECRET_KEY`), persists wallet info in S3 client state, and writes the per-uploader secret key into `/etc/ant-client.env`. Idempotent: re-runs reuse wallets already recorded in client state. Skipped under `--prod`.
- `clients start` / `clients stop` — start or stop the appropriate service per-VM based on role.
- `clients destroy` — drains every per-uploader wallet (ANT + ETH back to the funding wallet on Arbitrum Sepolia, less a small ETH gas reserve), then tears down VMs and wipes the per-testnet addresses folder at `s3://saorsa-testnet-addresses/saorsa-deploy/addresses/<testnet>/`. Drain failures abort the destroy so VMs and state remain available for retry — pass `--wallet-secret-key` (or set `ANT_DEPLOY_SECRET_KEY`) so funds know where to return.
- `clients clean` — remove services + artefacts without destroying the VMs; idempotent and role-tolerant.

Uploader flow (`uploader.sh`): uploads a random file via `ant`, appends the returned address to a local tail buffer capped at `ADDRESSES_TAIL_SIZE` (default 10k), and flushes the buffer to `upload-<region>-<client_index>.txt` under the testnet's addresses prefix every `S3_FLUSH_EVERY_N` addresses or `S3_FLUSH_INTERVAL_SECS` seconds, whichever comes first. The flush goes through a **presigned PUT URL** minted by `clients provision` (SigV4 max expiry: 7 days — re-provision to refresh on longer-lived testnets), so it keeps working on clock-skewed VMs whose own SigV4-signed requests S3 rejects (~15min tolerance); when no URL is provisioned it falls back to the signed `aws s3 cp` path.

Downloader flow (`downloader.sh`): lists the testnet's addresses prefix, picks a random uploader file, pulls it, shuffles one address, and runs `ant file download` against it. List + fetch use **unsigned HTTPS** against the public `saorsa-testnet-addresses` bucket (no SigV4), so clock-skewed downloaders are not starved of addresses.

#### Heap profiling

Pass `--heaptrack uploader` and/or `--heaptrack downloader` to `clients provision` to wrap every `ant` invocation with [`heaptrack`](https://github.com/KDE/heaptrack). The flag is repeatable. When set, the provisioner installs the `heaptrack` apt package on matching VMs and threads `HEAPTRACK=1` through `/etc/ant-client.env`, which causes the bash wrapper to prepend `heaptrack -o /var/log/ant-heaptrack/upload-<seq>` (or `download-<seq>`) to each `ant` call. The choice is persisted in `deploy_args.heaptrack` so the run is self-describing.

Profiles stay on the client VM — fetch them with `scp` before destroying the VMs:

```bash
scp -i ~/.ssh/ant_deploy "root@<client-ip>:/var/log/ant-heaptrack/*.gz" ./
heaptrack_gui ./upload-9.gz   # open in heaptrack_gui locally
```

Heaptrack adds significant runtime overhead (typically 2-5x slower) and the `.gz` files grow with allocation count — only enable it on the VMs you actually want to profile.

#### AWS Client S3 Setup

Client VMs need scoped AWS credentials for the shared addresses folder. The IAM user + policy + access key are managed by a small Terraform module:

```bash
cd deploy/saorsa_deploy/resources/aws-client-s3
terraform init
terraform apply
terraform output -raw access_key_id            # -> ANT_CLIENT_AWS_ACCESS_KEY_ID
terraform output -raw secret_access_key        # -> ANT_CLIENT_AWS_SECRET_ACCESS_KEY
```

Copy both into your `.env`. The module also owns the `saorsa-testnet-addresses` bucket itself: a dedicated **public-read** bucket (bucket policy grants anonymous `GetObject` + `ListBucket`; ACLs stay blocked) holding only testnet address lists. The IAM user has `s3:ListBucket` on it (scoped to the `saorsa-deploy/addresses/*` prefix) plus `GetObject`/`PutObject`/`DeleteObject` on objects under that prefix — nothing else. The bucket is deliberately separate from the tfstate bucket, which keeps full Block Public Access intact.

### `build ant-node` command

Build ant-node from a Git branch on an ephemeral DO droplet and upload the binary to S3.

```bash
uv run saorsa-deploy build ant-node --branch-name feature-x --repo-owner myorg
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--branch-name` | string | Yes | - | Git branch to build from |
| `--repo-owner` | string | Yes | - | GitHub repository owner |
| `--ssh-key-path` | string | No | `~/.ssh/id_rsa` | SSH key for provisioning the build VM |
| `--musl` | flag | No | off | Build against the static `x86_64-unknown-linux-musl` target |
| `--debug` | flag | No | off | Build **without** `--release` — a larger, slower binary with full debug info, intended for memory profiling. The artifact overwrites the deployment's S3 build key as usual (the key is namespaced by `--name`, and `up` rebuilds when the manifest toggles `debug`), so no separate fetch flag is needed. The same flag exists on `build ant`. Manifests set it per binary via `binaries.<ant|ant_node>.debug: true`. |

This command:
1. Creates a `c-16` (16 vCPU, 32GB RAM) DO droplet in lon1
2. Installs Rust, clones the repo, builds in release mode (or debug mode with `--debug`)
3. Uploads the binary to `s3://ant-node-builds/builds/{name}/{owner}/{branch}/ant-node`
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

### `elastic provision-telegraf` command

Install and configure Telegraf for Elasticsearch log forwarding on all testnet VMs. Formerly named `provision-telegraf`.

```bash
uv run saorsa-deploy elastic provision-telegraf --name DEV-01 \
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

Optional per-testnet monitoring stack: InfluxDB 2.x and Grafana on **two
separate VMs**, with Telegraf forwarding host-level metrics (CPU, memory, disk,
network) from testnet nodes. Grafana runs on its own (small) VM so its panel
rendering does not compete with InfluxDB ingest/query for CPU and RAM — the
cause of large dashboards (e.g. the per-node RSS panels) failing to render when
both shared one box. The InfluxDB VM carries the data volume; Grafana reaches
it over the public `:8086` endpoint.

The monitoring stack is standalone — it can be used by deployments from this
repository or external setups, and runs on **DigitalOcean (default) or OVH**.
OVH is markedly cheaper for compute, storage, and (unmetered) bandwidth; it
needs `OVH_CLOUD_PROJECT_SERVICE` plus the `OVH_APPLICATION_KEY` /
`OVH_APPLICATION_SECRET` / `OVH_CONSUMER_KEY` / `OVH_ENDPOINT` credentials and
an `ant_deploy` SSH key registered in the OVH project (same prerequisites as
`clients infra` for OVH).

### `monitoring infra` command

Create the InfluxDB VM (with attached storage) and the Grafana VM using
Terraform.

```bash
# DigitalOcean (default)
uv run saorsa-deploy monitoring infra --name DEV-01

# OVH
uv run saorsa-deploy monitoring infra --name PROD-MON --provider ovh \
  --region GRA9 --droplet-size b3-8 --volume-size 200
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--force` | flag | No | - | Delete stale Terraform state and proceed |
| `--name` | string | Yes | - | Monitoring stack name |
| `--provider` | string | No | `digitalocean` | `digitalocean` or `ovh` |
| `--region` | string | No | `lon1` (do) / `GRA9` (ovh) | Region for the monitoring VMs |
| `--volume-size` | int | No | 50 | InfluxDB data volume size in GB |
| `--volume-type` | string | No | `high-speed` | OVH block storage type (ignored for digitalocean) |
| `--droplet-size` | string | No | `s-4vcpu-8gb` (do) / `b3-8` (ovh) | InfluxDB VM size; bump for production fleets with many metric-forwarding VMs |
| `--grafana-size` | string | No | `s-1vcpu-2gb` (do) / `d2-4` (ovh) | Grafana VM size (it only renders panels, so it stays small) |

### `monitoring provision` command

Install and configure InfluxDB 2.x on the InfluxDB VM and Grafana on the
Grafana VM (pointing Grafana's datasource at the InfluxDB VM).

```bash
uv run saorsa-deploy monitoring provision --name DEV-01

# Production: cap retention so the data volume doesn't grow without bound
uv run saorsa-deploy monitoring provision --name PROD-MON --retention-days 14
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--name` | string | Yes | - | Monitoring stack name (must match `monitoring infra`) |
| `--ssh-key-path` | string | No | `~/.ssh/id_rsa` | SSH key for provisioning |
| `--retention-days` | int | No | 0 | InfluxDB bucket retention in days (`0` = keep forever, the dev default). Applied to both buckets at first provision |

On completion, prints connection details including InfluxDB URL, API token, Grafana URL, and a ready-to-paste `monitoring provision-telegraf` command.

This step creates **two** InfluxDB buckets — `testnet-metrics` (node-tier host
metrics, the `prometheus` per-node measurement, and application measurements
like `download` / `prod_upload` / `prod_canary_*`) and `client-metrics`
(client-tier host metrics only) — and mints an org-scoped Telegraf token with
read+write on both. Splitting host metrics across two buckets keeps the **Node
Host Statistics** and **Client Host Statistics** Grafana dashboards physically
isolated: each dashboard queries only its own bucket, so neither can render the
other's data.

### `monitoring provision-telegraf` command

Install Telegraf for InfluxDB metrics forwarding on testnet VMs. Collects host-level metrics: CPU, memory, disk space (all mount points), and network bandwidth.

```bash
# Using deployment state (loads IPs from S3)
uv run saorsa-deploy monitoring provision-telegraf --name DEV-01 \
  --influxdb-url http://1.2.3.4:8086 \
  --influxdb-token TOKEN

# Using explicit host IPs (for external setups)
uv run saorsa-deploy monitoring provision-telegraf \
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
| `--influxdb-bucket` | string | No | `testnet-metrics` | InfluxDB bucket for **node** host metrics (CLIENT hosts always use `client-metrics`) |
| `--ssh-key-path` | string | No | `~/.ssh/id_rsa` | SSH key for provisioning |

Either `--name` or `--host-ips` must be provided. The `--host-ips` flag allows provisioning machines from external testnet setups without any deployment state in this repository.

When `--name` is provided the command also enumerates client VMs from client state (if any exist) and provisions host-stat telegraf on them with `host_role="CLIENT"`. CLIENT-role hosts are routed to the dedicated **`client-metrics`** bucket (overriding `--influxdb-bucket`, which only governs node-tier hosts), so client host metrics surface on the **Client Host Statistics** dashboard while node metrics stay on **Node Host Statistics** — the two dashboards read from separate buckets and never cross-contaminate.

CLIENT-role hosts additionally get the client `ant` service's **memory** recorded into `client-metrics`: a `procstat` input matching `system.slice/ant-*.service` (per-process RSS, tagged `metric_source=ant-client`, 2 s interval) and a `cgroup` input reading `memory.current` + `memory.peak` (whole-service live + high-water peak) for the same glob. These stanzas (`resources/monitoring/telegraf_metrics_client.conf`) are appended **only** on CLIENT hosts, so node configs are byte-identical and the cgroup input never logs a missing-path error on the node fleet. The single `ant-*` glob matches whichever client unit a VM runs (`ant-uploader`, `ant-downloader`, `ant-prod-uploader`, `ant-prod-downloader`, `ant-prod-canary-upload`, `ant-prod-canary-probe`). Query `procstat` filtered to `process_name == "ant"` for per-process RSS, or the `cgroup` measurement's `memory.peak` for the true per-run high-water mark (caught even when a sub-2 s terminal spike clips the sampled RSS). The local-* prod kinds run no systemd unit and are not covered.

### `monitoring info` command

Print monitoring stack connection details from S3 state. Useful for retrieving credentials after provisioning, or for getting a ready-to-use Telegraf config for external setups.

```bash
# Print connection summary
uv run saorsa-deploy monitoring info --name DEV-01

# Emit a complete Telegraf TOML config to stdout
uv run saorsa-deploy monitoring info --name DEV-01 --telegraf-config > telegraf.conf
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--name` | string | Yes | - | Monitoring stack name |
| `--telegraf-config` | flag | No | - | Emit complete Telegraf TOML config to stdout |

### `monitoring destroy` command

Tear down monitoring infrastructure.

```bash
uv run saorsa-deploy monitoring destroy --name DEV-01
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--force` | flag | No | - | Skip confirmation prompt |
| `--name` | string | Yes | - | Monitoring stack name to destroy |

### Monitoring workflow example

```bash
# 1. Create monitoring VM
uv run saorsa-deploy monitoring infra --name DEV-01

# 2. Install InfluxDB + Grafana (prints connection details)
uv run saorsa-deploy monitoring provision --name DEV-01

# 3. Install Telegraf on testnet nodes
uv run saorsa-deploy monitoring provision-telegraf --name DEV-01 \
  --influxdb-url http://1.2.3.4:8086 \
  --influxdb-token TOKEN

# 4. View Grafana dashboards at http://1.2.3.4:3000

# 5. Get connection details later
uv run saorsa-deploy monitoring info --name DEV-01

# 6. Get a portable Telegraf config for external use
uv run saorsa-deploy monitoring info --name DEV-01 --telegraf-config

# 7. Tear down when done
uv run saorsa-deploy monitoring destroy --name DEV-01
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

The `prod bootstrap deploy` command deploys 7 hardened bootstrap nodes across 3 cloud providers for the production Autonomi network.

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

#### `prod bootstrap deploy`

Deploy the full production bootstrap network.

```bash
uv run saorsa-deploy prod bootstrap deploy
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

#### `prod bootstrap destroy`

Tear down all production infrastructure. Static IPs are preserved.

```bash
uv run saorsa-deploy prod bootstrap destroy
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--name` | string | `ant-production` | Deployment name |
| `--ssh-key-path` | string | `~/.ssh/ant-production` | Path to SSH private key |
| `--force` | flag | - | Skip confirmation prompt |

### Running continuous downloaders against production (`prod continuous-downloader`)

`prod continuous-downloader deploy` stands up a single client VM running a dedicated `prod_downloader.py` Python service against the canonical Autonomi production network, seeded with a static list of public file addresses. An InfluxDB + Grafana monitoring stack is created as part of the same command, and the existing `deployments record-results` workflow can be used to upsert download aggregates into PostgreSQL.

The service writes a per-op stdout/stderr log under `/var/log/ant-prod-downloader/{idx:04d}-{addr_prefix}.{stdout,stderr}` for **every** download — successes too, so slow-but-successful downloads can be analysed for performance after the fact. Testnet downloaders deliberately stay on the legacy `downloader.sh` and keep no per-op logs (testnet runs want maximum pressure on the network, not diagnostic baggage).

Unlike `prod bootstrap deploy`, this command does **not** provision any bootstrap/genesis/node infrastructure — it fetches the live peers list from [WithAutonomi/ant-client `resources/bootstrap_peers.toml`](https://raw.githubusercontent.com/WithAutonomi/ant-client/refs/heads/main/resources/bootstrap_peers.toml) at runtime and points the downloader service at those peers via `--bootstrap ip:port`.

```bash
uv run saorsa-deploy prod continuous-downloader deploy \
  --name PROD-DL-01 \
  --spec lon1 \
  --purpose "Soak-test 0.3.1 downloads from London" \
  --addresses-path my-addresses.txt   # optional, defaults to resources/prod_addresses.txt
```

Orchestrated sequence (each step is skipped when its state already exists, so the command is idempotent under reruns):

1. `monitoring infra` + `monitoring provision` for an InfluxDB VM in `lon1` named identically to `--name`.
2. `clients infra` for the single `--spec` (passed internally as one `--downloader`; no uploaders — prod mode is download-only).
3. The prod-specific provisioner (`provisioning/prod_downloader.py`) runs against the VM:
   - Fetches the prod peers list and writes `BOOTSTRAP_ARGS` against them.
   - Writes the address list to `/var/lib/ant-prod-downloader/addresses.txt`.
   - Deploys `prod_downloader.py` to `/root/ant-prod-downloader/` and writes the `ant-prod-downloader.service` systemd unit.
4. The orchestrator SSHes in and `systemctl enable --now ant-prod-downloader` to launch the service.

Arguments (for `prod continuous-downloader deploy`):

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--name` | string | — | Deployment name; also used for the paired monitoring stack |
| `--spec` | string | — | Downloader VM `region[:vm_size]` (single VM) |
| `--clock-skew` | `DURATION` | off | Disable time sync and shift the downloader VM's clock by a signed Go-style duration (e.g. `2h`, `-2h`, `30m`) — for testing clock-independence |
| `--bootstrap-cache` / `--no-bootstrap-cache` | flag | on | Ant peer cache mode. On (default): `ant` runs **without** `--bootstrap` and reads a `bootstrap_peers.toml` (production list) written to `/root/.config/ant/` by the provisioner — the cache is enabled. Off: legacy explicit `--bootstrap` invocation (cache disabled). |
| `--purpose` | string | — | Free-text description; persisted into `deploy_args` and shown by `deployments slack-post` |
| `--addresses-path` | path | shipped default | Local file of newline-separated addresses |
| `--sleep-interval-secs` | int | `900` | Seconds to sleep at the **end of each full pass** through the address list before starting the next cycle. `0` disables the sleep (back-to-back cycling). Persisted in `deploy_args.sleep_interval_secs`. |
| `--branch-name` / `--repo-owner` | string | — | Optional custom ant build |
| `--ant-version` | string | — | Optional explicit release version |
| `--monitoring-volume-size` | int | `50` | Monitoring VM attached volume size (GB) |
| `--ssh-key-path` | string | `~/.ssh/id_rsa` | SSH key |

The monitoring VM always uses region `lon1`.

After the command reports success, record download aggregates into Postgres on demand:

```bash
uv run saorsa-deploy deployments record-results \
  --name PROD-DL-01 --monitoring-name PROD-DL-01 --window -1h
```

The command registers a row in the `deployments` table with `deploy_args.kind = "prod-continuous-downloader"` so the web UI and API can distinguish these from testnet deployments.

To tear down:

```bash
uv run saorsa-deploy prod continuous-downloader destroy --name PROD-DL-01
# add --keep-monitoring to preserve the InfluxDB VM so historical metrics remain queryable
```

By default the monitoring VM is destroyed alongside the client. Pass `--keep-monitoring` when you want to keep the InfluxDB + Grafana stack running for after-the-fact analysis.

### Running one-shot downloaders against production (`prod downloaders`)

`prod downloaders deploy` is the one-shot sibling of `prod continuous-downloader`. The service walks the address list a **single time** and then exits cleanly; the systemd unit is written with `Restart=no` so the VM can be left alone until destroy. Same provisioner, same `prod_downloader.py` script (toggled by the `ONE_SHOT=true` env var), same monitoring stack, same `record-results` / `slack-post` integrations — the only operator-visible difference is the missing `--sleep-interval-secs` flag.

```bash
uv run saorsa-deploy prod downloaders deploy \
  --name PROD-DL-OS-01 \
  --spec lon1 \
  --purpose "One-pass benchmark of the v0.3.1 client" \
  --addresses-path my-addresses.txt   # optional, defaults to resources/prod_addresses.txt
```

Arguments are identical to `prod continuous-downloader deploy` apart from the omitted `--sleep-interval-secs`. The deployment row carries `deploy_args.kind = "prod-downloaders"`.

Teardown:

```bash
uv run saorsa-deploy prod downloaders destroy --name PROD-DL-OS-01
# add --keep-monitoring to preserve the InfluxDB VM
```

### Running uploaders against production (`prod uploaders`)

`prod uploaders deploy` stands up one or more client VMs that each upload a list of public files to the canonical Autonomi production network with `ant --json file upload --public`, recording per-file metrics in InfluxDB. Each VM downloads its source files at runtime from the URL list you supply, uploads each one in turn, and exits when its list is exhausted — teardown is explicit via `prod uploaders destroy`.

Like `prod continuous-downloader`, this command does not provision any bootstrap/genesis/node infrastructure. It fetches the live prod peers list at runtime and points the upload service at those peers.

```bash
uv run saorsa-deploy prod uploaders deploy \
  --name PROD-UP-01 \
  --secret-key "$ARBITRUM_ONE_SECRET_KEY" \
  --uploader lon1:s-2vcpu-4gb:concurrency=8:urls=./uploads-lon1.txt \
  --uploader nyc1:s-4vcpu-8gb:concurrency=16:urls=./uploads-nyc1.txt
```

Before any infrastructure is created, the command runs a **duplicate-file preflight**: it derives the file name of every upload target (the last path segment of each source URL, as recorded in the `deployment_prod_upload_results` table that `prod files` prints) and refuses to deploy if any of them already exists in the prod uploads list. This stops you from re-uploading — and being charged for, or merely re-verifying — a file that is already on the network. Pass `--allow-duplicate-files` to downgrade the collision to a warning and proceed anyway (e.g. to re-upload missing chunks, or to deliberately test that already-stored chunks are not paid for again). When no `DATABASE_URL` is configured there is nothing to compare against, so the check is skipped with a warning.

Orchestrated sequence (each step is skipped when its state already exists, so the command is idempotent under reruns):

1. `monitoring infra` + `monitoring provision` for an InfluxDB VM in `lon1` named identically to `--name`.
2. `clients infra` for one upload-role VM per `--uploader` spec (synthesised from the per-uploader region / vm_size).
3. A custom Pyinfra provisioner that installs the `ant` binary, deploys the `prod_uploader.py` Python service, writes per-host URL lists, writes `/etc/ant-prod-uploader.env`, and registers the `ant-prod-uploader.service` systemd unit (`Type=simple`, `Restart=no`).
4. Enables the service on each VM, which uploads each URL in turn and emits one Influx line per upload to the `prod_upload` measurement.

Arguments (for `prod uploaders deploy`):

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--name` | string | — | Deployment name; also used for the paired monitoring stack |
| `--uploader` | repeatable | — | Per-VM spec `REGION:VM_SIZE:urls=PATH[:payment=MODE][:log_level=LEVEL][:clock_skew=DURATION][:bootstrap_cache=BOOL][:forward_logs=BOOL][:memory_profiling=BOOL][:rust_log=SPEC]`. The first three parts are required. `log_level=LEVEL` sets the `ant` upload verbosity for that VM — `info` (default → `-v`), `debug` (`-vv`), or `trace` (`-vvv`); omit for info. `clock_skew=DURATION` disables time sync and shifts that VM's clock by a signed Go-style duration (e.g. `2h`, `-2h`, `30m`; testing only). `bootstrap_cache=true|false` (default true) — when true `ant` runs **without** `--bootstrap` (peer cache enabled, peers read from a provisioned `bootstrap_peers.toml`); false restores the legacy `--bootstrap` invocation. `forward_logs=true` forwards that VM's `ant` stdout+stderr to a **dedicated** Elasticsearch instance stood up by this deploy (index `telegraf-ant-clients-<name>`; torn down on destroy unless `--keep-elastic`). `memory_profiling=true` wraps that VM's `ant` invocation with `heaptrack` (installs the `heaptrack` apt package); profiles → `/var/log/ant-heaptrack/upload-<idx>.<pid>.zst` on the VM (scp off before destroy). `rust_log=SPEC` sets `RUST_LOG` (overriding the verbosity flag) and must be the **last** token (value may contain `:`). |
| `--forward-logs` / `--no-forward-logs` | flag | off | (`prod downloaders`/`continuous-downloader`/`canary` deploy) Forward the client `ant` stdout+stderr to a dedicated Elasticsearch instance (index `telegraf-ant-clients-<name>`), stood up by the command and torn down on destroy unless `--keep-elastic`. |
| `--memory-profiling` / `--no-memory-profiling` | flag | off | (`prod downloaders`/`continuous-downloader` deploy) Wrap the client `ant` invocation with `heaptrack` (installs the `heaptrack` apt package); profiles → `/var/log/ant-heaptrack/download-<seq>.<pid>.zst` on the VM. scp them off before destroy. |
| `--rust-log SPEC` | flag | — | (`prod downloaders`/`continuous-downloader`/`canary` deploy) Set `RUST_LOG` on the client service, overriding the `ant` verbosity flag. Full RUST_LOG syntax incl `::`. |
| `--keep-elastic` | flag | off | (prod destroy) Leave the dedicated client-log Elasticsearch VM running. Default: it is destroyed alongside the client/monitoring. |
| `--allow-duplicate-files` | flag | off | Skip the duplicate-file preflight and deploy even if an upload target already exists in the prod uploads list. Use to re-upload missing chunks or test the already-stored path. |
| `--secret-key` | string | `$ARBITRUM_ONE_SECRET_KEY` | Funded Arbitrum One wallet secret key shared across all uploaders. Deliberately distinct from the testnet `ANT_DEPLOY_SECRET_KEY` — prod uploads cost real ETH. |
| `--branch-name` / `--repo-owner` | string | — | Optional custom ant build |
| `--ant-version` | string | — | Optional explicit release version |
| `--monitoring-volume-size` | int | `50` | Monitoring VM attached volume size (GB) |
| `--ssh-key-path` | string | `~/.ssh/id_rsa` | SSH key |

The wallet secret key is shared across every uploader VM. Each uploader file is downloaded once from its source URL, uploaded with `--public`, then deleted from the VM.

After uploads have started, inspect them with:

```bash
uv run saorsa-deploy deployments print-uploads --name PROD-UP-01
# add --details for one full record per upload with no truncation
```

`deployments print-uploads` dispatches on the deployment kind. For `prod-uploaders` it queries the `prod_upload` Influx measurement and prints one Rich table per uploader VM with file name, size, status (success / partial / dl-fail), chunks stored vs total, public address, and duration.

When the run completes, persist every Influx record into the registry database for future comparison:

```bash
uv run saorsa-deploy deployments record-results --name PROD-UP-01
```

`deployments record-results` is kind-dispatched. For `prod-uploaders` it upserts each `prod_upload` Influx point into `deployment_prod_upload_results`, keyed by `(deployment_id, client_hostname, source_url)`. The `--window` flag is ignored for this kind — uploads are bounded runs, so the command always pulls from epoch. Re-running the command updates rows in place, so it is safe to run repeatedly.

To tear down:

```bash
uv run saorsa-deploy prod uploaders destroy --name PROD-UP-01
# add --keep-monitoring to preserve the InfluxDB VM so historical metrics remain queryable
```

The deployment registers a row in the `deployments` table with `deploy_args.kind = "prod-uploaders"`.

### Running uploads from the local machine (`prod local-uploads`)

`prod local-uploads` is the local-machine variant of `prod uploaders deploy`. It runs `ant --json file upload --public` sequentially against the production Autonomi network from whichever host the command is invoked on, instead of provisioning remote uploader VMs. Per-file results are written directly into the `deployment_prod_upload_results` Postgres table — there is no Influx or monitoring VM, and no infrastructure to tear down. The intended use case is testing prod uploads from a real home internet connection.

```bash
uv run saorsa-deploy prod local-uploads \
    --name LOCAL-UP-01 \
    --purpose "Smoke test prod uploads from home connection" \
    --secret-key 0xabc... \
    --local-dir-path /path/to/files \
    --payment merkle
```

Arguments (for `prod local-uploads`):

| Argument | Required | Description |
|----------|----------|-------------|
| `--name NAME` | yes | Deployment name; used to scope per-file rows in `deployment_prod_upload_results`. |
| `--purpose TEXT` | yes | Free-text description, persisted to `deploy_args` and surfaced by `deployments slack-post`. |
| `--urls FILE` / `--local-dir-path DIR` | one of | `--urls` reads a newline-separated URL list (each URL is downloaded to a temp file, uploaded, then the temp file is deleted; original URL targets are not touched). `--local-dir-path` recursively walks the given directory and uploads every regular file underneath; each file is identified by its path relative to the root (e.g. `sub/dir/foo.bin`). Source files are never modified or deleted. |
| `--secret-key KEY` | yes (or env) | Funded Arbitrum One mainnet wallet key. Falls back to `ARBITRUM_ONE_SECRET_KEY`. Uploads cost real ETH. |
| `--branch-name BRANCH` + `--repo-owner OWNER` | no | Pull a custom-built `ant` binary from S3 (must be paired). |
| `--ant-version VERSION` | no | Pin a specific released `ant` version. Mutually exclusive with `--branch-name`. |
| `--payment MODE` | no | `merkle` or `no-merkle`. Omit to let `ant` choose. |
| `--bootstrap-cache` / `--no-bootstrap-cache` | no | Ant peer cache mode (default on): `ant` runs **without** `--bootstrap` and reads `~/.config/ant/bootstrap_peers.toml`, which is created from the fetched prod list **only when missing** — an existing operator file is never overwritten. `--no-bootstrap-cache` restores the legacy explicit `--bootstrap` invocation (cache disabled). |
| `--log-level LEVEL` | no | `ant` upload verbosity: `info` (default → `-v`), `debug` (`-vv`), or `trace` (`-vvv`). This is the documented knob; it supersedes the deprecated, hidden `-v`/`--verbose` count flag when both are given. |

The command resolves the live prod bootstrap peers from upstream at startup, downloads the resolved `ant` binary into `~/.cache/saorsa-deploy/ant-binaries/<key>/ant` (pinned releases are cached across runs; `latest` and branch builds are re-fetched), and registers a row in the `deployments` table with `deploy_args.kind = "prod-local-uploads"`. On completion the deployment is marked `stopped` regardless of per-file failures — failures live in the per-row Postgres records.

There is no paired destroy command; nothing was provisioned.

`prod local-uploads` deployments have no Influx backend, so `deployments print-uploads` rejects them and points at the forthcoming `deployments print-recorded-results` command (reads `deployment_prod_upload_results` from Postgres). Until that lands, query the table directly via `psql`.

### Running downloads from the local machine (`prod local-downloads`)

`prod local-downloads` is the download-side equivalent of `prod local-uploads`. It runs `ant -v file download <address> -o <output>` sequentially against the production Autonomi network from whichever host the command is invoked on, instead of provisioning a remote downloader VM. Per-address results are written directly into the `deployment_prod_download_results` Postgres table — there is no Influx, no monitoring VM, and no infrastructure to tear down. The intended use case is testing prod downloads from a real home internet connection.

```bash
uv run saorsa-deploy prod local-downloads \
    --name LOCAL-DL-01 \
    --purpose "Smoke test prod downloads from home connection" \
    --addresses-path /path/to/addresses.txt
# add --keep-output-dir /path/to/output to retain the downloaded files
```

Arguments (for `prod local-downloads`):

| Argument | Required | Description |
|----------|----------|-------------|
| `--name NAME` | yes | Deployment name; used to scope per-address rows in `deployment_prod_download_results`. |
| `--purpose TEXT` | yes | Free-text description, persisted to `deploy_args` and surfaced by `deployments slack-post`. |
| `--addresses-path FILE` | yes | Newline-separated list of addresses. `#`-prefixed and blank lines are ignored. No default — the operator must supply the list explicitly. |
| `--keep-output-dir DIR` | no | When set, downloaded files are written into `DIR` and kept on disk. When omitted, each download goes to a per-run temp directory and is deleted immediately after the result row is recorded. |
| `--bootstrap-cache` / `--no-bootstrap-cache` | no | Ant peer cache mode (default on): `ant` runs **without** `--bootstrap` and reads `~/.config/ant/bootstrap_peers.toml`, which is created from the fetched prod list **only when missing** — an existing operator file is never overwritten. `--no-bootstrap-cache` restores the legacy explicit `--bootstrap` invocation (cache disabled). |
| `--branch-name BRANCH` + `--repo-owner OWNER` | no | Pull a custom-built `ant` binary from S3 (must be paired). |
| `--ant-version VERSION` | no | Pin a specific released `ant` version. Mutually exclusive with `--branch-name`. |

Unlike `prod local-uploads` there is no `--secret-key` / `--payment` — downloads are read-only against the network and cost nothing.

The command resolves the live prod bootstrap peers from upstream at startup, downloads the resolved `ant` binary into `~/.cache/saorsa-deploy/ant-binaries/<key>/ant` (pinned releases are cached across runs; `latest` and branch builds are re-fetched), and registers a row in the `deployments` table with `deploy_args.kind = "prod-local-downloads"`. On completion the deployment is marked `stopped` regardless of per-address failures — failures live in the per-row Postgres records.

There is no paired destroy command; nothing was provisioned.

Companion commands behave the same way as for `prod local-uploads`: `deployments record-results` refuses this kind (records are written at run time), `deployments print-recorded-results` renders per-address Rich tables (`--details` for the long form), and `deployments slack-post` posts a "Prod Downloads [Home]" summary.

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

### Running canaries against production (`prod canary`)

A *canary deployment* uploads a fixed list of public files to the production
Autonomi network and then continuously probes them for the rest of its
lifetime. The dataset (per-upload metadata + every probe outcome) is the
**product**; teardown defaults to keeping monitoring intact so the data
survives.

A canary deployment is composed of:

- **1 monitoring VM** (mandatory) — InfluxDB + Grafana, paired with the
  deployment, lives in `lon1`.
- **1 upload VM** — wallet-bearing, walks the URL list once and exits the
  upload service. Per-file logs land at
  `/var/log/ant-prod-canary/upload-{canary_id}.{stdout,stderr}`.
- **N probe VMs** — wallet-free, statically partition the canary set with
  `blake3(canary_id)[0] % N`, probe every alive canary on the hour and
  emit one `prod_canary_probe` Influx point per probe.

A canary is `lost` when **two consecutive failed probes from the same
probe VM report the same `missing_chunk_address`** (the *two-strike rule*).
Local SQLite on each probe VM applies the rule online; `record-results`
re-derives it globally from Postgres and writes the authoritative
`prod_canary_loss` rows.

#### Deploy

```bash
saorsa-deploy prod canary deploy \
  --name CANARY-01 \
  --upload-spec lon1 \
  --probe-spec lon1:count=1 \
  --probe-spec nyc3:count=1 \
  --urls-file ./urls.txt \
  --secret-key 0x... \
  --purpose "v1 canary"
```

| Flag | Required | Default | Notes |
|------|----------|---------|-------|
| `--name` | yes | — | Deployment name; also names the paired monitoring stack. |
| `--upload-spec REGION[:VM_SIZE]` | yes | vm_size = `s-1vcpu-2gb` | Single upload VM. |
| `--probe-spec REGION[:VM_SIZE]:count=N` | yes (≥1) | vm_size = `s-1vcpu-2gb` | Repeatable. Each VM gets a sequential `--probe-shard-index`; total shard count = sum of `count=N`. |
| `--secret-key` | yes (or env) | falls back to `ARBITRUM_ONE_SECRET_KEY` | Funded Arbitrum One mainnet wallet — uploads cost real ETH. |
| `--urls-file` | yes | — | Local newline-separated list of public source URLs. Comments (`#`) and blank lines ignored. |
| `--upload-sleep-secs` | no | `0` | Sleep between successive uploads. |
| `--probe-concurrency` | no | `4` | Concurrent ant download invocations per probe VM. |
| `--bootstrap-cache` / `--no-bootstrap-cache` | no | on | Ant peer cache mode on the upload **and** probe VMs. On (default): `ant` runs **without** `--bootstrap` and reads a provisioned `bootstrap_peers.toml` (cache enabled). Off: legacy explicit `--bootstrap` invocation (cache disabled). |
| `--monitoring-volume-size` | no | `50` | Monitoring VM volume size in GB. |
| `--branch-name`, `--repo-owner`, `--ant-version` | no | latest release | Same binary-resolution flags as `prod uploaders`. |
| `--purpose` | yes | — | Free-text description, persisted in `deploy_args.purpose`. |

Sequencing under one command: monitoring infra → monitoring provision →
clients infra (1 uploader-role + N downloader-role VMs) → upload VM
provision → probe VMs provision. The Deployment row is registered with
`deploy_args.kind = "prod-canary"`.

#### Persist + summarise

```bash
# Pull every Influx point into Postgres and re-derive the loss table.
saorsa-deploy deployments record-results --name CANARY-01

# Show alive / pending / confirmed-lost counts, hours-to-loss histogram,
# per-vantage breakdown, per-chunk-role breakdown.
saorsa-deploy deployments print-recorded-results --deployment-id <id>
```

Both commands branch on `deploy_args.kind` and route through dedicated
canary handlers; existing testnet / prod-uploaders / prod-continuous-downloader
behaviour is untouched.

#### Destroy

```bash
# Default: keep monitoring (the canary dataset is the product).
saorsa-deploy prod canary destroy --name CANARY-01

# Tear down monitoring too:
saorsa-deploy prod canary destroy --name CANARY-01 --no-keep-monitoring
```

Postgres rows are never touched by destroy.

### Running tests

```bash
uv run pytest tests/ -v
```

### Linting and formatting

```bash
uv run ruff format saorsa_deploy/ tests/
uv run ruff check saorsa_deploy/ tests/
```
