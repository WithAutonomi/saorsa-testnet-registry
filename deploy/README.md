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

### Parallel infrastructure creation

The five infrastructure-creation steps of a testnet/extension deploy
(`bootstraps infra`, `nodes infra`, `elastic infra`, `monitoring infra`,
`clients infra`) run **concurrently** as a single group rather than one after
another, since each VM tier is created independently. The only ordering that
remains is `bootstraps infra → nodes infra`: they share the deployment-state
object (and `nodes infra` needs the bootstrap nodes in state), so they run
sequentially in one lane. `elastic`, `monitoring`, and `clients` each own an
independent S3 state key and run as their own lanes. The critical path is
therefore the (usually longest) bootstraps→nodes chain, with the observability
tiers built in its shadow.

`plan` still lists all five commands (annotated `(parallel)`) so you can read the
exact argv. During `up`, each lane runs as its own `saorsa-deploy` subprocess
with its combined output tee'd to `.saorsa/infra-logs/lane<NN>-<cmd>.log` (the
paths are printed up front for `tail -f`), while the parent shows one live status
table — one row per lane. A lane stops at its first failing step (so `nodes
infra` is skipped if `bootstraps infra` fails); in-flight lanes are allowed to
finish (a running `terraform apply` is never killed mid-flight), and once every
lane settles, any failure prints the failed lanes' captured output and aborts the
deploy before provisioning begins.

For resume purposes the whole group is **one journal unit**: it is recorded as
done only when every lane succeeds, so a re-run with `--resume` either skips the
entire group or re-executes all of it. That is safe because each `infra` command
is idempotent — terraform reconciles already-created tiers as no-ops.

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

`nodes.log_level` additionally **lifts the node→Elasticsearch drop filter** so the
verbose lines reach ES, not just disk. The node forwarder drops any line whose
level is in `--drop-log-levels` (default `DEBUG,TRACE`); without this coupling a
`log_level: trace` testnet would write trace to disk but ES would still show only
INFO/WARN. The orchestrator derives the drop set from `nodes.log_level` — dropping
only the levels *more verbose* than the one configured: `trace` keeps everything
(`--drop-log-levels ""`), `debug` drops just `TRACE`, and `info`/unset leaves the
default in place. An explicit **`elastic.drop_log_levels`** always overrides this:
set `"DEBUG,TRACE"` to keep verbose logs on disk but out of ES for volume reasons,
or `""` to force-keep everything regardless of node level. The **client** forwarder
has no level filter (it ships every raw stderr line), so `rust_log` trace already
reaches ES; for high-volume trace runs, **`elastic.buffer_limit`** is now also
applied to the client forwarder (via `clients provision --forward-buffer-limit`) so
its small default output buffer doesn't overflow and drop forwarded lines.

**Keeping the logs/metrics after teardown:** `elastic.keep_on_destroy: true` and
`monitoring.keep_on_destroy: true` (testnet and testnet-extension kinds) make
`deployments down` tear down the clients, nodes and bootstraps but leave that
observability VM standing, so a run that went wrong can still be investigated
without paying for the node fleet. Both `down` paths honour it: the intent is
recorded in the component's own S3 state at `infra` time (via `elastic infra
--keep-on-destroy` / `monitoring infra --keep-on-destroy`), so `down --name
<deployment>` behaves the same as `down --config <manifest>` with no manifest in
hand. Default is `false` — teardown destroys everything, as before — and the key
requires the matching `enabled: true`, since keeping a stack that was never
created is a silent no-op.

Two things it deliberately does *not* do. It does **not** extend retention: a kept
Elasticsearch index still expires on its ILM policy (`elastic.retention_days`,
default 7 days), so raise that if the investigation needs longer. The testnet
InfluxDB bucket's retention is infinite by default, so kept metrics persist. And it
does **not** gate the primitive `elastic destroy` / `monitoring destroy` commands —
those are how you clean the retained stack up, and `down` prints the exact command
plus the endpoint for each stack it left running.

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

### `deployments provision-telegraf` command

Install Telegraf for **both** Elasticsearch log forwarding (systemd
`telegraf-ant-elasticsearch`) **and** InfluxDB host metrics (systemd
`telegraf-ant-metrics`) in a **single SSH pass per host group** — one Telegraf
deb install, one connect/run/disconnect. This replaces the former separate
`elastic provision-telegraf` and `monitoring provision-telegraf` commands.

> **Note:** in a `deployments up` run Telegraf is no longer installed by this
> command — it is **folded into the provision passes** (`nodes provision` /
> `nodes provision-genesis` / `bootstraps provision` take `--with-telegraf`, and
> `clients provision` installs client host metrics), so the node VM list is
> walked once. This standalone command is retained for **retrofit / manual
> re-runs** against an already-provisioned deployment.

A backend is provisioned only when its credentials resolve: pass the
`--elasticsearch-*` / `--influxdb-*` flags explicitly, or pass `--name` and the
command auto-loads whatever elastic / monitoring state the deployment has (so a
deployment with only one backend gets just that forwarder).

```bash
# Deployment state mode: loads node IPs + ES and InfluxDB credentials from state.
uv run saorsa-deploy deployments provision-telegraf --name DEV-01

# Explicit credentials (e.g. external setups / retrofit).
uv run saorsa-deploy deployments provision-telegraf \
  --host-ips-file hosts.txt \
  --elasticsearch-url https://es.example.com \
  --elasticsearch-username user --elasticsearch-password pass \
  --influxdb-url http://1.2.3.4:8086 --influxdb-token TOKEN
```

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--name` | string | No¹ | - | Deployment name (loads node IPs + ES/InfluxDB credentials from state) |
| `--host-ips-file` | string | No¹ | - | Text file of host IPs, one per line (retrofit) |
| `--inventory-path` | string | No¹ | - | Inventory JSON (reads ip/ssh_user/ssh_key/role per entry) |
| `--host-role` | choice | No | - | Force `GENESIS`/`BOOTSTRAP`/`WAN_NODE`/`CLIENT` for every host (retrofit) |
| `--elasticsearch-url` / `-username` / `-password` | string | No² | - | ES endpoint + credentials (auto-loaded from elastic state with `--name`) |
| `--influxdb-url` / `--influxdb-token` | string | No² | - | InfluxDB endpoint + token (auto-loaded from monitoring state with `--name`) |
| `--influxdb-org` | string | No | `ant` | InfluxDB organization |
| `--influxdb-bucket` | string | No | `testnet-metrics` | Node host-metrics bucket (CLIENT hosts always use `client-metrics`) |
| `--node-version` / `--branch-commit` / `--repo-owner` | string | No | / / `maidsafe` | ES global tags |
| `--drop-log-levels` | string | No | `DEBUG,TRACE` | ES log levels dropped before shipping (`""` keeps every line) |
| `--buffer-limit` / `--max-undelivered-lines` | int | No | / | ES forwarder buffer sizes |
| `--from-start` / `--only-new-logs` | flag | No | from-start | ES tail reads from beginning vs end |
| `--ant-log-dir` | string | No | `/mnt/ant-node-storage/log` | Override the tailed log directory |
| `--ssh-key-path` / `--ssh-user` | string | No | `~/.ssh/id_rsa` / `root` | SSH connection details |

¹ Exactly one of `--name` / `--host-ips-file` / `--inventory-path` is required.
² At least one backend's credentials must resolve (via flags or `--name` state).

On the `--name` path the command also enumerates client VMs from client state (if
any) and provisions host-stat Telegraf on them with `host_role="CLIENT"`, routed
to the dedicated **`client-metrics`** bucket (the client→ES forwarder is installed
separately by `clients provision`).

## Monitoring

Optional per-testnet monitoring stack: InfluxDB 2.x (always) plus an **optional**
Grafana VM, with Telegraf forwarding host-level metrics (CPU, memory, disk,
network) from testnet nodes.

**Grafana is opt-in and OFF by default** — by default only the InfluxDB VM is
created (metrics stay fully queryable via InfluxDB/Flux). Enable dashboards with
`monitoring infra --grafana` (manifest: `monitoring: { enabled: true, grafana:
true }`). When enabled, Grafana runs on its **own (small) VM** so its panel
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

Create the InfluxDB VM (with attached storage) using Terraform, plus the
optional Grafana VM when `--grafana` is passed.

