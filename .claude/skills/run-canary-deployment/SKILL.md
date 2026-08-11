---
name: run-canary-deployment
description: Stand up the release train's production canary — a small set of RC ant-node services on a single VM joined to the production Autonomi network. Reads the current Release Train issue from Linear for the ant-node RC version, updates the STG-03 manifest, deploys it, smoke-tests that metrics reach InfluxDB, logs reach Elasticsearch and the nodes joined the network, then records the canary on the "Canary deployment" sub-issue and closes it. Use when asked to "deploy the canary", "run the canary deployment", "stand up the canary", "deploy the RC to production", "kick off the canary for rc.N", or "do the canary deployment".
---

# run-canary-deployment

Stands up the current release train's **production canary**: a small set of release-candidate
`ant-node` services on a single VM, joined to the **real production Autonomi network**, in a
debugging configuration (`kind: prod-debug-nodes`). The point is to give the RC some genuine
production exposure before the full release ships.

The service count is whatever the manifest's `node_count` says — **the manifest is authoritative,
not this document**. It is currently **4**, and it is chosen by what the log forwarder can carry,
not by matching the production fleet; see Step 3.

This skill ends at **"the canary is up and healthy"**. It is the deployment half of the canary
work; the judgment half is the separate `evaluate-canary` skill, which runs later (normally
Wednesday) against the **still-live** canary and writes the `Canary verification` sub-issue.

Rules:

- **This runs against production and costs real money.** The nodes join the live network and
  sync real chunk data. Do not improvise with the configuration.
- **Coordination only.** Every action is a `saorsa-deploy` CLI command, a read-only probe, or a
  Linear update.
- **Never tear the canary down.** `evaluate-canary` needs it live, and teardown belongs to the
  Ship step. Nothing in this skill destroys the canary except the explicit rollback of a failed
  *infrastructure* step (Step 4).
- **A failed smoke check never closes the sub-issue.** Report it, leave the canary up, leave the
  sub-issue open, and hand back. A degraded canary is a *finding*, not something to repair — do
  not restart services, re-provision, or re-join nodes to make a check pass.
- **Unattended.** Report the extracted RC version, then proceed. Do not stop to show the plan or
  ask for approval — this is a standard, well-known configuration. Stop only on a failure or an
  unmet precondition.

## Step 1 — Read the release train issue

Find the current train:

1. `list_issues` with `project: "Releases"`, `team: "v2.0"`, `state: "In Progress"`.
2. Expect exactly one issue titled `Release Train — YYYY-MM-DD`. If there are none or more than
   one, stop and ask.
3. `get_issue` on it for the full description.

Extract:

| What | Where | Example |
|---|---|---|
| Train date | Issue title, `Release Train — <date>` | `2026-07-28` |
| `ant-node` RC version | `## Cut record` → versions table, `ant-node` row | `0.16.0-rc.1` |
| Risks to watch | `## Risks to watch in staging/canaries` | — |

Notes:

- **Only the `ant-node` version applies.** The canary is `kind: prod-debug-nodes`, which runs
  node services and no client — there is no `ant`/`ant-cli` binary in this manifest. (Contrast
  `run-staging-testnet`, where STG-01 needs both.) Do not go looking for a second version field.
- **Read the RC version carefully — the table's shape is not what you might expect.** The
  `## Cut record` table has columns `Crate | Version | rc-branch head`, and the `Version` cell
  holds *both* versions in one field as `<previous> → **<RC>**`:

  | Crate | Version | rc-branch head |
  | -- | -- | -- |
  | ant-node | 0.15.0 → **0.16.0-rc.1** | `48cffdb…` |

  You want the **bolded right-hand side** (`0.16.0-rc.1`), not the bare left-hand side. There is
  no separate "New" column. Cross-check against the `**RC tags**` bullets immediately below the
  table, which name it unambiguously (`v0.16.0-rc.1` → ant-node) — if the two disagree, stop and
  ask.
