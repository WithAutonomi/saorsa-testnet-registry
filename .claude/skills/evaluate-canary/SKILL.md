---
name: evaluate-canary
description: Evaluate a release train's production canary deployment (the ~10 RC ant-node services on a single VM) and produce the recorded canary verification. Reads the current Release Train issue from Linear, locates the canary in the registry, runs the hard-gate and observation checks over the run window via SSH + the canary's Elasticsearch/InfluxDB + record-analysis, emits a fixed-format verification report, posts it as a comment on the "Canary verification" sub-issue, and closes that sub-issue. Use when asked to "evaluate the canary", "verify the canary", "run canary verification", "do the canary verification for rc.N", "check the canary results", or "close Canary verification".
---

# evaluate-canary

Produces the **canary verification** for the current release train: the judgment-and-record
counterpart to the canary *deployment* (the `run-canary-deployment` skill, which stands the canary
up, smoke-tests it and leaves it running — a separate sub-issue, usually a separate session;
teardown belongs to Ship, after this verification). This skill reviews a still-running (or
already-torn-down) canary, writes a fixed-format report, records it on the `Canary verification`
sub-issue, and closes that sub-issue.

Rules:

- **Collate and record — do not fix.** Every action is a read: a `saorsa-deploy` CLI command, an
  SSH probe, an Elasticsearch/InfluxDB query, or a Linear update. Read-only on the canary hosts.
  Never restart a service, re-provision, re-join a node, or otherwise touch the canary — a
  degraded canary is a *finding*, not something to repair. If a service is down, report it; do
  not `systemctl start` it.
- **You do not issue the gate verdict.** That is `Gate verdict` (a separate skill, V2-724), which
  *consumes* this report. This skill stops at "canary verification recorded".
- **Hard-gate FAIL never auto-closes.** On any hard-gate failure, post the report and hand back to
  the RM with the sub-issue left open. Only an all-hard-gates-PASS run moves `Canary verification`
  to Done.
- **The canary is normally still up at verification time.** In the standard flow this runs on
  Wednesday, before Ship tears the canary down, so the live sources (SSH, the canary's ES and
  InfluxDB) are available. If the canary has already been torn down, say so and evaluate in
  degraded mode (Step 2) — the hard gates cannot be fully re-verified from Postgres alone.

## What the canary is

~10 release-candidate `ant-node` services on a **single VM**, joined to the **production**
Autonomi network in a debugging configuration, deployed via **`prod debug-nodes`**
(`deploy_args.kind = "prod-debug-nodes"`). That kind gives you, per node service
`ant-node-1 … ant-node-N`:

- `RUST_BACKTRACE=full` + `COLORBT_SHOW_HIDDEN=1` → real panic backtraces to the journal and ES.
- Core dumps under `/mnt/ant-node-storage/coredumps/core.*`.
- DEBUG logs forwarded to a **dedicated** Elasticsearch (`telegraf-ant-nodes-<name-lowercased>-*`).
- Host + **per-service** metrics forwarded to a **dedicated** InfluxDB (bucket `testnet-metrics`):
  `cpu` / `mem` / `net` / `disk` / `diskio` (host) and **`procstat`** (per-service, tagged
  `node_service=ant-node-<i>`, fields `memory_rss` / `cpu_usage` / `num_fds` / …).

> **Verified on a live canary (STG-03, 2026-07-23): the node emits no metrics.** ant-node does not
> run a metrics/OpenMetrics server, so nothing listens on `127.0.0.1:9100-9109` — the `--metrics-port`
> flag on the service and the `telegraf_prometheus.conf` scrape of those ports are **inert leftover
> config**, not a failed endpoint. Consequently the InfluxDB **`prometheus` measurement does not
> exist** and there is **no** app-level routing-table / records-stored / peer-count gauge to read
> anywhere. Do **not** go looking for a metrics server or treat its absence as a fault. Per-service
> resource data comes from **`procstat`** (Telegraf reading `/proc`, i.e. the OS's view of each
> process — not the node); connectivity and participation come from **ES logs** and **`du` on the
> chunk store** (below). (Always `schema.measurements(bucket:"testnet-metrics")` first, in case this
> ever changes — but as of this train, node metrics are simply not emitted.)

The skill **auto-detects the kind** from the registry (Step 2) and adapts: if the canary was
instead deployed as `prod-nodes` **without** `--monitoring`/`--forward-logs`, there is no ES or
InfluxDB and every ES/Influx check below degrades to SSH-only — call that out in the report.

