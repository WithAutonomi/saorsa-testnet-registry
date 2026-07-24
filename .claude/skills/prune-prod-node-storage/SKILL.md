---
name: prune-prod-node-storage
description: Reclaim disk space on ONE production ant-prod-01 host by remove+re-add of every ant-node service, one at a time, waiting for each re-added node to finish bootstrapping before the next. Removing a service deletes its data dir (frees space); re-adding it re-bootstraps so it only pulls the records it is now responsible for. Use when asked to "prune node storage", "reclaim disk on a prod host", "free disk space on a prod node", "remove and re-add the node services on <host>", "manually prune records on <host>", or "shrink the node data on a production VM". Runs UNATTENDED and can take hours per node (potentially days for a full host).
---

# prune-prod-node-storage

Manually reclaims disk on a **single production host** (`ant-prod-01`) by cycling every
`ant-node` service through **remove → re-add → wait-for-bootstrap**, one service at a time.

**Why this exists.** Nodes are supposed to self-prune records they are no longer responsible for
as the network topology evolves, but that process is currently broken, so data accumulates.
`prod services rm` deletes a node's data directory (freeing all the space it used); `prod services
add` recreates the service with an empty data dir, so on restart it re-bootstraps and pulls in
**only** the records it is now responsible for — the net effect is a smaller on-disk footprint.

**This run is UNATTENDED.** Do **not** ask the user to confirm removals or pause between services —
once started, work through every service end-to-end and print the summary at the end. The only
interactive step is choosing the host at the very start.

**This run is LONG.** A single node can take **several hours** to finish bootstrapping; a full
15-service host can take **days**. That is expected. Never impose a short timeout on the wait — the
poll loops until the node reports done (see the helper's unbounded `--wait` mode). Only individual
HTTP/SSH requests have modest timeouts; transient errors are retried, never fatal.

## Safety invariants (do not violate)

- **Exactly one service down at a time.** Never `rm` a second service before the previous one has
  been `add`ed **and** finished bootstrapping. `prod services add` clones the lowest-index *active*
  sibling's unit, so there must always be ≥1 active sibling — the one-at-a-time rule guarantees it,
  and it keeps the host's contribution to the network maximal throughout.
- **Removal is permanent and intentional.** `rm` deletes `/mnt/ant-node-storage/data/<svc>` and its
  log dir. That is the whole point (the data is re-pulled on re-add). Do not try to back it up.
- **Read-only until the loop.** Baseline measurement (Step 2) only reads. The first mutation is the
  first `rm` in Step 3.
- **Resumable.** Persist progress to the state file (below) after every service so a killed/resumed
  session continues rather than restarting the whole host.

## Prerequisites

- **Env vars for the ES poll helper** (the helper reads these; it has no baked credentials):
  - `PROD_ES_URL` = `http://162.19.238.53:9200`
  - `PROD_ES_API_KEY` = the ant-prod-01 ES ApiKey value.
  If either is unset the helper exits 1 — stop and ask the user to export both.
- **SSH** to the node VMs: key `~/.ssh/ant_deploy`, user `root`. (The inventory's `ssh_key` is
  usually already `~/.ssh/ant_deploy`; if an SSH auth fails, fall back to `~/.ssh/ant_deploy`.)
- **saorsa-deploy** runnable via `uv run saorsa-deploy` from the repo root. `prod services` is pure
  SSH — it needs **no** cloud credentials, no `DATABASE_URL`, no S3 state.
- **Production inventory** at `/home/chris/.autonomi/testnets/ant-prod-01/inventory.json`.

## Reference facts (baked in — do not rediscover)

- **Production Elasticsearch** (static, always on):
  - URL `http://162.19.238.53:9200`, user `ant-log-writer`, ApiKey auth (`Authorization: ApiKey …`).
  - Node index pattern: `telegraf-ant-nodes-ant-prod-01-*`.
  - Fields are **nested + text-typed**: aggregate/filter on `tag.host.keyword`,
    `tag.service_number.keyword`, `tag.level`; the log body is `tail.message` (use `match_phrase`);
    timestamp is `@timestamp` (UTC ISO). **`tag.host.keyword` = the inventory `name` UPPERCASED**
    (e.g. `ant-prod-01-hz-fsn1-1` → `ANT-PROD-01-HZ-FSN1-1`). Service id `tag.service_number.keyword`
    is `"1"`…`"15"`.