- The `## Cut record` table is authoritative over the earlier `### Final versions` table in the
  manifest section. That earlier one lists the *release* versions (`0.15.0 → 0.16.0`), not the RC
  the canary must run, and it can be superseded at cut time. Do not read the version from it.
- If there is no `## Cut record` section, the cut has not happened. Stop.
- The "Risks to watch" section names what to look hardest for; carry it into the Step 6 report so
  `evaluate-canary` has it in context.

Then list the train's sub-issues (`list_issues` with `parentId`) and note the **`Canary
deployment`** one. Move it to **In Progress** now — the deploy and its settle window occupy it for
a while, and leaving it in Todo makes a train that is actively standing up a canary look
untouched. You will comment on it and close it at the end. (There is also a `Canary verification`
sub-issue — that one belongs to `evaluate-canary`. Leave it alone, in Todo.)

Report the extracted values before continuing.

## Step 2 — Check preconditions

Stop and name the item that failed if any of these is not met:

- `DATABASE_URL` is set and reaches the registry. Check with `uv run saorsa-deploy deployments ls`.
- Cloud credentials for everything the manifest provisions:
  - the four `OVH_*` vars (`OVH_APPLICATION_KEY`, `OVH_APPLICATION_SECRET`, `OVH_CONSUMER_KEY`,
    `OVH_CLOUD_PROJECT_SERVICE`) — the **node VM**. OVH credentials are needed at destroy time too.
  - `DO_TOKEN` — the **monitoring (InfluxDB)** and **Elasticsearch** VMs.
  - `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — S3 deployment state.
- `~/.ssh/ant_deploy` exists locally. It is the key the manifest names and the one that actually
  works on the VM (see the gotcha in Step 5).
- No other `up` or `down` is running for `STG-03`. They share S3 and Terraform state and will race.

**Check the previous canary.** The name `STG-03` is reused every train, so `deployments ls` will
show rows from previous ones:

```bash
uv run saorsa-deploy deployments ls | grep STG-03
```

- **Note the highest existing `STG-03` id before you start.** This is how you prove at Step 6 that
  the row you report is *this* train's. A reused deployment name has silently no-op'd a recording
  command and lost a whole run's data before.
- If the most recent `STG-03` row is anything other than `destroyed` — `running`, `failed`, or
  stuck at `destroying` — **stop and ask**. Do not auto-destroy it and do not deploy over it. A
  previous canary may still be wanted, and a half-destroyed one means live cloud resources that a
  new deploy would collide with.

## Step 3 — Update the manifest

Always use this manifest, path relative to the repo root:

```
deploy/manifests/runs/staging/STG-03.yaml
```

Edit **two things and nothing else**:

- `prod_debug_nodes.binary.version` → the `ant-node` RC version from Step 1 (e.g. `0.16.0-rc.1`).
- `purpose` → `"Release Train <date>: canary production nodes"`.

**Everything else in that file is load-bearing.** In particular:

- `volume_size: 750` — fresh nodes sync chunk data from production and debug logs add to it fast
  (sized when the canary ran 10 services: ~220 GB of chunk sync plus ~11 GB/hr of logs, landing
  near 500 GB over 24h). It is deliberately kept at 750 GB at the lower node count: on OVH the
  volume size is ForceNew, so you cannot grow it later without recreating the VM, and the 100 GB
  default fills in well under an hour.
- `elastic_volume_size: 500` — debug-level node logs ingest at ~7–9 GB/hr (~200 GB/24h), which
  fills the default long before the 3-day ILM prunes anything.
- `node_count: 4`, `log_level: debug`, `rust_backtrace: "full"`, `coredumps: true` — these are
  what make the canary *debuggable*; `evaluate-canary` depends on all of them.
