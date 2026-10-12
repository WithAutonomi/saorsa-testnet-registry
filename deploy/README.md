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
extends: profiles/staging.yaml         # optional: deep-merge a reusable base (lists REPLACE)
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

#### Host firewall (ufw)

Every node-tier host (`nodes provision-genesis`, `bootstraps provision`,
`nodes provision`) and every client host (`clients provision`) runs a
`Disable host firewall (ufw)` op early in its provisioning pass:

```sh
command -v ufw >/dev/null 2>&1 && ufw disable >/dev/null 2>&1 || true
```

It is there because **Vultr's Ubuntu 24.04 image boots with `ufw` active**,
default-deny on incoming, and exactly one allow rule — `22/tcp`. Everything
else is dropped by netfilter *after* reaching the NIC, which is invisible until
something has to dial in: SSH works, provisioning works, and node-to-node
traffic works because nodes hole-punch outbound. Only direct inbound
connections fail. On run DEV-03 (V2-1410, registry id 632) clients made 6,821
direct QUIC attempts to node ports on the seven Vultr node VMs and **connected
none of them**, while the DigitalOcean VMs on the same fleet accepted tens of
thousands; not one of the 105 Vultr nodes was ever promoted to a `Direct`
address.

**Disable, not allow**: node services listen on OS-assigned high UDP ports (and
MASQUE relay servers on further OS-assigned ports), so an allow-list cannot
name them without changing how ports are assigned. Testnet VMs are disposable
and already exposed by design.

The command is idempotent and persistent (`ufw disable` writes `ENABLED=no` to
`/etc/ufw/ufw.conf`, so it survives a reboot), and a no-op on images with no
`ufw` binary — every other provider's — which is why it is added
unconditionally rather than per provider. It is the same string the NAT
simulation has run since June 2026; both call
`provisioning/firewall.py:build_disable_firewall_command()` so they cannot
drift.

Verify on any provisioned VM:

```bash
ssh root@<VM> "ufw status"          # expect 'Status: inactive' (or no ufw at all)
ssh root@<VM> "iptables -S INPUT"   # expect '-P INPUT ACCEPT'
```

Not covered: the monitoring, Elasticsearch, build and `prod uploaders` /
`prod downloaders` / `prod canary` client VMs, which nothing dials in to.

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

### `nodes swap-binary`

Replace the ant-node binary on every node VM of a **running** testnet and
restart the `ant-node-<i>` services on it. Unit files, ports, data
directories, NAT and clock-skew setup are left exactly as `nodes provision`
wrote them, so the services come back on the new build with the same
`--root-dir` — from the node's point of view the same thing an automatic
upgrade does (`--stop-on-upgrade` exit + `Restart=on-success`), only at a
moment you choose and immune to anyone publishing a release mid-run.

Use it for tests whose subject only exists on a node that already holds data
written by an older build — a storage-format migration, for instance: deploy
on the released version, let the uploaders fill it, then swap to the branch
under test.

```bash
# Phase 0: fleet on the current release
uv run saorsa-deploy nodes provision --name DEV-01 --node-count 15 --node-version 0.17.2
uv run saorsa-deploy nodes start --name DEV-01
# ... later, swap the whole fleet to the branch build (built with `build ant-node`)
uv run saorsa-deploy nodes swap-binary --name DEV-01 \
    --branch-name storage/file-chunk-store --repo-owner WithAutonomi
```

The target must be explicit — `--node-version` or `--branch-name`/`--repo-owner`
— there is no "latest" default. On each host the new binary is downloaded
beside the old one, `--version` is run on it, and only then is it renamed
over `/usr/local/bin/ant-node` (an atomic rename on the same filesystem); a
download that does not run (`ANT_SWAP:BAD_BINARY`) leaves the old binary and
the services untouched. All VMs are swapped in one parallel pass by default;
`--stagger-secs N` does them one at a time, `N` seconds apart. `--no-restart`
replaces the binary but leaves the services on the old one; `--region`
limits the swap to one region and `--hosts <ip,ip,…>` to named node VMs (the
two are mutually exclusive, and an address that is not a node VM of this
deployment is an error, not a silent skip). Bootstraps are not touched.

`--node-config <local.toml>` installs a node config file alongside the swap.
`ant-node --config` is a CLI flag with no environment alias, so the file is
written to `/etc/ant-node/config.toml` on each VM and every `ant-node-<i>`
service gets a systemd drop-in (`ant-node-<i>.service.d/10-node-config.conf`)
that re-declares its `ExecStart` from the main unit with `--config
/etc/ant-node/config.toml` appended. The main unit is never edited; re-running
with a different file regenerates the drop-ins. Every `NodeConfig` field has a
default, so the file only needs the keys being changed, and the CLI flags in
`ExecStart` (`--root-dir`, `--port`, …) still override it. It is applied at
swap time on purpose: the old binary would honour some of the same keys.

```bash
# e.g. a shed-test cohort: zero the migration hold/wave and squeeze the file store
cat > shed.toml <<'EOF'
[storage]
disk_reserve_mb = 95000
[storage.migration]
shed_hold_hours = 0
wave_hours = 0
copier_slack_mb = 0
EOF
uv run saorsa-deploy nodes swap-binary --name DEV-01-SHED \
    --branch-name storage/file-chunk-store --repo-owner WithAutonomi \
    --node-config shed.toml
```

Only TOML validity is checked locally; a key the installed build rejects
shows up as the services failing to come back (`ANT_SWAP:NOT_ACTIVE`), with
the reason in the node's journal. The file's content and SHA-256 are recorded
in the `nodes_binary_swapped` event.

`--hosts` is what makes a **per-VM** config possible: the installed file is one
file per invocation, so a cohort whose config differs per VM — a
`disk_reserve_mb` computed from that VM's own `df`, say — is swapped one host at
a time in a loop, which also makes `--stagger-secs` redundant for it.

```bash
# per-VM config: reserve all but half of each VM's own node payload
for ip in $(cat shed-vms.txt); do
    free_mb=$(ssh -i ~/.ssh/ant_deploy root@$ip "df -m --output=avail /mnt/ant-node-storage | tail -1")
    payload_mb=$(ssh -i ~/.ssh/ant_deploy root@$ip "du -sm /mnt/ant-node-storage/ant-node-* | awk '{s+=\$1} END {print s}'")
    printf '[storage]\ndisk_reserve_mb = %d\n[storage.migration]\nshed_hold_hours = 0\nwave_hours = 1\ncopier_slack_mb = 0\n' \
        $((free_mb - payload_mb / 2)) > shed-$ip.toml
    uv run saorsa-deploy nodes swap-binary --name DEV-01-SHED --hosts "$ip" \
        --branch-name storage/file-chunk-store --repo-owner WithAutonomi \
        --node-config shed-$ip.toml
done
```

A whole-fleet swap records a `nodes_binary_swapped` event on the deployment
and updates its `node_binary` in the registry, so the swap time is on the
run's timeline. A swap narrowed by `--region` or `--hosts` records the event but
deliberately leaves `node_binary` alone: the fleet is then running more than one
build, and recording a single one would mislead every later command that reads
it.

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

When a testnet run carries WASM (browser-path) client operations, the reply's
headline totals split into **`Native successes` / `Native failures` / `WASM
successes` / `WASM failures`** instead of one blended pair. The two transports
are not the same instrument — the WASM tier ran ~2.2x slower on upload and ~4.2x
on download on the first measured run, and it is judged against its own gates —
so a single total describes neither and moves with whatever VM mix the run used.
A `client_kind` of NULL counts as native; with no WASM records the output is
byte-identical to before.

#### `record-results` never erases `client_kind`

The transport of each operation is resolved from the deployment's **S3 client
state**, and `clients destroy` deletes that state. So a `record-results` re-run
after teardown resolves NULL for every host. The upsert therefore **coalesces**
that column: a NULL never overwrites a known transport, while a known transport
still updates.

Without that, re-running after teardown would relabel every WASM operation as
native — which pushes WASM failures into the **native hard floors**, the one
place they must never count. When the client state is unreadable the command
also warns, naming the WASM hosts it could not classify, so the silence is never
mistaken for "these rows are native".

Run `record-results` **before** `clients destroy`, as the process already
requires.

### Client VMs (uploaders / downloaders)

Client VMs run the ant binary as long-running upload-or-download workers against a testnet. Each VM runs *either* an uploader *or* a downloader — not both — and the downloader VMs pull addresses produced by uploaders through a per-testnet S3 folder.

Two further client roles exist and are documented separately: the WASM
(browser-path) pair below, and the **pointer** role (`--pointer` /
`clients.pointers[]`), which exercises paid mutable references rather than file
transfers — see "Pointer client VMs".

Commands:

