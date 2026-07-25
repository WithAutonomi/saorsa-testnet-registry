---
name: survey-prod-disk
description: Regularly survey disk pressure across the entire production ant-prod-01 node fleet, keep historical records, and drive service removals to reclaim space. Runs `prod services disk-survey` to rank every node service by disk usage (smallest first = deletion candidates) and store the survey, cross-checks production Elasticsearch for services that can no longer write records (disk-full), presents removal candidates, removes the ones you approve via `prod services rm` (audited), and reports today-vs-yesterday from the stored surveys + audit trail. Use when asked to "survey prod disk", "check fleet disk pressure", "do a disk survey", "find nodes to remove", "reclaim disk on the prod fleet", or "which prod nodes are out of space".
---

# survey-prod-disk

Periodic disk-pressure surveying of the **production** `ant-prod-01` node fleet, plus the
remove-to-reclaim workflow. A thin orchestration over three `saorsa-deploy` commands:
`prod services disk-survey` (collect + rank + store), `prod services rm` (remove, audited), and
`prod services survey-report` (today-vs-yesterday). The interactive value the skill adds on top is
the **Elasticsearch write-blocked cross-check** and the **candidate selection**.

## Why

Prod node VMs are chronically tight on disk (nodes' true responsible set ≈ disk size, and
self-pruning only evicts out-of-range records after a **3-day hysteresis**). When a disk fills,
ant-node hits its **~0.49 GiB store reserve** and starts **dropping records it can't write** — the
node keeps running but stops accepting stores. This skill finds those hosts, ranks removal
candidates (smallest services), and reclaims space by removing services (their responsibility
redistributes to the fleet). See the under-provisioning finding in project memory.

## Prerequisites

- **`DATABASE_URL`** — set, reaching the registry DB. Needed to store surveys and read history
  (`disk-survey` still prints without it, but nothing is recorded and `survey-report` can't run).
- **`PROD_ES_URL`** = `http://162.19.238.53:9200` and **`PROD_ES_API_KEY`** — the always-on prod
  Elasticsearch, for the write-blocked cross-check.
- **SSH** to node VMs: key `~/.ssh/ant_deploy`, user `root` (the survey SSHes every host).
- **Inventory** at `/home/chris/.autonomi/testnets/ant-prod-01/inventory.json`.

## Reference facts (baked in)

- **Prod ES** index `telegraf-ant-nodes-ant-prod-01-*`, ApiKey auth. Fields are nested + text:
  `tag.host.keyword` (inventory name UPPERCASED), `tag.service_number.keyword` (`"1"`…), log body
  `tail.message` (`match_phrase`), `@timestamp`.
- **Write-blocked signal** (a node that can't store): `tail.message` match_phrase
  **`Insufficient disk space`** — full line: `"Insufficient disk space: X GiB available, 0.49 GiB
  reserve required. Free disk space or increase the partition to continue storing chunks."`,
  target `ant_node::replication`. A host with any such line in the last ~1h is actively losing
  writes and is the top priority to relieve.
- **`rm` is permanent and immediate** — it stops/disables the service and deletes its data + log
  dir. No re-bootstrap needed (the network re-replicates its records elsewhere). Removing the
  *smallest* services costs the fleet the least while freeing the most-needed headroom.
  (To reclaim *within* a node instead of removing it — dropping only its stale out-of-range
  records while keeping its peer ID — use `prod services reset-data`; that's a different workflow.)

## Procedure

### Step 1 — Survey the fleet
```
uv run saorsa-deploy prod services disk-survey \
  --inventory-path /home/chris/.autonomi/testnets/ant-prod-01/inventory.json
```
This SSHes every host in parallel, prints all node services **ascending by data-dir size**
(smallest = deletion candidates) plus a per-host free-space table (most pressured first), and
**stores the survey** in the registry (note the printed survey id). Read back the ascending list
and the per-host free space.

### Step 2 — Elasticsearch write-blocked cross-check
For each host near-full in the survey (say < a few GiB free), check prod ES for the write-blocked
signal over a recent window, scoped by host + service:
```bash
ES=$PROD_ES_URL; AK="ApiKey $PROD_ES_API_KEY"; IDX='telegraf-ant-nodes-ant-prod-01-*'
# count of "Insufficient disk space" per host over the last hour (host tag is UPPERCASE)
curl -s -H "Authorization: $AK" -H 'Content-Type: application/json' "$ES/$IDX/_search" -d '{
  "size":0,"query":{"bool":{"filter":[
    {"match_phrase":{"tail.message":"Insufficient disk space"}},
    {"range":{"@timestamp":{"gte":"now-1h"}}}]}},
  "aggs":{"h":{"terms":{"field":"tag.host.keyword","size":100},
    "aggs":{"svc":{"terms":{"field":"tag.service_number.keyword","size":20}}}}}}'
```
Any host/service appearing here is **actively dropping records** — flag it as top priority.

### Step 3 — Present removal candidates
Present the user a short list of candidates, ordered by priority:
1. Services on **write-blocked** hosts (from Step 2) — relieving these stops active data loss.
2. Otherwise the **smallest** services on the **most-pressured** hosts (from Step 1).
Show, per candidate: host, service, size, host free space, and whether the host is write-blocked.
Recommend how many to remove per host to get each host a healthy free margin, but **let the user
choose** — do not remove anything unprompted.

### Step 4 — Remove the approved services
For each service the user approves:
```
uv run saorsa-deploy prod services rm \
  --inventory-path /home/chris/.autonomi/testnets/ant-prod-01/inventory.json \
  --name <host> --service-name ant-node-N
```
Each `rm` records an audit row (removed + reclaimed size) automatically. Removal is permanent;
the removed node's records re-replicate across the fleet. Re-run Step 1's survey afterward to
confirm the freed space (and to store an updated survey).

### Step 5 — Today-vs-yesterday report
```
uv run saorsa-deploy prod services survey-report \
  --inventory-path /home/chris/.autonomi/testnets/ant-prod-01/inventory.json
```
Compares the two most recent stored surveys: per-host free-disk delta, total node-data delta, and
the services added/removed/reset between them (from the audit trail), with the total reclaimed.
Summarise this for the user — it's the running picture of whether fleet pressure is improving.

## Notes / gotchas

- **`disk-survey` sizes are exact** (`du -sb`), unlike a single host's `ls` (human-rounded).
- The survey **stores every run**, so running it daily builds the history `survey-report` needs;
  a meaningful today-vs-yesterday needs at least two surveys on record.
- **Removal reduces the host's node count permanently** — that's the intended fix for an
  oversubscribed host (nodes' true footprint ≈ disk). It is not reversible by re-adding without a
  full fresh bootstrap; prefer removing genuinely-redundant/smallest services.
- If `disk-survey` reports host failures (SSH timeouts), those hosts are simply absent from the
  survey — note them; they are not counted as zero.