- **Bootstrap-complete marker** (what "finished bootstrapping" means): the ant-node INFO log line
  **`Replication bootstrap complete`** (static literal, `replication/mod.rs:complete_bootstrap()` —
  the single point that flips `is_bootstrapping=false` after every bootstrap fetch/verify drained,
  i.e. it has pulled in the records it is responsible for). INFO is above the node→ES drop filter,
  so it reaches prod ES. It is rare per service (fires ~once at (re)bootstrap), so the first hit
  after the re-add is the real completion.
- **`prod services` commands** (all: `--inventory-path <p> --name <host>`; `rm`/`add` also
  `--service-name ant-node-N`; optional `--ssh-key-path`):
  - `ls` → Rich table Service/Active/Enabled/Disk usage. Per-service size is remote
    `du -sb /mnt/ant-node-storage/data/<svc>`, shown **human-formatted (GiB) — lossy**. Globs the
    host's actual unit files (not the inventory).
  - `rm` → stop, disable, delete unit, delete data dir **and** log dir (frees the space), updates
    inventory.
  - `add` → clone lowest-index active sibling's unit (new port), fresh empty data/log dirs, enable +
    **start**. Uses the on-disk binary `/usr/local/bin/ant-node` (no download/version flag), does not
    re-fetch bootstrap peers. Empty data dir ⇒ node re-bootstraps on start.
  - Because `ls` sizes are lossy, take **exact byte** measurements with your own `du -sb` over SSH
    for the accounting; use `prod services ls` for the human-readable table the user expects to see.

## The poll helper

`scripts/poll_bootstrap.py` (stdlib only) runs the exact scoped ES query and, in `--wait` mode,
loops every 5 minutes until the node reports bootstrap-complete. Reads `PROD_ES_URL` /
`PROD_ES_API_KEY` from the env.

```
python3 .claude/skills/prune-prod-node-storage/scripts/poll_bootstrap.py \
    --host <inventory-name> --service ant-node-<N> --since <ISO8601-UTC> --wait
```
- Single-shot (no `--wait`): exit 0 = complete (prints first matching `@timestamp`), 2 = pending,
  1 = error.
- `--wait`: loops (default forever; `--interval` default 300s) until complete (exit 0), retrying
  transient ES errors. **Launch it as a background process** (foreground `sleep` is blocked, and the
  wait can span days) — you are re-invoked when it exits, then read its exit code.

## State file (resume)

`~/.autonomi/testnets/ant-prod-01/prune-runs/<host>-<YYYY-MM-DD>.json`:
```json
{
  "host": "ant-prod-01-hz-fsn1-1",
  "started_at": "2026-07-24T15:40:00Z",
  "free_avail_before_bytes": 12345678,
  "services_total": ["ant-node-1", "...", "ant-node-15"],
  "results": [
    {"service": "ant-node-1", "before_bytes": 33000000000, "after_bytes": 9000000000,
     "removed_at": "…", "add_at": "…", "bootstrap_complete_at": "…", "reclaimed_bytes": 24000000000}
  ]
}
```
On start, if a state file for this host exists, resume from the first service not yet in `results`
(do not redo completed services).

## Procedure

### Step 1 — Choose and validate the host
Ask the user for the host name (unless already given). Load
`/home/chris/.autonomi/testnets/ant-prod-01/inventory.json` and require the name to match a
`node_vms[].name`. If it doesn't, list the valid names and stop. Note the VM `ip`, `ssh_user`,
`ssh_key`, and its `services` list. Report the host + service count back.

### Step 2 — Baseline (read-only)
1. Free disk on the storage device (record `free_avail_before_bytes`):
   ```
   ssh -i ~/.ssh/ant_deploy -o StrictHostKeyChecking=accept-new root@<ip> \
     'df -B1 /mnt/ant-node-storage | tail -1'
   ```
   (Capture avail bytes; also note size/used for the report.)
2. Exact per-service sizes (authoritative bytes):
   ```
   ssh -i ~/.ssh/ant_deploy root@<ip> \
     'du -sb /mnt/ant-node-storage/data/ant-node-* 2>/dev/null'
   ```
3. Show the human-readable table the user asked for:
   ```
   uv run saorsa-deploy prod services ls \
     --inventory-path /home/chris/.autonomi/testnets/ant-prod-01/inventory.json --name <host>
   ```