```bash
# DigitalOcean (default) — InfluxDB only
uv run saorsa-deploy monitoring infra --name DEV-01

# InfluxDB + Grafana dashboards
uv run saorsa-deploy monitoring infra --name DEV-01 --grafana

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
| `--grafana` / `--no-grafana` | flag | No | `--no-grafana` | Also create the separate Grafana VM. Off by default (InfluxDB only) |
| `--grafana-size` | string | No | `s-1vcpu-2gb` (do) / `d2-4` (ovh) | Grafana VM size (only used with `--grafana`; it only renders panels, so it stays small) |
| `--keep-on-destroy` | flag | No | off | Record in this stack's state that `deployments down` must leave it standing, so the metrics stay queryable after the testnet is gone. Honoured by both `down --config` and `down --name`. `monitoring destroy` is never gated by it |

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

On completion, prints connection details including InfluxDB URL, API token, Grafana URL, and a ready-to-paste `deployments provision-telegraf` command.

This step creates **two** InfluxDB buckets — `testnet-metrics` (node-tier host
metrics, the `prometheus` per-node measurement, and application measurements
like `download` / `prod_upload` / `prod_canary_*`) and `client-metrics`
(client-tier host metrics only) — and mints an org-scoped Telegraf token with
read+write on both. Splitting host metrics across two buckets keeps the **Node
Host Statistics** and **Client Host Statistics** Grafana dashboards physically
isolated: each dashboard queries only its own bucket, so neither can render the
other's data.

### InfluxDB host-metrics Telegraf

Telegraf for InfluxDB host metrics (CPU, memory, disk space across all mount
points, network bandwidth) is installed by the unified
[`deployments provision-telegraf`](#deployments-provision-telegraf-command)
command, in the same pass as Elasticsearch log forwarding. When only monitoring
state exists for a deployment it provisions metrics only. The node host-metrics
bucket is `testnet-metrics`; CLIENT hosts are always routed to `client-metrics`.

#### Per-device disk I/O

Both tiers also collect **per-device disk I/O** via a `[[inputs.diskio]]` stanza in the shared base config (`resources/monitoring/telegraf_metrics.conf`). It is **on by default with no flag** — the client config is an append to the node base, so the one stanza covers node-tier and client-tier hosts alike, and reaches every install path (`nodes provision`, `nodes provision-genesis`, `bootstraps provision`, `clients provision`, the standalone `deployments provision-telegraf`, and `monitoring info --telegraf-config`).

This is distinct from `[[inputs.disk]]`, which is capacity only (used/free/total). The `diskio` measurement carries the monotonic counters `read_bytes` / `write_bytes`, `reads` / `writes`, `read_time` / `write_time`, `io_time`, `weighted_io_time`, plus the gauge `iops_in_progress`, each tagged with the device `name` (e.g. `sdb`). Throughput, IOPS, await and `%util` are all derivable from those.

**The `devices` list is include-only** — the plugin has no exclude option, so the glob `["sd*", "vd*", "nvme*", "xvd*", "dm-*"]` has to name everything wanted. It covers each provider's naming (DigitalOcean `vda`/`sda`, OVH + Hetzner `sda`/`sdb`, Vultr `vda`/`vdb`, Contabo root-only, NVMe) plus LVM `dm-*`. Crucially it does **not** match `loop*`: Ubuntu cloud images carry ~10 snapd loop devices that would otherwise add that many permanently-zero series per host. `skip_serial_number = true` keeps disk serials out of the tag set.

##### Identifying the node storage volume

diskio knows only kernel device names (`sdb`, `dm-0`) — never mountpoints — and the device backing `/mnt/ant-node-storage` differs by provider and by whether the volume is raw ext4 or LVM-striped. So the device is resolved **on the host at provision time** (`findmnt -nro SOURCE /mnt/ant-node-storage` → `lsblk -nro KNAME`, the same idiom `set_reserved_blocks.py` uses), written to `ANT_DATA_DEVICE` in `/etc/default/telegraf-ant-metrics`, and matched by a `[[processors.regex]]` scoped to `namepass = ["diskio"]` that tags that device's series **`volume_role = "node-storage"`**.

The tag lands in InfluxDB, so Grafana and `record-analysis` both filter on it directly — no join against the `disk` measurement, and no breakage on LVM hosts.

- **Root-disk providers** (Contabo, OVH 3-AZ) keep node data on the OS root filesystem, so there the **root device is tagged** `node-storage`. That series then necessarily includes OS I/O as well — unavoidable when the two share a disk.
- **Client VMs** run no node storage and never fall back to root; they get the sentinel `ANT_DATA_DEVICE=__none__`, which matches no real device, so nothing is tagged.
- Consumers must test `volume_role == "node-storage"` rather than merely testing for the tag's presence.

**Long-term archival.** `deployments record-analysis` pulls the diskio counters alongside CPU/memory/network and upserts per-host aggregates into `deployment_host_results.host_metrics` under **`disk_io_by_device`** — a dict keyed by kernel device name, each holding `volume_role` plus `read_bytes_per_sec` / `write_bytes_per_sec` / `reads_per_sec` / `writes_per_sec`, each a `{avg, min, max, samples}` stat. Rates are computed **within** a device (consecutive per-second deltas, negative deltas dropped as reboot counter resets).

Devices are deliberately **not** averaged together the way the network counters are pooled across interfaces: a host's root disk and its node storage volume are not interchangeable, and an idle root disk averaged against a busy data volume describes neither. `record-analysis`'s summary table therefore shows the **node storage volume's** figures (and names the device); `deployments print-recorded-results` renders every device, one row per (host, device), with the storage volume flagged in its Role column. `host_metrics` is a JSON column, so rows recorded before disk I/O existed simply lack the key and the section is skipped for them.

**Retrofitting an existing deployment.** Nothing is needed for new deployments. To add disk I/O to a deployment that is already running, re-run `deployments provision-telegraf --name <name>`: it regenerates the config from the same builder and restarts `telegraf-ant-metrics`.

### Bucketed host metrics — slicing a window after the run

`deployments record-analysis` writes host metrics twice: the whole-run rollup in `deployment_host_results` (unchanged, and still the cheap "how did the run look overall" view), **and** fixed-width time buckets in **`deployment_host_metric_buckets`**. The buckets exist so an arbitrary sub-window can be re-derived **after the monitoring stack is destroyed** — previously the window was fixed at capture time by `--window`, and once InfluxDB was gone it could never be revisited. That is what forced the rc.1 staging comparison to be reported as directional only: the two runs' stored windows were 3.37 h and 5.45 h, the latter including the mixed-version phase, so their CPU/memory/network figures were not like-for-like.

One row per `(host, bucket_start, metric, device, service)`, holding `avg`/`min`/`max`/`samples`. A tall numeric table rather than JSON-per-bucket, so a window is filtered server-side and re-aggregated in one `GROUP BY`.

```bash
# Default: 5-minute buckets
uv run saorsa-deploy deployments record-analysis --name STG-01

# Finer resolution for a run you expect to plot as a trend
uv run saorsa-deploy deployments record-analysis --name STG-01 --bucket-secs 60

# Rollup only — skip bucket capture entirely
uv run saorsa-deploy deployments record-analysis --name STG-01 --bucket-secs 0
```

**Bucket width does not affect accuracy.** For a window aligned to bucket boundaries, `sum(avg × samples) / sum(samples)` is *exactly* the mean of every underlying 10 s sample, and `MIN(min)` / `MAX(max)` / `SUM(samples)` are the exact extremes and count. Each bucket carries its own min and max, so a brief spike inside a bucket still surfaces as that bucket's `max` and as the window's `max`. Width trades only **how finely a window edge can be placed** (≤5 min by default, ~2.8 % of a 3 h window) and **trend-plot resolution** (65 points across a 5 h run at 300 s, 327 at 60 s) — never the numbers themselves. The 300 s default keeps a staging-sized run near 100 k rows rather than half a million; the registry runs on a small managed Postgres instance where table size is what costs query time.

Two things bound what is stored, both deliberate:

- **Disk I/O is bucketed only for the device tagged `node-storage`.** On a measured staging run, 348 of 421 device series were the root disk's boot/ESP partitions (`vda1`, `vda14`, `vda15`, `vda16`) whose counters merely duplicate the whole-disk counters — the majority of all rows, for no signal. The rollup still keeps **every** device.
- **Rates are computed before bucketing, never after.** The network and disk series are per-second deltas taken *within* an interface or device; bucketing the raw monotonic counters and differencing afterwards would produce nonsense. A rate sample is timestamped at the **end** of the interval it describes.

Reading a window back is `DeploymentRepository.get_host_metric_window(deployment_id, start, end)`, which returns the same shape as `host_metrics` (so a windowed re-derivation and the stored rollup are interchangeable) and does the sample-weighted mean server-side. **Averaging the per-bucket averages would be wrong** — buckets hold unequal sample counts at run boundaries and wherever a scrape was missed. The window is half-open on `bucket_start`, so a bucket is included whole or not at all.

Deployments recorded before this existed simply have no bucket rows, and consumers must degrade to the rollup. **No backfill is possible** — the source InfluxDB for a past run is destroyed with its monitoring stack.

#### `deployments prune-host-buckets`

Buckets are the bulk of what `record-analysis` writes. `prune-host-buckets` drops old ones while leaving `deployment_host_results` intact, so a pruned deployment degrades to exactly the whole-run view it would have had before bucketing — it keeps its analysis, it just can no longer be re-sliced.

```bash
# Preview first — the delete is irreversible and the data cannot be re-captured
uv run saorsa-deploy deployments prune-host-buckets --older-than 180 --dry-run
uv run saorsa-deploy deployments prune-host-buckets --older-than 180

