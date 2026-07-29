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
  the RM with the sub-issue left In Progress. Only an all-hard-gates-PASS run moves
  `Canary verification` to Done.
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

## RC artifacts are DEBUG builds — this changes how everything below is read

**Every `ant-node` `-rc.N` release artifact is built without `--release`, deliberately.**
`.github/workflows/release.yml` branches on `is_prerelease`: prerelease ⇒ `build_flags=""`,
`profile_dir=debug`, *"Building DEBUG (RC pre-release: logging enabled)"*. Verify on the VM —
a debug binary is ~460 MB and `file` reports *"with debug_info, not stripped"*; the optimised
production binary is ~19 MB and stripped:

```bash
stat -c %s /usr/local/bin/ant-node; file /usr/local/bin/ant-node
```

Three consequences, all load-bearing:

1. **`overflow-checks` are ON.** Arithmetic under/overflow **panics** on the canary where the
   shipped release **wraps silently**. So an arithmetic panic seen only on an RC is **not
   automatically a regression**, and a clean production journal is **not evidence of absence** —
   release builds are structurally incapable of reporting it. Establish provenance by diffing the
   source (Step 4), never by the RC-vs-prod presence/absence alone.
2. **`panic = "abort"` does NOT apply.** ant-node's workspace `[profile.release]` sets
   `panic = "abort"`, but a debug build uses `profile.dev` and **unwinds** — so tokio catches task
   panics and the process survives with `NRestarts=0`. "Panicked but did not crash" on an RC does
   not predict release behaviour; on the shipped profile the same panic aborts the process.
3. **Resource and throughput numbers are not comparable to production.** CPU, RSS, log volume,
   disk and network all reflect an unoptimised binary. Record them as NOT ASSESSED (Step 6).

Note the asymmetry when reasoning about *other* runs: a staging comparison of **RC vs RC** is
apples-to-apples and unaffected by any of this. Only a comparison that crosses the profile boundary
(RC vs shipped release, or canary vs production fleet) is confounded — and it is confounded
*against* the RC, so improvements measured that way are real and if anything understated.

## Cross-check against production

For any panic signature or anomaly, the production fleet is the natural control. The inventory is
`~/.autonomi/testnets/ant-prod-01/inventory.json` (`node_vms[].ip`, key `~/.ssh/ant_deploy`) —
**read-only, never mutate a production host from this skill.** Sample across all providers
(OVH / Hetzner / Contabo); ~15–20 hosts is plenty. Run them in parallel with `timeout`:

```bash
timeout 150 ssh -i ~/.ssh/ant_deploy -o StrictHostKeyChecking=no -o ConnectTimeout=8 \
  -o BatchMode=yes root@$ip '
  echo "ver=$(/usr/local/bin/ant-node --version 2>/dev/null | head -1 | awk "{print \$2}")"
  echo "journal_from=$(journalctl --no-pager -o short-iso 2>/dev/null | head -1 | cut -c1-10)"
  journalctl --no-pager 2>/dev/null | grep -aA2 "The application panicked" \
    | grep -aE "(Message:|Location:)" | sed "s/^.*\(Message:\|Location:\)/\1/" \
    | paste - - | sort | uniq -c | sort -rn'
```

Always report `journal_from` alongside the counts — a zero over a two-day journal means much less
than a zero over two months. Read the version **live**; the inventory's `binary.ant_node_version`
is stale (prod auto-upgrades).

Interpretation, in order:
- Signature present on prod ⇒ pre-existing, not an RC regression.
- Signature absent on prod **and** it is a `debug_assertions`-gated class (arithmetic overflow,
  bounds) ⇒ **inconclusive** — prod cannot report it. Say so; do not call it RC-specific.
- Signature absent on prod, class *can* fire in a release build, and the source differs between the
  shipped tag and the RC hash ⇒ genuine regression.

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
- **`Canary verification`** — the sub-issue you will close at the end. Move it to **In Progress**
  now. The verification is a long read-only sweep (SSH probes across every service, ES and
  InfluxDB queries, `record-analysis`), and leaving it in Todo makes the train look as though
  nobody has picked the verification up while it is actively running.

Leave `Canary deployment` in whatever state it is in — it belongs to `run-canary-deployment`. If
it is still In Progress rather than Done, that means the deploy skill hit a failed smoke check and
handed back; read its comment before going further, because you may be evaluating a canary that
was already known to be degraded.

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