Write the baseline into the state file. Determine the service list to process (every `ant-node-*`
`ls` reports present on the host, in numeric order).

### Step 3 — Per-service loop (UNATTENDED — do not pause)
For each `ant-node-N` in order, skipping any already recorded complete in the state file:

1. **Remove** (record `removed_at`):
   ```
   uv run saorsa-deploy prod services rm \
     --inventory-path /home/chris/.autonomi/testnets/ant-prod-01/inventory.json \
     --name <host> --service-name ant-node-N
   ```
2. **Capture the poll lower bound**, then **add** (record `add_at`):
   ```
   SINCE=$(date -u -d '2 minutes ago' +%Y-%m-%dT%H:%M:%SZ)   # 2-min back-margin for clock skew;
                                                             # safe because the service is absent then
   uv run saorsa-deploy prod services add \
     --inventory-path /home/chris/.autonomi/testnets/ant-prod-01/inventory.json \
     --name <host> --service-name ant-node-N
   ```
3. **Wait for bootstrap-complete** — launch the poll helper as a **background** process and wait for
   it to exit (you are notified on completion):
   ```
   python3 .claude/skills/prune-prod-node-storage/scripts/poll_bootstrap.py \
     --host <host> --service ant-node-N --since "$SINCE" --wait
   ```
   Exit 0 = complete (parse the JSON `timestamp` → `bootstrap_complete_at`). Exit 3 only if you set a
   `--max-wait-hours` (default is wait forever — leave it unbounded per the unattended intent). If it
   somehow exits non-zero without a deadline, re-check env/host/service and relaunch — do not skip
   ahead (that would break the one-service-at-a-time invariant).
4. **Measure the new size** and print the per-service delta:
   ```
   ssh -i ~/.ssh/ant_deploy root@<ip> 'du -sb /mnt/ant-node-storage/data/ant-node-N 2>/dev/null'
   ```
   `reclaimed_bytes = before_bytes − after_bytes`. Print a one-line result:
   `ant-node-N: 33.0 GiB → 9.1 GiB  (reclaimed 23.9 GiB)  bootstrapped in Hh Mm`.
5. **Persist** the result to the state file before moving to the next service.

### Step 4 — Final measurement and summary
1. `uv run saorsa-deploy prod services ls … --name <host>` (new per-service table).
2. Free disk after: `ssh … 'df -B1 /mnt/ant-node-storage | tail -1'` → `free_avail_after_bytes`.
3. Print the summary report (below). Mark the state file complete.

## Summary report (fixed format)

```
# Storage prune — <host> (ant-prod-01)

VM:        <ip>  (<provider> <region>)
Window:    <started_at> → <finished_at>   (<N> services, one at a time)
Device:    /mnt/ant-node-storage

Free space:   <free_before> → <free_after>   (reclaimed <delta>)

Per-service:
  service       before      after      reclaimed   bootstrap time
  ant-node-1    33.0 GiB    9.1 GiB     23.9 GiB    2h14m
  ...
  ----------------------------------------------------------------
  total                                 <sum> GiB

Notes:
- <any service that errored / was skipped / took unusually long, or "none">
```

Report **both** the authoritative free-disk delta (`df` after − before) and the summed per-service
reclaimed bytes; they should roughly agree. If they diverge materially, say so (re-added nodes keep
growing after their snapshot, and non-node writers touch the disk — call it out rather than hiding it).

## Gotchas

- **`ls` disk figures are lossy** (human-formatted GiB). Always use your own `du -sb` for the byte
  accounting; use `ls` only for the table the user sees.
- **`tag.host.keyword` is the inventory name UPPERCASED** — the helper uppercases for you, but if you
  hand-write an ES query, remember it.
- **The marker can appear historically.** Always scope the poll by `@timestamp >= add-time` (the
  helper's `--since`) so you detect *this* re-add's completion, not an old one. The 2-min back-margin
  is safe only because the service does not exist during that window (you just removed it).
- **Never run two removals concurrently** — you would strand the host with a down node and possibly
  leave no active sibling for `add` to clone.
- **Don't misread a transient ES/SSH error as done.** The helper retries transient errors in `--wait`
  mode; a non-zero exit without a deadline means a real problem — diagnose, don't skip.
- **`prod services` needs no S3/DB/cloud creds** — if one errors, it is SSH/inventory, not state.