- **`node_count` is forwarder-bound, not fleet-matched — do not raise it back to 10.** The canary
  ran 10 services until 2026-08-03, when the node count was measured against what the log
  forwarder can actually carry. At `log_level: debug` each service emits ~758 lines/s and a single
  Telegraf tops out ~2,700 lines/s — CPU-bound in its own parse chain, *not* blocked on
  Elasticsearch. At 10 services only **~36%** of node log lines reached ES, and the forwarder had
  silently wedged for three days. More services therefore buy *less* evidence, not more: they
  degrade the ES record that `evaluate-canary` reads. 4 is the deliberate re-baseline.
- `log_retention_days: 1` is deliberately short because the logs are durable in Elasticsearch.

Changing any of those is a re-baselining decision and needs the RM. The file's own comments record
the measurements behind the sizes — read them before touching anything.

Validate the edit:

```bash
uv run saorsa-deploy deployments config validate --config deploy/manifests/runs/staging/STG-03.yaml
```

Then proceed straight to the deploy. Do not stop to show a plan or ask for approval.

## Step 4 — Deploy

```bash
uv run saorsa-deploy deployments up --config deploy/manifests/runs/staging/STG-03.yaml
```

This creates the node VM (OVH), the InfluxDB VM and the Elasticsearch VM (both DigitalOcean),
provisions the node services and the forwarders, and starts the nodes. It takes a while — give it
a generous timeout and let it run.

When the last step completes, record the wall-clock time. That is **T0**. It goes in the report and
`evaluate-canary` uses it as the start of its window.

### When a deploy step fails

Two classes, handled differently.

**Infrastructure creation** (`nodes infra`, `monitoring infra`, `elastic infra` — anything creating
VMs or volumes at a provider). Do **not** retry. These are usually provider-side capacity or
maintenance problems and they last a while.

1. Stop the run.
2. Clear whatever was created:
   ```bash
   uv run saorsa-deploy deployments down --name STG-03
   ```
3. Report to the user: which provider and step failed, and the error.

Watch for the OVH-specific one: node infra that **hangs silently for ~60 minutes** rather than
erroring is the per-region 40-vCPU quota being exhausted, not a capacity outage. Report it as a
quota problem.

**Provisioning** (`nodes provision`, `monitoring provision`, `elastic provision` — anything
installing software over SSH). Usually transient SSH failures. Retry:

```bash
uv run saorsa-deploy deployments up --config deploy/manifests/runs/staging/STG-03.yaml --resume
```

`--resume` skips the already-completed steps using the journal. Without it a non-TTY journal hit
refuses rather than hanging.

Retry a few times. It usually passes. After about three attempts with no progress, treat it as
terminal: stop, `deployments down --name STG-03`, and report which host was stuck.

One provisioning failure looks different: a node provision that **hangs silently** rather than
erroring is usually a stalled S3 binary download on a throttled VM. The partial binary is then
skipped by the idempotency check on retry, so it must be deleted on that VM before `--resume` will
help.

Do not work around infrastructure or permission problems in any other way.

## Step 5 — Smoke test

**Wait ~10 minutes after T0 before judging any of this.** Telegraf's first flush, the
Elasticsearch index creation and the nodes' initial bootstrap all lag the end of provisioning, so
checking immediately reports a false failure on a perfectly healthy canary. One wait, then one
pass of the checks — this is not a retry loop.

Gather the connection details first:

```bash
uv run saorsa-deploy deployments generate-inventory --name STG-03 --ssh-key-path ~/.ssh/ant_deploy
uv run saorsa-deploy monitoring info --name STG-03   # InfluxDB URL, token, org, bucket
uv run saorsa-deploy elastic info    --name STG-03   # ES URL, user, password, API key, index pattern
```

From the inventory: `node_vms[0].ip` is the VM, and its `services` list is `["ant-node-1", …,
"ant-node-N"]` — N is the manifest's `node_count` (currently 4; read it off the inventory rather
than assuming). `observability_vms[]` holds the
`influxdb` and `elasticsearch` IPs.