> **Read this before writing any grep.** A panic here typically leaves **no systemd trace at
> all**, and **none** in Elasticsearch. On the 2026-07-29 canary the prescribed checks returned a
> clean 0 while 49 real panics sat in the journal. The panic scan below is the *primary* signal,
> not a cross-check, and it must run **unconditionally** — never gated on a unit first looking
> unhealthy.
>
> Three traps, all hit on that run:
> - **`grep "panicked at"` returns 0.** ant-node's **color-eyre** hook prints
>   `The application panicked (crashed).` then `Message:  <msg>` then `Location: <file>:<line>`,
>   *not* Rust's stock `thread '…' panicked at …`. Match **`The application panicked`**.
> - **A task-level panic leaves every systemd signal green.** A panic inside a spawned tokio task
>   unwinds only that task; the process survives, so `ActiveState=active`, `Result=success`,
>   `NRestarts=0`, no coredump, and `--state=failed` lists nothing. (RC builds unwind — see
>   "RC artifacts are debug builds". A *release* build with `panic = "abort"` would die instead.)
> - **`grep -i oom` false-positives on `tokio::loom`** frames — `loom` contains `oom`.

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
echo "== build ==";      stat -c %s /usr/local/bin/ant-node; file /usr/local/bin/ant-node
echo "== coredumps ==";  ls -1 /mnt/ant-node-storage/coredumps/core.* 2>/dev/null | wc -l
echo "== PANICS (authoritative) =="
journalctl --since "'"$T0_UTC"'" --no-pager 2>/dev/null | grep -ac "The application panicked"
echo "== panic messages + locations =="
journalctl --since "'"$T0_UTC"'" --no-pager 2>/dev/null \
  | grep -aA2 "The application panicked" \
  | grep -aE "(Message:|Location:)" | sed "s/^.*\(Message:\|Location:\)/\1/" \
  | paste - - | sort | uniq -c | sort -rn
echo "== real OOM kills (kernel only) =="
journalctl -k --since "'"$T0_UTC"'" --no-pager 2>/dev/null \
  | grep -acE "invoked oom-killer|Out of memory: Killed process"