# Scope to one deployment
uv run saorsa-deploy deployments prune-host-buckets --older-than 0 --name DEV-01
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--older-than N` | *(required)* | Delete buckets whose `bucket_start` is more than N days old. `0` deletes every bucket row. |
| `--name` | all deployments | Restrict the prune to one deployment. |
| `--dry-run` | off | Report what would be deleted without deleting it. |

The affected rows are always tabulated before deletion, `--dry-run` or not.

### Clock-skewed hosts

Every Flux `range()` in the Influx query paths carries an explicit far-future `stop`. This is not cosmetic. Flux defaults `stop` to `now()`, and `provisioning/clock_skew.py` deliberately jumps a VM's **system** clock — nodes by a fixed ±3 h in a per-host random direction, clients by an explicit signed duration — so a *forward*-skewed host's Telegraf writes every point hours ahead of real time. Those points fell outside the default range and were silently discarded.

The failure was shape-dependent, which is why it went unnoticed: on a long run a forward-skewed host merely lost its final skew's-worth of data, while on a short run it disappeared from the results entirely. On DEV-03 (2026-07-25) a +3 h node VM contributed 0 of 27 hosts to `record-analysis` while holding 460 `memory_rss` points in the bucket. The same defect applied to the file-op, prod-upload and canary queries.

Those hosts are now captured — but their timestamps remain displaced by the skew, so a sub-window re-derived from the buckets covers a *different* stretch of a skewed host's run than of everyone else's. `record-analysis` therefore prints a **`Clock offset detected`** warning listing any host whose observation window deviates from the fleet **median** by more than 15 minutes, with the signed offset. A positive offset is a clock running ahead; a negative one may instead mean the host stopped reporting early. The median is the reference deliberately — it needs no assumption about the clock of the machine running the command, and a handful of skewed hosts cannot drag it.

### Per-service node memory (RSS)

`record-analysis` also records **per-service RSS** for the node tier, under `rss_by_service` in `host_metrics` and as the `rss_bytes` bucket series. Telegraf has always collected it — four node-tier `[[inputs.procstat]]` blocks plus a regex processor deriving a `node_service` tag (`ant-node-3`) from `cgroup_full` — but nothing ever queried it, so process-level memory was absent from the registry entirely and could not be compared for *any* run recorded before this landed.

`mem_percent` is not a substitute: it is whole-host memory including page cache, so it cannot separate a genuine ant-node leak from cache growth, and it cannot attribute anything to one of the 15 services on a VM.

Keyed by `node_service`, never by PID — services restart and PIDs churn, while `ant-node-3` is stable. Deliberately **not** pooled into a host total, for the same reason disk devices are kept separate: the signal worth catching is a *single* service growing without bound, which a 15-service mean would dilute fifteenfold. `record-analysis`'s summary table shows only the peak service as a scalar; `deployments print-recorded-results` renders one row per (host, service). Client-tier RSS is a separate, already-working path in the `client-metrics` bucket (see below) and is unaffected.

CLIENT-role hosts additionally get the client `ant` service's **memory** recorded into `client-metrics`: a `procstat` input matching `system.slice/ant-*.service` (per-process RSS, tagged `metric_source=ant-client`, 2 s interval) and a `cgroup` input reading `memory.current` + `memory.peak` (whole-service live + high-water peak) for the same glob. These stanzas (`resources/monitoring/telegraf_metrics_client.conf`) are appended **only** on CLIENT hosts, so node configs are byte-identical and the cgroup input never logs a missing-path error on the node fleet. The single `ant-*` glob matches whichever client unit a VM runs (`ant-uploader`, `ant-downloader`, `ant-prod-uploader`, `ant-prod-downloader`, `ant-prod-canary-upload`, `ant-prod-canary-probe`). Query `procstat` filtered to `process_name == "ant"` for per-process RSS, or the `cgroup` measurement's `memory.peak` for the true per-run high-water mark (caught even when a sub-2 s terminal spike clips the sampled RSS). The local-* prod kinds run no systemd unit and are not covered.

### Normalised per-GB metrics (`deployment_derived_metrics`)

Raw measurements are not comparable across runs: a longer run moves more bytes, and a run with a different client mix stores a different amount of data. Two soft gates came out **NOT ASSESSED** in the rc.1 release-train verdict for exactly that reason — *outbound bytes per GB of user traffic* (which is directly the bandwidth bill) and *disk consumed per GB stored*. `record-analysis` now computes both at extraction time and persists them, with the components each is built from, to **`deployment_derived_metrics`**.

| Metric | Numerator | Denominator |
|--------|-----------|-------------|
| `outbound_bytes_per_gb_uploaded` | node-tier outbound bytes in the window | GB successfully uploaded in the window |
| `outbound_bytes_per_gb_traffic` | node-tier outbound bytes in the window | GB uploaded + GB downloaded |
| `disk_bytes_per_gb_stored` | node-storage volume growth, summed over node hosts | GB successfully uploaded |

Components stored in their own right: `node_outbound_bytes_total`, `node_inbound_bytes_total`, `node_storage_growth_bytes`, `gb_uploaded`, `gb_downloaded`. They are kept because a ratio alone cannot be sanity-checked, and because a later gate may want to normalise differently — which is impossible once the run's InfluxDB is destroyed.

Each row carries its window (`window_label`, `window_start`, `window_end`), its `numerator`/`denominator`, a `unit`, a `details` JSON breakdown, and a **`source`**. There is one row per `(deployment, metric, window_label)`: recomputing replaces, so a baseline comparison never picks between rival answers.

Four rules make the values trustworthy:

- **Node tier only, by construction.** The numerator covers whatever hosts are in the queried Influx bucket, which defaults to `testnet-metrics` — node, bootstrap and genesis VMs. Client VMs report to the separate `client-metrics` bucket, so their egress (the workload *generator*, not our bill) is excluded without any hostname heuristic.
- **Outbound bytes are summed from raw counter deltas, not integrated from the stored averages.** The rate samples are emitted per interface and pooled by *averaging*, so integrating them would report a two-NIC host's egress as the mean of its interfaces rather than their sum.
- **Storage growth is `last - first`, not `max - min`.** A host that filled its volume and was then pruned has not consumed that peak, and reporting the peak would make a pruning run look like a regression. The peak is recorded in `details.peak_growth_bytes` so the case stays visible.
- **The denominator counts successful ops that *finished* inside the window**, read from the per-op `deployment_upload_records` / `deployment_download_records` tables. `deployment_file_results` is an aggregate with no per-op timestamps and cannot be windowed at all. A failed upload stored nothing, so counting it would flatter the ratio exactly when the run went worst; its bytes are reported separately in `details`.

A metric that cannot be measured is **omitted, never estimated**: a ratio with an empty denominator, or the disk metrics on a run where no host reported a node-storage series, produce no row rather than a zero. Run `deployments record-results` *before* `record-analysis`, or the ratios have no denominator to normalise against.

This depends on filesystem occupancy, which Telegraf collected (`[[inputs.disk]]`) but nothing ever queried, so it reached the registry for the first time here. The node storage volume is identified by a `volume_role = "node-storage"` tag applied to the `disk` measurement's **`path`** (mountpoint) — not its `device` tag, whose value is the fstab-style source (`mapper/vg-lv`) and so never matches the kernel name `diskio` reports on an LVM-backed volume. The mountpoint is resolved on the host at provision time into `ANT_DATA_MOUNT`, alongside the existing `ANT_DATA_DEVICE`. Occupancy lands in `disk_usage_by_mount` in the rollup (with `first`, `last` and `growth_bytes`) and as the `disk_used_bytes` bucket series, restricted in buckets to the tagged mountpoint. **Retrofit a running deployment with `deployments provision-telegraf --name <name>`; no past run can be backfilled.**

#### `deployments derive-metrics`

Re-derives the same metrics for an arbitrary window from Postgres alone — bucket rows plus per-op records — so a phase can be sliced out **after** the monitoring stack is gone. This is what the bucketed host metrics exist for.

```bash
# Steady state only, for a like-for-like comparison against a baseline.
# Use --id for anything that is not the latest run of its name -- a baseline in
# particular, since staging names are reused every train.
uv run saorsa-deploy deployments derive-metrics --id 470 \
  --window-start 2026-07-25T17:00:55 --window-end 2026-07-25T20:00:55 \
  --label steady-state

# The latest run of a name can be addressed by name
uv run saorsa-deploy deployments derive-metrics --name STG-01 --label whole-span
```

Exactly one of `--id` / `--name`. **`--name` resolves to the most recently created run bearing it**, so an earlier run of a reused name is only reachable by id — and since `deployments compare --window-label` requires the label on *both* sides, a baseline comparison is unreachable without it.

| Argument | Default | Description |
|----------|---------|-------------|
| `--id` | *(one of these)* | Deployment id. Unambiguous; prefer it for a baseline. |
| `--name` | *(one of these)* | Deployment name, resolving to the most recent run with that name. |
| `--window-start` | first stored bucket | ISO-8601 UTC start. A naive timestamp is read as UTC. |
| `--window-end` | last bucket + one width | ISO-8601 UTC end (exclusive). |
| `--label` | `steady-state` | Names the window so several coexist for one run. Re-running the same label replaces it. |
| `--bucket-secs` | `0` (infer) | Override the width used to integrate averages into totals. Inference reads it from the stored buckets. |

The two paths are **not** equivalent, and the `source` column says which is which:

- `source=capture` — `record-analysis`, whole run, outbound summed from raw counter deltas. **Exact.**
- `source=buckets` — `derive-metrics`, outbound integrated as `avg × bucket width` and growth taken between the first and last bucket average. **Approximate**, bounded by the bucket width.

Never compare one against the other. `derive-metrics` refuses `--label full-run` outright, since that would replace an exact value with an approximation under the same name. The bucket width is **inferred** rather than assumed because it is not recorded on the rows, and integrating at the wrong width scales every byte total by exactly that factor with nothing to signal it; inference uses the most common gap between consecutive buckets, so a collection gap cannot move it.

`deployments print-recorded-results` renders both the per-mountpoint occupancy table and every stored window's derived metrics.

### Connection-error counts (`deployments record-log-counts`)

Counts a versioned set of named log strings in a run's Elasticsearch and stores the **counts** — never the lines — in `deployment_log_event_counts`, so `deployments compare` can gate on them.

```bash
# At extraction time, while the run's Elasticsearch is still alive
uv run saorsa-deploy deployments record-log-counts --name STG-01

# A labelled slice, matching a `derive-metrics --label steady-state` window
uv run saorsa-deploy deployments record-log-counts --id 474 \
  --window-start 2026-08-11T13:00:00 --window-end 2026-08-11T16:00:00 \
  --label steady-state
```

**Run it before teardown.** The deployment's Elasticsearch is per-run scratch and is destroyed with the testnet; there is no backfill and there never can be. This is the same constraint `record-analysis` has against InfluxDB.

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--id` / `--name` | int / string | Yes (one) | - | The run. Prefer `--id` for anything but the latest run of a reused name |
| `--elastic-name` | string | No | deployment name | Elastic stack to load credentials from |
| `--patterns` | path | No | bundled | Catalogue YAML (`resources/log_patterns/connection-errors.yaml`) |
| `--label` | string | No | `full-run` | Window label stored with the counts |
| `--window-start` / `--window-end` | ISO-8601 | No | - | Narrow the count. Requires a non-`full-run` `--label` |
| `--index-pattern` | string | No | from state | Override the node-log index glob |
| `--es-url` / `--es-username` / `--es-password` / `--es-api-key` | string | No | from state | Connect without the elastic S3 state |
| `--timeout` | int | No | `120` | Per-query timeout in seconds |

#### What is counted, and what a count means

The catalogue (`resources/log_patterns/connection-errors.yaml`) is git-versioned config, like the gate thresholds, and its `version` is stored on every recorded count. It names six strings across five groups; the comparison gates on **groups**, not individual strings.

`Connection attempt failed` (`saorsa_core::transport_handle`) and `connect_happy_eyeballs failed` (`saorsa_transport::happy_eyeballs`) share one group, combined with `max` rather than `sum`. They are the same failure logged at two layers — measured across the DEV-02 comparison at 1,415/1,415 on the baseline and 71,309/71,160 on the branch, tracking each other to within 0.2% across a 50x swing — so summing them would report one occurrence twice, and gating them separately would read as two independent corroborating signals.

Each count is stored with the **host cardinality**, the **top hosts**, the **level breakdown**, the **observed span**, and the **total forwarded node log lines** in the same scope. A bare count answers the wrong question in three ways: a rise concentrated on two hosts of seventy-two is a sick node or a service restart rather than a fleet-wide regression; a run that logged twice as much of everything has not regressed on connections; and a string emitted only at DEBUG may be absent from Elasticsearch entirely, because the node-tier forwarder drops `DEBUG,TRACE` by default.

Scope is **node-tier only**: `telegraf-ant-nodes-<name>-*`. Client logs go to a separate index with a different document shape, and whether they exist at all depends on the run's `forward_logs` setting.

#### What it refuses to record

An **empty index** aborts the command rather than recording zeros. A zero from a misdirected query or a run with log forwarding disabled is indistinguishable from a real absence, and would read as a total elimination of every connection error on the next comparison.

A **zero against a populated index** is recorded — that is a real measurement — but it is not self-explaining: "the bug is fixed", "the string was renamed" and "the string is emitted at a dropped level" look identical from Elasticsearch. The command prints a warning naming the zeroed patterns; confirm the literal against the pinned source before reporting a zero as an improvement.

A **narrowed window may not claim the `full-run` label**, mirroring `derive-metrics`. And a windowed count carries a clock-skew caveat: the filter is applied against each host's own clock, and testnet manifests skew node clocks by ±3h in a per-host random direction. An unfiltered count over the whole index has no such problem, which is why it is the default.

#### The gates

One soft gate per group, failing when the count rises more than `soft_gates.connection_error_increase_percent` (10%). Three behaviours worth knowing:

- **Absence is NOT ASSESSED on both sides, never zero.** No run recorded before this measurement existed has counts, and none can be backfilled — so the first comparison to use these gates reports NOT ASSESSED for all of them, and the second is the first real one. Treating a missing count as zero would show the candidate's real counts against nothing and read as a catastrophic regression.
- **Zero on both runs is a PASS**, not NOT ASSESSED — a permanently eliminated string is a measurement, not a missing one. A count appearing where the baseline had none is a **FAIL**: no percentage can express a rise from zero, but it exceeds any increase threshold by any reading.
- **The gate compares raw counts**, which scale with how long a run logged for. The per-hour rate and host spread are in each gate's detail, and the report warns when the two runs' logs span materially different periods (>1.1x) or when their catalogue versions differ.

### Staging gate comparison (`deployments compare`)

Compares one recorded run against a baseline run on their stored metrics, applies versioned gate thresholds, and emits a per-gate **PASS / FAIL / GRAY / NOT ASSESSED** verdict as machine-readable JSON plus a fixed-format markdown report. Deterministic by construction: same inputs, byte-identical report.