> **SSH key gotcha.** `generate-inventory` defaults `--ssh-key-path` to `~/.ssh/id_rsa`, which does
> **not** work on these VMs — pass `~/.ssh/ant_deploy` as above. If you hit
> `Permission denied (publickey)` anyway, confirm with a one-liner before any sweep:
> `ssh -i ~/.ssh/ant_deploy -o BatchMode=yes root@<ip> 'hostname'`.

### Check 1 — metrics are reaching InfluxDB

`POST <influx_url>/api/v2/query?org=<org>`, `Authorization: Token <token>`,
`Content-Type: application/vnd.flux`, `Accept: application/csv`. Bucket is `testnet-metrics`.

List what exists first:

```bash
curl -s -XPOST "$INFLUX_URL/api/v2/query?org=$ORG" \
  -H "Authorization: Token $TOKEN" -H 'Content-Type: application/vnd.flux' \
  -H 'Accept: application/csv' \
  -d 'import "influxdata/influxdb/schema"
      schema.measurements(bucket:"testnet-metrics")'
```

Expect `cpu`, `disk`, `diskio`, `mem`, `net`, `procstat`.

> **`prometheus` will be absent, and that is correct.** ant-node runs no metrics/OpenMetrics
> server, so nothing listens on `127.0.0.1:9100-9109`; the `--metrics-port` flag on the service and
> the Telegraf scrape of those ports are inert leftover config, not a failed endpoint. Do not go
> hunting for a metrics server and do not treat the missing measurement as a smoke-test failure.

Then confirm per-service data is actually flowing, for **all N services**:

```bash
curl -s -XPOST "$INFLUX_URL/api/v2/query?org=$ORG" \
  -H "Authorization: Token $TOKEN" -H 'Content-Type: application/vnd.flux' \
  -H 'Accept: application/csv' \
  -d 'from(bucket:"testnet-metrics")
        |> range(start: -15m)
        |> filter(fn: (r) => r._measurement == "procstat" and r._field == "memory_rss")
        |> group(columns: ["node_service"])
        |> count()'
```

**PASS** when host measurements are present and `procstat`/`memory_rss` returns a non-zero count
for every `node_service` from `ant-node-1` to `ant-node-N`. A missing service here means either
that service is not running or its Telegraf is not reporting — report which.

### Check 2 — logs are reaching Elasticsearch

The index is `telegraf-ant-nodes-<name-lowercased>-*`, i.e. `telegraf-ant-nodes-stg-03-*`.
`elastic info` prints the exact pattern — use what it prints.

```bash
ES=<es_url>; AK="ApiKey <api_key>"; IDX='telegraf-ant-nodes-stg-03-*'

# doc count over the last 15 minutes
curl -s -H "Authorization: $AK" -H 'Content-Type: application/json' "$ES/$IDX/_count" -d \
 '{"query":{"range":{"@timestamp":{"gte":"now-15m"}}}}'

# level histogram — sanity-check the shape of what is arriving
curl -s -H "Authorization: $AK" -H 'Content-Type: application/json' "$ES/$IDX/_search" -d \
 '{"size":0,"query":{"range":{"@timestamp":{"gte":"now-15m"}}},
   "aggs":{"lvl":{"terms":{"field":"tag.level.keyword","size":10}}}}'
```

> **ES fields are nested and text-typed — the single biggest footgun.** Aggregate and filter on
> `tag.level.keyword`, `tag.target.keyword`, `tag.service_number.keyword`. The log body is
> `tail.message` (use `match_phrase`). A bare `level` or `target` agg silently returns `[]`, and a
> bare `tag.level` agg errors because fielddata is disabled. Add `"track_total_hits": true` when
> you need a real count — the default caps the reported total at 10000.