'
```

The `uniq -c` on message+location is the highest-value line in this skill: it collapses hundreds of
backtrace lines into one row per distinct fault with a count, and immediately shows whether a
signature is one known class or several.

For any distinct signature found, pull the frames once to get the call path:

```bash
journalctl -u ant-node-<i> --since "<first-ts>" --until "<first-ts +40s>" --no-pager | head -40
```

**Do not rely on Elasticsearch for this gate.** Panics go to **stderr → journald**; telegraf tails
the structured JSON log, which never contains them. An ES panic query returns 0 through a real
panic storm. (ES remains right for log *content* — audit failures, WARN/ERROR rates, per-target
counts.)

**FAIL** if, for any service: `NRestarts > 0` without a benign explanation, `Result` is not
`success`, any `core.*` file exists, or a panic appears in the journal **that the shipping build
could also produce**. Report the offending services, the per-service counts, the distinct
message+location signatures, and the call path. `ActiveState=active` alone is **not** a pass — a
service can be up with `NRestarts=3`, and with a task-level panic it can be up having panicked 49
times.

> **A debug-only panic on unchanged code is NOT a gate failure.** This gate asks whether the
> *release being shipped* will crash in production. A panic that (a) can only fire under
> `debug_assertions`/`overflow-checks` — arithmetic overflow, bounds checks — and (b) sits in code
> byte-identical to the shipped tag, is a property of **how the canary was built**, not of the
> artifact being released. The shipped binary compiles that check out and cannot produce it, so it
> introduces no crash risk. Record it as a **finding** (its own section in the report, with a bug
> to file), mark the gate **PASS**, and say explicitly why.
>
> Marking such a panic FAIL is self-contradictory: you cannot argue in one paragraph that
> production is clean *because* release builds compile the check out, and in the next that the
> release fails a crash gate on it. The 2026-07-29 canary was initially recorded FAIL on exactly
> this mistake and had to be corrected.
>
> This is a narrow exemption, not a general one. It does **not** apply when the panic class can
> fire in a release build (explicit `panic!`, `unwrap`, `expect`, `assert!`), nor when the source
> differs from the shipped tag, nor to anything that actually killed or restarted a process. When
> in doubt, FAIL and hand to the RM.
>
> Whether the *underlying* fault matters in production is a separate question, and usually yes —
> an overflow that panics in debug **wraps silently** in release, which can be worse than a crash.
> Say so in the finding, name the plausible production consequence, and mark it unverified unless
> you have evidence. A gate PASS here means "this release does not crash", never "there is no bug".

**Establish provenance before calling anything a regression** — a panic here is not automatically
the RC's fault. Do all three:

1. **Diff the source.** The `Location:` line names the crate, the git checkout hash and the file.
   Compare that hash against the shipped tag:
   `git log --oneline <shipped-tag>..<rc-hash> -- <file>` and
   `git diff <shipped-tag> <rc-hash> -- <file>`. Identical code ⇒ **not a regression**, and say so
   plainly. (On 2026-07-29 the only commit between them was the release chore, touching just
   `Cargo.toml`/`Cargo.lock`.)
2. **Scan production for the same signature** — see "Cross-check against production" below.
3. **Check whether the signature is even *possible* in a release build.** Arithmetic overflow,
   array-bounds and other `debug_assertions`-gated panics **cannot fire** in an optimised build;
   they wrap or elide silently. For those, a clean production journal proves nothing.

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
- **Resource use vs production peers — record as `NOT ASSESSED`.** The canary is a **debug** build
  and the production fleet runs an **optimised** one (see "RC artifacts are debug builds"), so CPU,
  RSS, disk growth, outbound bytes and log volume are **not comparable** across the two. Report the
  raw figures from `record-analysis` (Step 7) / InfluxDB for the record, explicitly labelled as
  debug-build numbers, and mark the comparison NOT ASSESSED. Do **not** ask the operator for a
  production "normal band" and compare against it — that comparison cannot be made valid by better
  baseline data, because the confound is the build profile.
  What *is* still valid, and worth reporting: comparisons **within the canary** (service vs service,
  same binary) and **trends over the window** (below).
- **Memory trend — use `RssAnon`, not `memory_rss`.** `procstat`/`memory_rss` counts **mapped file
  pages**, and ant-node mmaps its LMDB chunk store, so `memory_rss` tracks store size and reads as a
  massive leak when there is none. On the 2026-07-29 canary two services showed 4.09/4.47 GB against
  0.22–1.05 GB elsewhere — a ~20× apparent leak that was entirely LMDB page cache.
  Telegraf does **not** collect `RssAnon`, so take it over SSH:

  ```bash
  ssh -i "$SSH_KEY" root@$IP 'for i in $(seq 1 '"$N"'); do
    p=$(systemctl show ant-node-$i -p MainPID --value)
    printf "ant-node-%-3s RSS=%s RssAnon=%s RssFile=%s\n" $i \
      "$(awk "/VmRSS/{print \$2}"   /proc/$p/status)" \
      "$(awk "/RssAnon/{print \$2}" /proc/$p/status)" \
      "$(awk "/RssFile/{print \$2}" /proc/$p/status)"
  done'
  ```

  `RssAnon` is the heap — that is the leak signal. A large `RssFile` is expected and benign.
  Then trend `procstat`/`memory_rss` grouped by `node_service` across the window for the **slope**
  (drop the first ~1h): a slope is a within-canary trend and stays meaningful, but read any absolute
  RSS number against `RssAnon` before calling it a leak.
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

## Step 8 — The verification report

Match the **house style of the staging verification** (see the first comment on any
`Staging verification — rc.N` sub-issue, e.g. V2-778): a headline result, a summary table, a
**prose** "What happened", then tables for the gate detail.

**"What happened" is the part that matters and it must be prose, not bullets.** Write it for
someone who was not on the run: say what the readings *mean*, not just what they were. Lead
paragraphs with a bold sentence carrying the point. Translate figures into consequences ("grew
their chunk stores from 26 GB to 217 GB … meaning they were being *paid* to store data, not merely
receiving replicated copies"). State plainly what the run could **not** tell us. Never present a
number whose comparison you know to be invalid without saying so in the same breath.

```
# Canary verification — rc.N

**Result: PASS | FAIL** — <one clause naming the gate if FAIL, plus the key qualifier>