```bash
# Compare an RC staging run against the run the last shipped release passed on
uv run saorsa-deploy deployments compare --id 474 --baseline-id 470

# A steady-state slice (both sides must be re-derived under the same label first)
uv run saorsa-deploy deployments compare --id 474 --baseline-id 470 \
  --window-label steady-state --markdown-out report.md
```

Both runs are named by **deployment id, never by name** — staging names are reused every train, and a name lookup resolves to whichever run was most recent.

#### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--id` | int | Yes | - | Deployment id of the candidate run |
| `--baseline-id` | int | Yes | - | Deployment id of the baseline run |
| `--window-label` | string | No | `full-run` | Which stored window to compare on both sides |
| `--thresholds` | path | No | bundled | Threshold config YAML (`resources/gates/staging-thresholds.yaml`) |
| `--patterns` | path | No | bundled | Log-pattern catalogue YAML (`resources/log_patterns/connection-errors.yaml`) |
| `--history` | string | No | - | Comma-separated ids of prior **accepted** runs, for the 2σ noise criterion |
| `--json-out` | path | No | - | Write the machine-readable verdict here |
| `--markdown-out` | path | No | - | Write the fixed-format markdown report here |
| `--print-markdown` | flag | No | - | Also print the report to stdout |
| `--no-persist` | flag | No | - | Do not store the verdict in the registry |

Exits `2` on a hard-gate failure (which blocks the ship with no override); soft-gate failures and GRAY exit `0`, since those are judgment calls.

#### What it covers

Two hard gates (upload success floor < 99%, download success floor < 99.5%) and eight soft gates: upload/download success rate, transfer duration p50/p95 **per file-size bucket**, node CPU, node RSS, memory leak slope, outbound bytes per GB of traffic, and disk bytes per GB stored. Plus one soft gate per **connection-error group** recorded by `deployments record-log-counts` (see below).

Transfer-duration percentiles are exact — they come from the per-op `deployment_upload_records` / `deployment_download_records`, which carry each operation's own `duration_ms` and timestamps. `deployment_file_results` is not used: it is an aggregate with no per-op timestamps and therefore cannot be windowed.

#### What it deliberately does NOT cover

Four hard gates from the release process are **not derivable from anything the registry stores**: **data integrity** (no content verification exists anywhere in the system), **crashes** (panics reach stderr/journald, not the JSON logs Telegraf forwards, and Elasticsearch dies at teardown), **mixed-version chunk storage** (an SSH check on `chunks.mdb` growth), and **production canaries** (a separate deployment with its own assessment). Those are gathered observationally and recorded by the `evaluate-staging-run` skill, alongside this report.

#### Comparability guards

The command refuses outright when the two runs cannot be validly compared:

- **Different `window_label`s** — a steady-state slice and a full run are not the same measurement.
- **Different derived-metric `source`s** — `capture` sums raw counter deltas and is exact, `buckets` integrates averaged rates and is approximate. Comparing them measures the method, not the release.
- **Window lengths differing by more than `max_window_length_ratio` (1.25x)** — replication continues after an upload completes, so a shorter window truncates the disk numerator while still counting the full denominator.

It **warns** (rather than refusing) when the two runs' **activity coverage** — the fraction of the window in which clients were actually transferring — differs by more than `max_activity_coverage_ratio` (1.3x). The per-GB numerators accumulate through idle time while the denominators do not, so a mostly-idle window inflates every ratio. Measured across four recorded runs: 39.2% coverage → 14.86 GB/GB, against 52–61% coverage → 9.1–10.1 GB/GB. A window-*length* check cannot catch this, because the length can be perfectly ordinary.

A metric that is legitimately absent (an empty denominator produces no row at all) is **NOT ASSESSED**, never a zero — a zero would trip every threshold and read as a catastrophic regression.

#### Statistics, and one honest substitution

The process spec names node CPU **median** and node RSS **p50**. Host metric buckets historically stored only `{avg, min, max, samples}`, so percentiles were unrecoverable. `record-analysis` now captures `p50`/`p95` at extraction time — exactly, in the whole-run rollup, and per bucket — but **no past run can be backfilled**, because the source InfluxDB is destroyed with the monitoring stack.

The report therefore marks which statistic each figure is:

- `[p50]` — the exact whole-run percentile from the rollup.
- `[p50~]` — a window's percentile *estimated* from per-bucket percentiles. Percentiles do not compose across buckets the way `avg`/`min`/`max` do.
- `[mean]` — no percentile was captured; a sample-weighted mean stands in.

Comparing two different statistics against each other is refused as NOT ASSESSED rather than attributing the method's difference to the release.

Node RSS is compared at a **percentile across services** (default p95), not the fleet maximum. With ~1000 services the maximum is an extreme order statistic that moves with the single unluckiest one: measured 470 → 474, the max moved +64% while the median moved +3.8% and the p95 +1.8%. The worst service is always reported in the gate detail, so an outlier is visible without deciding the gate alone. Set `node_rss_service_percentile: 100` to restore worst-service behaviour.

The **memory leak** check is absolute (a property of the run, not a comparison — a leaking baseline should not excuse the candidate) and is evaluated on the **median** service's fitted RSS slope. A tail percentile does not work here: fitting a line through 2h of a jittery RSS signal produces slopes symmetric about zero — measured across four runs, p5 of −40 to −60 %/h against p95 of +37 to +60 %/h, with ~50% of services showing a negative slope and the positive tail matching the negative tail to within a few points. A threshold on p95 would measure how noisy RSS is, not whether it leaks. A **tail asymmetry** figure is reported alongside (the negative tail is a free, self-calibrating noise reference for a leak confined to a subset of services) but is deliberately not gated on yet — four runs is too little history to set a threshold.

#### Thresholds

All thresholds live in `deploy/saorsa_deploy/resources/gates/staging-thresholds.yaml`, git-versioned and carrying a `version:` key that is echoed into every report and stored on the persisted verdict. The release process schedules a quarterly recalibration against accumulated run history; that must not require a release of the tool. Override with `--thresholds PATH`. Validation is strict and fails loudly — a threshold silently defaulting to zero would turn a gate into a rubber stamp while still printing PASS.

#### Persistence

The verdict is upserted into `deployment_gate_verdicts`, keyed `(deployment_id, baseline_deployment_id, window_label)` so re-running replaces its previous answer rather than accumulating rival ones. The row stores the full JSON payload, the rendered markdown, and the threshold config version, so the exact text attached to a release issue can be recovered later without recomputing anything.

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

# 3. Install Telegraf on testnet nodes (host metrics + log forwarding, one pass)
uv run saorsa-deploy deployments provision-telegraf --name DEV-01

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
| `--daily` | flag | off | Convenience for a permanent once-a-day cadence: sets `--sleep-interval-secs` to `86400` (24h between full passes). Overrides `--sleep-interval-secs` when both are given. |
| `--provider` | `{digitalocean,hetzner,vultr,ovh}` | `digitalocean` | Cloud provider for the downloader **client** VM. Non-DO providers are cheaper for a permanent run and cut DigitalOcean egress (which is pooled and overage-billed). Requires the provider's credentials in the environment (`HCLOUD_TOKEN` / `VULTR_API_KEY` / the four `OVH_*` vars). The client TF module is selected automatically. |
| `--monitoring-provider` | `{digitalocean,ovh}` | `digitalocean` | Cloud provider for the paired **monitoring** VM (InfluxDB/Grafana). Only DigitalOcean and OVH have monitoring Terraform modules. |
| `--monitoring-region` | string | `lon1` (DO) / provider default (OVH) | Region for the monitoring VM. |
| `--monitoring-droplet-size` | string | `s-2vcpu-4gb` (DO) / provider default (OVH) | InfluxDB VM size/flavor. |
| `--monitoring-volume-type` | string | `high-speed` | OVH only: monitoring data-volume type. |
| `--influx-retention-days` | int | `7` | InfluxDB bucket retention (days) for the monitoring stack. `0` = infinite. Applied at first `monitoring provision`. |
| `--log-retention-days` | int | `7` | Days to keep per-op download logs under `/var/log/ant-prod-downloader` before a **daily systemd timer** (`ant-prod-downloader-logprune.timer`) prunes them with `find -mtime +N -delete`. `0` disables pruning (keep logs forever). |
| `--branch-name` / `--repo-owner` | string | — | Optional custom ant build |
| `--ant-version` | string | — | Optional explicit release version |
| `--monitoring-volume-size` | int | `50` | Monitoring VM attached volume size (GB) |
| `--ssh-key-path` | string | `~/.ssh/id_rsa` | SSH key |

The monitoring VM defaults to region `lon1` on DigitalOcean; with `--monitoring-provider ovh` it uses the OVH default region (or `--monitoring-region`). The client VM and the monitoring VM are independent — e.g. a Hetzner client paired with an OVH (or DO) monitoring box.

#### Running a permanent downloader

This command is designed to be left running indefinitely to measure production download performance over time (and correlate it with releases). The defaults above make that safe out of the box:

- **Cadence** — pass `--daily` (≈ once-a-day; sleeps 24h after each full pass through the address list). Use `--sleep-interval-secs N` for any other interval.
- **Cheaper provider** — pass `--provider hetzner` (or `ovh`/`vultr`) for the client VM, and `--monitoring-provider ovh` to also move the monitoring box off DigitalOcean.
- **Log retention** — per-op logs are pruned after 7 days by default (`--log-retention-days`).
- **Influx retention** — the InfluxDB bucket is capped at 7 days by default (`--influx-retention-days`); metrics older than that expire.
- **Long-history archival** — because Influx retention is bounded, periodically copy results into Postgres with `deployments archive` (below) so per-download history is retained for the long term.

```bash
uv run saorsa-deploy prod continuous-downloader deploy \
  --name PROD-DL-01 \
  --spec fsn1:cpx41 --provider hetzner \
  --monitoring-provider ovh \
  --daily \
  --purpose "Permanent prod download soak"
```

After the command reports success, record download aggregates into Postgres on demand:

```bash
uv run saorsa-deploy deployments record-results \
  --name PROD-DL-01 --monitoring-name PROD-DL-01 --window -1h
```

#### Archiving results for long-term history (`deployments archive`)

`deployments archive` is a thin wrapper over `record-results` for keeping a long history despite the bounded InfluxDB retention. Run it from the machine that owns the registry DB (`DATABASE_URL` set) and can reach the monitoring VM's InfluxDB. It must run **at least as often as the Influx retention window** (default 7 days) so no per-op records expire before they are copied; the per-op `deployment_download_records` table is the authoritative long-term history (idempotent, full-range upsert).