**PASS** when the count is non-zero and the level histogram shows `DEBUG` (the manifest sets
`log_level: debug`) alongside `INFO`. A `DEBUG`-free histogram means the drop filter is stripping
what the canary exists to capture — report it.

### Check 3 — the nodes joined the network and see peers

> **There is no routing-table or peer-count gauge anywhere.** The node emits no metrics (see Check
> 1), so no InfluxDB measurement and no CLI reports peer counts. Connectivity is established from
> converging indirect evidence, and the verdict must be stated as **PASS (inferred)** — never imply
> you read a peer count you could not.

**(a) Every service is live on the wire.** Aggregate ES over a recent window by service:

```bash
curl -s -H "Authorization: $AK" -H 'Content-Type: application/json' "$ES/$IDX/_search" -d \
 '{"size":0,"query":{"range":{"@timestamp":{"gte":"now-15m"}}},
   "aggs":{"svc":{"terms":{"field":"tag.service_number.keyword","size":20}}}}'
```

All N services (`"1"`…`"N"`) must appear with a healthy volume of lines. A silent service is the
flag.

**(b) Peer/DHT activity is happening**, not just startup chatter. Sample the transport and DHT
targets and read a few bodies:

```bash
curl -s -H "Authorization: $AK" -H 'Content-Type: application/json' "$ES/$IDX/_search" -d \
 '{"size":0,"query":{"range":{"@timestamp":{"gte":"now-15m"}}},
   "aggs":{"tgt":{"terms":{"field":"tag.target.keyword","size":25}}}}'
```

Expect routing/transport/replication targets to be active. Then confirm the nodes are talking to
peers rather than sitting isolated — sample a handful of recent `tail.message` bodies on those
targets and check they reference peers/connections, not repeated bootstrap failures.

**(c) Corroborate over SSH.** Zero restarts and a store that is filling both mean the services
stayed in the network:

```bash
ssh -i ~/.ssh/ant_deploy -o StrictHostKeyChecking=no -o BatchMode=yes root@<ip> '
for u in $(systemctl list-unit-files --plain --no-legend "ant-node-[0-9]*.service" | awk "{print \$1}" | sort -V); do
  printf "%-22s NRestarts=%s Active=%s Sub=%s Result=%s\n" "$u" \
    "$(systemctl show $u -p NRestarts --value)" \
    "$(systemctl show $u -p ActiveState --value)" \
    "$(systemctl show $u -p SubState --value)" \
    "$(systemctl show $u -p Result --value)"
done
echo "== binary ==" ; /usr/local/bin/ant-node --version 2>&1 | head -1
echo "== store ==" ; du -cb /mnt/ant-node-storage/data/*/chunks.mdb 2>/dev/null | tail -1
'
```

- The loop enumerates the installed unit *files* rather than counting to a fixed N, so a service
  that failed to install is visible as an absence. **Check the row count equals `node_count`** —
  a short list is itself a finding. The `[0-9]` in the glob matters: a bare `ant-node-*.service`
  also matches the `ant-node-coredump-logprune` / `ant-node-debug-logprune` timer units, which
  are `inactive/dead` by design and would both inflate the count and read as dead services.
- `NRestarts` should be `0` and `ActiveState=active` for every service. `ActiveState=active` alone
  is **not** a pass — a service can be up with `NRestarts=3`.
- **Confirm the live binary is the RC**: `/usr/local/bin/ant-node --version` must match the version
  from Step 1. Do not try to parse it out of `systemctl show -p ExecStart`, whose `--value` is a
  `{ path=… ; argv[]=… }` struct rather than a bare path. A wrong version means the canary is not
  testing what the train ships — that invalidates the whole run, so stop and report.
- The chunk store is **LMDB** (`chunks.mdb`), not a `record_store/` directory — that path matches
  nothing and reads as a false zero. This early read is a baseline, not a criterion; LMDB
  preallocates, so a single absolute number proves nothing. `evaluate-canary` takes the spaced
  reads that establish growth.