## There is no node-evaluation CLI — this is important

`deployments record-results` / `print-recorded-results` do **not** handle the `prod-debug-nodes`
or `prod-nodes` kinds — they fall through to an empty file-ops path and record nothing useful for
passive storage nodes. So, exactly like the `run-staging-testnet` and `bootstrap-upgrade-check`
skills, the canary's health is read from:

| Signal | Source |
|---|---|
| Crash / panic / OOM / **restart counter** | SSH: `systemctl show … -p NRestarts`, `journalctl`, coredumps; cross-check ES |
| Joined + stayed connected | ES: every `tag.service_number` still logging in a recent window + transport/DHT activity + no restarts (there is **no** routing-table gauge — the node emits no metrics) |
| Chunks stored growth | SSH `du` on the LMDB chunk store (`chunks.mdb`/`paid_list.mdb`) — two spaced reads |
| Per-service RSS / CPU (memory trend) | InfluxDB `procstat` (tag `node_service`, field `memory_rss` / `cpu_usage`) |
| CPU / RSS / disk / outbound (host) | `deployments record-analysis` → Postgres (the one **durable** read-back — survives teardown) |
| Novel ERROR/WARN, economics | ES `telegraf-ant-nodes-<name-lowercased>-*` (fields are **nested** — see Step 3) |

The issue's framing ("the agent collates, it does not measure") is aspirational: nothing records
per-node health today, so this skill measures it live. State that as a known gap (final section)
rather than pretending a record command exists.

## Step 1 — Read the release train issue

Find the current train (same as the staging skill):

1. `list_issues` with `project: "Releases"`, `team: "v2.0"`, `state: "In Progress"`.
2. Expect exactly one issue titled `Release Train — YYYY-MM-DD`. If none or more than one, stop
   and ask.
3. `get_issue` on it for the full description.

Extract:

| What | Where | Example |
|---|---|---|
| Train date | Issue title | `2026-07-21` |
| `ant-node` RC version | `## Cut record` → Versions table, `ant-node` row, **New** column | `0.15.0-rc.1` |
| Previous `ant-node` release | Same table, **Old** column | `0.14.4` |
| Risks to watch | `## Risks to watch in staging/canaries` | — |
| Payment/pricing change on the train? | Manifest / risks (e.g. this train's **#176 price floor**) | yes/no |

The RC version is what the canary should be running; the "Risks to watch" section names the
signatures to look hardest for; the payment-change flag decides whether the **economics**
observation applies (Step 6).

Then `list_issues` with `parentId` = the train issue and note **both**:

- **`Canary deployment`** — the deploy sub-issue. Read it: the deploy skill records the canary's
  registry **name** (and often T0 / the start timestamp) here. This is your primary source for the
  deployment name and window.
- **`Canary verification`** — the sub-issue you will close at the end.

Report the extracted values before continuing.

## Step 2 — Locate the canary and set the window

Find the canary row in the registry:

```bash
uv run saorsa-deploy deployments ls
```

Identify the canary: the `prod-debug-nodes` (or `prod-nodes`) row whose name matches what
`Canary deployment` recorded and whose `Node Version` is this train's RC. Note its **name**,
**id**, **kind**, and **status**.

- If two rows share the name (names get reused across trains), the **highest id** is this train's —
  the same reused-name trap that has silently lost a run's metrics before. Record the id.
- Confirm the binary is the RC. Do **not** trust a stale inventory/DB version field — verify live
  in Step 4 with `ant-node --version` on the VM.

**Determine liveness** — this decides live vs degraded evaluation:

```bash
uv run saorsa-deploy deployments generate-inventory --name <name>
```

- **Succeeds** → the canary state exists; the VM is (or was very recently) alive. Full evaluation.
- **Fails / no state** → the canary has been torn down (`destroy` deletes S3 state, and by default
  destroys the dedicated ES + InfluxDB too). Degraded mode: you can only report the
  `record-analysis` host metrics already in Postgres (`print-recorded-results --id <id>`) and
  whatever the `Canary deployment` sub-issue recorded. **The hard gates cannot be re-verified.**
  Say so plainly, mark the hard gates `UNVERIFIABLE (torn down before verification)`, and hand back
  to the RM — this is a process gap (verification must run before Ship), not a PASS.

**Set the window.** T0 = the canary start time (from `Canary deployment`, else the deployment's
`created_at`). The window is T0 → now (or T0 → teardown). Treat roughly the **first 1h as
warm-up**, excluded from the memory-slope and resource-band judgments (Step 6), same convention as
the staging steady-state window.

**Preconditions** (stop and name the one that failed):

- `DATABASE_URL` is set and reaches the registry (`deployments ls` worked).
- The node VM's provider credentials + `AWS_*` (S3 state) are present, and the SSH key that
  `generate-inventory` names exists locally.

## Step 3 — Gather inventory and connection details

From `generate-inventory --name <name>`:

- `node_vms[]` → the VM `ip`, `ssh_user` (`root`), `ssh_key`, and per-node `services` (a list
  `["ant-node-1", …, "ant-node-N"]`).
- `observability_vms[]` → `[{role:"influxdb", ip}, {role:"elasticsearch", ip}]` for
  `prod-debug-nodes`.

> **SSH key gotcha (verified on STG-03):** the inventory's `ssh_key` is often the generic default
> `~/.ssh/id_rsa`, which is **not** the key that works — the deploy key `~/.ssh/ant_deploy` is.
> Try the inventory `ssh_key` first; on `Permission denied (publickey)` fall back to
> `~/.ssh/ant_deploy`. Confirm with a one-liner before the full sweep:
> `ssh -i ~/.ssh/ant_deploy -o BatchMode=yes root@<ip> 'hostname'`.

Connection details for the canary's own backends:

```bash
uv run saorsa-deploy elastic info    --name <name>   # ES URL, username, password, API key
uv run saorsa-deploy monitoring info --name <name>   # InfluxDB URL, token, org, bucket
```

If either reports no state, that backend was not stood up (a `prod-nodes` canary without
`--forward-logs`/`--monitoring`) — record which checks you therefore cannot run.

**Elasticsearch** — index `telegraf-ant-nodes-<name-lowercased>-*` (note `elastic info` prints the
exact pattern). Fields are **nested and text-typed**, which is the single biggest footgun:

- Log level: **`tag.level`** (aggregate/filter on **`tag.level.keyword`** — the text field has
  fielddata disabled, so a bare `tag.level` agg errors and a bare `level` agg silently returns `[]`).
- Log target: `tag.target` / **`tag.target.keyword`**. Service id: **`tag.service_number.keyword`**
  (values `"1"`…`"N"`). Host: `tag.host` (UPPERCASE, e.g. `STG-03-ANT-NODE-UK1-1`).
- The log line itself is **`tail.message`** (text — use `match_phrase`).
- Timestamp: `@timestamp`. Add `"track_total_hits": true` when you need a real count (default caps
  the reported total at 10000).

```bash
ES=<es_url>; AK='ApiKey <api_key>'; IDX='telegraf-ant-nodes-<name-lc>-*'
# level histogram over the whole window
curl -s -H "Authorization: $AK" -H 'Content-Type: application/json' "$ES/$IDX/_search" -d \
 '{"size":0,"aggs":{"lvl":{"terms":{"field":"tag.level.keyword","size":10}}}}'
# panic/OOM scan (0 = clean)
curl -s -H "Authorization: $AK" -H 'Content-Type: application/json' "$ES/$IDX/_count" -d \
 '{"query":{"match_phrase":{"tail.message":"panicked"}}}'
```

**InfluxDB** — `POST <influx_url>/api/v2/query?org=<org>`, `Authorization: Token <token>`,
`Content-Type: application/vnd.flux`, `Accept: application/csv`. **First list what exists**
(`import "influxdata/influxdb/schema" schema.measurements(bucket:"testnet-metrics")`) — expect
`cpu disk diskio mem net procstat`; **`prometheus` will be absent** (the node emits no metrics). Trend
per-service RSS from `procstat` (`_field=="memory_rss"`, group by `node_service`).

## Step 4 — Hard gate 1: no crash, panic, OOM, or unexplained restart

The signal is the **restart counter over the window**, not "is it running now". A service that
crashed and was auto-restarted (`Restart=always`) is up again but has failed the gate.

Probe every service on the VM. Parallelise with a per-host `timeout` (as in
`bootstrap-upgrade-check`). Canonical per-node probe (loop `i` over `1..N`):

```bash
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes \
    "root@$IP" '
for i in $(seq 1 '"$N"'); do
  u=ant-node-$i
  printf "%-12s NRestarts=%s Active=%s Sub=%s Result=%s Since=%s\n" "$u" \
    "$(systemctl show $u -p NRestarts --value)" \
    "$(systemctl show $u -p ActiveState --value)" \
    "$(systemctl show $u -p SubState --value)" \
    "$(systemctl show $u -p Result --value)" \
    "$(systemctl show $u -p ExecMainStartTimestamp --value)"
done
echo "== binary ==";     /usr/local/bin/ant-node --version 2>&1 | head -1
echo "== coredumps ==";  ls -1 /mnt/ant-node-storage/coredumps/core.* 2>/dev/null | wc -l
echo "== panics ==";     journalctl -u "ant-node-*" --since "'"$T0_UTC"'" --no-pager 2>/dev/null \
                           | grep -icE "panicked at|SIGSEGV|SIGABRT|out of memory|oom-kill|core dumped"
'
```

Cross-check ES over the window for panic/OOM signatures that on-disk log rotation may have pruned
(the debug logs are pruned aggressively — default `--log-retention-days 1` — precisely because
they are durable in ES):

```
GET telegraf-ant-nodes-<name-lc>-*/_search
{ "size": 20, "query": { "bool": {
  "filter": [ { "range": { "@timestamp": { "gte": "<T0>" } } } ],
  "should": [
    { "match_phrase": { "tail.message": "panicked" } },
    { "match_phrase": { "tail.message": "backtrace" } },
    { "match_phrase": { "tail.message": "SIGABRT" } },
    { "match_phrase": { "tail.message": "oom-kill" } }
  ], "minimum_should_match": 1 } } }
```

**FAIL** if, for any service: `NRestarts > 0` without a benign explanation, `Result` is not
`success`, any `core.*` file exists, or any panic/OOM appears in the journal or ES over the window.
Report the offending service, the count, and the first backtrace/OOM line. `ActiveState=active`
alone is **not** a pass — a service can be up with `NRestarts=3`.

Also confirm the live binary is the RC: **`/usr/local/bin/ant-node --version`** (the binary lives
at `/usr/local/bin/ant-node`; do **not** try to parse it out of `systemctl show -p ExecStart`,
whose `--value` is the `{ path=… ; argv[]=… }` struct, not a bare path). A wrong version means the
canary did not test what the train ships — flag it (not itself a crash-gate failure, but it
invalidates the run).

## Step 5 — Hard gate 2: joined production and stayed connected

Every canary must have joined the production network and stayed connected for the whole window —
no ejections, no persistent connectivity flapping.

**There is no routing-table gauge to read** (the node emits no metrics; no `prometheus`
measurement exists). So connectivity is established from converging indirect evidence, all of which
was available on STG-03:

- **Every service is currently live on the wire** — aggregate ES over a recent window (e.g.
  `now-15m`) by `tag.service_number.keyword`; **all N** must appear with a healthy volume of
  transport/DHT lines. A service that has gone silent is the flag:

  ```bash
  curl -s -H "Authorization: $AK" -H 'Content-Type: application/json' "$ES/$IDX/_search" -d \
   '{"size":0,"query":{"range":{"@timestamp":{"gte":"now-15m"}}},
     "aggs":{"svc":{"terms":{"field":"tag.service_number.keyword","size":20}}}}'
  ```

- **No sustained ejection/flapping** — over the window look at the *rate* of peer-removal /
  disconnect / re-bootstrap lines (`match_phrase` on `tail.message`: `"removed"`, `"disconnected"`,
  `"re-bootstrap"`, `"connection closed"`). Occasional churn is normal on a live network; a service
  cycling connect→drop→connect continuously is not.

- **Corroborating signals**: zero restarts (Step 4) and the chunk stores growing (Step 6, the nodes
  can only accept stores if peers are routing to them) both mean the services stayed in the network.

**FAIL** if any service went silent mid-window, or shows sustained flapping. Report which service
and the evidence (the per-service line count, or the churn rate).

Because the direct routing-table metric is unavailable, state the connectivity verdict as
**PASS (inferred)** and name the gap — do not imply you read a routing-table size you could not.

> Reachability note: on this network every healthy node is always reachable via a Direct
> (non-NAT) or Relay (NAT'd) tagged address; a canary that cannot stay connected is a canary-side
> problem to report, not something to work around.

## Step 6 — Observations (recorded, not blocking)

Read each, record the reading, and flag anything anomalous. These are not yet gates — but an
anomaly here is an early warning worth calling out prominently.

- **Participation.** Chunks stored **grew** over the window. The chunk store is **LMDB**
  (`chunks.mdb`), *not* a `record_store/` directory (that path matches nothing and reads as a false
  zero). There is no `prometheus` records gauge, so `du` is the measure — take **two spaced reads**
  (≥15 min apart) and require growth; a single absolute number proves nothing because LMDB
  preallocates:

  ```bash
  ssh -i "$SSH_KEY" root@$IP 'for i in $(seq 1 '"$N"'); do
    printf "ant-node-%s chunks=%s paid=%s\n" $i \
      "$(du -cb /mnt/ant-node-storage/data/ant-node-$i/chunks.mdb 2>/dev/null | tail -1 | cut -f1)" \
      "$(du -cb /mnt/ant-node-storage/data/ant-node-$i/paid_list.mdb 2>/dev/null | tail -1 | cut -f1)"
  done'
  ```

  `paid_list.mdb` growing alongside `chunks.mdb` is the stronger evidence — it shows *paid* stores,
  not just inbound replication. (On STG-03 the stores were 14–33 GB/node and climbing.)
- **Resource use vs production peers.** From `record-analysis` (Step 7) / InfluxDB: median CPU,
  RSS, disk growth, and outbound bytes. Per-service RSS/CPU comes from the **`procstat`** measurement
  (tag `node_service`, field `memory_rss` / `cpu_usage`) — *not* `prometheus`. Compare against the
  **normal band for production nodes of comparable age**. There is no automated prod-peer baseline
  wired in — obtain the fleet's normal band from the operator (or the prod dashboards) and compare
  by hand; flag any figure running hot. (STG-03 showed a wide per-service RSS spread — most services
  0.4–0.9 GB, three at 2.1–2.7 GB — the kind of thing to note and ask about.)
- **Memory trend.** Per-service RSS slope should be **~flat after warm-up** (drop the first ~1h).
  Trend `procstat`/`memory_rss` grouped by `node_service` across the window and report the slope; a
  persistent upward climb is a leak signal at production scale (complements the staging leak gate).
- **Logs.** No **novel** ERROR/WARN signatures vs the current release, and known-error rates not
  elevated. Get the level histogram (`tag.level.keyword`) and the ERROR/WARN breakdown by
  `tag.target.keyword` (add `"track_total_hits": true`), then sample a few `tail.message` bodies per
  hot target. Compare to what the shipped release normally emits (operator/prior-canary reference —
  no stored signature set, so it is a judgment call). On STG-03 this surfaced ERROR 1.7k / 570M
  (dominated by `ant_node::replication` "Audit failure … reason=Timeout" — a *known* class, not
  novel) — report the reading and whether each hot signature is known or new.
- **Economics** — **only if the train carries payment/pricing changes** (Step 1 flag; this train's
  #176 price floor is the example). Are the canaries **rejecting admissions the rest of the fleet
  accepts**? Filter ES to the payment targets and read the rate/samples:

  ```bash
  curl -s -H "Authorization: $AK" -H 'Content-Type: application/json' "$ES/$IDX/_search" -d \
   '{"size":5,"query":{"bool":{"filter":[
      {"prefix":{"tag.target.keyword":"ant_node::payment"}},
      {"terms":{"tag.level.keyword":["ERROR","WARN"]}}]}},
     "sort":[{"@timestamp":"desc"}],"_source":["tag.target","tag.level","tail.message"]}'
  ```

  On STG-03 this showed `ant_node::payment::verify` "Slow payment verification failed … proof_type=merkle
  … elapsed_ms=7000-15000" WARNs — payment-path *latency*, worth flagging on a price-floor train even
  though it is not an outright admission rejection. If the train carries no payment change, record
  `N/A (no pricing change on this train)`.

## Step 7 — Record the durable host metrics

`record-analysis` is the one read-back that produces real node data **and survives teardown**, so
snapshot it (idempotent upsert — safe even if the deploy skill already ran it):

```bash
uv run saorsa-deploy deployments record-analysis --name <name> \
  --summary "Canary verification — Release Train <date>, ant-node <RC>. Window T0=<ts>..<now>. Hard gates: crashes <PASS/FAIL>, connectivity <PASS/FAIL>. <one-line observation note>."
```

`record-analysis` reads the canary's InfluxDB, so run it **before** any teardown. Give it a
generous timeout (≥10 min). Then verify the row is this run's:

```bash
uv run saorsa-deploy deployments ls                              # confirm the canary id
uv run saorsa-deploy deployments print-recorded-results --id <id>   # NB: --id, not --name
```

`print-recorded-results` takes `--id` (not `--name`). Check the id matches the one from Step 2 and
that the summary echoed back is the one you just wrote — a reused name has silently no-op'd this
before.

## Step 8 — The verification report (fixed format)

Emit exactly this structure. Every hard gate gets PASS / FAIL / UNVERIFIABLE with its reading;
every observation gets its reading; anomalies are called out.

```
# Canary verification — Release Train <date> (rc.N)

Canary:   <name> (registry id <id>, kind <kind>), <N> × ant-node <RC> on 1 VM
Window:   T0 <ts> → <ts>  (warm-up first ~1h excluded from trends)
Mode:     live | degraded (torn down before verification)

## Hard gates — failure blocks the ship, no override

- [ ] Crashes/panics/OOM/restarts: PASS | FAIL | UNVERIFIABLE
      <max NRestarts across services; coredump count; first panic/OOM line if any>
- [ ] Joined & stayed connected:   PASS | FAIL | UNVERIFIABLE
      <routing-table populated? stable? any ejection/flapping, with counts>

HARD-GATE VERDICT: PASS | FAIL

## Observations (recorded, not blocking)

- Participation:      <chunks-stored growth; requests served; routing stable>
- Resource vs peers:  <CPU / RSS p50 / disk growth / outbound, vs normal band>
- Memory trend:       <per-service RSS slope after warm-up — flat? rising?>
- Logs:               <novel ERROR/WARN signatures; known-error rates>
- Economics:          <quote/admission reject rate | N/A (no pricing change)>

## Anomalies

<bullet list of anything off-band or novel, with the evidence — or "none">

## Notes / gaps

<any check that could not run: backend absent, degraded mode, prod-peer baseline
supplied manually, binary-version mismatch, etc.>
```

The binary is `ant`/`ant-node`; the RC version comes from the train's Cut record. Fill every field
— an unfillable field is itself a finding (say why).

## Step 9 — Record on Linear and finalize

1. Post the report (Step 8) as a **comment on the `Canary verification` sub-issue** — **not** the
   parent train issue. The parent's `Verdict` section is written later by `Gate verdict` (V2-724),
   which reads this report off the sub-issue; do not post to the parent or touch its Verdict.
2. **If the hard-gate verdict is PASS** (both gates PASS): move the `Canary verification` sub-issue
   to **Done**.
3. **If any hard gate is FAIL or UNVERIFIABLE**: post the report, **leave `Canary verification`
   open**, and hand back to the RM naming the failure. A hard-gate failure blocks the ship; a
   canary torn down before verification is a process gap. Never auto-close either.

Ask the RM before closing if any observation is anomalous enough that you are unsure — the sub-issue
closing is cheap to defer and expensive to reverse.

## Known gaps

State these in the report rather than working around them:

- **No node-evaluation CLI.** The hard gates require SSH; the observations require direct ES/Influx
  queries. `record-results`/`print-recorded-results` do not handle the node kinds; `record-analysis`
  is the only durable read-back and it captures host metrics only (not the ant-node Prometheus
  peers/records fields).
- **"Collates, does not measure" is not yet true.** Nothing records per-node canary health, so this
  skill measures it live. When a deploy-side recording command exists (V2-723), prefer its recorded
  observations and reduce the live probing.
- **No prod-peer baseline is wired in** for "resource vs peers" — it is a manual comparison against
  the operator-supplied normal band.
- **No stored ERROR/WARN signature set** for "novel vs current release" — a judgment call against
  operator/prior-canary knowledge.
- **Torn-down canary = unverifiable hard gates.** `destroy` deletes S3 state and, by default, the
  dedicated ES + InfluxDB. Verification must run before Ship; run it while the canary is up.
- **ant-node emits no metrics** (verified STG-03): the node runs no metrics/OpenMetrics server, so
  `--metrics-port` and the `telegraf_prometheus.conf` scrape of `9100-9109` are inert leftover config
  (not a failed endpoint — don't go hunting for a metrics server). The InfluxDB **`prometheus`
  measurement therefore does not exist**: no app-level routing-table / records-stored gauge anywhere.
  HG2 is inferred (Step 5) and participation is `du`-based (Step 6). Per-service RSS/CPU come from
  `procstat` (the OS's view via `/proc`), not the node. Check `schema.measurements` in case this
  ever changes.
- **ES fields are nested + text-typed.** Aggregations/filters use `tag.level.keyword` /
  `tag.target.keyword` / `tag.service_number.keyword`; the log body is `tail.message`. A bare
  `level`/`target` field silently returns nothing.
- **Kind is auto-detected.** The expected vehicle is `prod-debug-nodes`; a `prod-nodes` canary
  without monitoring/forwarding leaves only SSH.