| | |
|---|---|
| Canary | <name> **#<id>** — ant-node <RC>, <N> services on 1 VM |
| Vehicle | `<kind>` — <provider> <region> <size>, <volume>GB volume |
| Network | **Production** Autonomi mainnet |
| Window | T0 <ts> → <ts> (<N>h; first ~1h warm-up excluded from trends) |
| Build profile | **DEBUG** — every `-rc.N` artifact is built without `--release` by design |
| Backends | InfluxDB <ip> · Elasticsearch `telegraf-ant-nodes-<name-lc>-*` |
| Hard gates | **<n> pass, <n> fail** |
| Mode | live | degraded (torn down before verification) |

## What happened

<Several prose paragraphs. Did it stay up and participate? What failed and what
exactly is the fault? Is it a regression — and how do you know? What could not be
measured, and why? Bold the lead sentence of each paragraph.>

## Verdict

**Hard gates: <n> PASS, <n> FAIL.** <one line>

<If FAIL: the gate verdict is not this skill's to issue — record the FAIL as
measured, then list the material inputs for whoever does decide: is the code
changed vs production, what is the blast radius per event, how does it manifest
on the shipping build profile, and what the correct remedial output is.>

## Hard gates

| Gate | Verdict | Detail |
|---|---|---|
| Crashes / panics / OOM / restarts | **PASS/FAIL** | <counts per service; distinct message+location signatures; call path; NRestarts/Result/coredumps/OOM-kills> |
| Joined & stayed connected | **PASS** (inferred) / FAIL | <per-service inbound accepts and DHT connects; why inferred> |

## Observations

| Observation | Reading | Detail |
|---|---|---|
| Participation | | chunks.mdb + paid_list.mdb growth |
| Memory trend | | RssAnon per service — the leak signal |
| Resource vs peers | **NOT ASSESSED** | debug build; raw figures for the record only |
| Logs | | level histogram; novel vs known signatures; key rates |
| Economics | | payment-target WARN/ERROR, or N/A with the reason |

## Production cross-check

<Only when a panic/anomaly was found. Table: signature | production (version,
build profile) | canary. Always state how far back the journals reach, and
whether a production zero is meaningful for that signature class.>

## Notes and gaps

<Bullets: build profile, detection gaps, wrong-metric traps, checks that could
not run, unresolved questions, and the fact the canary is left running.>
```

Fill every field — an unfillable field is itself a finding, so say why. Mark `UNVERIFIABLE` (not
FAIL) for a gate that could not be checked because the canary was torn down early.

## Step 9 — Record on Linear and finalize

1. Post the report (Step 8) as **exactly one comment** on the `Canary verification` sub-issue —
   **not** the parent train issue. The parent's `Verdict` section is written later by
   `Gate verdict` (V2-724), which reads this report off the sub-issue; do not post to the parent or
   touch its Verdict.

   **One comment, always.** If later investigation changes the findings, **edit that comment**
   (`save_comment` with its `id`) so the sub-issue always holds a single current report. Do not
   append addenda or corrections as extra comments — a reader should never have to reconcile two
   versions to learn the verdict. If a correction materially reverses an earlier conclusion, note
   that inside the report where it belongs (e.g. "not a regression — the code is unchanged"), not
   as a separate comment.
2. **If the hard-gate verdict is PASS** (both gates PASS): move the `Canary verification` sub-issue
   from In Progress to **Done**.
3. **If any hard gate is FAIL or UNVERIFIABLE**: post the report, **leave `Canary verification` in
   In Progress** (not Done, and not back to Todo — the verification ran and produced a report;
   Todo would misreport it as never started), and hand back to the RM naming the failure. A
   hard-gate failure blocks the ship; a canary torn down before verification is a process gap.
   Never auto-close either.

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
- **"Resource vs peers" is not assessable at all**, and no baseline can fix it: the canary is a
  debug build, the production fleet is optimised. Record NOT ASSESSED with the raw debug-build
  figures for the record. See "RC artifacts are debug builds".
- **No stored ERROR/WARN signature set** for "novel vs current release" — a judgment call against
  operator/prior-canary knowledge.
- **Panics are invisible to every check except a journald text scan** — not in Elasticsearch, and
  a task-level panic leaves `NRestarts=0` / `Result=success` / nothing in `--state=failed`. The
  stock `grep "panicked at"` also misses ant-node's color-eyre format. See Step 4.
- **`memory_rss` cannot answer the leak question** on an LMDB-backed node — it counts mapped file
  pages. `RssAnon` (per-process `/proc/<pid>/status`, SSH only — Telegraf does not collect it) is
  the heap figure. See Step 6.
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