**PASS (inferred)** when all N services appear in ES with peer/DHT activity, `NRestarts=0`
everywhere, and the binary is the RC.

### If a check fails

Stop. Report which check failed and the evidence. **Leave the canary running** and **leave the
`Canary deployment` sub-issue open.** Do not restart services, re-provision, or tear anything down
to make a check pass — hand back to the RM with the reading.

## Step 6 — Report and update Linear

Emit this report, then post it as a comment on the **`Canary deployment`** sub-issue:

```
# Canary deployment — Release Train <date> (rc.N)

Canary:   STG-03 (registry id <id>), <N> × ant-node <RC> on 1 VM
Vehicle:  prod-debug-nodes — OVH <region> <vm_size>, <volume_size>GB volume
T0:       <timestamp, UTC>
Backends: InfluxDB <ip> (bucket testnet-metrics) · Elasticsearch <ip> (telegraf-ant-nodes-stg-03-*)
Access:   ssh -i ~/.ssh/ant_deploy root@<node vm ip>

## Smoke test

- Metrics → InfluxDB:   PASS | FAIL   <measurements present; procstat services reporting M/N>
- Logs → Elasticsearch: PASS | FAIL   <docs in last 15m; level histogram>
- Joined & seeing peers: PASS (inferred) | FAIL
                                      <services live in ES M/N; NRestarts max; binary version>

## Notes

<risks to watch from the train issue; anything off-band; any check that could not run>
```

Fill every field — an unfillable field is itself a finding, so say why.

**The registry name (`STG-03`) and T0 must both be in this comment.** `evaluate-canary` reads them
off this sub-issue as its primary source for the deployment name and window; without them it falls
back to guessing from `created_at`.

Confirm the id you report is this train's: take the newest `STG-03` id from `deployments ls` and
check it is greater than the id you noted in Step 2.

Then:

- **All three checks PASS** → move the `Canary deployment` sub-issue from In Progress to **Done**.
- **Any check FAILED** → leave the sub-issue **In Progress** (not Done, not back to Todo — the
  canary is up and the work is genuinely mid-flight), leave the canary running, and hand back to
  the RM naming the failure.

Leave the `Canary verification` sub-issue alone — that is `evaluate-canary`'s to close.

Finally, tell the user plainly that **the canary is now running on production and is deliberately
left up**: `evaluate-canary` needs it live, and teardown happens at Ship.

## Known gaps

State these in the report rather than working around them:

- **ant-node emits no metrics.** No metrics/OpenMetrics server, so the InfluxDB `prometheus`
  measurement does not exist and there is no routing-table, peer-count or records-stored gauge
  anywhere. "Joined and seeing peers" is therefore *inferred* from ES log activity plus zero
  restarts. `--metrics-port` and the Telegraf scrape of `9100-9109` are inert leftover config.
- **No node-evaluation CLI.** `deployments record-results` / `print-recorded-results` do not handle
  the `prod-debug-nodes` kind — they fall through to an empty file-ops path. `record-analysis` is
  the only durable read-back (host metrics only) and `evaluate-canary` runs it, not this skill.
- **No CLI reports chunk-store counts.** The store is LMDB, so the only measure is `du` on
  `chunks.mdb` / `paid_list.mdb`, which is byte size rather than a record count.
- **ES fields are nested and text-typed.** Aggregations need the `.keyword` subfields; the log body
  is `tail.message`. A bare `level`/`target` field silently returns nothing.
- **The inventory's SSH key default is wrong** (`~/.ssh/id_rsa`); the working key is
  `~/.ssh/ant_deploy`.
- **The smoke test is a point-in-time check.** It says the canary came up healthy, not that it
  stays healthy — sustained behaviour over the window is `evaluate-canary`'s job.
- **The canary is left running.** Teardown is the Ship step's, and tearing it down before
  verification makes the hard gates unverifiable.