- `clients infra --name <testnet> --uploader provider:region:file_size_mb[:vm_size][:payment=MODE][:ant=N][:eth=N][:clock_skew=DURATION][:bootstrap_cache=BOOL][:forward_logs=BOOL][:rust_log=SPEC] [--uploader ...] --downloader provider:region[:vm_size][:clock_skew=DURATION][:bootstrap_cache=BOOL][:forward_logs=BOOL][:rust_log=SPEC] [--downloader ...]` — create the VMs. The optional `forward_logs=true` key forwards that VM's `ant` stdout+stderr to the deployment's Elasticsearch (index `telegraf-ant-clients-<name>`; needs `elastic provision`); `rust_log=SPEC` sets `RUST_LOG` on the service (overriding `-v`) and **must be the last token** in the spec (its value may contain `:`). Each flag is repeatable; at least one of either is required. `provider` is **mandatory** and must be one of `digital_ocean`, `vultr`, `hetzner`, `ovh` — different uploaders/downloaders may target different providers (e.g. cheap-egress providers for the upload-heavy VMs). **Region names and VM sizes vary per provider and are passed through verbatim** — supply valid strings for the chosen provider (DO `s-2vcpu-4gb` / Vultr `vc2-2c-4gb` / Hetzner `cpx22` / OVH `b3-8`, regions like DO `lon1` / Vultr `lhr` / Hetzner `fsn1` / OVH `GRA9`); there is no per-provider validation. The optional `ant=` / `eth=` keys set per-uploader funding amounts (defaults: 100 ANT, 0.5 ETH). The optional `payment=` key sets the storage-payment mode (`merkle` or `no-merkle`). The optional `clock_skew=DURATION` key makes `clients provision` disable time sync and shift that VM's clock by a **signed Go-style duration** (e.g. `2h`, `-2h`, `30m`; a leading `-` runs the clock slow) — for testing the network's clock-independence; idempotent via a `/etc/ant-clock-skew-applied` marker. The optional `bootstrap_cache=true|false` key (default **true**) controls the ant peer cache per VM: when true the service invokes `ant` **without** `--bootstrap` (the cache is enabled; peers come from a `bootstrap_peers.toml` written to `/root/.config/ant/` by `clients provision`, containing the testnet's bootstrap addresses), and `bootstrap_cache=false` restores the legacy explicit `--bootstrap` invocation, which disables the cache. OVH requires `OVH_CLOUD_PROJECT_SERVICE` (the Public Cloud project ID) plus the `OVH_APPLICATION_KEY` / `OVH_APPLICATION_SECRET` / `OVH_CONSUMER_KEY` env vars; Vultr requires `VULTR_API_KEY`; Hetzner requires `HCLOUD_TOKEN`; DigitalOcean requires `DO_TOKEN`.
- `clients provision` — install the ant binary, `awscli`, and the relevant role's systemd unit (only one of `ant-uploader` / `ant-downloader` per VM). For each uploader, generates a fresh Arbitrum Sepolia EOA wallet, transfers the configured ANT + ETH from the funding wallet (`--wallet-secret-key` / `ANT_DEPLOY_SECRET_KEY`), persists wallet info in S3 client state, and writes the per-uploader secret key into `/etc/ant-client.env`. Idempotent: re-runs reuse wallets already recorded in client state. Skipped under `--prod`.
- `clients start` / `clients stop` — start or stop the appropriate service per-VM based on role.
- `clients destroy` — drains every per-uploader wallet (ANT + ETH back to the funding wallet on Arbitrum Sepolia, less a small ETH gas reserve), then tears down VMs and wipes the per-testnet addresses folder at `s3://saorsa-testnet-addresses/saorsa-deploy/addresses/<testnet>/`. Drain failures abort the destroy so VMs and state remain available for retry — pass `--wallet-secret-key` (or set `ANT_DEPLOY_SECRET_KEY`) so funds know where to return.
- `clients clean` — remove services + artefacts without destroying the VMs. It is handed IPs, never roles, so it stops and removes **every** role's unit (`ant-uploader`, `ant-downloader`, `ant-wasm-uploader`, `ant-wasm-downloader`, `ant-pointer`) — a role missing from that list leaves its service running on a "cleaned" VM.

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

### WASM client VMs (the browser path)

`ant-cli` **cannot** exercise the WebRTC-Direct browser path: it links native
`ant-core`, so it takes the native transport however it is invoked. WASM clients
run a Node.js program that imports `ant-core` **compiled to WASM** and reaches
nodes over WebRTC-Direct, which is the only way to measure the browser path the
way the native client is measured.

**No headless browser is involved.** Verified end to end against a local
`ant-devnet` (2026-09-07), not a cloud testnet: the client runs under **plain
Node.js**, with `node-datachannel` supplying `globalThis.RTCPeerConnection`.
HELLO, `find_node`, a 5 MiB download and a real paid upload with a
byte-identical round-trip all pass.

They are separate roles (`wasm-upload` / `wasm-download`) rather than a flag on
the native ones, because the role is what names the VM, its Terraform workspace
and its S3 state key — and telling the two transports apart in all three is the
entire point of running them side by side.

**Manifest** (`clients.wasm_uploaders[]` / `clients.wasm_downloaders[]`):

```yaml
testnet:
  binaries:
    ant_wasm:                 # branch form ONLY — see below
      branch: web-support
      repo_owner: WithAutonomi
  bootstraps:
    port: 10000
    webrtc_port: 42768        # pin the browser-facing address
  nodes:
    nat_percent: 0            # see "NAT" below
  clients:
    wasm_uploaders:
      - { provider: digital_ocean, region: lon1, file_size_mb: 10, ant: 200, eth: 0.1 }
    wasm_downloaders:
      - { provider: digital_ocean, region: lon1 }
```

**Addresses are one shared pool per deployment, read one way.** Both tiers
publish under the same S3 prefix. The WASM downloader lists the whole prefix, so
it may fetch content a native uploader published; the native `downloader.sh`
only lists `upload-*` keys, so it never draws a WASM upload. What is *not*
shared is the object each uploader owns: `client_index` restarts at 1 per role,
so the key is namespaced (`upload-<region>-<n>.txt` for native,
`wasm-upload-<region>-<n>.txt` for WASM). Without that, a native and a WASM
uploader in the same region would both own `upload-<region>-1.txt`, and since
each flush is a whole-object PUT they would repeatedly erase each other's
address lists.

**Each published line is `<address> <bytes>`.** ant-core refuses a browser-path
file larger than 1,000,000,000 bytes, but only after resolving its data map, so
a WASM downloader that drew the native 1000 MiB uploads from the shared pool
spent a fetch on every one to be told `invalid public file size 1048576000`.
Uploaders of both tiers now record the uploaded size beside the address, and
each downloader parses the first field. A downloader with `MAX_DOWNLOAD_BYTES`
set (`clients provision` sets it to the browser cap for the WASM roles; `0` or
unset means no bound) skips over-cap entries before fetching anything. A bare
line — every list written before this change — is never excluded, because an
unknown size is not a known-too-big one.

**CLI**: `clients infra --wasm-uploader provider:region:file_size_mb[:vm_size][:payment=MODE][:ant=N][:eth=N][:clock_skew=DURATION][:forward_logs=BOOL][:network=PROFILE][:rust_log=SPEC]`
and `--wasm-downloader provider:region[:vm_size][:clock_skew=DURATION][:forward_logs=BOOL][:network=PROFILE][:rust_log=SPEC]`.
Both are repeatable and take the same grammar as their native counterparts with
one exception: **there is no `bootstrap_cache`**. That option chooses between
ant's peer cache and an explicit `--bootstrap`, and a WASM client does neither —
it bootstraps from the deployment's WebRTC-Direct multiaddrs and discovers the
rest of the network over authenticated `find_node`. Passing the token is a hard
error rather than a silent no-op.

Example profile: `deploy/manifests/profiles/webrtc-wasm.yaml`.

#### The three extra pieces

1. **A pinned WebRTC port on the bootstrap tier** — `bootstraps.webrtc_port`
   (CLI `--webrtc-port` on `nodes provision-genesis` and `bootstraps provision`).
   Bootstraps are the one tier whose browser-facing address must not move: a WASM
   client bootstraps from it. Left unset, `ant-node` **derives** the WebRTC port from
   the QUIC port, so it changes whenever that does. Regular nodes deliberately
   keep the derived port — their address changes propagate over the DHT exactly
   as QUIC's do.

   The port is only half of a stable address. The multiaddr also embeds the
   node's **DTLS certificate hash**, which `ant-node` persists at
   `<root_dir>/webrtc-direct.pem`. That survives a restart — but **wiping a
   bootstrap's data volume rotates it and invalidates every cached browser
   address.**

2. **`build ant-wasm`** — builds the `ant-core` WASM package plus the TypeScript
   client and uploads a bundle to
   `s3://ant-node-builds/builds/<name>/<owner>/<branch>/ant-wasm-client.tar.gz`.
   A separate build path is unavoidable, not a preference: `ant-core` declares
   `default = ["native"]` and a `browser-wasm` feature mutually exclusive with
   it (a wasm32 build without it stops at an explicit `compile_error!`), and the
   artifact comes out of `wasm-pack`, not `cargo build --bin`. There is likewise
   no release form — no `ant-core` WASM package is published as a GitHub
   release, so `binaries.ant_wasm` accepts only the branch form.

   The ref may be a **branch or a git TAG** — `git clone --branch` accepts
   either, and nothing checks anything out afterwards. The staging profile uses
   a tag: the release train's `ant-cli-v<rc>`, or the shipped `ant-cli-v<current>`
   on a train with no ant-client RC.

   ```bash
   saorsa-deploy build ant-wasm --name WASM-01 \
     --branch-name ant-cli-v0.3.8-rc.1 --repo-owner WithAutonomi
   ```

   **`build ant-wasm-check`** runs that same build **locally**, in a temp
   directory, and deploys nothing. It exists because `build ant-wasm` costs a
   build VM and ~10 minutes before it can tell you ant-client's browser API has
   moved under the TypeScript harness — and in a release train that discovery
   lands at the last step before the client tier comes up, on a testnet whose
   node fleet is already running and billing. Exit 0 means `build ant-wasm`
   would succeed against that ref; anything else prints the failing step's
   output and leaves the working tree in place so you can open it.

   The commands both paths run live in one module
   (`saorsa_deploy/wasm_build.py`), because a local check that builds something
   slightly different is worse than no check at all: it reports a pass the real
   build will not honour.

   | Argument | Default | Notes |
   |---|---|---|
   | `--branch` | *required* | ant-client git ref — a branch **or** a release tag. |
   | `--repo-owner` | `WithAutonomi` | GitHub repository owner. |

   It **installs nothing**. If the `wasm32-unknown-unknown` Rust target,
   `wasm-pack` (pinned to the build VM's `0.15.0`) or Node >= 22 is missing, it
   exits non-zero naming each one with the exact command to install it.

   ```bash
   saorsa-deploy build ant-wasm-check --branch main --repo-owner WithAutonomi
   ```

3. **`deployments webrtc-addresses`** — SSHes the **bootstrap VMs**, reads each
   bootstrap service's `<root_dir>/webrtc-direct.multiaddr`, and saves them to
   deployment state as `bootstrap_webrtc_multiaddrs`. It cannot run at provision
   time because the address embeds the certificate hash, which exists only once
   a node has started; it must run before `clients provision`, because a WASM
   client has no other way into the network. `deployments up` sequences it
   automatically, after `nodes start`. Addresses whose advertised host is not
   the VM's public IP are rewritten to it (ant-node falls back to a routed local
   address, then loopback, when its transport has not yet learned an external
   one).

   **A WASM client gets the same bootstraps as a native client — no more.** The
   only difference between the two is the address format: the native tier is
   handed bare `ip:port` peers, while a browser client needs the full multiaddr,
   which also carries the UDP port, the DTLS certhash and the peer id. A command
   is needed only because the certhash does not exist until a node has run, so
   unlike `ip:port` the list cannot be written ahead of time.

   The node tier is deliberately **not** sampled. A client must reach the rest
   of the network by walking the DHT from its bootstraps, exactly as the native
   client does from its seven. An earlier revision handed each client ~32
   ordinary node addresses chosen across the peer-id keyspace; that is not a
   shape production can supply — a browser gets a small bootstrap list — so it
   masked any failure to discover peers instead of exposing it. **If uploads or
   downloads do not work from the bootstraps alone, that is a node-side defect
   to fix, not a reason to hand out more addresses.**

#### On the client VM

`clients provision` installs Node.js 22 and unpacks the bundle to
`/opt/ant-wasm-client`, then writes `/etc/ant-wasm-client.env` and the
`ant-wasm-uploader` / `ant-wasm-downloader` systemd unit. Native client VMs in
the same deployment see none of this. The bundle is always re-fetched (unlike the
`ant` binary, whose install is presence-gated — which is exactly why
`deployments upgrade-ant-binary` had to exist), so a re-provision after a rebuild
picks the new one up.

Payment works without any private key entering WASM: `ant-core` verifies each
storage quote and hands the verified set to a JavaScript callback, which does an
ERC-20 `approve` followed by one `payForQuotes` on the vault and returns the
**mined** transaction hash. The payment network (chain id, token, vault) is read
from a node's own `HELLO` rather than configured locally, so the client cannot
pay on a chain the network it is talking to does not use.

#### Results

WASM clients emit the **same** `upload` and `download` InfluxDB measurements as
the native ones, so `record-results`, `deployment_{upload,download}_records`,
`print-uploads` and `deployments compare` all work on them unchanged. The two
transports are told apart by a **`client_kind`** column (`native` / `wasm`) on
the per-op records, resolved from each VM's role — NULL on every row predating
the WASM client, which was necessarily native.

#### Two things to know before running a comparison

- **NAT.** Nodes behind the testnet's simulated inbound NAT are unreachable over
  WebRTC-Direct without relayed WebRTC, and each such dial burns a 10-second
  DataChannel timeout (`ant-node/docs/WEBRTC_DIRECT_TESTNET.md`). Set
  `nodes.nat_percent: 0` for a WebRTC run — and note that doing so makes the run
  incomparable to a native baseline that used a different NAT fraction.
- **Fleet size.** A WASM upload needs at least **7 initial PUT peers** before it
  will pay, so a small node fleet fails every upload with
  `Witnessed close group returned only N/7 initial PUT peers before payment`.

### Pointer client VMs

A **pointer** is a paid, owner-signed mutable reference: one owner key owns
exactly one pointer, forever, and every write — create and update alike — is one
paid single-node quote (ADR-0016 in `ant-node`). The pointer client tier
exercises them for the life of a testnet, so the behaviour is measured before it
reaches production.

It is a role of its own beside `upload` / `download` / `wasm-upload` /
`wasm-download`, for the reason the WASM roles are: the role names the VM, its
Terraform workspace and its S3 state key. It is also a different instrument. An
uploader measures a transfer; a pointer client measures whether a reference that
was written can be read back, and whether it decays with age — which ADR-0016
says it sometimes will, since an eclipsed reader can be handed an older, validly
signed state and cannot tell.

**Requires an `ant` binary that carries pointers.** A pre-pointer `ant-node`
answers a protocol error to every pointer message, so both binaries must come
from a build that has them.

#### Deploying

Manifest (`clients.pointers[]` on the `testnet` and `testnet-extension` kinds):

```yaml
  clients:
    pointers:
      - { provider: digital_ocean, region: lon1, ant: 200, eth: 0.5 }
      - { provider: digital_ocean, region: syd1, ant: 200, eth: 0.5 }
```

CLI (`clients infra --pointer`, repeatable):

```
provider:region[:vm_size][:ant=N][:eth=N][:clock_skew=DURATION]
  [:bootstrap_cache=BOOL][:forward_logs=BOOL][:network=PROFILE]
  [:create_interval_secs=N][:update_interval_secs=N][:sweep_interval_secs=N]
  [:chain_fraction=F][:pointer_max=N][:rust_log=SPEC]
```

| Option | Default | Meaning |
|---|---|---|
| `provider` | — | **Required.** `digital_ocean`, `vultr`, `hetzner` or `ovh`. |
| `region` | — | **Required.** Passed through verbatim; no per-provider validation. |
| `vm_size` | `s-2vcpu-4gb` | Passed through verbatim. |
| `ant` / `eth` | `100` / `0.5` | Per-VM wallet funding. Every write is paid, so this role carries a wallet exactly as an uploader does. |
| `clock_skew` | unset | Signed Go-style duration (`2h`, `-2h`, `30m`). |
| `network` | unset | Residential-link shaping; see "Simulating a residential connection". |
| `bootstrap_cache` | `true` | ant's peer cache, as for the native transfer clients. |
| `forward_logs` | unset | Forward this client's operation log to the deployment's Elasticsearch. Requires `elastic.enabled`. |
| `rust_log` | unset | `RUST_LOG` for the `ant pointer` invocations. **Must be the last token** in a CLI spec (its value may contain `:`). |
| `create_interval_secs` | `60` | Create a brand-new pointer this often. Creation runs for the WHOLE run, not as an up-front seed, so pointers of every age exist at every moment. |
| `update_interval_secs` | `30` | Update a uniformly-random owned pointer this often, then read it back. |
| `sweep_interval_secs` | `600` | MINIMUM gap between sweeps of every known pointer (own and foreign). A large pool makes a sweep take longer; it never drops pointers from one. |
| `chain_fraction` | `0.10` | Fraction of updates re-pointed at another OWNED pointer (target kind `pointer`) and then followed with `resolve`. `0` disables chaining. |
| `pointer_max` | `0` | Safety cap on how many pointers one client will own. `0` = uncapped, which is the intent. |

**The defaults are 180 paid writes per client per hour** — one Arbitrum Sepolia
transaction every 20 seconds. `clients estimate-gas --config <manifest>` sizes
the `eth:` figures straight from these rates, with no baseline needed: they are
deterministic, unlike an uploader's throughput.

**Deploy at least two, in regions far apart.** Each client sweeps its own
pointers AND every other pointer client's published ones, so every pointer is
read from a nearby and a distant vantage point. A pointer read is answered by a
quorum of its close group; whether that quorum agrees is not something one
vantage point can judge.

#### The workload

`ant-pointer` is a Python service (`resources/scripts/pointer_client.py`,
`Restart=always`) driving the native `ant pointer` subcommands over `--json`.
Three loops on one scheduler, strictly serial — they share a wallet, and two
concurrent writes would race the same nonce:

- **create** — `keygen` to a per-pointer seed file under
  `/var/lib/ant-pointer/keys/`, `create`, then `get` to confirm.
- **update** — update a random owned pointer, then `get` it back.
- **sweep** — read every owned pointer plus every other client's published ones,
  recording each pointer's age at read.

A durable ledger (`/var/lib/ant-pointer/ledger.json`) holds the expected
`(counter, target)` per owned pointer and the last-seen counter per foreign one,
so a restart resumes rather than abandoning pointers it has already paid for.
Owner keys live beside it, and **ownership is immutable** — a lost key is a
pointer that can never be updated again.

Targets are drawn from the shared upload address pool (the `upload-*` objects
the uploaders publish), so `resolve` checks end to end against a real data map;
with an empty pool it falls back to a random 32-byte address, so the role never
depends on the uploader tier. Each client publishes its own pointer addresses to
`pointer-<region>-<index>.txt` under the deployment's address prefix, through the
same presigned PUT the uploaders use.

Cost is measured as **paid, never as quoted**: `ant pointer create --json` prints
only the address, so ANT comes from `ant wallet balance` and native gas from an
`eth_getBalance` against the RPC the payment settles on, read either side of each
write. That is also the only way to see the ADR's warning that a write which
times out and is retried pays twice.

#### Outcome classes

Every operation lands in exactly one, and they are the vocabulary the reports and
the gates are written in:

| Outcome | Meaning |
|---|---|
| `ok` | The operation returned the expected state. |
| `stale` | A state older than the one written (or a foreign counter moving backwards). **EXPECTED at a non-zero rate** — only the trend is a signal. |
| `lost_write` | An update refused because the pointer moved in flight, or a write that fell short of its quorum. Paid for, not landed. |
| `not_found` | The close group agreed there is no pointer there. An ANSWER — `ant pointer get` exits 1 on an absent address and that maps here, not to `error`. |
| `read_shortfall` | Too few of the close group answered, or no state had enough peers behind it. The network declined to say. |
| `anomaly` | A counter ABOVE the one written. Only a write by something else to a pointer this client owns could cause it. |
| `error` | A protocol or RPC failure — the only class that indicates a fault. |

#### Reading the results

```bash
# Live, from the run's InfluxDB. The only view before record-results has run.
saorsa-deploy deployments print-pointers --name PTR-01

# Decay with age: sweep outcomes bucketed by the pointer's age at read,
# own and foreign pointers separately.
saorsa-deploy deployments print-pointers --name PTR-01 --by-age

# Extract to Postgres (deployment_pointer_records) — same pass as the file ops.
saorsa-deploy deployments record-results --name PTR-01

# Post-run, from Postgres. Survives teardown; Influx does not.
saorsa-deploy deployments print-pointers --id 612
saorsa-deploy deployments print-pointers --id 612 --window-start 2026-09-26T11:00:00 \
                                                  --window-end   2026-09-26T14:00:00
```

| Flag | Meaning |
|---|---|
| `--name <n>` | Read the deployment's **InfluxDB**. Live. Mutually exclusive with `--id`. |
| `--id <id>` | Read **Postgres**. Post-run. An id, not a name: staging names are reused every train. |
| `--window-start` / `--window-end` | ISO-8601. Narrows on **`finished_at`** — a write can take tens of seconds, so an op straddling a phase boundary belongs to the phase it completed in. |
| `--by-age` | The age table. Sweeps only (a `get` is the read that immediately confirms a write, so its age is always ~0). |
| `--age-bucket-secs` | Width of an `--by-age` bucket. Default 1800. |
| `--monitoring-name`, `--influx-url/-token/-org/-bucket` | Override the monitoring-state lookup on the `--name` path. |

The headline table carries, per host and combined: attempts and outcome counts
for each op; the read-your-writes match count; RPC-error writes; double payments;
writes that paid and did not land; write and read latency p50/p95; and the mean
cost per write in ANT and ETH.

#### The node's own view

`deployments record-log-counts` additionally counts ant-node's
`pointer_put_rpc` / `pointer_get_rpc` events per outcome with latency p50/p95,
and stores them beside the phrase counts in `deployment_log_event_counts` (keyed
`<event>:<outcome>`). They are what lets "the client was served a stale state" be
told apart from "a node answered stale". The catalogue also gains a `pointer`
group of node WARN/INFO lines — store failures, address collisions, replication
shortfalls, audit failures and prunes — which the existing log-count gate then
covers.

Everything about stale refusals, quorum shortfalls and unpaid offers is logged at
DEBUG, and the node-tier forwarder drops DEBUG at `log_level: info` (what staging
runs at), so those are deliberately **not** in the catalogue: they would read
zero for a reason that has nothing to do with pointers.

#### Gates

`deployments compare` emits eight `[pointer]`-suffixed gates: a write and a read
success floor, plus the stale-read rate, the lost-write rate, write and read
latency p50/p95, and the cost per write — all relative to the baseline. They are
emitted as **NOT ASSESSED, never omitted**, when the baseline has no pointer
records, which is every baseline on the first pointer-carrying train.

**No pointer gate ever reaches the exit code** — that stays the native file-op
hard floors'. The two FLOORS are additionally advisory by default
(`pointer_floor_gates_advisory` in `resources/gates/staging-thresholds.yaml`):
they are uncalibrated numbers on a tier no run has measured, and ADR-0016 says a
non-zero stale-read rate is expected without saying how large. The relative gates
count as ordinary soft gates — they ask only whether this release made the tier
worse than the last one did, which needs no calibration.

#### Profiles

- `deploy/manifests/profiles/pointers.yaml` — a short dev run (100 nodes, two
  pointer clients).
- `deploy/manifests/profiles/staging.yaml` — carries two pointer entries
  (DigitalOcean `lon1` and `syd1`). **The train's RC must carry pointers**; if it
  predates them the run operator strips the two entries for that train.

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
| `--skip-metrics` | flag | No | off | Provision **only** the ES log forwarder, leaving `telegraf-ant-metrics` untouched |
| `--skip-logs` | flag | No | off | Provision **only** the InfluxDB metrics forwarder, leaving `telegraf-ant-elasticsearch` untouched |
| `--node-memory-metrics` | flag | No | off | Also collect per-process RSS composition + `smaps_rollup` ([details](#per-process-memory-composition-ant_node_memory--opt-in)). Node-tier roles only |
| `--ssh-key-path` / `--ssh-user` | string | No | `~/.ssh/id_rsa` / `root` | SSH connection details |

¹ Exactly one of `--name` / `--host-ips-file` / `--inventory-path` is required.
² At least one backend's credentials must resolve (via flags or `--name` state).

`--skip-metrics` and `--skip-logs` are mirrors of each other, and both exist for
the same reason: `--name` is what resolves *both* backends, but it is also the
only source of the ES index name and of the `testnet_name` global tag that every
existing metrics series is keyed on. So neither forwarder can be excluded by
simply withholding `--name` — dropping it would blank the metrics tag and sever
each series from its history. Retrofitting one forwarder without disturbing the
other therefore needs an explicit flag.

On the `--name` path the command also enumerates client VMs from client state (if
any) and provisions host-stat Telegraf on them with `host_role="CLIENT"`, routed
to the dedicated **`client-metrics`** bucket (the client→ES forwarder is installed
separately by `clients provision`).

#### Traffic-accounting counters → InfluxDB (the ES forwarder's second output)

**Why this output exists.** An 18-hour bandwidth episode on a production node
(2026-09-14) was invisible in every dashboard even though the node's own
traffic-accounting counters (V2-623) had recorded it the whole time. The
counters sat in Elasticsearch while the `net.bytes_sent` they must be divided by
lives in InfluxDB, and the two label hosts differently (`tag.host.keyword`,
upper-cased, against a lowercase `host`) — so no query could put a numerator and
its denominator side by side. This output lands the counters in the same store,
under the same host tag, as the NIC figures.

Whenever a deployment has **both** elastic and monitoring state, the ES log
forwarder is rendered with a second output: an `[[outputs.influxdb_v2]]` that
writes the node's periodic **traffic-accounting summaries** (V2-623: cumulative
`udp_tx_bytes`, `bytes_relayed`, `wire_tx_bytes`, `fetch_response_tx_bytes`, …)
to the deployment's InfluxDB as well as to Elasticsearch. The output is scoped
with `namepass = ["tail"]` and a **`tagpass` on the `target` tag** —
`["*::traffic", "saorsa_transport::masque::relay_server"]` — which is mandatory:
without it every parsed log line would be written to Influx. Telegraf compiles
those values as separator-less globs, so `*::traffic` admits every
`<crate>::…::traffic` summary target, current
(`saorsa_transport::traffic`, `saorsa_core::traffic`,
`ant_node::replication::traffic`, `saorsa_transport::relay::traffic`) and the
`ant_node::{payment,storage,upgrade}::traffic` targets V2-834 adds — no deploy
change per new counter. The MASQUE relay-server summary lives under a component
target, hence the explicit entry. Points land in the `tail` measurement of the
node bucket (`testnet-metrics`), tagged `host` / `service_number` / `target`
exactly as the ES documents are — which means the **`host` tag is upper-cased**
(the forwarder upper-cases it for ES and processors run before every output);
the Grafana queries lower-case it before joining on the lowercase `net` host.

The credentials go into the ES forwarder's own env file (`/etc/default/telegraf-ant`
gains `INFLUXDB_URL` / `INFLUXDB_TOKEN` / `INFLUXDB_ORG` / `INFLUXDB_BUCKET`, the
same values `/etc/default/telegraf-ant-metrics` holds) via the same path that
populates the metrics env file. Without monitoring state the rendered ES config
and env file are byte-identical to before (asserted by a test). The ES output
itself is unchanged, so Elasticsearch volume does not grow.

**Retrofitting a running deployment:** `deployments provision-telegraf --name <name>
--skip-metrics` — the Influx credentials are resolved even under `--skip-metrics`,
so this regenerates and restarts only `telegraf-ant-elasticsearch`, now with the
Influx output, and leaves `telegraf-ant-metrics` alone. Add `--only-new-logs` on a
long-running fleet, or the restarted forwarder re-ingests every log still on disk
into ES. Should the token in the deployment's monitoring S3 state ever be stale,
`--influxdb-url` / `--influxdb-token` take precedence over state for **both**
forwarders. Cost measured on the prod fleet: ~1.1M points/day across ~4,000
series (73 hosts × ~11 services × 5 targets).

The panels that consume this are the **Egress accounting** dashboard, provisioned
by [`monitoring provision`](#monitoring-provision-command) alongside the host
dashboards.

## Sizing uploader gas funding (`clients estimate-gas`)

Testnet uploader VMs each get a generated wallet funded with a **fixed** amount of
native ETH (the `eth:` key on `clients.uploaders[]`, default `0.5`). Runway is
therefore inversely proportional to the gas price: when the price rises, the same
funding buys proportionally fewer payment transactions, and a client that runs dry
fails **every** subsequent upload.

That is not hypothetical. On staging run #566 (2026-09-01) the Arbitrum Sepolia base
fee had risen roughly 10x in three days — 0.0205 gwei flat for a fortnight, then
0.025 → 0.050 → 0.124 gwei on consecutive days — and six of ten uploaders exhausted
their wallets from ~T0+3h57m. Whole-run upload success came out at 96.02% and the
mixed-version phase at 79.35%, both below their hard-gate floors, for a reason with
nothing to do with the release under test. Throughput was not the cause: that run did
*less* work than its baseline with the same transaction counts.

`clients estimate-gas` is the read-only preflight. It changes nothing; it answers
whether a manifest's `eth:` figures will survive a full run at today's price.

```bash
uv run saorsa-deploy clients estimate-gas \
  --config deploy/manifests/runs/staging/STG-01.yaml \
  --baseline-id 566
```

| Argument | Default | Description |
|---|---|---|
| `--config` | required | Manifest whose `testnet.clients.uploaders` **and** `.wasm_uploaders` to size. |
| `--set` / `--name` | — | Manifest overrides, as elsewhere. |
| `--baseline-id` | — | Previous run to take upload throughput from. |
| `--uploads-per-hour` | — | Flat rate instead of a baseline run. |
| `--duration-hours` | baseline span, else 5 | Run length to size for. |
| `--base-fee-gwei` | read from chain | Size against a supplied fee; also works offline. |
| `--network` | `arbitrum-sepolia` | Which RPC supplies the base fee. |
| `--funder-balance-eth` | from `ANT_DEPLOY_SECRET_KEY` | Funding wallet balance. |
| `--model-path` | shipped config | Override the gas model. |

**Exit 2** means an uploader's `eth:`, or the funding wallet itself, is short. Exit 1
is an operator error (bad manifest, no `DATABASE_URL` with `--baseline-id`, unknown
network); exit 0 means the current funding is adequate.

Three design points worth knowing when reading the output:

- **Rates, not raw counts.** Each baseline client's rate is measured over its OWN
  active window and projected across the full run. A client that exhausted its wallet
  stopped early, so its raw count understates a funded client's work — sizing from it
  would reproduce the very shortfall the command exists to prevent. The command names
  the clients it corrected.
- **Scaled on base fee, not the effective price paid.** The effective price includes a
  priority fee and the estimator's 1.2x buffer; assuming those scale with the base fee
  makes them cancel in the ratio, which is more robust than predicting them.
- **The funder check is against the FULL total, not the deficit.** Top-up is
  per-wallet and sequential, so a funder that cannot cover the whole draw does not
  fail cleanly up front — it funds the first few uploaders and then runs dry, leaving
  a half-provisioned run.

### WASM (browser-path) uploaders

A WASM uploader holds its own funded wallet and pays real testnet gas exactly as a
native one does, so it is sized here too — labelled `[wasm]` in the table and
included in the funder total. Omitting it understated the fleet requirement by a
whole wallet per WASM uploader, against a check that compares the full total.

- **Its fixed per-upload cost is doubled.** A browser-path payment is **two**
  transactions where a native one is one: no private key ever enters WASM, so
  ant-core hands the quote set to a JS callback that does an ERC-20 `approve` and
  then a single `payForQuotes`. The fixed term *is* that per-transaction overhead,
  so it is paid twice; `eth_per_gb` is the quote payload and is unchanged. The
  multiplier is `wasm_fixed_cost_multiplier: 2.0` in the model config — derived from
  the transaction count, not calibrated against a measured WASM run, because there
  has not been one yet.
- **Throughput** comes from the baseline's own `client_kind = 'wasm'` upload records
  at a comparable size, computed exactly as native is (rate over the client's own
  active window). Until a baseline carries any, the **nearest native uploader by
  size** stands in and the row is labelled `approx: native twin <hostname>` in the
  `Basis` column. Borrowing is sound in that direction only: WASM measured ~6–8x
  slower on the same work, so a native rate projects more uploads than the WASM
  client will manage and **over**-funds it — and the surplus is swept back to the
  funder on destroy.
- **Native uploaders are sized from native records only** (`client_kind IS NULL OR
  'native'`). Without that filter, once WASM is in every staging baseline a native
  uploader would silently be sized from whichever tier happened to be faster at its
  size — a change to how the native tier has always been sized, made invisibly.

### Pointer clients

A pointer client holds its own funded wallet too, and every pointer write —
create and update alike — is one paid single-node quote. It is sized here,
labelled `[pointer]` and included in the funder total.

- **Sized from the manifest, not from a baseline.** The rates are a manifest
  setting (`create_interval_secs` + `update_interval_secs`), so the expected write
  count is arithmetic: `(3600/create + 3600/update) x duration`. There is nothing
  to project and no baseline to need — which also means a pointer client is sized
  correctly on the very first pointer-carrying run. `--uploads-per-hour`
  deliberately does NOT override it: it means uploads, and a pointer client
  performs none.
- **Its own fixed cost, and no per-GB term.** A pointer record is a fixed ~5 KB
  wire payload, so `eth_per_gb` does not apply at all; the cost is
  `fixed_eth_per_pointer_write`, scaled on base fee like everything else. The
  `Size` column reads `n/a` rather than `0 MB`, so it cannot be misread as a lost
  value.
- **SEEDED, not measured.** `fixed_eth_per_pointer_write` starts at
  `fixed_eth_per_upload`'s value, because a pointer write is the same shape of
  transaction as an upload's payment. Recalibrate it from the first
  pointer-carrying run's measured spend, which `deployments print-pointers`
  reports as "mean cost per write".

At the default rates a pointer client performs 180 writes an hour, so a 4h run is
~720 paid writes — the same order as an uploader's few hundred, not a rounding
error.

The model and its calibration live in
`deploy/saorsa_deploy/resources/gas/client-gas-model.yaml` — versioned config, not
code, so recalibrating after a price regime change needs no release. It carries the
six measured points it was fitted from, and the model's own known limits (payment
mode is not a variable; the fit is one run at one gas regime; Arbitrum folds L1
calldata cost into gas units rather than the price). `deploy/tests/test_gas_model.py`
asserts the shipped config still reproduces its own calibration, so a bad edit fails
there rather than four hours into a run.

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

Grafana (when enabled) is provisioned with five dashboards: **Node Host
Statistics**, **Client Host Statistics**, **Node Overview**, **Egress
accounting** (V2-1225) and its per-service companion **Node traffic by
service**. Egress accounting reconciles the node's own traffic-accounting
counters — shipped to Influx by the ES forwarder, see
[Traffic-accounting counters → InfluxDB](#traffic-accounting-counters--influxdb-the-es-forwarders-second-output)
— against the host NIC, per host, from the one `testnet-metrics` bucket:

1. **Egress accounting coverage (%)** = (`udp_tx_bytes` + `relay_carrier_udp_tx_bytes`
   + `bytes_relayed` from both relay servers) / `net.bytes_sent`, red below 80%;
   the legend's *mean* is the figure for the selected range.
2. **Unaccounted egress** as a rate, mean over the range, top-15 table.
3. / 4. **Socket ground truth — egress / ingress (%)**: `sock_tx_bytes` /
   `net.bytes_sent` and `sock_rx_bytes` / `net.bytes_recv` — the socket-boundary
   counters V2-834 adds, i.e. the figure its "within 2% of the NIC" acceptance
   gate is measured by. Empty on builds predating V2-834. (An earlier revision of
   that branch left the relay server's own-socket ingress out of `sock_rx_bytes`
   — bootstraps read 21–33% — fixed upstream at this dashboard's request.)
5. **Egress decomposition** (stacked, summed over the selected hosts): DHT wire
   messaging, chunk GET serving, replication fetch responses, both relay servers,
   relay-client stream, QUIC retransmission, stateless packets, failed dials.
   Layers overlap (attribution, not a partition); V2-1226 is the full picture.
6. **Relay throughput** with the `forwarded_to_client_bytes` /
   `forwarded_to_target_bytes` split, over both relay servers.
7. **Active relay reservations / sessions**. 8. **Replication serving**
   (`fetch_response_tx_bytes`). Plus a text panel defining the numerator.

The numerator is *only* `udp_tx_bytes` (+ `relay_carrier_udp_tx_bytes`, which the
V2-834 build splits out of it for a NAT'd node's carrier to its relay) plus
`bytes_relayed` under **both** `saorsa_transport::relay::traffic` (the stream
relay server on raw UDP sockets) and `saorsa_transport::masque::relay_server`
(the MASQUE reservation relay, whose summary is only emitted once a CONNECT has
arrived); `wire_tx_bytes` and `fetch_response_tx_bytes` are excluded because they
count the same bytes at a higher layer. Until V2-834 C.1 lands the numerator can
over-count relayed bytes, so coverage is an upper bound.

Four facts the Flux is built on, all measured (ant-prod-01's ES on 2026-09-17; a
DEV-01 run of the V2-834 branch on 2026-09-18) rather than assumed:

- **Counters are cumulative per process and emitted every 5 minutes**, so every
  panel differences them over 10-minute windows (`nonNegative`: a service restart
  drops that window rather than going negative) and sums across the host's
  services with a per-`(host, _time)` `sum()` — never a second `aggregateWindow`,
  which re-stamps each increase one window late against the NIC side.
- **One series can carry several lines per tick.** The V2-834 build emits the
  endpoint summary once per endpoint (two lines ~30 ms apart, one with
  `udp_tx_bytes = 0`), and `relay::traffic` interleaves per-session lines with the
  summary under the same field names. Differenced as one series that flaps
  `0, X, 0, X` and `nonNegative` keeps +X every tick — measured at 25× the NIC. So
  each series is windowed with `max` rather than `last`: the summary total is ≥
  every other line in the window. (A restart then costs a window and a half
  rather than one, and a second endpoint carrying real bytes would be
  under-counted — the fix for that is an endpoint tag upstream.)
- **Everything before the window must be push-downable, and the window scales
  with the range.** A 24-hour view of a 13-host testnet hit Grafana's 30 s
  timeout on every panel that touches `net`: the raw 10-second samples (~450k
  rows/day here, ~5M for 48 h of prod) and every `tail` line were being
  materialised in Flux because `map`/`group` sat before `aggregateWindow`. With
  the window placed directly after the filters the storage engine returns one row
  per series per window and the same query takes 2 s; the window widens with the
  range (10 m ≤ 6 h, 30 m ≤ 24 h, 1 h beyond) so row count stays roughly constant,
  and the two sides are combined with `union` + `pivot` rather than `join()`.
- **The partial window at the range end is dropped on both sides.** The summaries
  tick every 5 minutes and `net` every 10 seconds, so a view cut a minute after a
  tick pairs 5 minutes of counter bytes with 1 minute of NIC bytes (measured at
  5×, dragging a 2-hour legend mean from 95% to 116%). The newest point is
  therefore up to one window old.
- **Host case differs on the two sides and is not simply lowercase:** `tail`
  carries the upper-cased host, `net` the OS hostname, which on a testnet keeps
  the deployment name's case (`DEV-01-ant-node-lon1-1`). Both sides are
  lower-cased and the Host variable is matched case-insensitively; the variable
  itself is pinned to the `cpu` measurement so the `tail` points don't list every
  host twice.
- **On a NAT-simulated VM `veth-nat` is the busiest interface and points the
  wrong way** (its `bytes_sent` is traffic sent *into* the node's netns). The NIC
  side excludes `lo`, `all` and `veth*` and takes the busiest remaining interface
  per window, since interface names differ by provider.

**Node traffic by service** is the same data one level down. Every counter above
exists per node service in Influx (`service_number` tag) but the egress dashboard
sums them per host, because coverage is a ratio to the VM's one NIC. The
production fleet runs 700+ services, so this dashboard never draws a line per
service fleet-wide: fleet-wide it **ranks** — four top-20 tables (relay bytes
forwarded, socket egress, socket ingress, chunk GET serving), each row a
`(host, service)` with its mean rate over the range and its busiest window,
costing the same at 70 services or 7,000 — and lines are drawn only for the
services of **one** host, chosen in a single-select Host variable. Pick the host
of a row that stands out and the four lower panels show its ~15 services over
time. Same counters, same windows, same push-down query shape.

Measured on that DEV-01 run once all of the above was in place, every host reads
82–97% on coverage and on both ground-truth panels, at 2 h and 24 h alike — the remaining gap being what
V2-834's 2% gate has yet to close. Panel builders:
`provisioning/egress_dashboard.py`.

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

#### Per-process memory composition — opt-in (`--node-memory-metrics`)

`procstat.memory_rss` is a single total and cannot say what the memory is *made of* — the difference between a leak and a growing LMDB chunk-store mmap. Measured on a production node, `ant-node-9` held 880 MB RSS of which 469 MB was file-backed; an hour later, 3.48 GB of which 3.07 GB was file-backed. Read as a total that looks alarming; read by composition it is the chunk store doing its job. That ambiguity is not hypothetical: it is what made `memory_rss` misread as a **20x leak** on STG-03 run 475.

`--node-memory-metrics` turns on composition collection in **two tiers**.

##### Tier 1 — native `procstat` mmap fields (the routine signal)

Telegraf's own `procstat` plugin collects smaps-derived fields natively; they were simply permission-gated. Granting the unit `CAP_SYS_PTRACE` unlocks `memory_pss`, `memory_anonymous`, `memory_private_clean`/`_dirty`, `memory_shared_clean`/`_dirty`, `memory_referenced` and `memory_swap` — **no new plugin and no collector script**.

The base config now names `properties` explicitly to **exclude** telegraf's default `mmap`, and the opt-in fragment re-declares the same process sets with `properties = ["mmap"]` at a **60 s** interval. That split is load-bearing: mmap reads the whole of `/proc/<pid>/smaps`, and at the 10 s agent interval it costs 6.78% of one core versus 1.15% at 60 s. Two tiers keep `cpu_usage`, `num_fds`, `num_threads` and `memory_rss` at their existing 10 s resolution while paying the mmap cost once a minute.

##### Tier 2 — a systemd timer for what `procstat` cannot give

`ant-node-memory-collector.timer` runs a small collector reading `/proc/<pid>/status` and `/proc/<pid>/smaps_rollup`, emitting the `ant_node_memory` measurement. It supplies the fields `procstat` has no equivalent for:

`rss_anon` · `rss_file` · `rss_shmem` · `vm_swap` · `rollup_pss_anon` · `rollup_pss_file` · `rollup_pss_shmem` · `rollup_pss_dirty` · `rollup_swap_pss` · **`rollup_lazy_free`** · `rollup_anon_huge_pages` · `rollup_file_pmd_mapped` · `rollup_shmem_pmd_mapped` (plus the rollup's own totals for cross-checking).

**`LazyFree` is the point of this tier.** `MADV_FREE`'d pages stay resident until the kernel reclaims them under pressure, producing exactly the V2-970 shape: RSS grows, then collapses with the PID unchanged. Note it is a **lower bound** — the kernel documents that implementation optimisations can make it under-report, so a non-zero or collapsing value is evidence of `MADV_FREE` while zero does not exclude it.

The three huge-page fields are collected together on purpose: `AnonHugePages` covers anonymous THP only, so on its own it says nothing about a file-backed mapping such as the LMDB chunk store.

**Tags.** Because this tier posts to InfluxDB directly rather than through Telegraf, it attaches the global tags itself: `host`, `testnet_name` and `host_role`, matching what Telegraf writes for `procstat` so the two measurements can be joined. `host` is not optional — without it every machine's `ant-node-1` collapses into one series across the fleet, and `record-analysis` discards points lacking a host, so the archival path silently produces nothing. `host` defaults to the system hostname (what Telegraf uses); `ANT_HOSTNAME` or `--host` override.

Cadence is `--node-memory-interval` (default **300 s**; use **60** on a host under active investigation). The timer is jittered by a tenth of its interval so the fleet's page-table walks do not land together. One pass over 13 services costs ~732 ms, so ~0.24% of a core at 300 s.

**Deliberately not a Telegraf `inputs.exec`.** That route measured ~14.9% of one core on a production node, of which ~12.5% was telegraf overhead that could not be accounted for — it persisted with only a trivial input configured and with the interval stretched to 600 s. The timer costs only the collector's own work.

##### Capability, and why it is `CAP_SYS_PTRACE`

`/proc/<pid>/smaps` and `smaps_rollup` are gated on `ptrace_may_access(PTRACE_MODE_READ_FSCREDS)` against the target, **not** on file permissions — so the `CAP_DAC_READ_SEARCH` the unit already held does not satisfy them, while world-readable `/proc/<pid>/status` needs nothing. The failure mode is *silent and partial*: the cheap sources report normally while composition returns nothing at all, which cost this feature one canary cycle before it was found.

`CAP_PERFMON` is the narrower grant (it confers neither `/proc/<pid>/mem` nor ptrace attachment) and was tested first on the production kernel 6.8.0-86: **0 of 13 processes readable**, identical to no capability, with and without `NoNewPrivileges`. `CAP_SYS_PTRACE` is therefore the fallback, and it works with `NoNewPrivileges=yes`.

The grant is applied **only** on hosts that opt in, alongside a matching `CapabilityBoundingSet` and `NoNewPrivileges=yes`. Verify after deployment with `grep CapEff /proc/<telegraf-pid>/status` — `0x80004` is both capabilities, `0x4` is `CAP_DAC_READ_SEARCH` alone.

##### Units, archival and limits

Telegraf reports `memory_rss` in **bytes** but every smaps-derived field in **kB**. That inconsistency is corrected once at capture, so everything stored in Postgres is bytes.

`record-analysis` writes composition into `deployment_host_results.host_metrics` under `memory_composition_by_service` (a JSON column, so no migration). It is **excluded from `deployment_host_metric_buckets`** by construction: one per-service bucket metric costs ~164 k rows (~35 MB) per 990-node staging run against a table already at 76% of the registry database. See V2-973.

`memory_rss − memory_anonymous` is **non-anonymous/file-or-shared resident**, not "file-backed" and not "LMDB" — it can include any file mapping plus shmem. `Pss_Anon`/`Pss_File`/`Pss_Shmem` give a cleaner backing-type split, but PSS is proportional accounting and still does not attribute residency to a particular file. Proving LMDB attribution needs a pathname-level `smaps` snapshot, which is **not yet implemented**.

##### Deployment and rollback

```bash
saorsa-deploy deployments provision-telegraf \
  --name ant-prod-01 \
  --inventory-path ~/.autonomi/testnets/ant-prod-01/inventory.json \
  --ssh-key-path ~/.ssh/ant_deploy \
  --skip-logs \
  --node-memory-metrics \
  --node-memory-interval 60
```

`--inventory-path` rather than `--name` alone for host selection: on a deployment whose state seeds the real production bootstrap peers, the `--name` path tries to SSH machines we do not own. `--name` is still required — it supplies the `testnet_name` global tag every existing series is keyed on — and `--skip-logs` is therefore how the Elasticsearch forwarder is left untouched.

Rollback: re-run without `--node-memory-metrics`. The mmap tier and the capability are removed and telegraf restarts. The timer is left enabled, so also `systemctl disable --now ant-node-memory-collector.timer` on any host that had it.

### Bucketed host metrics — slicing a window after the run

`deployments record-analysis` writes host metrics twice: the whole-run rollup in `deployment_host_results` (unchanged, and still the cheap "how did the run look overall" view), **and** fixed-width time buckets in **`deployment_host_metric_buckets`**. The buckets exist so an arbitrary sub-window can be re-derived **after the monitoring stack is destroyed** — previously the window was fixed at capture time by `--window`, and once InfluxDB was gone it could never be revisited. That is what forced the rc.1 staging comparison to be reported as directional only: the two runs' stored windows were 3.37 h and 5.45 h, the latter including the mixed-version phase, so their CPU/memory/network figures were not like-for-like.

One row per `(host, bucket_start, metric, device, service)`, holding `avg`/`min`/`max`/`samples`. A tall numeric table rather than JSON-per-bucket, so a window is filtered server-side and re-aggregated in one `GROUP BY`.

**Why the default is 300s.** Width does not affect accuracy — for a
boundary-aligned window `sum(avg*samples)/sum(samples)` is exactly the mean of
the underlying 10s samples, and `MIN(min)`/`MAX(max)`/`SUM(samples)` are exact,
at any width. It trades only window-edge precision (≤5 min) and trend-plot
resolution. What it buys is table size: 300s keeps a staging-sized run near
**100k rows (~18 MB)** instead of ~500k. The registry runs on DigitalOcean's
smallest managed Postgres (1 vCPU, ~1 GB RAM → `shared_buffers=190MB`, 10 GB
disk), where table size *is* query time.

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

It also counts **ant-node's pointer RPC latency events** — `pointer_put_rpc` and
`pointer_get_rpc` on the `ant_node::storage::rpc_latency` target — per outcome,
with latency p50/p95, and stores them in the same table keyed `<event>:<outcome>`.
They are the node's own view of the pointer tier, so `outcome=error` as a fraction
of the event total can be read directly, and "the client was served a stale state"
can be told apart from "a node answered stale". Best-effort, unlike the phrase
counts: a run with no pointer clients emits none of these events and a node build
predating pointers has no such target, so a query failure warns and leaves the
gated phrase counts alone. An event with no documents records nothing rather than
zeros — zeros would read on the next comparison as a pointer tier that stopped
working.

Catalogue **v4** (V2-1413) adds two groups for the V2-1363 pointer A/B. The
`subtree_audit_failure` group counts the auditor-side
`warn!("Audit: {challenged_peer} failed subtree audit ({reason:?})")`, one
pattern per `AuditFailureReason` variant at ant-node v0.21.0 — `Timeout`,
`MalformedResponse`, `DigestMismatch`, `KeyAbsent`, `Rejected` — so the group
sums to the **true** failure total and no reason is silently uncounted.
`tail.message` is analysed, so the parentheses are discarded and it is the
reason *token* that discriminates; it is distinct per variant, so exactly one
pattern matches any one message. The `storage_commitment_rotated` group counts
the rotation line itself, which is not a failure but is the only info-level
evidence that commitments were rotating at all — see
`analysis pointer-commitments` below for the `pointers=` split the phrase count
cannot make. ant-node #238's two new strings are **deliberately not** in the
catalogue, with the reason recorded in the YAML: one shares its message with the
pre-existing heavy-pool drops so a phrase count cannot discriminate them, and
the other is `debug!`, which the node forwarder drops at `log_level: info`. Both
of #238's regression modes surface as `subtree_audit_fail_timeout` rising
instead. Every pre-existing key is untouched, so v3 and v4 counts of them are
comparable; it is only the new keys that the four prior pointer runs
(612/614/615/627) have no counts for, which the comparison reports as NOT
ASSESSED.

Catalogue **v5** (V2-1441) adds the first **advisory** group,
`payment_rpc_failure` (`Payment RPC failures (node)`). Every node verifies every
paid PUT against the payment vault over an EVM JSON-RPC endpoint, and on a
testnet that is the anonymous public Arbitrum Sepolia one, shared by the whole
fleet and by the rest of the internet. When it rate-limits, the node rejects the
PUT with `payment failed`, the client retries the chunk, and every upload slows
down for a reason that has nothing to do with the release — which is exactly
what the V2-1359 A/B (runs 635/639/650) read as a 41–46% upload-duration
regression until the raw logs showed ~8,900 node-side `HTTP error 429` lines an
hour. The group counts ant-node's `Slow payment verification failed … : {e}`
WARN (`ant_node::payment::verify`) by two phrases, `HTTP error 429` and
`completedPayments lookup failed`, combined with `max`; a verification that
fails in under 500 ms is logged at DEBUG and dropped, so the count is a lower
bound. **Advisory** means the row is rendered like any other, marked
`(advisory)`, but never reaches the overall verdict — a FAIL here would blame
the release for the weather. Instead the group carries a `confounds:` sentence
naming the gates it casts doubt on, and `deployments compare` repeats it as a
**warning** whenever the candidate's per-hour rate is at least 2× the
baseline's (or the baseline has no such counts), so the caveat sits at the top
of the report beside the upload-duration gates it applies to. Nothing existing
changed, so v4 and v5 counts of every pre-existing key are comparable.

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

The catalogue (`resources/log_patterns/connection-errors.yaml`) is git-versioned config, like the gate thresholds, and its `version` is stored on every recorded count. Its strings are organised into groups; the comparison gates on **groups**, not individual strings, and a group marked `advisory: true` is reported but never decides the verdict (see catalogue v5 above).

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
| `--baseline-id` | int | One of these two | - | Deployment id of the baseline run |
| `--new-baseline` | flag | One of these two | - | This run has no comparable predecessor — see below |
| `--window-label` | string | No | `full-run` | Which stored window to compare on both sides |
| `--thresholds` | path | No | bundled | Threshold config YAML (`resources/gates/staging-thresholds.yaml`) |
| `--patterns` | path | No | bundled | Log-pattern catalogue YAML (`resources/log_patterns/connection-errors.yaml`) |
| `--history` | string | No | - | Comma-separated ids of prior **accepted** runs, for the 2σ noise criterion |
| `--json-out` | path | No | - | Write the machine-readable verdict here |
| `--markdown-out` | path | No | - | Write the fixed-format markdown report here |
| `--print-markdown` | flag | No | - | Also print the report to stdout |
| `--no-persist` | flag | No | - | Do not store the verdict in the registry |

Exits `2` on a hard-gate failure (which blocks the ship with no override); soft-gate failures and GRAY exit `0`, since those are judgment calls.

#### `--new-baseline`: a run with nothing to compare against

When the staging profile changes shape — as it did when the WASM client tier was
added and `nat_percent` went from 30 to 10 — **nothing recorded before the change
is a valid baseline for anything recorded after it**. `--new-baseline` is how that
run is still assessed and recorded:

```bash
uv run saorsa-deploy deployments compare --id 596 --new-baseline \
  --window-label steady-state --markdown-out report.md
```

- It is **mutually exclusive with `--baseline-id`, and exactly one is required** —
  there is no default, because which of the two applies is the operator's call and
  getting it wrong silently produces the wrong verdict.
- Only the **single-run** gates are evaluated: the native success floors (absolute
  numbers, so they stand on their own) and the WASM floors (advisory, as always).
- **Every relative gate is emitted as NOT ASSESSED** with the exact reason
  `new baseline` — success rate, per-bucket p50/p95, node CPU, node RSS, leak
  slope, outbound/GB, disk/GB and every connection-error group, for both
  transports. *Emitted*, not omitted: a gate missing from the report reads as one
  nobody thought about, and the entire point of this report is that an unmeasured
  gate can never be mistaken for a passing one.
- The **comparability guards below are skipped**, since there is nothing on the
  other side to be comparable to. `--window-label` is still required and the
  candidate must carry it.
- The report header reads `Baseline: none (new baseline)`.
- **Exit code is unchanged**: `2` only on a native hard-floor FAIL.
- The verdict is stored with a NULL `baseline_deployment_id`. Postgres treats
  NULLs as distinct, so a **partial unique index** on `(deployment_id,
  window_label) WHERE baseline_deployment_id IS NULL` is what keeps a re-run
  replacing its previous answer instead of accumulating rows.

There is deliberately **no guard against comparing across a rebaseline** — the
tool cannot know which profile change invalidated which history. That judgment
belongs to the release process, and the `evaluate-staging-run` skill enforces it.

#### What it covers

Two hard gates (upload success floor < 99%, download success floor < 99.5%) and eight soft gates: upload/download success rate, transfer duration p50/p95 **per file-size bucket**, node CPU, node RSS, memory leak slope, outbound bytes per GB of traffic, and disk bytes per GB stored. Plus one soft gate per **connection-error group** recorded by `deployments record-log-counts` (see below).

Transfer-duration percentiles are exact — they come from the per-op `deployment_upload_records` / `deployment_download_records`, which carry each operation's own `duration_ms` and timestamps. `deployment_file_results` is not used: it is an aggregate with no per-op timestamps and therefore cannot be windowed.

A run carrying pointer clients adds eight `[pointer]` gates — see "Pointer gates"
below.

#### Per-transport gates, and the two advisory settings

When a run carries WASM (browser-path) client records, every client gate is
evaluated **once per transport** and suffixed `[native]` / `[wasm]`. With no WASM
records the gates are emitted exactly as before, unsuffixed. Node-tier gates (CPU,
RSS, leak, per-GB ratios, log counts) are never split — one node fleet serves both
transports.

The two KINDS of WASM gate get opposite answers, so `resources/gates/staging-thresholds.yaml`
carries **two required booleans**:

| Key | Shipped | Covers | Why |
|---|---|---|---|
| `wasm_floor_gates_advisory` | `true` | `Upload/Download success floor [wasm]` | The floors are absolute numbers calibrated on the native transport. WASM does not meet them — 92.1% upload / 94.9% download against 99% / 99.5% on the first measured run — and the gap is **NAT**: a peer behind the simulated inbound NAT is unreachable over WebRTC-Direct until relayed WebRTC exists. That is not the release under test, so gating on it would hard-fail every run on a tier that is not yet what ships. |
| `wasm_relative_gates_advisory` | `false` | `[wasm]` success rate and per-bucket p50/p95 | These need no absolute calibration: they ask only whether *this* release made the WASM tier worse than the baseline did — exactly what a soft gate is for. So they **COUNT**, as SOFT gates, like their native twins. |

Both keys are **required**; an absent one is an error, not a default, because it
decides whether a failing gate blocks the ship. The retired single
`wasm_gates_advisory` fails validation naming both replacements rather than being
silently ignored.

**No WASM gate ever reaches the exit code.** A counted `[wasm]` relative FAIL makes
the overall verdict `SOFT FAIL` — a judgment call, reported in full — while exit `2`
stays reserved for the native hard floors.

#### Pointer gates

A run with pointer records gains eight `[pointer]`-suffixed gates: a **write** and
a **read** success floor (absolute, judged on the candidate alone), and the
**stale-read rate**, **lost-write rate**, **write latency p50/p95**, **read latency
p50/p95** and **ANT cost per write**, all relative to the baseline.

Reads and writes are never pooled. A pointer client performs far more reads than
writes, so one rate over both would let the sweeps bury a write failure entirely.

The stale-read rate is **compared and never floored**. ADR-0016 is explicit that
an eclipsed reader can be handed an older, validly signed state and cannot tell,
so a non-zero rate IS the steady state — only a rise against the baseline is a
finding. `stale` still counts as a failure for the success rate, because it is not
the state the client wrote; the two figures are reported side by side and neither
is readable without the other.

When the baseline has no pointer records — which is every baseline on the first
pointer-carrying train — the relative gates are emitted as **NOT ASSESSED** with
the reason `baseline has no pointer records`. Emitted, not omitted: a gate that
vanishes from the report reads as one nobody thought about. The two floors are
still evaluated, and they survive `--new-baseline` for the same reason the native
hard floors do — they need no baseline.

| Key | Shipped | Covers | Why |
|---|---|---|---|
| `pointer_floor_gates_advisory` | `true` | `Pointer write/read success floor [pointer]` | Uncalibrated. No run has ever measured a pointer tier, and ADR-0016 says a non-zero stale-read rate is expected without saying how large — so a floor set today is a guess, and gating a ship on a guess is worse than reporting it. Set to `false` once a few pointer-carrying trains have moved the floors to numbers with history behind them. |

**No pointer gate ever reaches the exit code** — advisory or not. Every one is
`soft`, so the worst a pointer regression produces is `SOFT FAIL`; exit `2` stays
reserved for the native hard floors.

#### Data integrity (download content verification)

A **hard** gate, `Data integrity (download content verification)`, counting downloads whose content did not match the SHA-256 their uploader published. Its threshold is `hard_gates.download_verify_mismatch_max`, shipped at **`0`** — the one number in the config that is legitimately zero, because there is no fraction of corrupt content a release may ship, and it is a COUNT rather than a percentage for the same reason.

Candidate-only, like the success floors: a mismatch is wrong in absolute terms rather than relative to what the last release managed, so a baseline contributes nothing and the gate **survives `--new-baseline`** intact.

**A mismatch never touches the availability floors.** A download that fails is an availability problem; one that returns different bytes is a correctness problem. Folding the second into the first would let a run with corrupt content pass on a high enough success rate — the same line the production upload verifier draws.

Three verdicts:

| Verdict | When |
|---|---|
| `PASS` | At least one download was verified and none mismatched |
| `FAIL` | Any download mismatched |
| `NOT ASSESSED` | Nothing was verified at all — reason `no verified downloads recorded` |

The detail line reports `<ok> ok / <mismatch> mismatch / <unavailable> unavailable` plus **coverage**: verified downloads ÷ successful downloads. Only `ok` + `mismatch` are in the denominator. An `unavailable` download was never checked against anything (its address line carried no hash) and a NULL was never checkable at all (the download failed, or the row predates verification), so counting either as a pass would report an unverified run as a verified one.

**Native testnet clients only.** `uploader.sh` hashes each generated file before invoking `ant` and publishes `<address> <bytes> <sha256>`; `downloader.sh` carries that hash through selection, hashes the output before deleting it, and emits `verify_status` on the `download` Influx point (`0` ok, `1` mismatch, `2` unavailable, omitted entirely on a failed download), with `expected_sha256`/`actual_sha256` on a mismatch only. `record-results` lands them in `deployment_download_records.verify_status` and the uploader's hash in `deployment_upload_records.sha256`. The browser (WASM) tier publishes no hash and verifies nothing (V2-1360), and neither do the production client kinds (V2-1361) — their rows carry NULL and contribute nothing, which is what the coverage figure is for. Two- and one-field address lines still parse, so a mixed run keeps working.

#### What it deliberately does NOT cover

Three hard gates from the release process are **not derivable from anything the registry stores**: **crashes** (panics reach stderr/journald, not the JSON logs Telegraf forwards, and Elasticsearch dies at teardown), **mixed-version chunk storage** (an SSH check on `chunks.mdb` growth), and **production canaries** (a separate deployment with its own assessment). Those are gathered observationally and recorded by the `evaluate-staging-run` skill, alongside this report.

#### Comparability guards

The command refuses outright when the two runs cannot be validly compared:

- **Different `window_label`s** — a steady-state slice and a full run are not the same measurement.
- **Different derived-metric `source`s** — `capture` sums raw counter deltas and is exact, `buckets` integrates averaged rates and is approximate. Comparing them measures the method, not the release.
- **Window lengths differing by more than `max_window_length_ratio` (1.25x)** — replication continues after an upload completes, so a shorter window truncates the disk numerator while still counting the full denominator.

It **warns** (rather than refusing) when the two runs' **activity coverage** — the fraction of the window in which clients were actually transferring — differs by more than `max_activity_coverage_ratio` (1.3x). The per-GB numerators accumulate through idle time while the denominators do not, so a mostly-idle window inflates every ratio. Measured across four recorded runs: 39.2% coverage → 14.86 GB/GB, against 52–61% coverage → 9.1–10.1 GB/GB. A window-*length* check cannot catch this, because the length can be perfectly ordinary.

**Both guards can pass on a comparison that is still wrong, so prefer a labelled
phase window over `full-run`.** A full-run window is the span of observed
*metrics*, not the run's shape: a −3h node clock skew drags a host's first points
well before T0, and any post-measurement phase (the mixed-version hour) is swept
in even when the baseline never ran one. On STG-01 #474 against #470 both guards
passed — length 1.13x, coverage 1.12x — while the candidate carried 3h05m of
pre-run data plus that extra phase. The guards check size and idleness, not phase
composition, and nothing records phase boundaries, so it cannot be detected;
`compare` therefore *warns* on `--window-label full-run`. Re-slicing that pair to
steady state moved CPU from +19.28% to +15.29% and disk/GB from −2.35% to
−1.07%, with both runs landing at ~8.7 GB/GB. Slice **both** runs first with
`derive-metrics --id <id> --window-start/-end --label steady-state` — `--id`, not
`--name`, since a name resolves to the newest run bearing it and staging names
are reused every train.

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

The leak window is computed **per host, from that host's own last bucket**, not
from a fleet-wide `max(bucket_start)`. Testnet manifests skew node clocks by ±3h
and Telegraf stamps every point by the host's own clock, so one fleet-wide
cut-off lands inside only the fast hosts' data: on run 470 it assessed **15 of
997 services**.

#### Thresholds

All thresholds live in `deploy/saorsa_deploy/resources/gates/staging-thresholds.yaml`, git-versioned and carrying a `version:` key that is echoed into every report and stored on the persisted verdict. The release process schedules a quarterly recalibration against accumulated run history; that must not require a release of the tool. Override with `--thresholds PATH`. Validation is strict and fails loudly — a threshold silently defaulting to zero would turn a gate into a rubber stamp while still printing PASS.

#### Persistence

The verdict is upserted into `deployment_gate_verdicts`, keyed `(deployment_id, baseline_deployment_id, window_label)` so re-running replaces its previous answer rather than accumulating rival ones. The row stores the full JSON payload, the rendered markdown, and the threshold config version, so the exact text attached to a release issue can be recovered later without recomputing anything.

### Run-time measurement (`analysis`)

The `analysis` command group holds the tooling testnet runs use to take the
measurements their gates rest on. It grew out of the keep-alive A/B
(V2-996/V2-988), the backstop run (V2-1059) and the relay-teardown run
(V2-1061); the remote-side shell payloads are kept verbatim from those runs,
where they were proven on live fleets.

Every command takes `--name <deployment>` and reads hosts, the NAT tier and the
SSH key from `~/.autonomi/testnets/<name>/inventory.json` (`deployments
generate-inventory --name <name>` writes it). An IP that is not in the
inventory is refused rather than treated as an ad-hoc host.

**Why the NAT tier matters everywhere.** NAT-simulated VMs run every node
service inside the `ant_nat` network namespace. `/proc/net/snmp` is
per-namespace, so telegraf — which runs on the host — reports ~0 UDP for those
VMs, and InfluxDB's fleet series silently covers only the plain tier. Captures,
counter snapshots and iptables rules on a NAT'd VM are therefore taken inside
the namespace (`ip netns exec ant_nat …`, interface `veth-node`); plain VMs use
the default-route interface. The commands decide this per VM from the
inventory's `nat_simulated` flag.

**Everything ES/InfluxDB-backed dies with the testnet.** Extract before
teardown; there is no backfill.

| Command | What it does |
|---|---|
| `analysis snapshot --name N --tag T [--ips … \| --tier nat\|plain\|all] [--out F]` | One JSON line per VM of `/proc` UDP datagram / byte / `RcvbufErrors` counters from **both** namespaces. Take one at every phase boundary; a phase figure is the delta between two snapshots (`phase-extract` computes it). |
| `analysis capture --name N --seconds S --tag T --ips …` | File pcap (`/var/tmp/saorsa-analysis-<tag>.pcap`, snaplen 96, `udp and not port 53 and not port 123`) on each VM through a **transient systemd unit**, so it survives the SSH session dying. Aborts if any host fails to start; blocks until done; exits non-zero if any host's span is < 95% of the request. Use for windows that must equal the measurement window. |
| `analysis capture-stream --name N --seconds S --tag T --ips …` | Streams tcpdump into `flowstat` on the VM; only a JSON summary survives. Use for loaded phases (a 10-min loaded pcap is 8–21 GB). Pushes the analyser to the VM first. |
| `analysis fetch --name N --tag T --ips … --out-dir D` | scp back the pcap / json / log / tcpdump.err / probe log for a tag. |
| `analysis restart-nodes --name N --ips … [--services …] [--stagger-seconds 30] --tag T [--out F]` | Graceful stop + start of every (or the named) ant-node service on each VM in turn, recording per-service **stop duration** (the shutdown cost) and per-VM UTC window edges. Exits non-zero if any service is not active afterwards. |
| `analysis stall-probe --name N --ip IP --seconds S --peers K --tag T [--dry-run]` | Blocks UDP to/from K live peers, one at a time, for S seconds via iptables in the nodes' namespace, with trap-cleaned rules and a JSONL log. Always `--dry-run` first on the real VM, then one 3 s smoke probe. |
| `analysis rot-chunks --name N --tag T (--apply --ips … [--services …] [--fraction 0.5] [--min-files 100] \| --verify \| --attribute [--start … --end …]) [--out F] [--json-out F]` | Corrupt a deterministic sample of chunk files **in place** on the given node VMs, then re-read them from disk, then attribute each removal to an audit detection or to a fetch. See below. |
| `analysis smoke --name N` | ES + InfluxDB reachable, node logs arriving, `net` fields present. |
| `analysis log-count --name N --phrase P --start … --end … [--index node\|client] [--hosts …] [--json-out F\|-]` | Count of a log phrase in a window with host cardinality, top hosts and level breakdown. |
| `analysis counter-delta --name N --field tail.X --start … --end …` | Delta and rate of a cumulative per-service counter (per host, per service, max − min; single-sample services reported, not folded in as zero). |
| `analysis pointer-commitments --name N --start … --end … [--hosts …] [--min-rotations N] [--min-hosts N] [--json-out F\|-]` | Commitment-rotation coverage: total rotations, how many carry `pointers=`, how many report `pointers=0`, the non-zero remainder and the host spread of it. See below. |
| `analysis udp-rate --name N --start … --end … [--every 1m]` | Per-host UDP out-datagram rate from InfluxDB (`udp_outdatagrams`, lower case) — plain tier only, see above. |
| `analysis relay-report --name N --start … --end … [--hosts …]` | Counts of every relay-lifecycle phrase plus the `relay recv stream closed` ÷ `Proactive relay torn down` ratio (V2-1061's gate metric), optionally for a host cohort. |
| `analysis teardowns --name N --start … --end … --out F.jsonl` | Exports each `Proactive relay torn down` document (ts, host, service, `relay_addr`, `relay_server`, `allocation_id`). |
| `analysis relay-servers --name N --start … --end …` | Ranks relay servers by number of NAT'd clients, mapped to inventory VMs. |
| `analysis phase-extract --name N --label L --start … --end … [--snap-before F --snap-after F]` | The per-phase record set: dial success/failure, relay events, close-probe phrases, cumulative counters, InfluxDB UDP (plain tier) and the snapshot delta (both tiers). Writes `extract-<label>.json`. |
| `analysis flowstat PCAP [--expect-seconds S] [--label L] [--json-out F] [--lifetimes-out F]` | Per-flow analysis: per-direction keep-alive cadence, edge-censored lifetime histogram, concurrency, churn. Stdlib only; the same file runs on a VM. |
| `analysis backstop PCAP [--local IP]` | Dialler/acceptor role inference and acceptor-originated unsolicited datagrams (the accept-side keep-alive fingerprint). |
| `analysis probe-score PCAP --probes F.jsonl --local IP` | Joins a stall-probe log with a pcap: SURVIVED / DIED per probed flow. |
| `analysis close-delivery PCAP --teardowns F.jsonl --local IP` | For each flow to a torn-down relay address that ended around the teardown: TOLD (last datagram inbound, ≤ 1 outbound after) vs UNTOLD (≥ 2 outbound after the last inbound — the dialler pinging into the void). |

#### `analysis rot-chunks` — injecting the corruption a run has to measure (V2-1442)

ant-node PR #242 (V2-1382) makes a node whose chunk file no longer hashes to its
key report that key from round 1 of a subtree audit, remove the file, stop
claiming the key and let replication refetch it. **Nothing rots on its own
inside a six-hour testnet**, so a run that tests the repair path has to corrupt
the files itself and then prove, from disk and from Elasticsearch, what happened
to each key. The three modes are one invocation each.

**`--apply`** resolves each service's root dir from its unit (`--root-dir` out of
`systemctl show -p ExecStart --value`, falling back to
`/mnt/ant-node-storage/data/<service>`), lists every chunk file under
`<root>/chunks/<xy>/<64 hex>` with its size and sha256, samples `round(n ×
--fraction)` of them with `random.Random(f"{tag}:{host}:{service}")`, copies each
one to `/mnt/ant-node-storage/rot-backup/<tag>/<service>/` and overwrites the
original with the six bytes `rotted`. A service holding fewer than `--min-files`
files is recorded as `too_few_files:<n>` and **not touched at all**. Hosts run in
parallel, services on a host in order. The manifest records each service's own
`rotted_at`, taken from the host's clock at the start of its rot.

**A per-key failure is not a skip.** While any key of a service rotted, the
service is `injected` and its failures are listed in the record's `errors` field
rather than in `skipped` — a transient `cp` would otherwise take a service out of
the run's `injected` count although its keys are in fact rotted. The exit code is
still 1.

The manifest is `~/.autonomi/testnets/<name>/rot-chunks-<tag>.json` (or `--out`)
and is the input to both other modes: it holds the rotted keys and, as the
control set, every untouched key with its size and sha256. **A tag whose manifest
already exists is refused (exit 2)** rather than rotting a second slice. Exit 1
if a host was unreachable or any key failed to rot; the manifest is still
written with what succeeded.

**`--verify`** re-reads every key in the manifest and classifies it: a rotted key
is `removed` (absent), `rotted` (still the injected bytes), `repaired`
(byte-identical to the backup) or `other`; a control key is `missing`, `changed`
or `unchanged`. **A key of a service that answered nothing is `unknown`** — not
read, as against read and found absent. That distinction is load-bearing: an
unreachable host would otherwise report a removal for every injected key and a
loss for every control key, which is exactly the shape of the result the run is
gating on. `unknown` is in no other bucket, so `repaired` + `removed` +
`still_rotted` + `other` falls short of `rotted_keys` by precisely the keys
nobody could read, and the summary names the hosts. It writes
`rot-chunks-<tag>.verify-<UTC yyyymmddThhmmssZ>.json` (or `--json-out`), whose
`.keys[]` a phase script filters on `.class == "repaired"`. **Classifications
never change the exit code** — it is a report. Exit 1 only for an unreachable
host or a missing manifest.

**`--attribute`** joins the manifest with the node log index over
`applied_at`..now (or `--start`/`--end`), matching five ant-node phrases and
pulling the 64-hex key out of each message. Per rotted key it reports the first
detection and the first removal **on that host**, and hence `audit` (detected at
or before the removal — the path under test), `fetch` (removed with no prior
detection), `detected_only` or `none`. It also prints the five phrase counts per
target host and, as the control, **the removal lines on hosts the manifest never
touched**. Output goes to `rot-chunks-<tag>.attribute-<UTC ts>.json`.

`first_detection_latency_s` is measured from the service's own `rotted_at`, not
from `applied_at`: the latter precedes the probe and the full-service `sha256sum`
listing pass by however long those take, which would silently pad every latency.
`applied_at` stays the ES window start, where a floor earlier than the first rot
is only ever safe. `targets_detected` divides by the hosts something was actually
injected on, so a host skipped for `too_few_files` is not counted as a detection
miss.

Each mode prints one unwrapped summary line for the run's phase script to grep:

```
ROT_APPLY tag=<tag> hosts=<n> services=<n> injected=<n> skipped=<n> rotted_keys=<n> key_errors=<n>
ROT_VERIFY tag=<tag> verified_at=<ts> rotted_keys=<n> repaired=<n> removed=<n> still_rotted=<n> other=<n> intact=<n> unchanged=<n> changed=<n> missing=<n> unknown=<n> unreachable_hosts=<ip,… | ->
ROT_ATTRIBUTE tag=<tag> window=<start>..<end> rotted_keys=<n> detected=<n> removed=<n> audit=<n> fetch=<n> detected_only=<n> none=<n> targets_detected=<n>/<m> overflow_lines=<n> not_removable_lines=<n> nontarget_removals=<n>
```

`injected` and `skipped` count **services**, not keys; they sum to `services`.
Every timestamp the tool writes or prints is UTC ISO-8601 to the second with a
`Z`, so a phase script can compare them as plain strings, and the path of every
JSON file written is printed.

Two warnings:

* **The file is overwritten in place, never deleted or renamed** — the redirect
  truncates and rewrites the same inode under the same name. A *missing* file
  goes down a different code path in ant-node, so a run that deleted them would
  measure something else entirely.
* **Keep the node's `verify_on_read` at its default**, or nothing is repaired:
  with verification off the node hands the rotted bytes back and never reports
  the key, and every rotted key reads as `rotted` for the whole run.

`--ips` is required by `--apply` (the target set is never implicit) and ignored
by the other two modes, which take their host set from the manifest.

**Keys are staged on the host as a file, never passed on the command line.** ssh
hands the whole `bash -s -- …` command to the remote shell as one argument, which
the kernel caps at `MAX_ARG_STRLEN` (128 KiB) — about 1,950 keys — and `--verify`
sends a service's rotted *and* control keys together, so a busy service would
fail outright. `remote.push_file` writes the list to `/var/tmp/` and the remote
script deletes it when it is done. The `stdin_extra` seam cannot carry it: bash
reads a piped script in blocks, so a `while read` loop consumes the rest of the
script text and the statement after the loop never runs (measured, at every
payload size).

#### The failure the capture commands guard against (V2-988, 2026-08-15)

Arm B's decisive 20-minute idle window died 29 seconds in and nobody noticed
until analysis, by which time clients had restarted and the window was gone;
the run was abandoned. The capture had been owned by the SSH session, and a
truncated window does not look broken: it yields an **all-zero lifetime
histogram** (nothing can be born *and* die in a short window, so everything is
censored — which reads as "no connections died") and a **downward-biased
cadence**. Hence: captures launch through `systemd-run` so PID 1 owns them,
`capture` verifies the pcap is growing before reporting success, every capture's
span is verified against the request before any derived number is read, and
`flowstat --expect-seconds` exits non-zero on a short window.

#### `analysis pointer-commitments` — making a clean audit count falsifiable (V2-1413)

`deployments record-log-counts` counts the auditor-side
`failed subtree audit ({reason:?})` WARN, one catalogue pattern per
`AuditFailureReason` variant, summed into the `subtree_audit_failure` group. The
A/B that decides ant-node #238 turns on the `DigestMismatch` member. But a
candidate arm reporting zero of them is **unfalsifiable on its own**: it reads
identically to a run whose audits never opened a pointer leaf, and ant-node
v0.21.0 has no info-level line for a successful round-2 pointer opening.

The one available proxy is the commitment rotation,
`info!("Storage commitment rotated: hash={hash} key_count={key_count} pointers={pointer_count}")`.
The whole line is one unparsed `tail.message` — `hash=`, `key_count=` and
`pointers=` are **not** fields — so telling `pointers=0` from `pointers=7` needs
a wildcard on `tail.message.keyword`, which `match_phrase` cannot express and
the catalogue therefore cannot answer. Hence a command rather than a seventh
pattern. The query shape is taken verbatim from `evaluate_commitment` in
`scripts/rollout-watch/pointer_signals.py`, already proven against ant-prod-01;
the only differences are the index and the absence of a version arm (a testnet
run is single-version).

Four statuses:

* **NOT APPLICABLE** — no rotation in the window. Says nothing about coverage;
  widen the window rather than reading a zero failure count as clean.
* **FAIL** — either some rotations carry no `pointers=` at all (a service on the
  fleet is not pointer-capable, so the audit counts describe two builds), or
  every rotation reports `pointers=0` (commitments rotated and never covered a
  pointer leaf — exactly the unfalsifiable case).
* **INSUFFICIENT** — a supplied `--min-rotations` / `--min-hosts` floor was not
  met. The detail line names both observed values and both floors.
* **PASS** — every rotation carries `pointers=` and some report a non-zero
  count, with the host spread of the non-zero ones alongside it.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--min-rotations` | int | No | — | Floor on rotations observed; below it the status is `INSUFFICIENT`. |
| `--min-hosts` | int | No | — | Floor on the number of rotating hosts; below it the status is `INSUFFICIENT`. |
| `--json-out` | path | No | — | Write the verdict as JSON here; `-` or `/dev/stdout` writes it to stdout as the only output. |

**Composition and sufficiency are different questions, and the floors are how
the second one gets asked.** The rotation line is emitted at most hourly per
node (`COMMITMENT_ROTATION_INTERVAL_SECS = 3600`, no jitter) and only when the
node's responsible key set changed, so a short window legitimately holds one or
two rotations. One rotation on one host carrying a non-zero `pointers=` is
perfectly well formed, and before V2-1450 it printed **PASS** — which is what
DEV-06 run 647 reported against its own criterion of ≥20 rotations on ≥5 hosts,
a miss by more than an order of magnitude the status did not reflect. So pass
the run's own floors and read `INSUFFICIENT`; with **neither** floor supplied
the compositional verdict still stands but its detail line says in as many
words that volume and host spread were **not assessed**.

Two orderings inside the verdict are deliberate:

* a rotation **missing** `pointers=` FAILs at any volume — one mis-shaped line
  proves a non-pointer-capable service is on the fleet, which is a statement
  about the lines rather than about how many there were;
* a shortfall outranks the all-`pointers=0` FAIL — "no commitment ever covered a
  pointer leaf" is a claim about absence, and one rotation cannot support it.

The floors are measured against total rotations and rotating hosts, not against
the non-zero subsets: those two describe the *observation*, so one floor means
the same thing whichever verdict follows (`non_zero` is 0 by definition in the
all-zero case, and a floor on it would turn that genuine finding into a
shortfall every time).

It exits **0 whatever it finds**: this is a measurement taken before teardown,
not a gate. `deployments compare` is untouched.

#### `--json-out -` — machine-readable output for an unattended runner

Every `analysis` command that takes `--json-out` accepts **`-` or
`/dev/stdout`** to mean "the JSON *is* the output": the object goes to stdout
exactly once and **nothing else does** — no `wrote …` trailer, no table, no
preamble, no `[dim]` footnote. (The one exception is `analysis flowstat`, which
is pushed to node VMs and run under a bare `python3`, so it may not import the
shared helper — see its row above. It is always driven with a real path.) That
is what makes

```bash
cnum() { uv run saorsa-deploy analysis log-count --name "$NAME" --index client \
         --phrase "$1" --start "$2" --end "$3" --json-out - 2>/dev/null \
         | jq -r '.count // 0'; }
```

return the true count. A real path keeps today's behaviour, trailer included.

Before V2-1450 these commands printed the object as their own human output
*and* wrote it to the `--json-out` destination, so `--json-out /dev/stdout`
emitted it twice with the trailer on top: `jq` parsed the duplicates and then
aborted on the trailer, and the `// 0` default turned that into a confident
**0**. That is how DEV-06 run 647's reachability table came out as thirty zeros
for a fleet with 100% upload and download success. The `tail -1` mitigation
carried in several run issues does not cover it — the failure is a parse abort,
not a stray preamble line.

#### Query traps these commands bake in

* ES normalises `tag.host` to **upper case**; host filters match both spellings.
* Tag fields are analysed — aggregations use `.keyword`; the message field is
  `tail.message` (no document has `message`); counts use `track_total_hits`.
* Telegraf lowercases the InfluxDB field to `udp_outdatagrams`; the `/proc`
  spelling returns a silent zero.
* Every Flux `range()` carries an explicit `stop`, and Flux CSV is split on
  raw text (blank lines separate tables; `iter_lines()` eats them).
* Cumulative counters are emitted every 300 s, so a window's effective
  coverage is up to 5 min shorter than the window.

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

## Recording releases and channel state

`saorsa-deploy releases ...` answers "what is on stable, what is on beta, and
how long has the beta soaked" from the registry. Before this existed the answer
lived only in GitHub tags and hand-transcribed prose in a Linear issue, and
nothing at all recorded a beta that the channel rolled past without promoting.

Two tables, alongside the existing `release_candidates` set:

- **`releases`** — one row per `(binary_target, version)`, where `version` is the
  **final, suffix-free** version (`0.17.1`). It is a table of its own rather than
  lifecycle columns on `release_candidates` because an `rc.N` respin cuts a *new*
  RC row: a train that needed three respins has three RC rows but ships one
  release. The release is the lineage that shipped.
- **`release_crates`** — the release manifest, one row per released crate.
  Deliberately not a reuse of `release_candidate_crates`: those carry the
  rc-suffixed versions and the RC tags (`evmlib 0.9.1-rc.1`,
  `ant-cli-v0.3.3-rc.1`), whereas a released crate carries the suffix-free
  version and the final tag.

Granularity matches the RC tables — **one row per binary target**. A train
records two releases (`ant-node` and `ant`) sharing one `rc_branch`; some trains
ship only one.

### The lifecycle

```
        ship                promote
  (nothing) ──▶ shipped_to_beta ──▶ promoted     (terminal)
                       │
                       └── rolled-past ──▶ rolled_past  (terminal)
```

`shipped_to_beta` is the only entry state. `promoted` and `rolled_past` are
**terminal** and the repository refuses to leave them: a stable version that
quietly stopped being stable, or a dead train that came back to life with no
recorded reason, would make every later answer untrustworthy.

`DATABASE_URL` must be set. If it is not, these commands exit non-zero and say
so — they do not silently no-op the way the deployment registry does.

### `releases ship`

Recorded by the **ship-to-beta** skill at Wednesday's beta ship, once the
prerelease has been verified. Idempotent on `(binary-target, version)`, so a
re-run after a partial failure refreshes the row rather than failing.

| Argument | Required | Description |
|---|---|---|
| `--binary-target` | yes | `ant` or `ant-node` |
| `--version` | yes | The **final, suffix-free** version (`0.17.1`) — *not* the beta version. The suffix belongs in `--beta-tag`. This is what makes the row the same lineage `promote` later completes. |
| `--rc-branch` | yes | e.g. `rc-2026.8.2` |
| `--beta-tag` | no | `v0.17.1-beta.1`, or `ant-cli-v0.3.3-beta.1` for ant |
| `--beta-tag-sha` | no | The commit the beta tag points at (the rc-branch head) |
| `--shipped-at` | no | ISO-8601 or `YYYY-MM-DD`. Defaults to now |
| `--planned-promotion` | no | The following Monday — the soak deadline `releases current` reports |
| `--rc-version` | no | e.g. `0.17.1-rc.1`, used to link the `release_candidates` row by natural key. A miss warns and records the ship anyway |
| `--notes` | no | Free text |

A bare `YYYY-MM-DD` is read as midnight **UTC**, and a naive timestamp likewise,
so the same command run from two machines records the same instant.

```bash
saorsa-deploy releases ship \
  --binary-target ant-node --version 0.17.1 --rc-branch rc-2026.8.2 \
  --beta-tag v0.17.1-beta.1 --beta-tag-sha "$SHA" \
  --planned-promotion 2026-08-24 --rc-version 0.17.1-rc.1
```

### `releases promote`

Recorded by the **release-from-rc** skill at Monday's promotion. Takes a JSON
payload rather than flags because it carries the release manifest. The release
must already be recorded as shipped to beta — a promotion with no ship is a
release that skipped the soak.

```bash
saorsa-deploy releases promote --from-file /tmp/release-manifest.json
```

```json
{
  "binary_target": "ant-node",
  "version": "0.17.1",
  "final_tag": "v0.17.1",
  "final_tag_sha": "cf7de43532833773f352a343a4201f3036623601",
  "promoted_at": "2026-08-24T09:00:00Z",
  "rc_version": "0.17.1-rc.1",
  "notes": "optional",
  "crates": [
    {
      "crate_name": "ant-cli",
      "version": "0.3.3",
      "repo_slug": "WithAutonomi/ant-client",
      "tag": "ant-cli-v0.3.3",
      "tag_sha": "cf7de435..."
    }
  ]
}
```

`binary_target` and `version` are required; every other top-level key is
optional.

**`--allow-missing-ship`** is the deliberate exception to "the release must
already be shipped to beta": it records the promotion with `shipped_to_beta_at`
left NULL, which reads as *never on beta* rather than as a soak of unknown
length. Use it for releases promoted under the **pre-beta train structure** and
for backfill; it needs `rc_branch` in the payload, since no ship record supplied
it. If a ship record does exist, the flag changes nothing — the real soak is
preserved.
 Within `crates[]`, `crate_name`, `version` and `repo_slug` are
required and `tag`/`tag_sha` are optional — `ant-core` is bumped with every
ant-client release and tagged with none of them. Supplying `crates` **replaces**
the manifest, so a re-run after a correction leaves no stale crate behind.

### `releases rolled-past`

Recorded when a beta health verdict comes back **NO-PROMOTE**: the train dies
unpromoted, stable stays put, and the beta channel moves on with the next
Wednesday's ship.

| Argument | Required | Description |
|---|---|---|
| `--id` | one of | Release id (preferred) |
| `--binary-target` + `--version` | one of | Natural-key lookup |
| `--reason` | **yes** | Why the train died. Required — a rolled-past row whose reason is unknown tells the next release manager nothing that the release simply being absent would not |

### `releases current`

The one-shot answer. Per binary target: current stable, current beta, soak age
against the planned promotion date, and the betas **rolled past since the
current stable was promoted**. That last count is the widened stable
version-jump window the next promotion has to carry — a fleet that skipped two
betas jumps N+3, not N+1.

```bash
saorsa-deploy releases current
saorsa-deploy releases current --binary-target ant-node
```

Soak state has three values, not two: elapsed, not yet elapsed, and **unknown**
(the ship recorded no planned promotion date). "No deadline recorded" is
deliberately not reported as "the deadline has not arrived".

### `releases ls` / `releases show`

`ls` lists releases newest-first with their manifest crates indented underneath;
`--binary-target`, `--outcome` (`shipped_to_beta` / `promoted` / `rolled_past`)
and `--limit` filter it. `show` renders one release and its full manifest, taking
either `--id` or `--binary-target` + `--version`.

### Exit codes

`2` for bad input or usage (unparseable timestamp, malformed payload, a refused
lifecycle transition); `1` for no database or a release that could not be found.

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

### Upgrading the production bootstraps ahead of the fleet (`prod bootstrap upgrade`)

The seven production bootstraps are every client's way into the network, so they
are upgraded to a new ant-node release **ahead** of the fleet's own 24h staged
rollout — and, in the same restart, have their WebRTC-Direct listener pinned to a
fixed UDP port. Unpinned, ant-node derives that port afresh on every start, so any
published browser address goes stale the moment the service restarts.

```bash
uv run saorsa-deploy prod bootstrap upgrade --node-version 0.20.0 -y
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--node-version` | string | **required** | Released ant-node version to install (e.g. `0.20.0`) |
| `--inventory-path` | string | `~/.autonomi/testnets/ant-prod-01/bootstrap-inventory.json` | Bootstrap inventory JSON |
| `--webrtc-port` | int | `10001` | UDP port to pin the WebRTC-Direct listener to |
| `--limit-to` | string | every host | Comma-separated host names or regions, upgraded in that order (e.g. `nrt,syd`) |
| `--skip-firewall` | flag | - | Skip the provider firewall preflight |
| `--ssh-key-path` | string | per-host `ssh_key` | Override the inventory's SSH key |
| `-y`, `--yes` | flag | - | Proceed without the confirmation prompt (required on a non-TTY) |

Order of operations:

1. **Local preflight.** Fetch `SHA256SUMS.txt` and `ant-node-cli-linux-x64.tar.gz`
   for the version, check the archive's sha256 against the sums file, extract it and
   run `--version`. That verified archive is what is uploaded to every host, so all
   seven demonstrably run identical bytes.
2. **Firewall preflight** (unless `--skip-firewall`). Hetzner hosts get a `udp/<port>`
   inbound rule added to their attached firewall via `hcloud` (skipped if one already
   exists); the AWS host gets the same via `aws ec2 authorize-security-group-ingress`
   against the security group of the instance holding its public IP, treating
   `InvalidPermission.Duplicate` as success; the four Vultr hosts have no firewall
   group attached and need nothing. Needs `HCLOUD_TOKEN` and AWS credentials in the
   environment. This runs for the whole tier up front — restarting a host onto a
   listener the provider still blocks would fail its probe and roll it straight back.
3. **Per host, serially, in inventory (or `--limit-to`) order.** The binary path, root
   dir, log dir and service user are read from the live unit's `ExecStart` — **not**
   from `bootstrap_production.BINARY_INSTALL_PATH` (which read `/usr/local/bin/ant-node`
   until V2-1309 corrected it to the `/opt/ant-node/ant-node` the tier actually runs;
   the rule stays "read the unit", because a constant cannot know what a host was
   given by hand). A unit that does not
   pass `--stop-on-upgrade` or `--root-dir` is refused. The archive is uploaded,
   extracted, `chown`ed to the service user and run with `--version` on the host; the
   old binary is copied to `<binary>.backup`, the new one is moved onto the binary
   path, and **the whole binary directory is then `chown -R`ed to the service user**
   and verified with `find <dir> ! -user <svc>` — any output at all raises, which
   rolls the host back and stops the run (see "the ownership invariant" below); the
   unit gains one appended `--webrtc-direct-port <port>` argument line and nothing
   else changes; then `daemon-reload` and `restart`.
4. **Health gate**, up to 1200 s, reading only what the restart wrote: the node's own
   JSON log must carry `ant-node starting` at the requested **version** (an old binary
   brought back by `Restart=always` writes that line too), then
   `ADR-0015 WebRTC Direct listening` bound to the pinned port, then
   `systemctl is-active` = `active`.
   The 1200 s is calibrated, not generous: ant-node 0.20.0 opens the WebRTC listener
   only after rejoining the network, and the original 180 s **rolled back a healthy
   host**. Measured on the production tier at 1m43s and 2m36s (nrt) and over 3m29s
   (syd); after the 2026-09-25 `set-peers` restart it took 14m26s (fsn1) and 13m35s
   (ash), which 600 s would also have failed.
5. **ICE probe** from this machine to `<inventory ip>:<port>` — three attempts, 5 s
   each. A plain STUN binding is not enough: the listener's admission filter answers
   anything unauthenticated with silence, so the probe sends a real ICE connectivity
   check (see below).
6. **On any failure**: the backup binary and the original unit are restored — the
   restore `chown -R`s the binary directory too, for the same reason the install does
   — the service is restarted, `RESTORED` is printed and the run **stops**; hosts
   after it are untouched.
7. **On success**: 60 s settle, then the next host. After the last one,
   `webrtc-addresses` runs automatically and prints the file it wrote. **Collection
   covers the whole inventory even under `--limit-to`** (see below).

For a `--node-version` below `0.20.0` the WebRTC half is reported as `not expected`:
the **unit is left alone**, the gate passes on the start line alone, the probe is
skipped and no address list is collected. The unit exemption is load-bearing —
ant-node 0.19.0 has no `--webrtc-direct-port` at all (verified on the live tier), and
clap exits on an unknown argument, so appending it would leave a bootstrap that
cannot start. That is what makes a same-version rehearsal
(`--node-version <current> --limit-to nrt -y`) exercise the whole path safely.

#### The ownership invariant

**When this command leaves a host, every entry under the directory holding the
binary (`/opt/ant-node` on the live tier) is owned by the unit's `User=`.** The
command *establishes* that, it does not merely test for it.

It is load-bearing because ant-node's own auto-upgrader runs as the service user and,
before replacing the binary, writes `<binary>.backup` with `fs::copy` — which cannot
overwrite a file it does not own. Six of the seven hosts log in as `root`, and `cp`
without `-p` creates its destination owned by the invoking user, so until V2-1412 this
command left a `root:root` backup behind. ant-bootstrap-nrt and ant-bootstrap-syd then
sat retrying hourly on 0.20.0 — `Backup creation failed: Permission denied (os error
13)` — for the whole of the 0.21.0 rollout, alive and serving but a release behind,
with nothing alerting on it. The condition was created on 2026-09-24 and first became
visible 35 hours into the *next* train's rollout.

`chown -R` on the directory, not `cp -p` and not a chown of the one file: `cp -p`
preserves the *source's* owner, so a root-owned live binary would yield a root-owned
backup again, and a per-file chown covers only the files this command knows about —
the directory also holds `bootstrap_peers.toml` and the upgrader's own `ant-upgrade-*`
temp dir, which are the service user's anyway. The `find` afterwards should never
fire; its job is to fail loudly if a chown did not take, rather than leave a host
silently un-upgradeable until the next train.

`prod bootstrap deploy` applies the same rule at provision time (`chown -R` on
`/opt/ant-node` after the binary install, before the unit is written), so a rebuilt
bootstrap does not come up with a root-owned directory — in which the upgrader's
`tempdir_in(binary_dir)` would fail outright.

#### `--limit-to` never shrinks the published address list

The address collection that runs after the last host reads the **whole inventory**,
never the `--limit-to` subset, and says so when they differ. The file it writes is the
complete published bootstrap set that every prod WASM client kind consumes, so
rewriting it from the two hosts a remediation touched would silently narrow every
browser client's way into the network to those two. The all-or-nothing guard inside
collection protects against an unreachable host; it cannot tell a short input list
from a complete one.

### Reporting the bootstrap tier (`prod bootstrap status`)

Read-only. Nothing is written, no service is restarted, and no host is refused — it is
what an operator (or the maintenance skill) reads *before* deciding whether a mutating
verb may be used, so a host it cannot describe is the one it most needs to describe.

```bash
uv run saorsa-deploy prod bootstrap status
uv run saorsa-deploy prod bootstrap status --json | jq '.[] | {name, backup}'
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--inventory-path` | string | `~/.autonomi/testnets/ant-prod-01/bootstrap-inventory.json` | Bootstrap inventory JSON |
| `--limit-to` | string | every host | Comma-separated host names or regions, reported in that order (e.g. `nrt,syd`) |
| `--ssh-key-path` | string | per-host `ssh_key` | Override the inventory's SSH key |
| `--json` | flag | - | Write the rows as a JSON array, the only thing on stdout |

Per host, every read through the same `sudo`-wrapping the mutating verbs use:

| JSON field | Source |
|------------|--------|
| `name`, `ip`, `provider` | the inventory |
| `reachable`, `error` | whether every read below succeeded |
| `unit_ok`, `unit_error` | `cat /etc/systemd/system/ant-bootstrap.service`. A unit `upgrade` would **refuse** still produces a row, with the refusal in `unit_error` |
| `service_user`, `binary_path`, `root_dir`, `log_dir` | the parsed unit (`null` where unextractable) |
| `webrtc_port`, `stop_on_upgrade` | the parsed unit |
| `on_disk_version` | `<binary_path> --version`; `null` if it will not run |
| `running_version` | the `version` of the `ant-node starting` line belonging to the **current** run: `systemctl show -p ExecMainStartTimestamp` gives the date, and only that date's `*.log` is grepped. `null` if systemd will not say or the log has rotated away — an older start line is never substituted, since it belongs to a run that has ended |
| `active` | `systemctl is-active ant-bootstrap`, stripped (`active`, `inactive`, `failed`, …) |
| `backup` | `{present, owner, size}` from `stat -c '%U:%G %s' <binary_path>.backup`. **This is the field the V2-1412 incident is read from**: an owner that is not the service user is a host whose own upgrader can never upgrade it again |
| `foreign_owned` | the paths `find <dirname(binary_path)> ! -user <service_user>` printed; `[]` when clean |

`on_disk_version` and `running_version` disagreeing is its own finding: an install that
landed but could not restart, or a service down since before one.

Scoping the start-line grep to the service's own start date is not an optimisation.
ant-node rotates its log daily and keeps 14, so **the newest file is the wrong one**: a
healthy bootstrap that last restarted when it was upgraded has no start line in today's
file at all — measured on all seven live hosts on 2026-10-06, where reading only the
newest reported "no running version" for every one of them. And walking the files
newest-first instead means reading everything written since that restart: 4.7 GB on
ant-bootstrap-nrt, off a production disk, for one line.

**Exit code: `0` when every host was reachable, `1` when any was not.** It is a
measurement, not a gate — the exit code says only whether the report is complete.
There is deliberately no `--expect-version`: the caller compares the values.

### Publishing the bootstrap WebRTC addresses (`prod bootstrap webrtc-addresses`)

```bash
uv run saorsa-deploy prod bootstrap webrtc-addresses
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--inventory-path` | string | `~/.autonomi/testnets/ant-prod-01/bootstrap-inventory.json` | Bootstrap inventory JSON |
| `--out` | string | `deploy/saorsa_deploy/resources/prod_bootstrap_webrtc_multiaddrs.txt` | Where to write the list |
| `--ssh-key-path` | string | per-host `ssh_key` | Override the inventory's SSH key |

Reads `<root_dir>/webrtc-direct.multiaddr` off each bootstrap (root dir from the live
unit), validates it, rewrites the `/ip4/<host>` component to the **inventory** IP —
the two Hetzner hosts carry a second public address on the same interface and only
the inventory one is in the client's bootstrap list — probes it, and writes one
multiaddr per line in inventory order.

**All or nothing**: a missing file, a malformed address or a failed probe on any host
exits non-zero, naming the host, and leaves the existing file untouched. A short list
published as the production bootstrap set would silently narrow every browser
client's way in.

#### The ICE reachability probe

`saorsa_deploy/webrtc_probe.py` — `probe(ip, port, timeout, attempts=1)`, standard
library only so the rollout-watch scripts bundle can import it. It sends a STUN
Binding Request carrying `USERNAME` = `saorsa+webrtc+v2/<client-pwd>:<client-ufrag>`
(the V2 credential scheme a WebRTC-Direct listener admits an unsolicited dialler
under), a short-term `MESSAGE-INTEGRITY` keyed on that server fragment, and a
`FINGERPRINT`. The listener replies with a connectivity check of its own; any
well-formed STUN message back from the probed address inside the timeout is a pass.
DTLS is deliberately not completed — the question is whether packets reach the
listener and come back, not whether a browser can fetch a chunk.

### Rewriting the bootstrap peer lists (`prod bootstrap set-peers`)

Each of the seven units carries `--bootstrap` arguments naming the peers that host
dials on a cold start. They were written by hand and never revisited: as of
2026-09-24 five hosts listed the same three addresses, of which one was a current
bootstrap and two answered nothing, and another listed three that appear in no
inventory at all. The tier worked anyway, because every host had a populated
`routing_snapshot.json` and peer cache — so the defect was invisible right up until
a host lost its data directory and had at most one live peer to rejoin through.

```bash
uv run saorsa-deploy prod bootstrap set-peers -y
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--inventory-path` | string | `~/.autonomi/testnets/ant-prod-01/bootstrap-inventory.json` | Bootstrap inventory JSON |
| `--peer-port` | int | `10000` | Port every peer is listed on — the port the tier listens on, and the one the client's `bootstrap_peers.toml` uses |
| `--limit-to` | string | every host | Comma-separated host names or regions, rewritten in that order (e.g. `nrt,syd`). The peer list is still computed from the **whole** inventory |
| `--ssh-key-path` | string | per-host `ssh_key` | Override the inventory's SSH key |
| `-y`, `--yes` | flag | - | Proceed without the confirmation prompt (required on a non-TTY) |

Per host, serially, in inventory (or `--limit-to`) order: read the live unit, rewrite
its `--bootstrap` arguments to **the other inventory hosts on `--peer-port`, never
itself**, `daemon-reload`, `restart`, wait for `ant-node starting` in the host's own
log and `systemctl is-active` = `active`, then settle 60 s before the next host. Any
failure restores the unit the command found, restarts onto it, prints `RESTORED` and
**stops the run** — hosts after it are untouched. The AWS host logs in as `ubuntu`
and wraps its commands in `sudo -n`, like the other verbs here.

Two properties make it safe to run against all seven:

* **Only the `--bootstrap` lines move.** Every other line survives byte for byte —
  these units carry hand-maintained arguments (the rewards address, the log flags)
  that nothing in this tool knows the reasons for, and a re-render from
  `build_systemd_unit` would discard them.
* **It is idempotent.** A host already listing exactly those peers, in that order, is
  left alone and **not restarted**, so a re-run after a failure part way through the
  tier only touches what is left.

Unlike `upgrade` this swaps no binary, so the gate does not match on a version: the
host comes back on the build it went down on, and the only question a start line has
to answer is whether it came back at all.

`prod bootstrap deploy` applies the same rule at provision time — every node, genesis
included, is given the other inventory hosts on the node port — so a redeployed
bootstrap no longer comes up with the single genesis peer it used to get.

### Running production maintenance from an issue (unattended)

Testnet runs are driven unattended from a Linear issue by the
`run-testnet-from-issue` skill plus `scripts/run-testnet-issues.sh` (independent
runs) or `scripts/run-testnet-chain.sh` (a chain, where the first run that is not a
clean `PASS` stops the rest). Production maintenance has the same shape and a
deliberately different discipline:

```bash
scripts/run-maintenance-issues.sh V2-1445[,V2-1446,...]
```

Each issue is handed to the **`run-prod-maintenance-from-issue`** skill in a headless
`claude -p` session. Independent mode only — a maintenance that depends on another one
finishing is a judgement a human makes between the two. Exit `0` only if every run was
a `PASS`; `130` on Ctrl-C. `CLAUDE_MODEL`, `MAX_RESUMES` and `LOG_DIR` (default
`~/.autonomi/maintenance-runs`) are the same knobs the testnet scripts take.

`scripts/lib/issue-runs.sh` holds what both share: the loop, the verdict parser, the
resume-on-API-error behaviour and the interrupt handler, parameterised by skill name,
session-name prefix and interrupt policy. `scripts/lib/testnet-runs.sh` is a thin
wrapper over it that selects the testnet skill and the testnet policy.

**The two interrupt policies are the point of the split.** A testnet run's Ctrl-C
kills any `saorsa-deploy deployments up` it started and names what may be left — the
fleet is scratch, and a half-built one bills for nothing. A maintenance run's Ctrl-C
**waits** for any `saorsa-deploy prod …` process to finish and stops the session only
after it has; **the command is never signalled**. Killing one part way through —
between the binary swap and the restart, say — is the one way this tooling can itself
break a production host, and only the live command can run its own rollback. The order
matters: the command runs as a grandchild of the claude session, via its Bash tool, so
stopping the session first could kill it by proxy. Ctrl-C therefore blocks until the
command finishes. The operator who truly must kill it does so by hand, knowingly.

#### The issue contract

The skill refuses an issue, before doing anything, that is missing any of these
headings, naming the one it could not find: `## Purpose`, `## Target` (tier, hosts,
inventory, SSH key), `## Procedure` (the exact commands, in order),
`## Preconditions`, `## Validation` (checks with **expected values**),
`## Stop conditions`, `## Hard ceiling` (a duration from the start of the Procedure)
and `## Rollback`.

It also refuses an issue whose Procedure or Validation contains a step that is not a
`uv run saorsa-deploy …` command, a read-only check (SSH running only
`stat`/`cat`/`ls`/`find`/`grep`/`systemctl is-active`/`--version`, `git` reads, an
HTTP GET) or a Linear update. **A raw mutating SSH or shell step is refused.** If a
maintenance needs something no command can do, the answer is a change to
`saorsa-deploy`, with tests, not a skill that types shell at production.

Three rules it runs under, all for the same reason:

* **A mutating command is never re-run after a failure.** A non-zero exit is a stop
  condition: these commands are serial and gated, so the host they stopped on is the
  only one in an unknown state and the hosts after it are untouched. The run is
  abandoned, the host's final state is reported — in particular whether the command
  printed `RESTORED` or `RESTORE INCOMPLETE` — and the next step is a human's, most
  likely a second maintenance issue.
* **At the hard ceiling no new command is started**, and one already running is waited
  for however long it takes.
* **A command whose log exists was started and is never started again**, which is what
  makes a resumed session (the driver resumes one that died on an API error) safe. The
  Procedure's commands are written to
  `~/.autonomi/maintenance-runs/<issue>/commands.sh` before any of them runs, and each
  is teed to its own `<n>-<verb>.log` in the same directory with its UTC start, end
  and exit code.

#### Verdicts

| verdict | when |
| -- | -- |
| `PASS` | the whole Procedure ran, every Validation check matched, no warning |
| `WARN` | no check failed, but there is a warning — including "the run did nothing because a precondition made it unnecessary" |
| `FAIL` | at least one Validation check did not match |
| `ABANDONED` | a precondition failed, a stop condition fired, or the ceiling was reached before the Procedure completed |

The results comment leads with the verdict and then the warnings — `RESTORE INCOMPLETE`
is always the first warning — followed by the Validation table, a per-host before/after
table from the read-only status the issue names (`prod bootstrap status --json` for the
bootstrap tier), every command with its times and exit code, and the deviations. The
issue **always** ends `In Review`, never `Done` or `Canceled`, whatever the verdict.

The final line is `RUN COMPLETE <issue> <verdict> -`; the trailing dash keeps the field
count identical to the testnet line, so one parser serves both.

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

### The daily production write test (`prod continuous-uploader`)

Nothing else in this repo tests **writes** to production on a schedule. The
daily download test (`prod continuous-downloader`) re-fetches the same fixed
list, which proves old data survives; if writing broke tomorrow, no run here
would notice. This kind closes that gap: one VM uploads a few pool files a day
and reads them straight back, and VMs in other regions independently download
every address and compare a SHA-256 against what was actually uploaded.

```bash
# Once, before the first deploy: create the scoped registry role the VMs use.
saorsa-deploy prod pool client-role
export POOL_DATABASE_URL='postgresql://ant_pool_client:...@host/db?sslmode=require'
export ARBITRUM_ONE_SECRET_KEY='0x...'      # funded Arbitrum One wallet

saorsa-deploy deployments up --config deploy/manifests/runs/PROD-UL-DAILY-01.yaml
saorsa-deploy deployments print-recorded-results --name PROD-UL-DAILY-01 --details
saorsa-deploy deployments down --name PROD-UL-DAILY-01
```

#### How it differs from every other pool consumer

`prod uploaders`, `prod canary` and `prod local-uploads` all draw their files
**before any infrastructure exists**, because they need the whole set up front
and a shortfall discovered after Terraform has spent mainnet money is
unacceptable. This kind runs forever, so there is no "whole set". The uploader
VM holds its own database connection and calls `claim_pool_files` **at the
start of each cycle**, which means:

* nothing is reserved in advance, so the canary's inventory is not locked up by
  a deployment that will not use it for months;
* a teardown strands nothing, because only the current cycle's files are ever
  claimed;
* the run is genuinely open-ended rather than a batch with an expiry date.

Deploy time therefore only performs an **availability preflight** — it checks
the pool can serve at least one cycle, prints the remaining runway, and refuses
before building anything if it cannot. It deliberately does not claim.

#### What the VMs can reach

The uploader and verifiers are the only VM-side services in this repo that talk
to the registry. They run under a venv at `/opt/ant-registry-client` where
`saorsa-db` is installed, so they call the same tested `claim_pool_files` the
operator-side commands do — the canonical-id resolution, the race arbitration
on the partial unique index and the all-or-nothing pre-check are not
reimplemented, because a second copy that drifts double-spends files.

`prod pool client-role` mints a least-privilege role for them:

| | tables |
|---|---|
| SELECT | `prod_file_pool`, `prod_file_pool_digests`, `prod_file_pool_tags`, `deployments` |
| SELECT/INSERT/UPDATE | `prod_file_pool_claims`, `deployment_prod_upload_results`, `deployment_prod_upload_verifications` |
| never granted | DELETE anywhere, any DDL, everything else in the registry |

The connection string is printed, never stored — the manifest names an
environment variable (`pool_database_url_env`), exactly as it does for the
wallet key.

#### Content verification

The uploader takes a SHA-256 over **the exact bytes it hands to `ant`** and
stores it on the upload row. Every verifier hashes what it downloads and
compares. This does not depend on the pool entry carrying a catalogued digest —
many carry only md5/sha1 and some carry none — and it is the first real content
verification anywhere in this system; the data-integrity gate in `deployments
compare` reads NOT ASSESSED precisely because none existed.

Four verdicts are recorded, and `digest_mismatch` is deliberately **not** a
flavour of failure:

| status | meaning |
|---|---|
| `ok` | downloaded and the hash matched |
| `digest_mismatch` | the network returned bytes, and they were **wrong** |
| `download_failed` | the address could not be retrieved |
| `inconclusive` | no verdict possible (no stored hash, timeout) — never counted as a loss |

A recorded mismatch is never overwritten by a later success. Bytes that came
back wrong once are a permanent fact; a later `ok` means the network healed.

**Only `ok` and `digest_mismatch` are final.** A `download_failed` or
`inconclusive` address is re-checked on the next cycle — otherwise a single
transient blip on a verifier would brand a perfectly healthy file unavailable
forever, an error in the one direction that matters here. A file that genuinely
cannot be fetched is therefore retried every cycle, which is the signal worth
having. Because a retry overwrites the row, `attempts` and `first_failed_at`
record whether something failed once or has been failing all week;
`print-recorded-results` surfaces the longest-standing failure per region.

#### Why the verifiers do not divide the work

Every verifier checks **every** address. The canary shards its probes across
VMs for throughput; here a second region confirming *the same file* is the
entire point, so sharding would defeat it.

They also keep **no local state**. "Which addresses has this host not checked?"
is a query against the verification table, so a replaced VM resumes exactly
where its predecessor stopped and an outage of any length is caught up rather
than silently skipped. (The canary probe needs a SQLite ledger only because it
cannot reach Postgres.)

#### Results live in Postgres, not InfluxDB

Both VM roles write results to Postgres as they happen. InfluxDB receives the
same measurements (`prod_upload`, `prod_upload_verify`,
`prod_upload_pool_shortfall`) for dashboards, but is **not** load-bearing —
its retention is bounded, so a verifier offline longer than the window would
otherwise miss those addresses permanently.

Consequently **`deployments record-results` is a no-op for this kind** and says
so; use `deployments print-recorded-results`, which renders per-region
verification counts alongside the uploads.

#### When the source origin is flaky

Most of the pool is sourced from the Internet Archive, which routinely answers
500/503 for minutes at a time. A source fetch that fails is **not** a statement
about the production network — nothing was uploaded — so it is retried with
exponential backoff (`10, 20, 40, 80s`, jittered ±50%, five attempts by
default) before being given up on. A 404 or 403 is a fact about the file rather
than the weather and is not retried at all.

If a file remains unfetchable, another of the **same size class** is claimed in
its place, so the cycle still measures production rather than losing the day.
Substitutions are capped per cycle: during a full origin outage every
replacement fails too, and an uncapped loop would only churn pool claims.

| env var | default | meaning |
|---|---|---|
| `SOURCE_FETCH_ATTEMPTS` | `5` | total tries per source file |
| `SOURCE_FETCH_BACKOFF_SECS` | `10` | first retry wait; doubles, capped at 300s |
| `SOURCE_MAX_REPLACEMENTS` | `3` | substitutions allowed per cycle |

Results show these as **`src-fail`** — deliberately not `dl-fail`, which in a
deployment that downloads its own uploads back reads as "the read-back failed",
i.e. as a production fault, when it means the opposite.

#### Telling "broken" from "untested"

Every cycle writes one `prod_upload_cycle` point carrying `attempted`,
`succeeded`, `partial`, `source_failed`, `replacements` and a
`produced_signal` boolean. A day when every source fetch failed and a day when
every upload was rejected are otherwise indistinguishable, and only the second
is an incident. Because the point is unconditional it doubles as a heartbeat:
no `prod_upload_cycle` in 25 hours means the service itself is down.

#### Running out of files

A cycle that cannot claim what it asked for records a shortfall to the
`prod_upload_pool_shortfall` measurement — its own measurement, so "the pool
ran dry" can never be misread as "the network refused an upload" — and sleeps.
Topping the pool up heals it with no redeploy.

#### Disk sizing

The uploader writes each source file to local disk before uploading and each
verifier downloads it back before hashing, so **every VM's disk must exceed the
largest file the pool request can draw**. The manifest validates this and
refuses at `config validate` time rather than filling a root filesystem at 3am.
An `xlarge` request with no `pool_max_size` is refused outright, because the
disk needed is then unknowable.

Client egress is roughly 4× payload from chunk fan-out, so a large daily file
is a real bandwidth cost on metered or pooled providers.

#### Arguments (`prod continuous-uploader deploy`)

| Flag | Default | Description |
|---|---|---|
| `--name` | required | Deployment name; also names the monitoring stack |
| `--spec` | required | Uploader VM as `region[:vm_size]` |
| `--verifier` | `[]`, repeatable | A verifier as `[provider:]region[:vm_size]` |
| `--pool` | required | Files per cycle, e.g. `tiny:2,small:1` |
| `--pool-min-size` / `--pool-max-size` | none | Narrow every class (min inclusive, max exclusive) |
| `--sleep-interval-secs` | `86400` | Sleep at the end of each cycle |
| `--verify-interval-secs` | `3600` | Verifier cycle; epoch-aligned so vantages line up |
| `--readback-verify` / `--no-readback-verify` | on | Uploader downloads each file straight back |
| `--pool-database-url-env` | `POOL_DATABASE_URL` | Env var holding the scoped registry URL |
| `--secret-key` | `ARBITRUM_ONE_SECRET_KEY` | Funded Arbitrum One wallet — real ETH |
| `--provider` | `digitalocean` | Uploader VM provider (all four) |
| `--monitoring-provider` | `digitalocean` | Monitoring VM provider (DO/OVH) |
| `--influx-retention-days` | `7` | Dashboard data only; results are in Postgres |
| `--log-retention-days` | `7` | Per-op log prune window; `0` keeps forever |
| `--payment` | none | `merkle` / `no-merkle` |
| `--log-level` | `info` | ant verbosity on the uploader; refused with `--client-kind wasm` |
| `--client-kind` | `native` | Uploader transport: `native` (`ant` over QUIC) or `wasm` (browser path) |
| `--wasm-branch-name` | none | ant-client branch **or tag** to build the bundle from; required with `--client-kind wasm` |
| `--wasm-repo-owner` | `maidsafe` | GitHub owner of that ant-client repo |
| `--wasm-bootstrap-multiaddrs-file` | committed list | Override the production bootstrap WebRTC multiaddrs |
| `--skip-bootstrap-probe` | off | Skip the preflight ICE probe (a just-restarted tier is silent for 2–15 min) |
| `--purpose` | required | Free text, persisted to the registry |

#### The browser path on the daily write test (`--client-kind wasm`)

`--client-kind wasm` (manifest `prod_continuous_uploader.client_kind`) makes the
**uploader** service spawn `ant-wasm` — ant-core compiled to WASM over
WebRTC-Direct — instead of the `ant` binary, so the daily test answers whether a
*browser* client can still write to production. Nothing else does: `ant-cli`
links native ant-core and so takes the native transport however it is invoked.
Example manifest: `deploy/manifests/profiles/prod-wasm-continuous-uploader.yaml`.

**One transport per deployment.** The browser path measured several times slower,
so a blended daily series would describe neither instrument. A WASM write test is
its own deployment with its own daily file quota, alongside the native one.

Everything around the spawn is unchanged — the per-cycle `claim_pool_files`, the
SHA-256 over the bytes handed to the uploader, the Postgres writes, the
replacement logic, the unit names and the restart policies. Five things differ:

- **The argv.** Upload is `ant-wasm file upload --public [--merkle|--no-merkle]
  --payment-marker <path> <file>`; read-back is `ant-wasm file download <addr> -o
  <path>`. No `--evm-network` and no `--bootstrap`: the bootstraps come from
  `BOOTSTRAP_MULTIADDRS` in the service env and the payment identity from a
  node's own HELLO.
- **The read-back runs over the same transport** — the uploader VM's own view as
  a browser user. **The independent verifier VMs stay native**, so a
  `digest_mismatch` remains attributable to the upload rather than to the browser
  read path (which `prod downloaders --client-kind wasm` covers).
- **A failed upload is classified by whether a payment went out.** A payment
  means chunks may be in production, so the file's claim is marked **partial** and
  a canary must never draw it; no payment means nothing reached the network, so
  the claim is **released** and the file is untouched. Two signals say a payment
  went out and either is enough: the result JSON's `payment_sent`, and the
  `--payment-marker` file — which is the only evidence left when the OOM killer
  reaches the WASM child (by far the largest RSS on the VM) after the money went
  out but before it printed anything. A signal exit is recorded as
  `error_kind=crash`. **The native rule is unchanged**: no result document plus a
  non-zero exit still means `ant` never started, and releases.
- **The pool draw is narrowed to ant-core's browser cap** (1,000,000,000 bytes).
  `large` and `xlarge` are refused at validation — every file in them is over the
  cap, so every draw would permanently spend an entry that could never be
  uploaded — and a `medium` request has the cap written into `--pool-max-size`,
  which `deployments plan` shows.
- **Results stay separate.** Rows carry `client_kind='wasm'` in
  `deployment_prod_upload_results` (`print-recorded-results` labels the
  transport), and the Influx copies go to the `wasm-client-metrics` bucket so no
  dashboard can average two transports measured several times apart.

The uploader VM defaults to **`s-4vcpu-8gb`** and a DigitalOcean slug below 8 GB
is **refused**: a browser-path transfer holds ~4x the file in WASM memory, and an
under-sized box turns every upload into an OOM kill that reads as a network fault.
Other providers' size names cannot be read, so those only warn. Nothing on a WASM
uploader runs `ant`, so the binary is not installed there — the verifiers still
get it.

**Refused rather than ignored**, because a silently-dropped option leaves the
operator reading their own manifest as a description of the run:
`--no-bootstrap-cache`/`bootstrap_cache` (it selects ant's peer cache versus an
explicit `--bootstrap`, and a browser client does neither) and
`--log-level`/`log_level` (the `ant` verbosity flag; `ant-wasm` has none and
reads `RUST_LOG` straight from its environment, so use `--rust-log`).

The bundle comes from `build ant-wasm`, which `deployments up` emits as a
resumable step before the deploy — no ant-core WASM package is published as a
release, so `branch` is the only form and it takes a tag as readily as a branch.
Every bootstrap multiaddr is ICE-probed **before any infrastructure is created**;
none answering refuses the deploy, a partial answer warns and names the dead
entries. `generate-inventory` records `client_kind`, `wasm_build`,
`wasm_bootstrap` and the `ant_wasm_*` paths.

`prod continuous-uploader destroy --name X [--keep-monitoring]` stops both
services first so nothing claims a file mid-teardown, then warns about any
claim the uploader never resolved. Those are **not** released automatically: a
claim that might correspond to a real upload must never be freed by a sweep,
which is how a file gets uploaded twice.

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

These kinds keep no deployment state of their own — they join production rather than standing up nodes — so the inventory is built from the S3 client/monitoring state plus the registry row. `node_vms` and `bootstrap_vms` are empty (listing production's peers as our VMs would mislead anything that tried to SSH into them) and no devnet manifest is written, but `client_vms` carries the VM's IP, provider, region and SSH details, `observability_vms` carries the InfluxDB/Grafana/Elasticsearch VMs, and a `client_config` section (schema 7) describes the run:

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

`deployments upgrade-ant-binary` force-installs the latest (or a pinned/branch) native `ant` binary on an already-running prod client VM over SSH, and restarts the service that VM runs — no Terraform infrastructure is touched. It exists because the binary-install steps used by `deploy` are presence-gated (they skip if `ant` is already installed at `/root/.local/bin/ant`), so re-running `deploy` against an existing deployment never picks up a new release.

```bash
uv run saorsa-deploy deployments upgrade-ant-binary --name PROD-DL-01
# the daily write test: uploader VM + every verifier VM, each restarting its own unit
uv run saorsa-deploy deployments upgrade-ant-binary --name PROD-UL-01
# the browser-path write test: its NATIVE verifier VMs only
uv run saorsa-deploy deployments upgrade-ant-binary --name PROD-WUL-01
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
| `--restart` / `--no-restart` | flag | `--restart` | Restart each host's own service after the binary swap |

**Supported kinds, and the unit each host gets.** Hosts are grouped by the systemd unit they actually run, and each group gets its own `systemctl restart`:

| Kind | Role in client state | Unit restarted |
|------|----------------------|----------------|
| `prod-downloaders` | every VM | `ant-prod-downloader` |
| `prod-continuous-downloader` | every VM | `ant-prod-downloader` |
| `prod-continuous-uploader` | `upload` | `ant-prod-continuous-uploader` |
| `prod-continuous-uploader` | anything else (verifier) | `ant-prod-upload-verifier` |

`prod-continuous-uploader` is the only multi-role kind: one VM uploads and the rest independently read every address back, and **both roles install and execute the native `ant` binary**, so both want the upgrade — but under different units, which is why one hard-coded unit name is not enough.

**A WASM uploader VM is skipped.** When the deployment's `deploy_args.client_kind` is `wasm` (PROD-WUL-01), its uploader VM runs the browser path and has no `ant` on it at all — the provisioner's install op returns early for it — so nothing is installed there and its service is not restarted. Its verifier VMs are deliberately native, and they do get the upgrade. The transport is read from the registry rather than probed on the host; absent, it is treated as native, matching the provisioner's own default. `PROD-WDL-01` is wholly WASM: nothing on it executes the native `ant` binary, so the command **warns and names `deployments upgrade-wasm-client`** rather than refusing — accepting a download deployment predates this command understanding roles, and narrowing that is a separate decision.

Any other kind is refused (kind is validated via the registry when `DATABASE_URL` is set; the command still runs without it, skipping validation and assuming a downloader deployment, which it says). The `ant_binary_upgrade_started` / `_completed` / `_failed` events are recorded for the deployment, and a successful run updates `deploy_args.ant_binary` so `deployments slack-post` reflects the new binary.

**One unreachable host does not stop the others.** Each group is upgraded independently and failures are accumulated, so a verifier that is down still leaves the uploader upgraded with an accurate record of which hosts took the binary — the alternative is a halt that leaves the fleet on a mix with nothing saying which is which. The summary reports per role (`uploader: 1/1`, `verifier: 2/2`), and a non-zero exit plus an `ant_binary_upgrade_failed` event follow any failure.

**`--no-restart` note**: the `prod_downloader.py` service has no checkpoint/resume state — every start walks its full address list from the beginning. For `prod-continuous-downloader` this is harmless (it loops forever anyway), but restarting a `prod-downloaders` (one-shot) deployment that has already completed its single pass re-runs the entire address list, duplicating Influx points and any already-recorded results. Pass `--no-restart` for a completed one-shot deployment and restart the service manually only when you actually want another full pass.

### Upgrading the WASM client in place (`deployments upgrade-wasm-client`)

`deployments upgrade-wasm-client` replaces the WASM (browser-path) client bundle on an already-running `prod-continuous-uploader` or `prod-continuous-downloader` VM over SSH and restarts its service — no Terraform infrastructure is touched and no registry row is created.

It exists because PROD-WUL-01 and PROD-WDL-01 run permanently against production and the point of them is a *series*: what the browser client can do against production today, next to what it could do last week. Iterating on the client means changing the binary under that series without breaking it, and neither existing route does that — `deployments upgrade-ant-binary` covers only the native `ant` binary, and re-running `deployments up` calls `record_deployment_created` again, risking a fresh registry row (a new series rather than a continuation of the old one).

```bash
# one build, installed on both deployments
uv run saorsa-deploy deployments upgrade-wasm-client \
  --name PROD-WDL-01 --name PROD-WUL-01 \
  --branch-name main --repo-owner WithAutonomi
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--name` | string (repeatable) | required | Deployment to upgrade. Every named deployment gets the SAME bundle from ONE build |
| `--branch-name` | string | required | ant-client git ref to build from — a branch OR a tag (`main`, `ant-cli-v0.3.8`) |
| `--repo-owner` | string | `WithAutonomi` | GitHub repository owner |
| `--ssh-key-path` | string | `~/.ssh/id_rsa` | SSH key used to reach the client VM(s) |
| `--build-vm-size` | string | `c-32` | DigitalOcean droplet size for the build VM |

**Scope.** Only `prod-continuous-uploader` and `prod-continuous-downloader` deployments whose `deploy_args.client_kind` is `wasm`. Anything else — another kind, a native deployment, a name the registry does not know — is refused **before anything is built**, with every problem in the invocation listed at once. The one-shot kinds (`prod-downloaders`, `prod-uploaders`) are rebuilt by every `up` and need no in-place upgrade; a testnet's WASM tier is torn down with its testnet. `DATABASE_URL` is required: the kind, the transport and the old build all come from the registry, and the upgrade is recorded there.

**One build for all named deployments.** The bundle is built once, under the first named deployment's name, and that same bundle URL is installed everywhere. That is the point of passing several names: installing identical bytes is what keeps the deployments comparable afterwards, where a build per deployment could give each one a different commit of a branch that moved in between.

**On each target VM**, in order: fetch and unpack the bundle (`require_cli`, so a bundle with no `dist/cli.js` is rejected on the VM rather than failing every subsequent transfer), reinstall the `/usr/local/bin/ant-wasm` wrapper, **redeploy the host-side service script** (`prod_downloader.py` or `prod_continuous_uploader.py`), then `systemctl restart` that kind's unit — `ant-prod-downloader` or `ant-prod-continuous-uploader`. The continuous uploader's **verifier VMs are deliberately not touched**: they are native by design, so that a digest mismatch they report stays attributable to the upload rather than to a browser read path. (Their native `ant` binary is upgraded by `deployments upgrade-ant-binary --name PROD-WUL-01`.)

**Why the service script goes with the bundle.** The bundle is the half of the path that *performs* a transfer; the service script is the half that *records* it. Upgrading one without the other leaves the recording half behind, and that is not hypothetical: PROD-WUL-01 was provisioned on 2026-09-26, the `client_build` plumbing landed on 2026-09-27 17:43, and its bundle was upgraded 78 minutes later — so from then on its per-op log on the host carried `client_build` while every single one of its `deployment_prod_upload_results` rows had the column empty, because the script writing those rows was still the one from the day before. Redeploying it is cheap (the unit is being restarted anyway) and makes the pairing unbreakable.

**Timing is the operator's.** Run it outside the daily window. A restart that triggers a same-day cycle is acceptable — the uploader claims fresh files, the downloader re-walks its list — but it is a cycle and it costs what a cycle costs. A failure on one deployment is reported and the command carries on with the rest (the alternative is a set left on two builds with nothing recording which is which), exiting non-zero at the end.

**Recording.** On success, `deploy_args.wasm_build` is replaced with the new bundle's `build-info.json` and a `wasm_client_upgraded` event records `{old_commit, new_commit, ref, repo_owner, upgraded_at}`; the output prints old → new per deployment. A failure records `wasm_client_upgrade_failed` and leaves `deploy_args` alone, so the registry never claims a build that a VM did not take. If the deployment's run manifest under `deploy/manifests/runs/` still pins a different `wasm_binary.branch`, the command warns that the next `deployments up` would roll the upgrade back.

#### Which build performed each transfer (`client_build`)

`deploy_args.wasm_build` holds only the **current** build and is overwritten, so on its own it cannot separate a deployment's results before an upgrade from its results after — which is the only reason to upgrade in place. The build therefore travels with each operation:

* **`ant-wasm`** reads `build-info.json` from its own bundle and adds `client_build` (the ant-client commit) to the final JSON line it prints. A bundle built before provenance existed reports `null`.
* **The services** (`prod_downloader.py`, `prod_continuous_uploader.py`) put it on their Influx points — `download`, `prod_upload` and `prod_upload_verify`. A native service reports none and its points are byte-identical to before.
* **Postgres** carries a nullable `client_build` column on `deployment_download_records`, `deployment_prod_upload_results` and `deployment_prod_upload_verifications`, written by `record-results` (download kinds) and by the continuous uploader's own direct writes. NULL means "unknown build": every native row, every native verifier row, and every row predating the column. Like `client_kind`, the upsert COALESCEs — a source with no build never erases one already recorded.
* **`deployments print-recorded-results`** splits its tables and its totals footers by `client_build` whenever more than one build appears for a deployment. With a single build, or none, the output is exactly what it was.

No past row can be backfilled: the Influx points never held the field.

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

**What `--dir` batching is worth, and the traps it hides.** The per-file path
measured **163ms/file** against the managed Postgres while reading and hashing
the file took **1ms** — ~99% was round-trip latency, so 300k files would have
taken ~13.6 hours. `add_pool_files_bulk` is a fixed ~7 statements per 500-file
batch instead of ~4 statements plus a COMMIT per file. Four per-row traps turned
up while building it, every one caught by a test asserting statement count is
**flat in file count** rather than by timing:

* ORM `session.add` per row issues `INSERT..RETURNING` per object — use Core
  `insert()` with a list.
* A `session.refresh()` loop plus a lazy relationship load reintroduced a SELECT
  per row *after* the inserts were already batched.
* Duplicate links were written with an UPDATE each (207 links = 5.3s) — now one
  ORM bulk-update-by-primary-key.
* Returning ORM instances let `session.commit()` expire them, so the caller's
  summary re-fetched every row at two statements each (~46s for 1156 files)
  *after* the progress bar had closed. It now returns detached `PoolFileRecord`
  values that cannot talk to the database.

Re-runs are flat too — an interrupted import skips known files without a
round-trip each. Semantics match the per-file path exactly, including
duplicate-digest linking *within* a batch, where the pre-flight query cannot see
an uncommitted sibling.

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
* **`up --resume` reuses the same files — but only while the draw is still
  live.** The selection is persisted to S3, so a retried deploy does not draw a
  fresh set while the first sits claimed. That persistence is keyed on the
  deployment **name**, which every prod name reuses each release train, so a new
  run finds the previous run's draw under its own key. It is therefore reused
  only if every file still carries the exact claim the draw recorded, still
  `CLAIMED` (`selection_is_live` in `pool_selection.py`); a claim that resolved
  to `UPLOADED`, was released, or was replaced by a different one means an
  earlier run finished with that file, and the draw is discarded and taken
  again. Without that check, STG-UL-01 on 2026-08-18 was handed the four files
  the 2026-08-11 run had already uploaded, and only the duplicate-file gate
  stopped it re-uploading them for real money. `prod uploaders destroy` deletes
  the persisted draw too, since a destroyed deployment has nothing to resume.
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

### The browser path on the production download kinds (`--client-kind wasm`)

Both download kinds can run their transfers over **WebRTC-Direct** — ant-core
compiled to WASM, the browser path — instead of the native `ant` binary. It
measures what a browser client actually experiences against the real production
network, which nothing else here does: `ant-cli` links native ant-core, so it
takes the native transport however it is invoked.

`--client-kind wasm` (manifest `client_kind: wasm`) is the whole switch. **One
transport per deployment**: the native and WASM tiers are not the same instrument
— the WASM tier measured ~4.2x slower on download — so a blended result describes
neither, and the transport is a property of the deployment rather than of a VM.

```bash
uv run saorsa-deploy prod downloaders deploy \
  --name PROD-WASM-DL-01 \
  --spec lon1:s-4vcpu-8gb \
  --purpose "Browser-path download measurement" \
  --client-kind wasm \
  --wasm-branch-name ant-cli-v0.3.8-rc.1 --wasm-repo-owner WithAutonomi \
  --addresses-path my-addresses.txt \
  --forward-logs
```

The bundle must be built first — `deployments up` emits a resumable
`build ant-wasm` step ahead of the deploy, and a bare CLI invocation needs
`saorsa-deploy build ant-wasm --name <name> --branch-name <ref> --repo-owner
<owner>` run against the same `--name`.

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--client-kind` | `native\|wasm` | `native` | Which transport the client VM runs |
| `--wasm-branch-name` | string | — | ant-client branch **or tag** the bundle was built from. Required with `wasm` |
| `--wasm-repo-owner` | string | `maidsafe` | GitHub owner of that ant-client repo |
| `--wasm-bootstrap-multiaddrs-file` | path | the committed list | Override the production bootstrap WebRTC multiaddrs |
| `--skip-bootstrap-probe` | flag | off | Skip the preflight ICE probe of those listeners |

Manifest keys: `client_kind`, `wasm_binary: {branch, repo_owner}` and
`wasm_bootstrap_multiaddrs_file` under `prod_downloaders:` /
`prod_continuous_downloader:`. Examples:
`deploy/manifests/profiles/prod-wasm-downloaders.yaml` (one-shot) and
`deploy/manifests/profiles/prod-wasm-continuous-downloader.yaml` (the permanent
daily test — the browser-path sibling of the native daily downloader, walking the
same address list on the same cycle so the two transports fetch the same content).

**`wasm_binary.branch` takes a branch or a tag, and there is no `version`.** No
ant-core WASM package is published as a GitHub release, so there is nothing for a
version to name; `build ant-wasm` clones with `git clone --branch <ref>`, which
accepts either, and that is how an RC is pinned. A `version:` is refused with a
message pointing at the tag form.

**The VM needs at least 8 GB of RAM, and that is enforced.** A browser-path
download holds ~4x the file in WASM memory (900 MB measured at 3.9 GB RSS on
smoke run 587), so a smaller box makes every transfer an OOM kill — which shows
up in the results as a network fault rather than as an under-sized VM. A
DigitalOcean slug encodes its memory, so one below the floor is **refused** and a
size-less `--spec` **defaults to `s-4vcpu-8gb`**; other providers' names cannot be
read, so those only warn.

**The bootstraps are probed before any infrastructure is created.** A browser
client bootstraps from the production WebRTC-Direct listeners and has no other
way in, so every entry in
`resources/prod_bootstrap_webrtc_multiaddrs.txt` (refresh it with
`prod bootstrap webrtc-addresses`) gets a real ICE connectivity check — not a
STUN binding, because saorsa-transport's admission filter returns
`Decision::Ignore` on every failure and an unauthenticated probe cannot tell a
firewalled port from a healthy one. If **nothing** answers the deploy refuses:
the alternative is a VM that bills from creation and fails every operation. A
**partial** answer warns and names the dead entries and then proceeds — ant-core
cools a failed endpoint down for 30 minutes and moves on, so a live subset is a
working deployment, but the run's results have to be read in that light. Pass
`--skip-bootstrap-probe` after a bootstrap restart: ant-node 0.20.0 opens the
listener 2–15 minutes later, so a just-upgraded tier is legitimately silent. The
outcome is recorded in `deploy_args.wasm_bootstrap` and rendered by
`deployments slack-post`.

**Three options are refused, not ignored.** `bootstrap_cache` selects between
ant's peer cache and an explicit `--bootstrap`, and a browser client does
neither; `memory_profiling` and `download_diagnostics` both act on the `ant`
process, which never runs. A silently-dropped option would leave the operator
reading their own manifest as a description of the run. `rust_log`, `network` and
`clock_skew` all work normally.

**The service, unit, logs and restart policy are the native ones.** The same
`prod_downloader.py` walks the same address list with the same `ONE_SHOT` /
`SLEEP_INTERVAL_SECS` behaviour, writes the same per-op
`/var/log/ant-prod-downloader/{op:04d}-{addr}.{stdout,stderr}` pair, and runs
under the same `ant-prod-downloader` unit. It spawns
`ant-wasm file download <addr> -o <path>` instead of `ant` — **no
`--evm-network`** (a download pays for nothing) and **no `--bootstrap`** (the
multiaddrs are in the service environment). `ant-wasm` is a single-operation CLI
installed at `/usr/local/bin/ant-wasm` that prints a final JSON line on stdout in
the same shape as `ant --json`; human-readable progress goes to stderr, which is
why `--forward-logs` works unchanged on this path.

Because the transfer is a **child process**, an OOM kill lands on it rather than
on the Python parent — it is by far the largest RSS on the VM — so the parent
sees a signal exit, records the operation as `error_kind=crash`, and carries on
to the next address. That is why this needs no checkpoint file, no restart-policy
change and no inflight journal.

**Over-cap entries are skipped, not failed.** ant-core refuses a browser-path
file above 1,000,000,000 bytes. That is a property of the transport, not of the
network — the content is stored and perfectly healthy — so counting it as a
download failure would report a transport limitation as a network fault. Address
lines may be `<address>[ <bytes>]`: when the declared size is over the cap the
service **skips up front** without spawning anything, and when only the refusal
reveals it the child's `error_kind=over_cap` is recorded as a skip too. A **bare
line is never skipped** — unknown is not "too big", so an address list written
before sizes were published keeps working. Skipped rows are stored (the operator
needs to see that the list held an uncarriable entry) but excluded from **every**
success-rate denominator: the aggregates, `deployments compare`, `clients
estimate-gas` and the Slack reply.

**Results stay separate at every layer.** WASM operations go to the
`wasm-client-metrics` InfluxDB bucket, so no dashboard panel or ad-hoc query can
average two transports measured ~4x apart; `record-results` reads both buckets,
deduplicates, and records each row with `client_kind` taken from
`deploy_args.client_kind`. It is read from there rather than from the S3 client
state because that state dies at teardown, and a re-run after `down` would
otherwise relabel every WASM operation as native — pushing its failures into the
native hard floors, the one place they must never count. Each download row also
carries the **`address`** it fetched (both transports emit it now; nothing else
maps an operation to its target). `print-downloads`,
`print-recorded-results`, `slack-post` and `record-results --update-slack` render
the per-transport split with the skip and crash counts, and the Slack post names
the bundle's commit and the probe result.

**Build provenance.** `build ant-wasm` runs `npm ci` against the committed
lockfile and writes a `build-info.json` into the tarball root plus an S3 sidecar
(`builds/<name>/<owner>/<ref>/ant-wasm-client.build-info.json`) holding the
ant-client commit, the ref, the resolved `node-datachannel` version, the
`wasm-pack` version and the build timestamp. The deploy reads the sidecar into
`deploy_args.wasm_build`; a bundle built before it existed warns and records
`null`. The commit is what identifies the code — `--wasm-branch-name` may name a
moving branch, so "built from `web-support`" says nothing about what ran.

Out of scope here: the WASM upload side (V2-1334 / V2-1335), a released WASM
version (V2-1337), `prod canary` (native by design) and the `local-*` kinds.

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

#### The browser path on `prod uploaders` (`--wasm-uploader`)

A `prod-uploaders` deployment can run a **browser-path** uploader alongside (or instead of) the native ones: `--wasm-uploader` stands up a VM whose `ant-prod-uploader` service spawns `ant-wasm` — `ant-core` compiled to WASM, reaching the network over WebRTC-Direct — instead of the `ant` binary. It measures what a browser client actually experiences uploading to the real production network, which nothing else does: `ant-cli` links native `ant-core`, so it takes the native transport however it is invoked.

Unlike the download kinds, the transport here is **per uploader, not per deployment**. A native uploader and a browser-path uploader in one run share the wallet, the monitoring stack and the prod bootstrap list, so the two transports are measured against the same network at the same time — and their results stay separate at every layer.

```bash
uv run saorsa-deploy prod uploaders deploy \
  --name PROD-WASM-UL-01 \
  --purpose "Browser-path upload measurement, with a native control" \
  --uploader      lon1:s-2vcpu-4gb:pool=tiny:2:pool_min_size=5MB \
  --wasm-uploader lon1:pool=tiny:2:pool_min_size=5MB \
  --wasm-branch-name ant-cli-v0.3.8-rc.1 --wasm-repo-owner WithAutonomi
```

Everything WASM-specific is resolved and **proved before any infrastructure exists**: the bundle must be in S3 (built by `build ant-wasm`, which `deployments up` emits as a resumable step), and every production bootstrap WebRTC multiaddr is ICE-probed. Zero answering refuses the deploy — the alternative is a VM that bills from creation and fails every upload; a partial answer warns and names the dead entries.

Per-uploader differences from the native tier:

* **argv:** `ant-wasm file upload --public [--merkle|--no-merkle] --payment-marker <op>.paid <path>`. No `--evm-network` (the payment identity comes from a node's own HELLO) and no `--bootstrap` (`BOOTSTRAP_MULTIADDRS` is in the env file). Everything else about the service is identical — the same unit, the same fetch → upload → delete loop, the same per-op `.stdout`/`.stderr` under `/var/log/ant-prod-uploader/`.
* **The browser file cap.** ant-core refuses a file above **1,000,000,000 bytes**, so the `large` and `xlarge` pool classes are **refused at validation** (every file in them is over the cap, and a drawn one would be a permanently-spent pool entry that could never be uploaded) and a `medium` request is implicitly narrowed with `pool_max_size=1000000000`, which `deployments plan` shows. A fetched file over the cap is recorded as `over_cap` and never uploaded.
* **Results go to the `wasm-client-metrics` Influx bucket**, so no dashboard panel or ad-hoc query can average two transports measured several times apart. `record-results` reads **both** buckets and labels each row's `client_kind`; `print-uploads`, `print-recorded-results` and the `--update-slack` reply all split their totals per transport.
* **Refused, not ignored:** `bootstrap_cache` (selects ant's peer cache vs an explicit `--bootstrap`; a browser client does neither), `memory_profiling` and `log_level` (both act on the `ant` process, which never runs). `payment`, `forward_logs`, `rust_log`, `network` and `clock_skew` all work normally.
* **VM size** defaults to `s-4vcpu-8gb` and a DigitalOcean slug below 8 GB is **refused**: a browser-path transfer holds ~4x the file in WASM memory, so an under-sized box turns every upload into an OOM kill, which reads as a network fault.

**Pool claims resolve by outcome**, and the distinction is what keeps the pool honest:

| Result | Row status | Claim |
|--------|-----------|-------|
| success | `success` | `uploaded` — the entry is spent |
| failure **after** a payment was submitted | `partial_upload` | `partial` — chunks ARE in production, so uploaders may complete it but a canary must never draw it |
| failure **before** any payment | `failed` | released — nothing reached the network, so the file is untouched |
| file over the browser cap | `over_cap` | released — nothing was attempted |
| source fetch failed | `download_failed` | released |

"After payment" means the result JSON says `payment_sent: true` **or** the `--payment-marker` file exists. The marker covers the case the JSON cannot: a child killed (typically by the OOM killer, which reaches the WASM process long before the Python parent) after the money went out but before it printed anything. Trusting only the JSON would return a paid-for file to the pool.

Extra arguments on `prod uploaders deploy`:

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--wasm-uploader` | repeatable | — | Browser-path uploader spec `REGION[:VM_SIZE]:urls=PATH\|pool=CLASS:COUNT[:pool_min_size=SIZE][:pool_max_size=SIZE][:exclude_partial=BOOL][:payment=MODE][:clock_skew=DURATION][:network=PROFILE][:forward_logs=BOOL][:rust_log=SPEC]`. `VM_SIZE` is optional (defaults to `s-4vcpu-8gb`). `bootstrap_cache`, `memory_profiling` and `log_level` are refused by name. `rust_log` must be the **last** token. |
| `--wasm-branch-name` | string | — | ant-client branch **or tag** to build the bundle from. Required with `--wasm-uploader`: no ant-core WASM package is published as a release. |
| `--wasm-repo-owner` | string | `maidsafe` | GitHub owner of the ant-client repo |
| `--wasm-bootstrap-multiaddrs-file` | string | committed list | Override `resources/prod_bootstrap_webrtc_multiaddrs.txt`. No URL form — nothing publishes these addresses at one. |
| `--skip-bootstrap-probe` | flag | off | Skip the preflight ICE probe. Use after a bootstrap restart: ant-node 0.20.0 opens the listener 2–15 minutes later. |

Manifest form (`kind: prod-uploaders`): `prod_uploaders.wasm_uploaders[]` plus `prod_uploaders.wasm_binary: {branch, repo_owner}` and the optional `wasm_bootstrap_multiaddrs_file`. `uploaders` may be empty for a WASM-only run. Example: `manifests/profiles/prod-wasm-uploaders.yaml`.

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

#### Surveying the whole fleet (`disk-survey` / `survey-report`)

`prod services ls` looks at one host. `prod services disk-survey` walks
**every** host in an inventory in parallel, ranks all node services ascending
by data-dir size (smallest first = deletion candidates), prints per-host free
space most-pressured-first, and stores the survey in the registry so
`prod services survey-report` can read today against yesterday.

```bash
saorsa-deploy prod services disk-survey \
  --inventory-path ~/.autonomi/testnets/ant-prod-01/inventory.json

saorsa-deploy prod services survey-report \
  --inventory-path ~/.autonomi/testnets/ant-prod-01/inventory.json
```

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--inventory-path` | yes | — | Inventory JSON; its `testnet_id` keys the stored surveys. |
| `--provider` | no | every provider | Restrict the survey to one provider. |
| `--limit-to` | no | every host | Comma-separated VM names to survey. |
| `--ssh-key-path` | no | each host's inventory `ssh_key` | Override the SSH key for every host. |
| `--no-store` | no | store | Print the survey without persisting it. |
| `--survey-retries` | no | `1` | Extra attempts for a host whose probe failed. `0` disables. |
| `--retry-backoff-secs` | no | `5` | Base backoff before each retry, multiplied by attempt number. |
| `--max-parallel` | no | `16` | Maximum concurrent host probes. |

**A host that fails its probe is not a host with no data.** The survey used to
make one SSH attempt per host and drop the host entirely when it failed, so the
fleet total was computed over whatever remained — a silent under-count that
reads as reclaimed disk. Survey 39 (2026-09-01) reported node data falling
28.1 → 26.9 TiB, an apparent 1.192 TiB reclaim; two hosts had simply dropped
out, and across the 63 hosts present in both runs node data had actually
**grown 21.5 GiB**. The two hosts were not down — they answered ICMP, port 22
was open, SSH worked by hand, 15/15 services were running. They were
CPU-saturated, so the probe missed its deadline. "Host is slow" is exactly the
condition a disk-pressure survey most needs to see.

Three things follow from that, and all three are load-bearing:

- **Failed hosts are retried serially, after the parallel pass**, with a longer
  deadline (300s vs 120s). Serial and afterwards because sixteen concurrent
  `du` walks are themselves part of why a loaded host misses its deadline —
  retrying inside the worker retries into the same storm — and with a longer
  deadline because re-running an identical 120s probe against a host that just
  missed 120s fails identically. An **authentication** failure is never
  retried: a rejected key will be rejected again.
- **The delta is computed like-for-like over the hosts both runs reached**, in
  both `disk-survey` (against the previous stored survey) and `survey-report`.
  A retry only reduces how often a host drops; any host can still drop, and a
  whole-survey total then describes a different fleet. Both numbers are printed
  — the raw one explicitly labelled as spanning differing host sets — and when
  they point in opposite directions the output says outright that the raw total
  is misleading. Neither is ever silently substituted for the other.
- **The failed set is stored with the survey** (`failed_hosts`,
  `attempted_host_count`), so `survey-report` renders the caveat
  retrospectively instead of the failure existing only in the scrollback of
  whoever ran it. Both columns are **nullable with no backfill**: NULL means a
  survey predating the feature, whose failed set is *unknown* — deliberately
  not the same claim as an empty set ("every host was reached").

Failures are reported grouped by reason, because they are not equivalent: a
timeout means the host is probably up but saturated (its disk still counts, we
just failed to read it) while a refusal means it may genuinely be gone.

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
# Pull the new Influx points into Postgres and re-derive the loss table.
saorsa-deploy deployments record-results --name CANARY-01

# Show alive / pending / confirmed-lost counts, hours-to-loss histogram,
# per-vantage breakdown, per-chunk-role breakdown.
saorsa-deploy deployments print-recorded-results --deployment-id <id>
```

Both commands branch on `deploy_args.kind` and route through dedicated
canary handlers; existing testnet / prod-uploaders / prod-continuous-downloader
behaviour is untouched.

##### The probe pull is incremental

A canary's probe series only grows — one point per canary per hour, for the
life of the deployment — so re-reading and re-writing all of it would make the
cost of a daily run a function of the deployment's **age** rather than of what
happened that day. PROD-CANARY-04's first publisher run proved it: 5,062 s for
423,108 probe points, of which 3,164 s was the write phase at one
`INSERT … ON CONFLICT` and one `commit()` per row.

So `record-results` pulls only what it does not already hold, and writes in
batches of 1,000 rows per statement and per commit.

| Flag | Default | Notes |
|------|---------|-------|
| `--since <ISO-8601>` | see below | Flux `range(start:)` for the **probe** query. `--since 0` forces a full pull. |
| `--full` | off | Alias for `--since 0`. |

With neither flag the window opens **six hours before the newest `probed_at`
already recorded for this deployment**. The margin is there because a probe VM
may have a skewed clock and a point may arrive late; re-reading the overlap
costs nothing, since the upsert rewrites an already-recorded probe with the
same values. A deployment with **no** probes in Postgres — the first ever run —
pulls the whole series, because there is no high-water mark and a shorter
window would silently lose history.

The upload and chunk queries are **not** windowed. They are the canary set
itself (378 rows on PROD-CANARY-04), written once when the upload service
walked its list and never again, so there is nothing for a window to save.

Two properties keep the derivation correct under a windowed pull:

* **Loss and the unavailability episodes are re-derived from Postgres, not
  from the window.** The two-strike rule needs both of its strikes, and an
  episode can open long before the window does.
* **The derivation is scoped to the canaries this run recorded a probe for.**
  A canary with no new probe has the history it had last run, so its derived
  rows are already right — and were being rewritten every day for nothing. Its
  loss row is also not a candidate for the stale-row deletion, since the
  absence of a fresh verdict says nothing about it. A full pull covers every
  canary, which is what keeps `--full` a true rebuild — use it after a change
  to how a past probe should be classified.

`--full` is also the way to repair the table if a probe VM's clock jumped
backwards by more than the margin.

Every run prints its phase split (Influx query / writes / derivation, plus the
file-pool claim resolution), because this command runs unattended on a daily
timer and that is the only way to see which phase is growing.

An **already-uploaded file-pool claim is skipped** for this kind. A canary's
upload rows never change after the run that wrote them, so re-resolving a spent
claim rewrites it with the values it already holds — a read and a commit per
claim, every day. An open claim is still resolved.

#### Publishing the public dataset (`prod canary export`)

`prod canary export` turns a canary's recorded probe history into a small,
public-safe JSON dataset and publishes it to S3, where the
**`canary.autonomi.com`** site reads it. Nothing else renders the canary record
for anyone outside the registry.

```
saorsa-deploy prod canary export --name <deployment name> [--id <deployment id>]
    (--bucket <bucket> [--prefix v1] | --out-dir <dir>)
    [--title <text>] [--stale-after-hours 6] [--as-of <ISO-8601 UTC>]
```

| Flag | Required | Default | Notes |
|------|----------|---------|-------|
| `--name` | yes | — | Canary deployment name. Lower-cased it is also the **series id** (`PROD-CANARY-04` → `prod-canary-04`). |
| `--id` | no | — | Deployment id, overriding `--name`'s resolution. With several rows of one name the newest is used and a warning names the ids. |
| `--bucket` | one of | — | S3 bucket. Writes `<prefix>/index.json` and `<prefix>/<series id>.json` with `put_object`, `ContentType: application/json`, `CacheControl: public, max-age=300`. Credentials come from the standard AWS chain; **no S3 deployment state is read**. |
| `--out-dir` | one of | — | Write the same two files to a local directory instead. Mutually exclusive with `--bucket`; exactly one is required. |
| `--prefix` | no | `v1` | Key prefix in the bucket. Ignored with `--out-dir`. |
| `--title` | no | the deployment name | The series' display title. |
| `--stale-after-hours` | no | `6` | Mark the series stale when the newest probe is older than this before `--as-of`. |
| `--as-of` | no | the real clock | The instant treated as "now" for the rolling windows and the staleness check. Exists so the output is reproducible. |

Requires `DATABASE_URL` and **reads Postgres only** — no Influx, no SSH, no
deployment state. The deployment must have `deploy_args.kind == "prod-canary"`;
anything else exits 1. A **destroyed** canary is still exportable: a finished
run stays on the site as history. Exit 0 on success, printing one line per file
written with its size. Export after `record-results`, which is what puts the
probes in Postgres in the first place.

The index is **merged, not rewritten**: the existing `index.json` is read first
(a 404 means none), this series' entry is replaced or added, and every other
series' entry is kept — each series is published by its own command run. An
existing `index.json` that is not valid JSON is refused rather than overwritten.

##### Counting rules

* **Probes** are `prod_canary_probes` rows for the deployment's canaries, taken
  at their *effective* status — the same `_effective_status` the recorded
  results use, so a timeout-dominated failure is `inconclusive` here too.
* **Vantages** are the probe VMs' regions, parsed out of the hostname with
  `-download-(?P<region>[a-z]+[0-9]+)-[0-9]+$` and labelled from a fixed table
  in code (`lon1: London`, `nyc1`/`nyc3: New York`, `sgp1: Singapore`, `ams3`,
  `fra1`, `sfo3`, `tor1`, `blr1`, `syd1`; an unknown region is labelled with its
  own code). A probe from any **non-matching** host is the upload VM's own
  post-upload read-back: excluded from every rate and every cell, counted only
  as `initial_verification_ok`. **Hostnames never appear in the output.**
* **Conclusive** probes are `ok` or `failed`; a rate is `ok / (ok + failed)`,
  `null` when that denominator is zero. `inconclusive` is counted and exported
  but is never in a denominator.
* **Episodes** are `prod_canary_unavailability` rows, open from
  `confirmed_unavailable_at` to `recovered_at`, or to `data_to` while ongoing.
  **The two ends are not the same kind of instant**, and the difference lands on
  a day boundary: `recovered_at` *is* the successful probe that proved the file
  available, so a recovered episode is `[confirmed, recovered)` and a recovery at
  exactly midnight leaves the new day green; `data_to` is only the end of what is
  known, so an ongoing episode is `[confirmed, data_to]` and its last day stays
  red. A row whose `recovered_at` precedes its own `confirmed_unavailable_at` is
  not a span: it is dropped with a logged warning and counted nowhere, rather than
  matching no day and quietly reading as available.
* **Days** are UTC calendar days from the earliest `uploaded_at` to `data_to`
  inclusive. `data_to` is the latest `probed_at` of any **included** probe (with
  no vantage probe at all it falls back to the last upload, which the staleness
  check then reports).
* **Cells**, one character per day per file: `R` if any of the file's episodes
  overlaps the day, else `G` if at least one conclusive probe was made that day,
  else `X`. `failed_checks` gives, per `G` day, the number of `failed` probes
  that day when non-zero — which is what lets the site show "one check failed,
  the next succeeded" without colouring the cell.
* **Rolling windows** `30d`/`90d` are probes with `probed_at` in
  `(as_of − N days, as_of]`.
* **Stale**: `stale_reasons` gains `"no_recent_probes"` when `data_to` is older
  than `--stale-after-hours` before `as_of`; `stale` is true when the list is
  non-empty. **A series with no vantage probe at all is always stale**, whatever
  `data_to` says — a canary exported before `record-results` has ever run has no
  rows, so `data_to` can only fall back to `as_of` itself, and without this the
  document would publish an empty series as healthy and up to the minute.

##### Schema (version 1)

The schema is the **contract** with the site, mirrored verbatim in its own
issue. The documents carry exactly these keys and nothing else — no hostname, no
IP, no `deploy_args`, and `source_url` is the only URL — so a column added to
`prod_canary_probes` cannot leak by default. Output is
`json.dumps(..., sort_keys=True, separators=(",", ":"))` plus a trailing
newline, so the same rows and the same `--as-of` give byte-identical files.

`<prefix>/index.json`:

```json
{
  "schema_version": 1,
  "generated_at": "2026-10-09T09:31:02Z",
  "series": [
    {
      "id": "prod-canary-04",
      "kind": "prod-canary",
      "title": "Canary 4",
      "path": "prod-canary-04.json",
      "generated_at": "2026-10-09T09:31:02Z",
      "data_to": "2026-10-09T08:03:12Z",
      "stale": false,
      "summary": { "...": "identical to the series file's summary object" }
    }
  ]
}
```

`<prefix>/<series id>.json`:

```json
{
  "schema_version": 1,
  "generated_at": "2026-10-09T09:31:02Z",
  "series": {
    "id": "prod-canary-04",
    "kind": "prod-canary",
    "title": "Canary 4",
    "uploaded_from": "2026-08-22",
    "uploaded_to": "2026-08-23",
    "first_probe_at": "2026-08-22T22:00:03Z",
    "data_to": "2026-10-09T08:03:12Z",
    "probe_interval_hours": 1,
    "vantages": [
      { "id": "lon1", "label": "London" },
      { "id": "nyc1", "label": "New York" },
      { "id": "sgp1", "label": "Singapore" }
    ]
  },
  "stale": false,
  "stale_reasons": [],
  "summary": {
    "files": 378,
    "bytes": 32162403640,
    "chunks": 9280,
    "initial_verification_ok": 378,
    "days_observed": 49,
    "files_never_unavailable": 378,
    "files_available_now": 378,
    "files_unavailable_now": 0,
    "episodes": { "total": 0, "recovered": 0, "ongoing": 0 },
    "smallest_bytes": 5277908,
    "largest_bytes": 485255912
  },
  "rates": {
    "all": { "ok": 0, "failed": 0, "inconclusive": 0, "rate": null },
    "30d": { "ok": 0, "failed": 0, "inconclusive": 0, "rate": null },
    "90d": { "ok": 0, "failed": 0, "inconclusive": 0, "rate": null },
    "by_vantage": {
      "lon1": { "all": {}, "30d": {}, "90d": {} }
    }
  },
  "days": ["2026-08-22", "2026-08-23"],
  "files": [
    {
      "id": "6336b4b0-…",
      "name": "example.avi",
      "bytes": 36299308,
      "chunks": 13,
      "address": "<64 hex chars>",
      "source_url": "https://archive.org/download/…",
      "uploaded_at": "2026-08-22T21:38:59Z",
      "available_now": true,
      "cells": "GG",
      "failed_checks": { "2026-08-23": 1 },
      "episodes": [
        { "from": "2026-09-01T12:00:00Z", "to": null, "chunk": "<64 hex chars>" }
      ]
    }
  ]
}
```

* `cells` has exactly `len(days)` characters from `G`, `R`, `X`, in `days` order.
* `rate` is a float in `[0, 1]` rounded to 6 decimals, or `null`.
* `days_observed` is `len(days)`.
* `files_never_unavailable` counts files with no episode; `files_unavailable_now`
  counts files with an ongoing episode; `files_available_now` is
  `files − files_unavailable_now`.
* `name` is the stored `file_name` URL-decoded for display; `source_url` is
  stored as-is.
* `failed_checks` and `episodes` are omitted when empty.
* Every object in `by_vantage` has the same shape as `rates.all`.
* Timestamps are UTC ISO-8601 with a `Z` suffix and no fractional seconds; dates
  are `YYYY-MM-DD`.
* Files are ordered by `uploaded_at`, then `id`.

#### Publishing the dataset on a timer (`provision-publisher`)

The `canary.autonomi.com` site reads its JSON dataset from S3. That dataset
comes out of Postgres, and Postgres is only current once `record-results` has
pulled the canary's InfluxDB points — so until both commands run on a
schedule, the site is only as fresh as the last time an operator remembered.

`prod canary provision-publisher` installs a **daily timer on the canary's
monitoring VM** that runs `deployments record-results` and then
`prod canary export`. The monitoring VM is the right host: `prod canary
destroy` keeps it by default, it already carries the Slack reporter, and
InfluxDB is local to it.

**The daily run is incremental.** `record-results` pulls only the probes
Postgres does not already hold (see "The probe pull is incremental" above), so
a day's run writes a day's points — around 6,000 on PROD-CANARY-04 — rather
than the whole series. The **first** run on a deployment that has never been
recorded is the expensive one: it pulls and writes everything, which on
PROD-CANARY-04 was 423,108 points. `TimeoutStartSec=3h` is sized for that run,
not for the daily one.

```bash
saorsa-deploy prod canary provision-publisher \
  --name CANARY-01 \
  --bucket canary-datasets

# Install without triggering a run:
saorsa-deploy prod canary provision-publisher \
  --name CANARY-01 --bucket canary-datasets --no-run-now
```

| Flag | Required | Default | Notes |
|------|----------|---------|-------|
| `--name` | yes | — | Canary deployment name; also the monitoring stack name. Must exist in the registry with `deploy_args.kind == "prod-canary"`. |
| `--bucket` | yes | — | S3 bucket the dataset is written to. |
| `--prefix` | no | `v1` | Key prefix within the bucket. |
| `--title` | no | `--name` | Dataset title shown on the site. |
| `--on-calendar` | no | `*-*-* 09:30:00` | Systemd `OnCalendar` for the timer — after the Slack reporter's 09:00. |
| `--env-file` | no | `~/.config/saorsa-deploy/canary-publisher.env` | The VM's own credentials; see below. |
| `--run-now` / `--no-run-now` | no | **on** | Run once after install and wait for it, printing `last-run.json`. A failing run fails the command. |
| `--ssh-key-path` | no | `~/.ssh/id_rsa` | SSH key for the monitoring VM (user `root`). |

```bash
saorsa-deploy prod canary unprovision-publisher --name CANARY-01
saorsa-deploy prod canary unprovision-publisher --name CANARY-01 --purge
```

| Flag | Required | Default | Notes |
|------|----------|---------|-------|
| `--name` | yes | — | Canary deployment name. |
| `--purge` | no | off | Also remove the venv, the uploaded wheels and `/var/lib/ant-canary-publisher`. |
| `--ssh-key-path` | no | `~/.ssh/id_rsa` | SSH key for the monitoring VM. |

**The env-file contract.** `--env-file` is a `KEY=value` file holding the
credentials the *VM* uses — deliberately not the operator's own, and
deliberately prefixed so the two cannot be confused:

```
CANARY_PUBLISHER_DATABASE_URL=postgresql://...     # required
CANARY_PUBLISHER_AWS_ACCESS_KEY_ID=...             # required
CANARY_PUBLISHER_AWS_SECRET_ACCESS_KEY=...         # required
CANARY_PUBLISHER_REGION=eu-west-2                  # optional, this default
```

Every missing key is named **at once**, before the registry is read, before
the monitoring state is loaded and before a wheel is built — the env file is
the only check that costs nothing, so a typo should not be discovered after
ten minutes of work.

**VM layout**, all owned by `root`:

| Path | Mode | Content |
|---|---|---|
| `/opt/ant-canary-publisher/venv/` | — | Python 3.13 venv with `saorsa-db` + `saorsa-deploy` |
| `/opt/ant-canary-publisher/run.sh` | 0755 | the two-step run script |
| `/etc/ant-canary-publisher.env` | 0600 | `DATABASE_URL`, the AWS keys, `CANARY_*`, `INFLUX_*` |
| `/var/lib/ant-canary-publisher/last-run.json` | 0750 dir | which step ran, how long it took, which one failed |
| `/etc/systemd/system/ant-canary-publisher.service` | 0644 | `Type=oneshot`, `TimeoutStartSec=3h` |
| `/etc/systemd/system/ant-canary-publisher.timer` | 0644 | `OnCalendar=…`, `Persistent=true`, `RandomizedDelaySec=0` |

The env file and `run.sh` are **staged locally and uploaded with
`files.put`**, never written by a `cat > … <<EOF` shell op: the env file
carries a database password, an AWS secret key and the Influx token, and a
heredoc op puts all three in the VM's `/proc/<pid>/cmdline` for the op's
duration and into any pyinfra verbose log — and leaves the file
world-readable until a following `chmod`. `files.put` creates it `0600`
outright. Every value is **double-quoted with `\` and `"` escaped**, because
systemd treats a backslash in an *unquoted* `EnvironmentFile` value as an
escape and strips edge whitespace, so an unquoted password containing `\`
would arrive altered — as a credential that simply fails to authenticate,
with nothing on the VM to say why.

`uv` (pinned, and the installed version is verified) supplies the interpreter:
the `deploy` package requires Python ≥ 3.13 and Ubuntu 24.04 ships 3.12, so
there is no system interpreter that can run it. `uv venv --clear` is used, not
bare `uv venv` — re-running the command *is* the upgrade path, and uv 0.9.24
only warns that it will require `--clear` to replace an existing venv, so
without the flag the next version bump would turn every re-provision into a
failure. The installer runs with `UV_NO_MODIFY_PATH=1`, since every command
names `uv` absolutely and a `PATH` line would be an edit to a shared VM's
login environment for no gain.

The two wheels are built on the operator's machine with
`uv build --wheel --package …` and uploaded over the SSH connection pyinfra
already has — installing from a git URL is not an option, since the VM holds
no GitHub credential and the repository may be private. **Both packages
therefore need an explicit `[tool.hatch.build.targets.wheel]` exclude list**:
hatchling honours only the **root** `.gitignore`, so
`saorsa_deploy/resources/wasm-client/node_modules/` — ignored by a nested
`.gitignore` alone — was swept into the wheel on any tree that had ever run
`build ant-wasm-check`, taking it from 1.3 MB to 23.7 MB (12,478 of 12,888
entries) and meaning the VM ran whatever was on the operator's disk rather
than the tree. `build_publisher_wheels` also refuses a wheel over
`MAX_WHEEL_BYTES` (8 MB) as a tripwire for the next such path.

After installing, the command verifies that both
`saorsa-deploy --help` and `saorsa-deploy prod canary export --help` exit 0 on
the VM, quoting the output if either does not: a `saorsa-deploy` predating
`prod canary export` would install cleanly and then fail every night at
step 2.

`run.sh` passes `--influx-url http://localhost:8086` and `--influx-token`
explicitly, so `record-results` never falls back to `load_monitoring_state` —
**the VM has no credential for the tfstate bucket**, and the fallback would
make the timer fail nightly with a boto3 credentials error.
`deploy/tests/test_prod_canary_publisher.py` pins that property.

It is `set -uo pipefail`, deliberately **not** `-e`: the point of
`last-run.json` is to say which step failed, and `-e` would exit before
anything could write it. A failed step stops the run and the next firing
retries from the top — both steps are idempotent (`record-results` upserts,
`export` overwrites).

**Upgrading** the publisher after a change to either package is just
re-running `provision-publisher`: it rebuilds the wheels, reinstalls them,
rewrites the env file and units, reloads systemd and re-enables the timer.

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

### Watching a production rollout (`prod rollout-watch`)

`prod rollout-watch` reports a stable rollout out of Elasticsearch alone:
how much of the fleet is on the new version, what the auto-upgrade machinery
logged, which services restarted, and — while the fleet is mixed — whether the
two arms are behaving the same. It is **stateless**. Every run queries ES
afresh: no SQLite database, no cursor to back-date, no `init` step, nothing to
seed before the rollout starts.

```bash
uv run saorsa-deploy prod rollout-watch --to-version 0.20.0
uv run saorsa-deploy prod rollout-watch --to-version 0.20.0 --window 6h --json-out watch.json
uv run saorsa-deploy prod rollout-watch --to-version 0.20.0 --pretty
```

| Argument | Required | Default | Description |
|---|---|---|---|
| `--to-version` | yes | — | The version being rolled out, matched exactly against `tag.node_version`. |
| `--name` | no | `ant-prod-01` | Deployment whose elastic state supplies the ES URL, API key and the `telegraf-ant-nodes-<name>-*` index. No other credential path. |
| `--window` | no | `1h` | The health window ending now (`15m`, `1h`, `6h`, `24h`). Adoption is always measured over the last 15 minutes. |
| `--json-out` | no | — | Also write the full result as JSON to this path. |
| `--pretty` | no | off | Render the five sections as terminal tables instead of markdown. |

The report is one markdown block on stdout, in five sections.

`--pretty` swaps that markdown for `rich` tables: the same five sections, the
same numbers, drawn for a terminal rather than for a paste into an issue, with
the version on target, a failure row, a restart loop and a diverging per-arm
ratio coloured. It is **presentation only** — both renderers read the one result
dict `build_report` returns, so they cannot disagree, and neither can
`--json-out`, which is written either way. The markdown stays the default
because the report's consumers are the rollout write-ups.

**1. Adoption.** The denominator is every distinct `(host, service_number)` that
logged in the last 15 minutes — "running services", including the bootstraps. A
service's version is the `tag.node_version` on its most recent document. The
bucket named `unknown` is labelled `unknown (unstamped, pre-<to-version>)`,
because builds older than ant-node #225 do not stamp a version at all (a missing
tag means the same thing and lands in the same bucket). That label rule applies
**only** to the literal `unknown`: on every later rollout the old arm is a real
version and appears under its own value. Below the table is the checkpoint band
— `none` / `10–20%` / `20–50%` / `50–99%` / `complete` (≥ 99%).

**2. Upgrade events.** Counts of the auto-upgrade lifecycle messages in the
window, then two tables. Every category is printed even at zero, because a zero
is a measurement.

Each row matches a message ant-node logs **exactly once per event**, so a
document count is an event count and nothing has to be deduplicated. That
matters because the obvious matchers do not have the property: a start logs two
lines back to back (`Starting auto-apply upgrade...` from `node.rs` and
`Starting auto-apply upgrade from {cur} to {new}` from `upgrade/apply.rs`), so
only the second is counted — their common prefix counts every attempt twice.
A success logs `Upgrade to {} successful, initiating graceful shutdown` once
per upgraded service on every path; the one service per host that actually
replaces the shared binary additionally logs `Successfully upgraded to version
{}! Restarting...` and every other service on that host logs `Binary already
upgraded to ...` instead. Those two are therefore **details** of a success, not
successes, and are printed under it as `— of which binary replaced` and `— of
which shared binary reused`, followed by the arithmetic check `replaced +
reused = succeeded`. `retry backoff` counts `Upgrade rollout delay elapsed but
previous apply did not succeed`: a non-zero count against an empty failures
table is a failure the matchers missed, which is the only reason the row is
there.

Every node.rs line above logs under `tag.target = ant_node::node`, so the query
matches that target as well as `ant_node` and the `ant_node::upgrade` prefix.

**Failures** lists each failure prefix with the `(host/service)` pairs that
reported it, and is the only §2 table that breaches the rollout gate.
`Error during upgrade process:` is emitted from three sites, so it is split by
the text after the prefix: the check-phase variants (the GitHub release check
and the release-cache lock — `TRANSIENT_ERROR_MARKERS`) attempted nothing and
go to a separate informational **transient check errors** table, annotated
`all recovered` when every service that reported one is on the to-version at
report time. Everything else, including a variant a future ant-node adds, is a
failure: downgrading one is an explicit addition to that list. A failure whose
detail contains `Failed to spawn new binary` is annotated *"binary already
replaced on disk; old version still running"* — the one case that leaves a host
half-upgraded. A rolled-back apply logs both its specific failure line and the
generic one, so it produces two failure rows; that is deliberate and the
verdict is the same either way.

**3. Restarts.** Services with more than one `ant-node starting` document. A
service that upgraded once has exactly two in a window spanning its upgrade, so
only counts **above** two are flagged `restart-loop`.

**4. Per-arm health.** `new` is every service on the to-version, `old` is every
other. Service-hours are the 15-minute buckets a service logged in, times 0.25.
Reported per arm: ERROR and WARN documents per 1,000 service-hours for the top
15 `tag.target` values, the `Connection attempt failed` rate, and
`wire_tx_bytes` per service-hour (**not** `udp_tx_bytes` — saorsa-transport #169
changed what that counts, so it is not comparable across a mixed fleet).

When **either** arm is under 5% of running services the table is omitted and the
section prints `Per-arm comparison suspended: <new|old> arm is <n> services
(< 5% of running)`: a handful of services produce rates dominated by whichever
host happens to be sick, and an arm of zero produces no rate at all. It is the
new arm that is short before the rollout starts and the old one by the end. The
four health queries are not issued at all in that case, which is most of the
report's cost.

**5. Version telemetry.** The distinct `tag.node_commit` values on the new arm
(expect one) and the number of new-arm documents in the last 15 minutes with no
`tag.node_version` (expect zero).

**Every query is an aggregation (`size: 0`), never a document scroll.** The
production node index takes ~**13.7M documents every two hours**, so scrolling
any part of it is not an option at any window length. `tag.*` fields are
analysed, so every `terms`/`composite`/`prefix` goes through the `.keyword`
subfield, and host values are upper-case.

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

### A standalone Elasticsearch instance (`kind: elasticsearch`)

`deployments up --config <manifest>` with `kind: elasticsearch` stands up an
Elasticsearch instance that is **not part of any deployment** — no nodes, no
clients, no binary. It exists so log producers outside our own fleet (the
community beta-channel cohort) have somewhere to forward to, and it is read by
the `beta-health-verdict` skill and the beta rollout watch.

It reuses the existing `elastic infra` / `elastic provision` / `elastic destroy`
commands and the same `saorsa-deploy/elastic/{name}.json` state; what the kind
adds is `--standalone` (register a `Deployment` row with
`deploy_args.kind = "elasticsearch"`) and the public-ingest shape below.

Example manifest: `deploy/manifests/profiles/elasticsearch.yaml`.

#### Public ingest is a different security posture, not a flag

A testnet's Elasticsearch is reachable only by our own VMs for a few hours, so
it deliberately runs **plaintext on 9200 with anonymous access enabled**
(`remote_monitoring_collector`, so Telegraf can read the version endpoint). An
instance community machines write to is on the open internet, where a credential
in cleartext on every request is not acceptable. `public_ingest: true` therefore
changes the instance:

| | testnet stack (default) | `public_ingest: true` |
|---|---|---|
| ES bind address | `0.0.0.0` | `127.0.0.1` |
| Anonymous access | enabled | **disabled** |
| Firewall | 22, 9200 | 22, **443** (9200 closed, 80 never opened) |
| Transport | plaintext HTTP | TLS at a Caddy reverse proxy |
| Exposed API | the whole ES REST surface | `_bulk` / `_doc` / `_create` writes only |
| Address | droplet IP | **reserved IP** (survives a rebuild) |

The proxy allowlists write endpoints rather than denying known-bad ones, so an
endpoint added by a future Elasticsearch version is unreachable by default
rather than newly exposed. Everything else gets a bare 404.

It also appends `filter_path=errors,items.*.error` to every request. Elasticsearch
otherwise answers a bulk request with a result object **per document** (~200+
bytes each), making the response roughly the size of the request that produced
it — pure egress, on every batch, forever. Forcing it at the proxy means it holds
regardless of how a community forwarder is configured.

#### DNS: the one manual step

The certificate is obtained **during provisioning**, so `ingest_hostname` must
already resolve to the instance by then — and the address does not exist until
`elastic infra` has run. The first deploy is therefore two-phase:

1. `up` runs `elastic infra`, which prints the **reserved IP**.
2. Point the A record at it. In Cloudflare it must be **DNS only (grey cloud)**:
   a proxied record terminates TLS at Cloudflare, so issuance fails here.
3. `up --resume` continues into `elastic provision`, which **preflights the
   record** — naming the record and the address it should carry if it is wrong —
   and then obtains the certificate.

This is why the kind emits two steps rather than one wrapper command: a resume
retries provisioning alone instead of recreating the VM. And it really is
one-off — the reserved IP survives a droplet rebuild, so record it in
`reserved_ip` and the DNS never needs touching again.

Port **80 is never opened**: the certificate is issued over TLS-ALPN-01 on 443,
so there is no HTTP-01 challenge to serve and no cleartext listener to redirect
from.

Let's Encrypt allows only **5 duplicate certificates per week** and the lockout
outlasts the debugging session that causes it, so use
`letsencrypt_staging: true` while iterating on a deploy of the same hostname.
Staging certificates are not publicly trusted.

#### The ingest contract (what a forwarder writes)

Verified end to end against Elasticsearch 8.17.4 with security enabled, using a
key minted by `elastic mint-key`.

| | |
|---|---|
| Endpoint | `POST https://<ingest_hostname>/_bulk` (the proxy is transparent — it is the Elasticsearch API, not a translation layer) |
| Auth | `Authorization: ApiKey <encoded>` — a real Elasticsearch API key, **not** a bearer token |
| Content-Type | `application/x-ndjson`, trailing newline required |
| Action | **`create` only** — `index` returns a per-item 403 |
| Index | `beta-nodes-YYYY.MM.DD` (UTC), inside the `beta-nodes-*` pattern the key is scoped to |
| Time field | **`@timestamp`**, RFC3339 |
| Max body | **50 MB** at the proxy (413 above it); Elasticsearch's own limit is 100 MB |

**`create`, not `index`.** The key grants `create_doc`, which permits creating a
document but not overwriting one — so a bulk `index` action is rejected per item
with a `security_exception`, and `create` is the only action that works.
`POST /{index}/_doc` (auto-ID) and `POST /{index}/_create/{id}` also work for
single documents. Granting `index` instead would let one participant overwrite
another's document by `_id`.

**Errors are per item, not per request.** A `_bulk` call returns HTTP 200 even
when individual documents are rejected, so a client must read
`items[].status` — checking only the HTTP status will silently discard failures.
Three statuses matter: `201` indexed, `200` **dropped by the level filter**
(expected, not an error), and `403`/`429`/`400` genuine failures.

**The proxy forces `filter_path=errors,items.*.status,items.*.error`.** Measured
on a 500-document batch: 100 KB unfiltered, 13 KB with this filter.
`items.*.error` alone is 16 bytes but Elasticsearch then **collapses the items
array to only the failures**, so positions no longer correspond to the submitted
documents — a client cannot tell which document a 429 belongs to and must retry
the whole batch, duplicating everything that already succeeded. `status` is
therefore not optional.

Reads are refused: `_search` against the index returns 403, as does `GET /`.
The mapped fields are `@timestamp`, `message`, `level`, `target`, `peer_id`,
`version`, `commit`, `host`, `beta_user`, plus the forwarding daemon's
identity set — `node_id`, `service`, `binary_version`, `channel`, `os`, `arch`
— all keywords so they aggregate. Unmapped fields are accepted via dynamic
mapping, and `ignore_malformed` stops one bad value failing a whole batch.

#### Controlling the log level, server-side

A community participant can edit their own forwarder, so a level set there is a
request, not a control. The policy is therefore enforced at ingest, by an
Elasticsearch pipeline attached to the index as **`index.final_pipeline`**:

```bash
saorsa-deploy elastic set-log-level --name BETA-LOGS-01 --min-level debug
```

`min_log_level` (manifest) / `--min-log-level` (provision) sets the starting
policy, default **`info`**. The drop set is derived the same way as the node
Telegraf forwarder's `--drop-log-levels` — drop only what is strictly *more*
verbose than the minimum:

| `min_log_level` | dropped at ingest |
|---|---|
| `info` (default) | `DEBUG`, `TRACE` |
| `debug` | `TRACE` |
| `trace` | nothing |

`WARN` and `ERROR` are never dropped at any setting, and a document with **no**
`level` field is kept — losing logs of an unanticipated shape is worse than
keeping a few we did not mean to.

Four properties, all verified against a real Elasticsearch 8.17.4:

- **`final_pipeline` cannot be bypassed.** A client passing `?pipeline=_none`
  still had its DEBUG documents dropped. **`default_pipeline` can** — the same
  request bypassed it entirely and the document landed unfiltered, so never use
  it for anything an untrusted client must not be able to opt out of. The proxy
  additionally strips a client-supplied `pipeline` parameter (`uri query
  -pipeline`), so the two mechanisms back each other up.
- **Dropped documents look like success**: `status: 200, result: "noop"`, with
  the bulk response reporting `errors: false`. That is deliberate — a `fail`
  processor would return a per-document error and most forwarders would retry
  the same batch forever. The trade is that drops are **invisible**: nothing
  counts them, and participants are not told.
- **Changing the level needs no redeploy.** `set-log-level` rewrites the
  *pipeline body*; the index template already names the pipeline, so the new
  policy applies to the next document written — no restart, no reindex.
  Already-indexed logs are untouched; this filters what is accepted from here on.
- **The `reroute` processor is rejected in a final_pipeline** ("final pipeline
  can't change the target index"), so the tempting "quarantine verbose logs into
  a cheap 1-day index to see who is over-sending" design does not work in the
  enforceable slot. It works only in `default_pipeline` — which is viable *only*
  because the proxy strips the `pipeline` parameter.

This protects the **disk and the index, not the network**: a dropped document
has already crossed the wire and been parsed. Ingress is free on DigitalOcean so
that is acceptable, but level filtering is not a bandwidth control — the
disk-size cap is.

#### Per-user ingest credentials

Community logs land in `beta-nodes-*` under the ILM policy, and each participant
gets their **own** Elasticsearch API key rather than a shared one — so a leak
re-keys one participant instead of all of them, and documents can be attributed.

```bash
saorsa-deploy elastic mint-key   --name BETA-LOGS-01 --user <handle> [--expiration 90d]
saorsa-deploy elastic list-keys  --name BETA-LOGS-01
saorsa-deploy elastic revoke-key --name BETA-LOGS-01 --user <handle>
```

The privileges are `create_doc` + `auto_configure` on `beta-nodes-*` and nothing
else. There is deliberately **no `read`**: a leaked key can add logs and do
nothing more — it cannot pull another participant's logs, cannot overwrite an
existing document, and cannot touch any other index. (The pre-existing
`ant_log_writer` role is *not* reused: it grants `read` and `manage`.)

Keys **do not expire** by default. An expiring credential sounds prudent, but a
beta participant runs their node indefinitely, so a fixed lifetime means chasing
everyone to re-key on a rolling schedule — and a forwarder whose key lapses fails
silently from our side, because nobody is watching their logs stop arriving.
Revocation is the control that matters, and it is immediate and per-participant.
Pass `--expiration 90d` to time-box a key deliberately.

Note an expiry **cannot be cleared** once set: Elasticsearch's update-API-key
endpoint rejects a null expiration, though it will accept a longer one. So a key
minted with a lifetime can be extended (`PUT /_security/api_key/<id>` with
`{"expiration": "36500d"}`) but never made truly perpetual — set it at mint time
or not at all.

The encoded credential is shown **once** — if it is lost, revoke and re-mint.
`revoke-key` reports honestly when no active key matched, rather than claiming a
revocation that did not happen.

| Manifest key | Default | Notes |
|---|---|---|
| `provider` | `digitalocean` | The only provider whose ES volume grows online; OVH's size is ForceNew, Contabo has no volume |
| `region` | per provider (`lon1`) | |
| `droplet_size` | per provider (`s-4vcpu-8gb`) | 8 GB RAM → 4 GB auto-sized heap |
| `volume_size` | `100` (GB) | The binding constraint; grow online as the cohort grows |
| `shards` | `4` | One indexing thread per shard; match the vCPU count |
| `retention_days` | `14` | ILM delete phase; the beta soak is 5 days |
| `min_log_level` | `info` | Server-side ingest filter; change later with `elastic set-log-level` |
| `public_ingest` | `false` | Requires `ingest_hostname` |
| `ingest_hostname` | — | The name the certificate is issued for; must resolve first |
| `reserved_ip` | — | Adopt an existing reserved IP on a rebuild. DigitalOcean only |
| `letsencrypt_staging` | `false` | Avoid the 5-per-week duplicate-certificate limit while iterating |
| `keep_on_destroy` | `false` | Set `true` for a permanent instance |

#### Inventory

`deployments generate-inventory --name <name>` covers this kind, so the
instance's access details can be handed to a person or another agent without
anyone reading S3 state. `up` emits it automatically as a **`best_effort`**
final step — the instance is already up and billing by then, so failing to
describe it must not fail the run.

The file is **schema 6** (`elasticsearch_config`, alongside v5's
`client_config`). `node_vms`/`bootstrap_vms`/`client_vms` are empty — this kind
stands up one observability VM and nothing else — so `load_node_inventory`
correctly rejects it, exactly as it does a client inventory.

It carries two clearly separated routes, because conflating them is the trap:

- **`ingest`** — the public write path. URL, index pattern and naming,
  `@timestamp`, the `create`-only rule, the body cap and the current
  `min_log_level`.
- **`read`** — the query path, which is **not** the public URL. Elasticsearch
  binds to loopback and 9200 is firewalled, so the read key returns 404 at the
  proxy — indistinguishable from a missing index. The section therefore gives
  `http://localhost:9200`, a ready-made `ssh -L` tunnel command, and says why.

The read-only API key **is** in the file; the admin password is deliberately
**not**. The provisioner sets the `elastic` superuser and the log-writer from
one secret, so the stored password is effectively superuser — anyone entitled to
it has SSH and `elastic info`. `generate-inventory` prints a warning naming what
the file contains, so it is never a surprise to whoever forwards it.

Fields recording the instance's own policy (`retention_days`,
`min_log_level`) come from what was provisioned, not the CLI defaults. An
instance provisioned before those were recorded reports them as absent rather
than guessing.

#### Growing the disk

`volume_size` is the knob to watch, and it grows **online** on DigitalOcean:
bump it in the manifest and `terraform apply -var=volume_size=N` in
`.saorsa/workspaces/digitalocean-elastic-<NAME>-<region>` plans
`0 add / 1 change / 0 destroy`, then `resize2fs /dev/sda` on the VM. No
Elasticsearch restart, state stays consistent.

### Running tests

```bash
uv run pytest tests/ -v
```

### Linting and formatting

```bash
uv run ruff format saorsa_deploy/ tests/
uv run ruff check saorsa_deploy/ tests/
```