```bash
# One-shot (ideal for cron / a scheduled agent):
uv run saorsa-deploy deployments archive --name PROD-DL-01

# Or a self-contained long-runner that archives every 6h:
uv run saorsa-deploy deployments archive --name PROD-DL-01 --loop --interval-secs 21600
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--name` | string | — | Deployment name (must match a registry row). |
| `--monitoring-name` | string | `--name` | Monitoring stack to load Influx creds from. |
| `--window` | string | `-7d` | Flux range start for the aggregate rollup. Per-op records are always pulled full-range regardless. |
| `--loop` | flag | off | Keep archiving forever, sleeping `--interval-secs` between runs; a transient failure is logged and the loop continues. |
| `--interval-secs` | int | `21600` | With `--loop`: seconds between runs (6h). |
| `--influx-url/-token/-org/-bucket` | string | from monitoring state | Override the Influx connection. |
| `--update-slack` | flag | off | Thread a results summary onto the deployment's Slack message after each run (best-effort). |

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

Arguments are identical to `prod continuous-downloader deploy` apart from the omitted `--sleep-interval-secs`, `--influx-retention-days` and `--log-retention-days` (a one-shot run needs no retention timers). The deployment row carries `deploy_args.kind = "prod-downloaders"`.

**Provider selection.** Like the continuous variant, the client VM and the monitoring VM pick their providers independently:

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--provider` | `digitalocean\|hetzner\|vultr\|ovh` | `digitalocean` | Cloud provider for the downloader VM. Needs that provider's credentials in the environment (`HCLOUD_TOKEN`, `VULTR_API_KEY`, the four `OVH_*` vars) |
| `--monitoring-provider` | `digitalocean\|ovh` | `digitalocean` | Provider for the paired monitoring VM — only these two have monitoring Terraform modules |
| `--monitoring-region` | string | `lon1` (DO) / provider default (OVH) | Override the monitoring VM region |
| `--monitoring-droplet-size` | string | `s-2vcpu-4gb` (DO) / provider default (OVH) | Override the monitoring VM size |
| `--monitoring-volume-type` | string | `high-speed` | Monitoring VM volume type (OVH only) |

Destroy reads both providers back from S3 state, so no provider flags are needed on teardown — but OVH's credentials must still be present in the environment at destroy time.

**Per-request download diagnostics.**

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--download-diagnostics` / `--no-download-diagnostics` | flag | off | Pass `--download-diagnostics <PATH>` to each `ant file download`, writing one JSONL record per chunk-fetch attempt |

Default off, because the flag only exists on instrumented `ant` builds — a stock binary rejects it and every download would fail. When on, each download gets its **own** file at `/var/log/ant-prod-downloader/<op>.diag.jsonl`, beside that operation's `.stdout`/`.stderr`. The path is per-operation because `ant` opens it with truncate: a single shared path across a multi-address run would leave only the last address's records. The existing `--log-retention-days` prune timer covers these files already (it deletes everything under the log dir by age, regardless of extension).

Both options are also available on `prod continuous-downloader deploy` and as manifest keys (`provider`, `monitoring_provider`, `monitoring_region`, `monitoring_droplet_size`, `monitoring_volume_type`, `download_diagnostics`) under `prod_downloaders:` / `prod_continuous_downloader:`.

**Inventory.** `deployments up` generates one automatically for both download kinds, as the step after the deploy and before `slack-post`. It is best-effort: a snapshot failure warns rather than halting a run whose VMs are already up. Run it by hand against an existing deployment with `deployments generate-inventory --name <name>`, which writes `~/.autonomi/testnets/<name>/inventory.json` (override with `--output`):

```bash
uv run saorsa-deploy deployments generate-inventory --name PROD-DL-OS-01 --ssh-key-path ~/.ssh/ant_deploy
```

These kinds keep no deployment state of their own — they join production rather than standing up nodes — so the inventory is built from the S3 client/monitoring state plus the registry row. `node_vms` and `bootstrap_vms` are empty (listing production's peers as our VMs would mislead anything that tried to SSH into them) and no devnet manifest is written, but `client_vms` carries the VM's IP, provider, region and SSH details, `observability_vms` carries the InfluxDB/Grafana/Elasticsearch VMs, and a `client_config` section (schema 5) describes the run:

| Field | Description |
|-------|-------------|
| `kind` / `one_shot` | Which download kind, and whether it exits after one pass |
| `addresses` / `address_count` / `addresses_file` | The exact address list the run walks |
| `binary` | The resolved `ant` source (release version, or branch + owner) |
| `spec` / `provider` / `monitoring_provider` | Where it runs |
| `settings` | `bootstrap_cache`, `download_diagnostics`, `memory_profiling`, `forward_logs`, `rust_log`, `clock_skew_seconds`, `network_profile`, `sleep_interval_secs` |
| `paths` | On-VM service name, binary, env file, download dir, log dir, and globs for the `.stdout` / `.stderr` / `.diag.jsonl` / heaptrack artefacts |

The `paths` block matters at teardown: nothing collects the per-op logs or the diagnostics JSONL automatically and they die with the VM, so `scp` them off first.

The address list is only recorded in the registry, so `client_config` needs a reachable `DATABASE_URL`. Without one the command still writes the VM and observability sections and warns that the configuration is missing, rather than failing.

Teardown:

```bash
uv run saorsa-deploy prod downloaders destroy --name PROD-DL-OS-01
# add --keep-monitoring to preserve the InfluxDB VM
```

### Upgrading the `ant` binary in place (`deployments upgrade-ant-binary`)

`deployments upgrade-ant-binary` force-installs the latest (or a pinned/branch) `ant` binary on an already-running `prod-downloaders` or `prod-continuous-downloader` client VM over SSH, and restarts the service — no Terraform infrastructure is touched. It exists because the binary-install step used by `deploy` is presence-gated (it skips if `ant` is already installed at `/root/.local/bin/ant`), so re-running `deploy` against an existing deployment never picks up a new release.

```bash
uv run saorsa-deploy deployments upgrade-ant-binary --name PROD-DL-01
# pin a specific release instead of latest:
uv run saorsa-deploy deployments upgrade-ant-binary --name PROD-DL-01 --ant-version 0.3.1
# use a custom branch build:
uv run saorsa-deploy deployments upgrade-ant-binary --name PROD-DL-01 --branch-name my-fix --repo-owner myuser
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--name` | string | required | Deployment name (its S3 client state supplies the VM IP(s)) |
| `--branch-name` | string | none | Use a custom-built `ant` binary from this branch (requires `--repo-owner`) |
| `--repo-owner` | string | none | GitHub repo owner for the custom-built binary |
| `--ant-version` | string | none (latest) | Pin a specific `ant` release version instead of resolving `latest` |
| `--ssh-key-path` | string | `~/.ssh/id_rsa` | SSH key used to reach the VM(s) |
| `--restart` / `--no-restart` | flag | `--restart` | Restart `ant-prod-downloader` after the binary swap |

Only `prod-downloaders` and `prod-continuous-downloader` deployments are supported (kind is validated via the registry when `DATABASE_URL` is set; the command still runs without it, skipping validation). The `ant_binary_upgrade_started` / `_completed` / `_failed` events are recorded for the deployment, and a successful run updates `deploy_args.ant_binary` so `deployments slack-post` reflects the new binary.

**`--no-restart` note**: the `prod_downloader.py` service has no checkpoint/resume state — every start walks its full address list from the beginning. For `prod-continuous-downloader` this is harmless (it loops forever anyway), but restarting a `prod-downloaders` (one-shot) deployment that has already completed its single pass re-runs the entire address list, duplicating Influx points and any already-recorded results. Pass `--no-restart` for a completed one-shot deployment and restart the service manually only when you actually want another full pass.

### Managing the production file pool (`prod pool`)

Production uploads must use **real files**, and a file must **never be uploaded
twice** — a canary measuring chunk loss on content that has already been on the
network for months is measuring nothing. The pool is the catalogue those files
come from and the ledger of what has been handed out.

Two kinds of entry live in one catalogue:

| Kind | What it is | Who can use it |
|------|-----------|----------------|
| `url` | a public URL | `prod uploaders`, `prod canary` (a VM fetches it at runtime) |
| `local` | an absolute path on this machine | `prod local-uploads`, the desktop application |

**The consumer chooses the kind, never a flag.** A remote uploader VM cannot
read a path on your laptop, so there is deliberately no way to write a manifest
that asks it to.

#### Size classes

Selection is by size class, derived from the file's byte count — never stored,
so a boundary change reclassifies the whole catalogue at once.

| Class | Range (binary, half-open) |
|-------|---------------------------|
| `tiny` | 0 – 10 MiB |
| `small` | 10 – 100 MiB |
| `medium` | 100 MiB – 1 GiB |
| `large` | 1 – 5 GiB |
| `xlarge` | 5 GiB+ |

Exactly 10 MiB is `small`. Note a file of 1,045,869,721 bytes reads as "1.05 GB"
decimal but is 997 MiB, so it is `medium`.

These are **not** the `file_size_buckets_mb` in
`resources/gates/staging-thresholds.yaml`, which bucket *transfer durations* for
the staging gates and are recalibrated quarterly. Do not wire one into the other.

#### The states of a file

| State | Meaning | Drawable again? |
|-------|---------|-----------------|
| `available` | never handed out | yes |
| `claimed` | handed out, not confirmed on the network | no |
| `uploaded` | confirmed, with a datamap address recorded | no, ever |
| `partial` | an upload stored some chunks and stopped | **yes, except by canaries** |

`claimed` is **not** evidence that anything reached production. For a
deployment-backed claim it usually means "uploaded but `record-results` has not
run yet"; for a manual checkout it means you still have to say what happened.

`partial` is the awkward middle. The file is not on the network in full, so it
goes back in the pool — completing it is useful work, and re-uploading missing
chunks is a real test case. But chunks of it **are** in production, sometimes
for months, so it is not untouched either. That distinction is why it is a
status of its own rather than a `released` claim, and the claim keeps the chunk
counts (`342 of 400 stored`) rather than only recording that it failed.

**Canary selection excludes partials by construction** — not via a flag, so no
manifest can opt into it. A loss measurement over content that is already partly
stored is measuring something other than what it reports, and would produce a
number that looks credible and is not.

#### Commands

| Command | What it does |
|---------|--------------|
| `prod pool add --url/--urls-file/--dir/--path` | Catalogue files. URLs are size-probed; local files are hashed SHA1+SHA256. `--dir` batches its database work, so a directory of thousands takes seconds rather than minutes. `--verbose` prints a line per file instead of a progress bar. |
| `prod pool add --from-listing internet-archive:<item> [--size CLASS] [--min-size SIZE] [--max-size SIZE] [--limit N]` | Bulk-catalogue a public collection. Entries arrive pre-sized and pre-hashed from the source's metadata — nothing is downloaded. `--min-size`/`--max-size` express what a class cannot, e.g. `--size tiny --min-size 5MB`. |
| `prod pool list [--status available\|claimed\|uploaded\|partial] [--kind] [--size] [--min-size] [--max-size] [--tag] [--since] [--limit] [--all]` | What is in the pool and what state it is in. `--min-size`/`--max-size` are the same bounds a manifest applies. |
| `prod pool show <file> \| --address <addr>` | One file in full, including its upload result and costs. Reverse lookup by datamap address. |
| `prod pool stats` | Availability per size class, split by kind. |
| `prod pool checkout --size CLASS [--count N] [--kind] [--consumer LABEL] [--reuse-open]` | Take a file by hand, for the desktop application or anything else. |
| `prod pool claims [--consumer] [--source] [--stale AGE] [--unresolvable] [--all]` | What is checked out. Open claims by default. |
| `prod pool history <file>` | Every claim ever made against one file. |
| `prod pool mark-uploaded <file> --address <addr> [--chunks-stored …] [--storage-cost-atto …]` | Record that a checked-out file reached the network. |
| `prod pool release <file>…` | Give claimed file(s) back. |
| `prod pool verify [--kind] [--limit] [--no-rehash]` | Re-check entries: rotted URLs, moved files, changed sizes. |
| `prod pool import-history [--dry-run]` | Mark files already on the network as spent, from **both** upload tables. Partially-uploaded history lands as `partial`, not as spent. |

Everything is addressed by **file id**, never claim id — a file carries at most
one blocking claim, so a claim id is an implementation detail.

Tags are repeatable on both `add` and `list` (`--tag prc --tag video`), stored
lower-cased in an indexed side table, and `list --tag a --tag b` matches **any**
of them, not all.

#### The desktop-application flow

Nothing in a GUI application can write to this database, so the **checkout** is
what spends the file, not the upload:

```bash
saorsa-deploy prod pool checkout --size small --count 1 --consumer desktop-app
# … upload it in the app …
saorsa-deploy prod pool mark-uploaded 118 --address <datamap-address>
# or, if it did not happen:
saorsa-deploy prod pool release 118
```

Come back later without the id? `prod pool claims` lists everything open, and
`--consumer desktop-app` narrows it to yours.

#### Drawing from the pool in a deploy

| Kind | Manifest | CLI |
|------|----------|-----|
| `prod-uploaders` | `pool: {small: 3, medium: 2}` per uploader, exclusive with `urls` | `--uploader lon1:s-4vcpu-8gb:pool=small:3,medium:2` |
| `prod-canary` | `pool: {…}`, exclusive with `urls_file` | `--pool tiny:40,small:230,medium:110` |
| `prod-local-uploads` | `pool: {…}`, exclusive with `urls`/`local_dir` | `--from-pool tiny:10` |

```yaml
kind: prod-uploaders
name: PROD-UL-05
binary:
  ant:
    version: 0.5.2
prod_uploaders:
  uploaders:
    - region: lon1
      vm_size: s-4vcpu-8gb
      concurrency: 4
      pool:
        small: 3
        medium: 2
```

Four things this guarantees:

* **Selection runs before any infrastructure.** A shortfall aborts with no VMs
  created and no mainnet spend, and the error names which inventory fell short —
  plenty of large *local* files are no help to a remote uploader.
* **Selection is all-or-nothing.** A request for 40 tiny + 230 small + 110
  medium that runs short on medium claims *nothing*, rather than burning 270
  files on a run that then aborts.
* **`up --resume` reuses the same files.** The selection is persisted to S3, so
  a retried deploy does not draw a fresh set while the first sits claimed.
* **The duplicate preflight becomes redundant.** `--allow-duplicate-files` and
  the name-based check still apply to the legacy `urls=` path, but a pool
  selection cannot return a spent file by construction.

#### Narrowing a request by byte size

A size class is sometimes too coarse. The canary is the case that forced this:
it measures chunk loss, the **chunk floor is 4**, so a 14 KB file buys exactly
the same minimum observation as a 5 MB one while costing the same single,
permanently-spent pool entry. "`tiny`, but at least 5 MB" is not a class.

Every pool consumer therefore accepts **`pool_min_size`** and
**`pool_max_size`**, which *narrow* each requested class without touching the
counts — so the shape of a selection (37 tiny / 233 small / 112 medium) is
preserved.

| Kind | Manifest | CLI |
|------|----------|-----|
| `prod-uploaders` | `pool_min_size: 5MB` per uploader | spec tokens `:pool_min_size=5MB:pool_max_size=1GB` |
| `prod-canary` | `pool_min_size: 5MB` | `--pool-min-size 5MB --pool-max-size 1GB` |
| `prod-local-uploads` | `pool_min_size: 5MB` | `--pool-min-size 5MB --pool-max-size 1GB` |

```yaml
prod_canary:
  pool:
    tiny: 37
    small: 233
    medium: 112
  pool_min_size: 5MB       # keeps the sub-5MB bottom of `tiny` out
```

* **Units are binary and a bare number is bytes** — `5MB` is 5 MiB, matching how
  every size in this tool is displayed, so a bound pasted back from output
  selects the file it was read from. `1.5GB`, `512KB` and `5242880` all work.
* **`min` is inclusive, `max` exclusive**, matching the half-open class
  boundaries. `pool_min_size: 10MB` with `pool_max_size: 100MB` is therefore
  *exactly* the `small` class, not almost.
* **Bounds that exclude a requested class are refused at validate time** —
  `pool_max_size: 5MB` alongside `small: 3` errors immediately rather than
  surfacing later as "0 available", which reads as an empty pool and sends you
  cataloguing files that would not have been selected either.
* **A shortfall names the bound.** "2 available" without it reads as a
  whole-class figure.

The same bounds work on `prod pool list --min-size/--max-size`, so inventory can
be checked against the exact selection a run will make, and on `prod pool add
--from-listing --min-size/--max-size` when sourcing for it.

#### Keeping the pool stocked

A canary run consumes roughly **350 distinct files, permanently**, once per
release train — measured at 382 / 311 / 313 across the three runs to date,
concentrated in `small` and `medium` with none at all in `large`/`xlarge`. Hand
curation cannot feed that, which is what `--from-listing` is for:

```bash
saorsa-deploy prod pool add --from-listing internet-archive:<item> --size small --limit 250
saorsa-deploy prod pool stats
```

Internet Archive item metadata publishes name, size, MD5 and SHA1 for every
file, so hundreds of entries are catalogued in seconds without downloading
anything — and those real checksums are what let an IA URL and a local copy of
the same file be recognised as one piece of content, so the pool cannot hand out
both.

#### Two honest limits

**`import-history` matches on file name.** That is all the recorded history
holds — no digests, and no way to add them now. A false match costs you a pool
entry (undone with `release`); a missed match re-uploads content already on the
network. The direction of that error is deliberate.

**A URL from a source publishing no checksum, which you never download, cannot
be deduplicated before upload.** `verify` reports how many entries are in that
state rather than implying a coverage the tool does not have. The datamap
address catches it afterwards; `pool add --materialise` avoids it entirely by
downloading once and cataloguing a single local entry.

#### Before you tear a run down

Remote upload results live in the deployment's InfluxDB until
`deployments record-results` pulls them into Postgres — and the teardown
destroys that VM. `prod uploaders destroy` (and `prod canary destroy
--no-keep-monitoring`) therefore check first and warn loudly if results are
missing, comparing recorded rows against the deployment's pool claims, which say
exactly how many files the run took. It **warns rather than blocks**: refusing on
a non-TTY would strand billable infrastructure when `down` runs unattended.
`--skip-results-check` silences it.

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

### Running crash-debug nodes against production (`prod debug-nodes`)

`prod debug-nodes deploy` stands up a small, isolated, **permanent** set of ant-node services that join the **production** network configured for panic/crash debugging — the sibling of `prod nodes` (which is heaptrack memory profiling). Each service runs with `RUST_BACKTRACE` **and `COLORBT_SHOW_HIDDEN=1`** (ant-node's color-backtrace filters every frame otherwise, printing `<empty backtrace>`), core dumps enabled (with `gdb` + `elfutils`/`eu-stack` installed on the VM to symbolise the file cores in place), and DEBUG-level logs forwarded to a **dedicated** short-retention Elasticsearch, plus a **dedicated** small InfluxDB for host/RSS metrics. None of it touches the main prod monitoring stack. Registers a `Deployment` row with `deploy_args.kind = "prod-debug-nodes"`.

> **Note:** the release ant-node binary is stripped, so gdb/eu-stack give limited symbols against the file cores — for a fully symbolised backtrace pull the core to a box with an *unstripped* build of the same version. The higher-value signal is usually the live backtrace: with `COLORBT_SHOW_HIDDEN=1` + `RUST_BACKTRACE=full`, the next panic prints real frames straight to the journal/ES, no core needed. `systemd-coredump` is intentionally **not** installed (it would either be inert against the file-based `core_pattern` or force cores off the data volume, breaking the retention timer).

```bash
# Contabo VDS node, DEBUG logs + backtraces + core dumps, dedicated small ES + InfluxDB.
saorsa-deploy prod debug-nodes deploy \
  --name DEBUG-NODE-01 \
  --purpose "permanent prod crash capture" \
  --provider contabo --region EU --vm-size V10 \
  --contabo-image-id <ubuntu-24.04-uuid> \
  --branch-name main --repo-owner WithAutonomi --debug \
  --ssh-key-path ~/.ssh/ant_deploy
```

| Argument | Required | Description |
|---|---|---|
| `--name NAME` | yes | Deployment name (also used for the paired monitoring/ES stacks). |
| `--purpose TEXT` | yes | Free-text description; persisted to `deploy_args`. |
| `--provider {digitalocean,hetzner,vultr,ovh,contabo}` | no | Node VM provider (default digitalocean). |
| `--region REGION` | yes | Node region (e.g. `lon1`, `fsn1`, `GRA9`, `EU`). |
| `--vm-size SIZE` | no | Node VM size/plan/flavor/product-id (per-provider default; Contabo default `V10` VDS). |
| `--vm-count N` / `--node-count N` | no | VMs per region (default 1) / ant-node services per VM (default **4** — deliberately not the prod fleet's 15; see "Log volume is forwarder-bound" below). |
| `--volume-size GB` | no | Attached data volume (default 100). **Ignored on Contabo** (root disk only). |
| `--log-level {info,debug,trace}` | no | Single global level (default `debug`). ant-node has no per-module env filter, so per-module `RUST_LOG` targeting is not possible. |
| `--rust-backtrace VAL` | no | `RUST_BACKTRACE` on each service (default `full`; `1` for a terser trace, or `""` to disable). Services also always get `COLORBT_SHOW_HIDDEN=1`. |
| `--coredumps` / `--no-coredumps` | no | Enable core dumps (`LimitCORE=infinity` + `kernel.core_pattern` under the data path) **and install `gdb` + `elfutils` on the VM to read them**. Default on. |
| `--coredump-retention-days N` | no | Daily prune of cores older than N days (default 7; 0 = keep forever). |
| `--log-retention-days N` | no | Daily prune of rotated ant-node debug logs under `/mnt/ant-node-storage/log` older than N days (default **2**; 0 = keep forever). Also runs once immediately on install for prompt relief on a full disk. The prune **restarts the ES log forwarder** — see below. |
| `--heaptrack` / `--no-heaptrack` | no | Also wrap services in heaptrack (default off — use `prod nodes` for that). |
| `--monitoring` / `--no-monitoring` | no | Dedicated InfluxDB (default on). `--monitoring-provider {digitalocean,ovh,contabo}`, `--monitoring-region`, `--monitoring-droplet-size`, `--monitoring-volume-size`, `--grafana`, `--influx-retention-days` (default 7). |
| `--forward-logs` / `--no-forward-logs` | no | Dedicated Elasticsearch for node logs (default on). `--elastic-provider {digitalocean,ovh,contabo}`, `--elastic-region`, `--elastic-droplet-size`, `--elastic-volume-size`, `--elastic-retention-days` (default 3), `--elastic-shards` (default 1), `--elastic-drop-log-levels`. |
| `--branch-name` + `--repo-owner` / `--node-version` | no | Custom ant-node branch build (build first with `build ant-node --debug`) / pinned release. `--debug` records the branch build as a debug build. |
| `--contabo-image-id UUID` | when any Contabo tier | Ubuntu image UUID (resolve via `cntb get images`). |
| `--contabo-ssh-public-key-path PATH` | no | Operator public key registered as a `contabo_secret` (default `~/.ssh/id_rsa.pub`). |
| `--rewards-address` / `--port` / `--ssh-key-path` | no | Prod rewards address / initial listen port (default 10000) / SSH key. |

**The log prune must restart the log forwarder.** Telegraf's `tail` input holds an
open descriptor for every file it matches and has no idle-close, so a plain
`find … -delete` only removes the directory entry: the file leaves `du` while
ext4 keeps the blocks, and `df` climbs to 100% while the visible tree looks
small. Measured on ant-prod-debug-01 (2026-08-02): **694 GiB across 70
descriptors** on a 968 GB root disk reporting **0 bytes free** with only 271 GB
visible. The `ant-node-debug-logprune.service` unit therefore carries a second
`ExecStart` — `-/usr/bin/systemctl try-restart telegraf-ant-elasticsearch.service`
— ordered after the `find` (the unlink must land first; the restart is what
frees the blocks). It is `try-restart` so it no-ops under `--no-forward-logs`,
and `-`-prefixed so a forwarder that fails to return cannot fail the prune. It
is unconditional rather than gated on the `find` having deleted anything,
because ant-node's own `--log-max-files` self-pruning strands descriptors the
same way. Consequently this kind provisions the forwarder with
`from_beginning = false`: telegraf keeps no tail offset across restarts, so
reading from the beginning would re-ingest every remaining log on every prune.
**Any new prune timer that deletes files something else is tailing needs the
same treatment.**

**Log volume is forwarder-bound — Elasticsearch is a sample, not a copy.** At
`--log-level debug` each ant-node service emits roughly **758 log lines/s**
(222 bytes average), and a single telegraf forwarder tops out near **2 700
lines/s** — it is CPU-bound in the JSON parse plus the `rename`/`starlark`/
`strings` processor chain, not blocked on Elasticsearch (measured from the node
VM, ES accepted ~8 000 docs/s at telegraf's own batch size, with 0 write
rejections and 13% CPU). So `--node-count` sets how much of your debug logging
survives: at 10 services only ~36% of lines reached ES, and the forwarder had
previously wedged for three days (190M docs/day → 5 684). The default of **4**
keeps production under that ceiling. Raise it only if you accept that ES holds a
partial sample and the **on-disk logs are the authoritative record** — which
`--log-retention-days` then discards, so raise that too.

`prod debug-nodes destroy --name <name>` tears down the node VMs and, by default, the paired monitoring + Elasticsearch stacks (`--keep-monitoring` / `--keep-elastic` to retain them). `scp root@<vm>:/mnt/ant-node-storage/coredumps/core.*` off the box before destroy — and, if the run mattered, the debug logs under `/mnt/ant-node-storage/log` too: Elasticsearch is only a partial copy of them (see above).

Fully declarative via `deployments up/plan/down --config` (`kind: prod-debug-nodes`); example manifest at `manifests/profiles/prod-debug-nodes.yaml`. `up` is resumable, so a retry skips the ~10-minute debug build.

**Contabo provider.** Contabo (nodes + monitoring/ES) authenticates via `CNTB_OAUTH2_CLIENT_ID` / `CNTB_OAUTH2_CLIENT_SECRET` / `CNTB_OAUTH2_USER` (account email) / `CNTB_OAUTH2_PASS` (API password) — the `contabo/contabo` Terraform provider reads them from the environment. Contabo has **no attachable block volume** (root-disk only, sized up front, not growable online) and bills on **committed monthly contracts** (no hourly billing / proration), which suits permanent debug nodes. Images are referenced by UUID (`--contabo-image-id`).

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

### Reclaiming ext4 reserved space (`prod set-volume-reserve`)

ext4 reserves **5% of a filesystem's blocks for root by default**. On a large
node data volume that is many GB the node can never use — the volume reports
`100%` full (`Avail 0`) while the raw device still has headroom (e.g. `378G` used
of a `394G` device). Because these volumes hold only node data (never the OS
root), the reserve can safely drop to **1%**, reclaiming ~4%.

**Future volumes** get the 1% reserve automatically: it is applied with
`tune2fs -m 1` at format time in the volume-mount and striped-volume
provisioners (`provisioning/volume.py`), so any newly-created data volume — on
any block-volume provider — starts at 1%.

**Existing fleets** are retrofitted with `prod set-volume-reserve`, which reads a
`generate-inventory` inventory and, per node VM, SSHes in and runs `tune2fs -m`
on whichever device backs `/mnt/ant-node-storage`. The device is autodetected
via `findmnt` (works for both raw-ext4 and LVM-striped nodes) — nothing is
hardcoded. `tune2fs` runs against the live mounted filesystem, so **no ant-node
service is stopped**. Root-disk providers (Contabo, OVH 3-AZ) keep node data on
the OS root filesystem and are **skipped** so that filesystem's reserve is never
touched.

```bash
# Dry-run against a single VM first (interactive confirm unless -y)
saorsa-deploy prod set-volume-reserve \
  --inventory-path ~/.autonomi/testnets/ant-prod-01/inventory.json \
  --limit-to ant-prod-01-hz-ash-1

# Retrofit the whole fleet, continuing past any per-VM failure
saorsa-deploy prod set-volume-reserve \
  --inventory-path ~/.autonomi/testnets/ant-prod-01/inventory.json \
  --keep-going -y
```

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--inventory-path` | yes | — | `generate-inventory` JSON (schema_version 3, 4 or 5). |
| `--reserve-percent` | no | `1` | ext4 reserved-block percentage to set. |
| `--provider` | no | all | Restrict to one provider (`aws`/`digitalocean`/`hetzner`/`ovh`/`vultr`/`contabo`). |
| `--limit-to` | no | all | Comma-separated VM names to restrict the run to. |
| `--ssh-key-path` | no | per-VM | Override the SSH key for every VM. |
| `--keep-going` | no | off | Continue past per-VM failures instead of stopping. |
| `-y`, `--yes` | no | off | Skip the interactive confirmation. |

Every step is idempotent — re-running once a volume is already at 1% is a no-op.
The command targets **node VMs only** (like `prod extend-unmanaged-volumes`);
bootstrap VMs are not covered.

### Managing individual node services (`prod services`)

A node VM can run into its storage quota with no headroom left, while a
cloud-side volume-size increase needs human approval that can take 24-48h
(e.g. Hetzner). In the meantime the only way to free disk space is to remove
one `ant-node-N` service; once the quota increase lands, a matching service
can be added back. `prod services ls/rm/add` does exactly that, against a
single named host in an inventory — it is a maintenance tool (no manifest, no
DB/orchestrator wiring) and works against **any** host in a given inventory,
not just prod fleets.

```bash
# See what's running and how much disk each service is using
saorsa-deploy prod services ls \
  --inventory-path ~/.autonomi/testnets/ant-prod-01/inventory.json \
  --name ant-prod-01-hz-ash-1

# Free space by removing the biggest one
saorsa-deploy prod services rm \
  --inventory-path ~/.autonomi/testnets/ant-prod-01/inventory.json \
  --name ant-prod-01-hz-ash-1 \
  --service-name ant-node-7

# ...once the volume-size increase lands, add it back
saorsa-deploy prod services add \
  --inventory-path ~/.autonomi/testnets/ant-prod-01/inventory.json \
  --name ant-prod-01-hz-ash-1 \
  --service-name ant-node-7
```

| Argument | Required | Default | Applies to | Description |
|----------|----------|---------|------------|-------------|
| `--inventory-path` | yes | — | all | Inventory JSON (schema_version 3, 4 or 5) containing the host. |
| `--name` | yes | — | all | Host name, matched against the inventory's `name` field. |
| `--service-name` | `ls`: no / `rm`, `add`: yes | `ls`: list all | all | `ant-node-<N>`. On `ls`, restricts output to one service; omit to list every service on the host. |
| `--ssh-key-path` | no | host's inventory `ssh_key` | all | Override the SSH key for this one host. |

Services are discovered by globbing `ant-node-*` unit files on the host —
never by trusting `--node-count` or the inventory's `services` list, both of
which can drift after a prior `rm`/`add`.

- **`ls`** reports each service's `systemctl is-active`/`is-enabled` state and
  its data directory's size (`du -sb` on `/mnt/ant-node-storage/data/<service>`).
- **`rm`** stops, disables, and deletes the unit file plus its data AND log
  directories — every command is scoped to the exact targeted service name
  (never a glob), so sibling services on the same host are never touched.
- **`add`** clones its config from the lowest-indexed **active** sibling
  service already on the host: binary path, EVM network, NAT wrapping
  (`ip netns exec ant_nat`), log level, bootstrap peers, restart policy,
  `RUST_BACKTRACE`/`COLORBT_SHOW_HIDDEN` env, `LimitCORE` (coredumps),
  upgrade channel, and rewards address are all read back from the sibling's
  live unit file and reproduced exactly. The node port and metrics port (if
  the sibling has them) are derived by offsetting the sibling's values by the
  difference between the two service indices, matching how
  `nodes provision` assigns them (`base + index`) in the first place. The new
  service is written, enabled, **and started** — `add` brings a working node
  back online, not just an enabled-but-stopped one. Fails clearly if the host
  has no active sibling to clone from, or if the target service name already
  exists. heaptrack wrapping (the `prod nodes` kind) is **not** cloned — a
  heaptrack-wrapped sibling yields a new service without heaptrack.

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

### Running profiling nodes against production (`prod nodes`)

`prod nodes deploy` stands up node VM(s) — each running `node_count` ant-node
services (default **15**, matching the prod fleet) — that join the **production**
Autonomi network, with every service wrapped in **heaptrack** for memory
profiling. It is built for chasing the intermittent ant-node memory leak under
real production load with a **debug build** (full debug info).

It reuses the whole node lifecycle rather than the migration ceremony: the
command seeds the deployment's S3 state with the production bootstrap peers
(fetched from the WithAutonomi `bootstrap_peers.toml`), then drives the existing
`nodes infra` (multi-provider VM + data-volume creation) and `nodes start`; the
provisioning pass is prod-mode (no `--evm-network` → arbitrum-one,
`Restart=always`, `LimitNOFILE=65536`, `--stop-on-upgrade` with **no** upgrade
channel so the pinned debug build keeps running) and folds in heaptrack plus,
optionally, InfluxDB host-metric forwarding.

heaptrack traces are written **per service** to the attached data volume
(`/mnt/ant-node-storage/heaptrack/ant-node-<i>.<pid>.zst`), not the root disk —
15 debug nodes generate large, growing traces. heaptrack writes incrementally,
so even an OOM/SIGKILL leaves an analysable trace.

#### Build the debug binary first

A debug build is produced by the separate `build ant-node --debug` step (drops
`--release`; artifact from `target/debug`). With a manifest the orchestrator adds
this step automatically from `binary.debug: true`; from the CLI, run it yourself
against the **same** `--name`:

```bash
saorsa-deploy build ant-node --name PROF-NODE-01 \
  --branch-name main --repo-owner WithAutonomi --debug
```

#### Deploy

```bash
saorsa-deploy prod nodes deploy \
  --name PROF-NODE-01 \
  --provider hetzner --region fsn1 --vm-size cpx42 \
  --node-count 15 \
  --branch-name main --repo-owner WithAutonomi --debug \
  --monitoring \
  --purpose "ant-node leak profiling"
```

| Flag | Required | Default | Notes |
|------|----------|---------|-------|
| `--name` | yes | — | Deployment name; also names the optional paired monitoring stack. |
| `--provider` | no | `digitalocean` | `digitalocean` \| `hetzner` \| `vultr` \| `ovh`. Reuses the node TF modules. Non-DO is cheaper for prod egress; Vultr/OVH have block-storage caveats. |
| `--region` | yes | — | Provider region for the node VM(s). |
| `--vm-size` | no | per-provider (DO `s-4vcpu-8gb`, Hetzner `cpx42`, Vultr `vhf-4c-16gb`, OVH `b3-16`) | VM size/plan/flavor. |
| `--vm-count` | no | `1` | Node VMs to create in the region. |
| `--node-count` | no | `15` | ant-node services per VM. |
| `--volume-size` | no | `100` | Attached data-volume GB; node data + heaptrack traces live here. |
| `--heaptrack` / `--no-heaptrack` | no | **on** | Wrap every service with heaptrack. On is the whole point; `--no-heaptrack` runs plain prod nodes. |
| `--monitoring` / `--no-monitoring` | no | off | Pair an InfluxDB VM and forward host/per-service RSS metrics (bucket `testnet-metrics`). |
| `--grafana` / `--no-grafana` | no | off | With `--monitoring`, also create a Grafana VM. |
| `--monitoring-volume-size` | no | `50` | Monitoring VM volume size in GB. |
| `--log-retention-days` | no | `7` | Daily timer deletes traces older than N days; `0` keeps forever. |
| `--rewards-address` | no | standard prod address | EVM rewards address for the node services. |
| `--log-level` | no | ant-node default | Single global level (`info`/`debug`/`trace`); ant-node has no per-module filter. |
| `--port` | no | `10000` | Initial listen port; services use `port..port+node_count-1`. |
| `--branch-name` + `--repo-owner` | no | latest release | Custom ant-node build (built first via `build ant-node`). |
| `--node-version` | no | — | Pinned release instead (mutually exclusive with branch). Releases ship optimised — no debug builds. |
| `--debug` | no | off | Informational: record the binary as a debug build for `slack-post`. The real build is `build ant-node --debug`. |
| `--purpose` | yes | — | Free-text description, persisted in `deploy_args.purpose`. |

Sequencing under one command: seed prod bootstraps → `nodes infra` → (optional
`monitoring infra`/`provision`) → prod-mode `nodes provision` + heaptrack →
`nodes start` → install the trace-prune timer. The Deployment row is registered
with `deploy_args.kind = "prod-nodes"`.

#### Retrieve traces + destroy

```bash
# Pull the heaptrack traces off before tearing down:
scp root@<vm>:/mnt/ant-node-storage/heaptrack/ant-node-*.zst ./traces/

# Default: also destroys the paired monitoring stack.
saorsa-deploy prod nodes destroy --name PROF-NODE-01
# Keep monitoring for after-the-fact analysis:
saorsa-deploy prod nodes destroy --name PROF-NODE-01 --keep-monitoring
```

#### Manifest

Fully declarative via `deployments up/plan/down --config` (`kind: prod-nodes`).
Because `up` is resumable, a re-run after a mid-sequence failure skips the
already-done ~10-minute debug build. See
`manifests/profiles/prod-nodes.yaml`.

### Simulating a residential connection (`network`)

Client VMs sit on ~gigabit cloud links, which is not how real users reach the
network — and upstream rate, the thing that dominates an upload, is where a home
line differs most (a cloud VM uploads at roughly a gigabit; UK FTTC manages 18
Mbit, ADSL 1 Mbit). Any client VM can therefore be shaped with `tc` to behave
like a residential connection, selected by a named profile so runs are
reproducible and comparable across trains.

Supported on `prod uploaders`, `prod downloaders`, `prod continuous-downloader`
(the primary use case) and on testnet/extension `clients.uploaders[]` /
`clients.downloaders[]` (secondary). Not wired to `prod canary` or the `local-*`
kinds. Omitting it leaves a deployment byte-identical to one built before the
feature existed.

#### Profiles

Shipped in `saorsa_deploy/resources/network/profiles.yaml`, git-versioned with a
`version:` that is echoed into every recorded run — bump it when a profile's
numbers change, or two runs labelled `home-fttc` will mean different things.

| Profile | Down | Up | Added RTT | Jitter | Loss |
|---|---|---|---|---|---|
| `home-adsl` | 10 Mbit | 1 Mbit | 30 ms | 5 ms | 0.2% |
| `home-calibrated` | 27.4 Mbit | 6.29 Mbit | 12.7 ms | 0.2 ms | 0 |
| `home-fttc` | 60 Mbit | 18 Mbit | 15 ms | 3 ms | 0.05% |
| `home-fttp` | 300 Mbit | 50 Mbit | 8 ms | 2 ms | 0.01% |
| `mobile-4g` | 30 Mbit | 8 Mbit | 60 ms | 20 ms | 0.5% |

`home-calibrated` is the only measured profile (a real TalkTalk line, 2026-07-30);
the rest are representative figures. Its loss is recorded as 0 because 0/300
packets were lost — that is *below the resolution of that sample*, not proof of a
lossless line.

**Sizing the run.** Client egress is roughly 4x payload (chunk fan-out), so at
`home-adsl`'s 1 Mbit up a 100 MB upload takes ~67 minutes and 1 GB takes ~11
hours. `home-fttc` is the practical choice for most runs while still being ~50x
slower than the VM's native link.

#### Usage

```yaml
# testnet — per client VM, so one run can span several conditions plus a control
clients:
  uploaders:
    - { provider: digital_ocean, region: nyc1, file_size_mb: 100, network: home-fttc }
    - { provider: digital_ocean, region: sfo3, file_size_mb: 100 }   # unshaped control
  downloaders:
    - { provider: digital_ocean, region: tor1, network: home-adsl }

# prod uploaders — per entry
prod_uploaders:
  uploaders:
    - { region: lon1, vm_size: s-4vcpu-8gb, urls: lists/a.txt, network: home-calibrated }

# prod downloaders / continuous-downloader — spec-level
prod_continuous_downloader:
  spec: fsn1:cpx41
  network: home-fttc
```

An ad-hoc set can be given inline instead of a name (manifest only — the CLI
token takes a bare name, since client specs are colon-delimited):

```yaml
    network: { rate_down: 30mbit, rate_up: 5mbit, delay: 25ms, jitter: 5ms, loss: 0.2 }
```

CLI equivalents:

```
--uploader digital_ocean:nyc1:100:s-2vcpu-4gb:network=home-fttc
--downloader digital_ocean:tor1:network=home-adsl
prod continuous-downloader deploy --network home-fttc ...
prod downloaders deploy --network 'rate_down=30mbit,rate_up=5mbit,delay=25ms'
```

| Field | Meaning |
|---|---|
| `rate_down` / `rate_up` | tc rate tokens (`kbit`/`mbit`). Both required. Asymmetry is the point. |
| `delay` | Added to **round-trip** time, counted once. Applied on egress only, so it is not doubled. This is added latency on top of the VM's own RTT to peers, not an absolute target. |
| `jitter` | Variation around `delay`, normally distributed. |
| `loss` | Percent, applied **per direction**. Inbound loss is what makes a download behave like a home download. |

#### What is exempt, and why

Traffic on TCP ports 22 (SSH), 8086 (InfluxDB) and 9200 (Elasticsearch) bypasses
the shaper. Without that, a shaped VM's own Telegraf metrics and forwarded client
logs would compete with the transfer for the capped bandwidth: the measured
transfer would come out slower than a real user's (no real user ships their own
logs), and the run's results could arrive incomplete — degrading the instrument
being used to take the measurement. The exemption is keyed on port rather than
address because the Elasticsearch VM is created *after* the client VMs are
provisioned, so its address does not exist when shaping is applied.

#### Implementation notes

* An `htb` hierarchy with a shaped default class and an unshaped exempt class;
  `netem` for delay/jitter/loss; `cake` (falling back to `fq_codel`) pinned as the
  AQM at the leaf. Queue depth is deliberately not a user knob — a rate cap always
  implies a queue, and leaving its depth to chance gives either tail-drop collapse
  or bufferbloat that silently swamps the configured `delay`.
* Ingress is shaped via an `ifb` mirror device, because `tc` shapes egress only.
  Without it the downstream cap would not apply to downloads at all.
* Applied by a systemd oneshot unit (`ant-network-impairment.service`), so the
  shaping survives a reboot — `tc` state is otherwise kernel-only. The apply script
  is convergent: re-provisioning replaces the rules rather than stacking them (two
  `netem delay 30ms` rules would be 60 ms).
* Verify on a live VM with `tc qdisc show` and by comparing idle RTT to RTT under
  load; a profile configured for 15 ms that shows 900 ms loaded means the queue is
  misbehaving.

#### Recording

The profile *name* is written to the `network_profile` column on
`deployment_upload_records`, `deployment_download_records`,
`deployment_prod_upload_results` and `deployment_prod_download_results`, so a run's
transfers can be filtered and compared without joining through JSON. The full
resolved knob set (rates, delay, jitter, loss, profile-set version) is stored once
in the deployment's `deploy_args` — that is what keeps a past run interpretable
after the profile file has moved on. NULL means unshaped, which is every row
predating the feature.

**Do not enable this on a staging gate run.** `deployments compare` gates on
transfer p50/p95, and a shaped client's percentiles are not comparable to an
unshaped baseline.

### Running tests

```bash
uv run pytest tests/ -v
```

### Linting and formatting

```bash
uv run ruff format saorsa_deploy/ tests/
uv run ruff check saorsa_deploy/ tests/
```
