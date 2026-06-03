---
name: track-upgrades-setup
description: Interview the user to gather what's needed to stand up auto-upgrade tracking against a production (or other long-running) testnet, and then generate a self-contained bash runbook that seeds the SQLite tracker DB — and, only when no permanent Elasticsearch pipeline exists for the deployment, also wires up ad-hoc Elasticsearch + Telegraf log forwarding — so `track-upgrades sync`/`summary`/`details` can be run against the rollout. Use this skill whenever the user asks to "set up upgrade tracking", "track an upgrade", "monitor an auto-upgrade", "watch nodes upgrade", "create the tracker", "prep upgrade tracking", or otherwise wants the saorsa-deploy command sequence for instrumenting an auto-upgrade laid out as an executable script — even if they don't say the word "runbook" or "tracker". Prefer this skill over hand-rolling the command sequence yourself; it knows when the permanent pipeline (e.g. ant-prod-01's always-on ES + Telegraf, live since 2026-06-03) makes the infra steps unnecessary, that the `--since` cursor must be back-dated *before* the rollout fires (and must stay within the permanent instance's 7-day ILM retention), and — for the ad-hoc path — that volume filtering relies on the `provision-telegraf --drop-log-levels` default (`DEBUG,TRACE`) and the elastic infra → provision → telegraf → init ordering is fragile.
---

# track-upgrades-setup

Generate an executable bash runbook that prepares the upgrade-tracking pipeline for a rollout, based on a short interview. There are two modes:

- **Permanent-pipeline mode** (preferred when available): the deployment already has an always-on Elasticsearch instance with Telegraf log forwarding running on every node VM — `ant-prod-01` has had this since 2026-06-03 (OVH-hosted, 7-day ILM retention, DEBUG/TRACE dropped at the agents). The runbook reduces to seeding the tracker DB (`track-upgrades init`) plus an optional verification query. **No infra is created and no teardown runbook is emitted** — the pipeline outlives the rollout.
- **Ad-hoc mode**: no permanent pipeline exists for this deployment, so the runbook stands up Elasticsearch + Telegraf log forwarding first, then seeds the tracker DB. A matching teardown runbook is also emitted.

## Why this skill exists

`saorsa-deploy track-upgrades sync` reads upgrade-lifecycle log lines out of an Elasticsearch index that the prod nodes' Telegraf agents ship into. `track-upgrades init` resolves the ES credentials from the elastic state registered under the deployment name, so it works identically against a permanent or ad-hoc instance — the skill's job is to know which steps are needed around it.

The hard-won lessons this skill bakes in:

- **Check for a permanent pipeline before building an ad-hoc one.** `uv run saorsa-deploy elastic info --name <name>` succeeding means an ES stack is registered for the deployment. If Telegraf log forwarding is also already running fleet-wide (true for `ant-prod-01` since 2026-06-03), the entire infra half of the old runbook is dead weight — and a teardown would *destroy shared production monitoring*. Permanent-pipeline mode exists so neither can happen.
- **`provision-telegraf --drop-log-levels DEBUG,TRACE` (the default) is non-optional for prod-scale runs** (ad-hoc mode only — the permanent pipeline already runs with this filter). Without log-level filtering, the bulk of log volume is operational chatter (DHT iteration pings, transport channel sends, peer-registration notices). On a 990-node testnet that saturates a default `s-8vcpu-16gb` ES VM in well under an hour, Telegraf buffers fill, oldest lines are evicted, and the upgrade-detection events the tracker depends on get silently dropped. The earlier `--drop-targets` flag was removed upstream; the level-based default replaces it and is strongly preferred over relaxing it. Seen and diagnosed in production.
- **`track-upgrades init` writes the cursor at "now" by default.** If the rollout has already started, you've blinded the tracker — pass `--since <ISO timestamp before rollout>` to backdate. Init runs entirely offline against ES (just stores credentials in metadata), so it must run *before* you want sync to find anything.
- **Back-dated replay differs by mode.** In permanent-pipeline mode the historical events are *already in ES* (the agents never stopped shipping), so a back-dated `--since` is the whole story — as long as it stays inside the instance's ILM retention (7 days on ant-prod-01) and after the pipeline went live (2026-06-03 ~17:40 UTC). In ad-hoc mode, replay additionally needs telegraf provisioned with `--from-start` so the on-disk log files are re-read; with `force_document_id = true` in the ES output, re-shipping is idempotent — no duplicates.
- **Bootstrap nodes are tracked separately.** This skill only sets up tracking for the regular node VMs in `node_vms[]` of the inventory. Bootstrap nodes (in `bootstrap-inventory.json`) are checked via SSH directly — see the companion `bootstrap-upgrade-check` skill for that. (On the permanent pipeline the bootstraps *do* forward logs into the same index, tagged `host_role=BOOTSTRAP`; sync simply counts their docs as `skipped_unknown` because they match no tracker row. Harmless.)

## Output contract

In **permanent-pipeline mode**, one file is generated, placed in `runbooks/`:

- `runbooks/<NAME>-<YYYYMMDD-HHMMSS>-track-upgrades-setup.sh`

No teardown runbook — there is nothing to tear down. The tracker SQLite DB is the only per-rollout artifact, and re-running `init` for the next rollout wipes and reseeds it.

In **ad-hoc mode**, two files are generated:

- `runbooks/<NAME>-<YYYYMMDD-HHMMSS>-track-upgrades-setup.sh`
- `runbooks/<NAME>-<YYYYMMDD-HHMMSS>-track-upgrades-teardown.sh`

The timestamp suffix is computed at generation time, not runtime, so re-running this skill gives the user a linear record of attempts.

Both files are executable (`chmod +x`). Neither is committed — if `runbooks/` isn't in `.gitignore` already, remind the user.

Both scripts start with `set -euo pipefail` so the first failing command halts execution. Do not add `|| true` or swallow errors anywhere.

## The interview

Ask questions **in the order below**. Ask them one at a time so the user can answer conversationally. After each answer, echo back your understanding in one short line before moving on. If the user gives you a partial spec upfront, extract what you can, confirm it, and only ask about the gaps.

### 1. Testnet name

Required. This is the deployment name registered with `saorsa-deploy` — typically `ant-prod-01` for the production network, or a testnet name like `DEV-01` for non-prod tracking. The same value is used as:

- The Elasticsearch stack name (`elastic infra/provision --name X`)
- The `tag.testnet_name` Telegraf stamps onto every shipped log line
- The tracker DB filename root (`~/.saorsa-deploy/upgrades/<name>.db`)

It must match an existing inventory file at `~/.autonomi/testnets/<name>/inventory.json`. The skill should sanity-check that file exists before proceeding; if not, stop and tell the user — running the runbook against a missing inventory will fail with no useful diagnostics.

### 2. Pipeline mode (detect, then confirm)

Once you have the name, determine which mode applies — don't ask the user cold:

1. Run `uv run saorsa-deploy elastic info --name <name>`. Success means an ES stack is already registered for this deployment.
2. If it succeeds, ask the user to confirm that Telegraf log forwarding is also already running on the node VMs (for `ant-prod-01` the answer is yes — the permanent OVH pipeline has been live since 2026-06-03). Both true → **permanent-pipeline mode**: skip questions 4 (SSH key), 6 (log-level filter), and 7 (ES sizing) entirely; the runbook contains no infra steps and no teardown is generated.
3. If `elastic info` fails, or ES exists but the fleet has no log forwarders → **ad-hoc mode**: ask everything.

Never generate an ad-hoc runbook for a deployment that has a permanent pipeline — its teardown would destroy shared production monitoring, and the duplicate ES stack would double the fleet's log-shipping for nothing.

### 3. Version pair (from / to)

Both required. `--from-version` is what is currently running on every node; `--to-version` is the version every node is expected to upgrade to. These are stored in tracker metadata and become the headline of the `summary` command.

Use semver-shaped strings exactly as the binary reports them (e.g. `0.11.0`, `0.11.1`). The state machine matches the `tail.version` field on `"ant-node starting"` log lines literally — a typo in `--to-version` means no node will ever be marked completed.

### 4. SSH key (ad-hoc mode only)

Ask for the absolute path to the SSH private key used to provision telegraf on the node VMs. Default for prod is `~/.ssh/ant_deploy`. Don't auto-fill; ask explicitly because the right key changes per testnet and the wrong one fails opaquely partway through provisioning. Skip entirely in permanent-pipeline mode — the runbook never SSHes anywhere.

### 5. `--since` cursor placement

This is the most consequential question; ask it carefully.

`track-upgrades init` defaults the sync cursor to "now". Anything emitted before that is invisible to sync forever (until the next `init`, which wipes and reseeds). Three cases:

- **Setting up *before* the rollout fires** — the right answer. Pass nothing; the default-now cursor is fine. Confirm with the user that the rollout has not yet started.
- **Setting up *after* the rollout has already partly fired** — pass `--since <ISO timestamp comfortably before rollout>` so sync can replay the early events. In permanent-pipeline mode that's all that's needed — the events are already sitting in ES. In ad-hoc mode the matching telegraf step needs `--from-start` (which the ad-hoc runbook always sets — see Flag rules).
- **Re-running tracking against a previous rollout** (rare) — same as above, with a `--since` from before that rollout.

Always ask the user when the rollout fired (or will fire). If the rollout is in the past, derive a `--since` value 1–2 hours before that. If the rollout is in the future or just-now, omit `--since` entirely.

**Permanent-pipeline retention bound:** the permanent instance ages indices out via ILM (7 days on ant-prod-01), and only contains data from the day the pipeline went live (2026-06-03 ~17:40 UTC) onward. A `--since` older than either bound silently finds nothing for the missing window. If the user needs a replay beyond retention, the escape hatch is an explicit `elastic provision-telegraf --name <name> --inventory-path <inv> --from-start` re-run against the permanent instance (idempotent, `force_document_id = true`), but warn that it re-ships every on-disk log file from byte 0 across the whole fleet — only do it deliberately.

### 6. Log-level filter (ad-hoc mode only — the permanent pipeline already drops DEBUG,TRACE at the agents)

`provision-telegraf` no longer accepts `--drop-targets` — it was removed upstream. The replacement is `--drop-log-levels`, which defaults to `DEBUG,TRACE`. That default is what keeps prod-scale ES alive; the tracker only reads INFO-level `ant_node` lifecycle events so dropping DEBUG/TRACE costs it nothing.

- **Default** — do not pass the flag at all; let the binary apply its default (`DEBUG,TRACE`). Strongly recommended for prod-scale tracking.
- **Override** — only when the user has a specific reason (e.g. ad-hoc forensic queries that need DEBUG, AND they've right-sized ES for the unfiltered volume). If they pick this, flag the implication: ES sizing must be increased, or the pipeline will wedge within hours. To pass an explicit value, use `--drop-log-levels '<comma-separated levels>'` or an empty string to disable filtering entirely.

### 7. ES sizing (ad-hoc mode only; offer defaults, take override)

The saorsa-testnet-registry repo currently codifies the working defaults in `Justfile`:

- Region: `lon1`
- Droplet size: `s-8vcpu-16gb`
- Volume size: `500` GB
- Shards: `8`
- Retention: `7` days

These work *only when the default `--drop-log-levels DEBUG,TRACE` filter is in place*. Without filtering, even `s-16vcpu-32gb` won't keep up at prod scale — the bottleneck is per-shard merge work, not raw CPU.

Always show these defaults back to the user and ask if they want to override any. For non-prod or smaller testnets, `s-4vcpu-8gb` and `100` GB are usually fine.

### 8. Optional verification step

Offer a final post-init verification that queries ES via curl, confirms the index is being written, and surfaces the per-host doc count + latest timestamp. Defaults to **on** because it's cheap and catches the most common silent failures (wrong index pattern, wrong testnet_name tag, telegraf wedged on one or more hosts). In permanent-pipeline mode the verification runs immediately (the index already exists and is being written); in ad-hoc mode it sleeps 90s first to let freshly provisioned telegraf ship its first batch.

The verification block uses the credentials stored in elastic state (loaded via `elastic info --name X`) so it doesn't add any new secrets to the runbook.

## Generation rules

Once the interview is done, before writing the files:

1. **Recap the plan** back to the user as a bullet list, starting with the **mode** (permanent pipeline vs ad-hoc), then the inventory path, version pair, `--since` (or "default-now"), and — ad-hoc only — key path, log-level filter, and ES sizing. Let them correct anything. Do not generate files until they say go.
2. **Compute the timestamp once** (`date +%Y%m%d-%H%M%S`) and use it in the filename(s).
3. **Create `runbooks/` if it doesn't exist.**
4. **Write the file(s)** (one in permanent mode, two in ad-hoc mode), then `chmod +x` them.
5. **Do not run the generated script.** Just report the path(s) and tell the user what to do next, including the post-runbook commands they'll want (`track-upgrades sync`, `summary`, `details`).
6. **If `runbooks/` is not already in `.gitignore`**, mention it — don't edit `.gitignore` yourself.
7. **Mention the companion bootstrap-check skill** in the closing message. Bootstrap nodes are not in the tracker DB, and a separate skill (`bootstrap-upgrade-check`) handles SSH-based status checks for them.

### Flag rules

- Always include `--name "$NAME"` on every saorsa-deploy command.
- (Ad-hoc only) The `provision-telegraf` step must always include `--from-start`. Do NOT pass `--drop-targets` — it was removed from saorsa-deploy. Volume filtering relies on the binary's default `--drop-log-levels DEBUG,TRACE`; emit `--drop-log-levels '<value>'` only if the user supplied an explicit override in §6.
- Only emit `--since "$SINCE"` on `track-upgrades init` when the user supplied a value in §5. Omit it (let the default be "now") otherwise.
- (Ad-hoc only) Only emit non-default ES sizing flags. The defaults are spelled out in the template's `infra` and `provision` calls — override only the ones the user changed.
- Never emit `--reset` on `track-upgrades init`; the post-fix init unconditionally wipes the tracker DB before reseeding.

## Setup runbook template — permanent-pipeline mode

Use this when §2 resolved to permanent-pipeline mode. The pipeline (ES + fleet-wide Telegraf) already exists and outlives the rollout; the runbook only seeds the tracker DB and optionally verifies the pipeline is flowing. **No teardown runbook is generated in this mode.**

```bash
#!/usr/bin/env bash
# Runbook: track-upgrades setup for {{NAME}} (permanent ES pipeline)
# Generated: {{TIMESTAMP_HUMAN}}
# Plan:
#   Testnet:        {{NAME}}
#   Inventory:      {{INVENTORY_PATH}}
#   From version:   {{FROM_VERSION}}
#   To version:     {{TO_VERSION}}
#   Cursor:         {{SINCE_OR_DEFAULT_NOW}}
#   Pipeline:       permanent ES + always-on telegraf (no infra steps, no teardown)

set -euo pipefail

NAME="{{NAME}}"
INVENTORY="{{INVENTORY_PATH}}"
FROM_VERSION="{{FROM_VERSION}}"
TO_VERSION="{{TO_VERSION}}"

cd "$(dirname "$0")/.."

# --- Preflight ---
if [[ ! -r "$INVENTORY" ]]; then
  echo "ERROR: inventory not readable: $INVENTORY" >&2
  exit 1
fi
# Fail fast if the permanent ES stack isn't registered for this name.
uv run saorsa-deploy elastic info --name "$NAME" > /dev/null

# --- 1. Seed the tracker SQLite DB ---
# The permanent pipeline ships INFO-level lifecycle events from every node VM
# continuously, so there are no elastic infra / provision / provision-telegraf
# steps. init resolves the permanent instance's credentials from elastic
# state by name and stores them in tracker metadata. The cursor controls
# which ES docs sync will ever look at — anything emitted before it is
# invisible. A back-dated cursor replays events already in ES (bounded by
# the instance's ILM retention).
uv run saorsa-deploy track-upgrades init \
  --name "$NAME" \
  --inventory-path "$INVENTORY" \
  --from-version "$FROM_VERSION" \
  --to-version "$TO_VERSION" \
  {{#if since}}--since "{{SINCE_ISO}}" \
  {{/if}}

{{#if verify}}
# --- 2. Verify the pipeline is flowing ---
# The permanent pipeline is already live, so no settling sleep is needed.
# `elastic info` prints a Rich box with │ borders, so strip those first.
ES_INFO=$(uv run saorsa-deploy elastic info --name "$NAME" 2>&1 | sed 's/│//g')
ES_URL=$(echo "$ES_INFO" | awk '/URL:/{for(i=1;i<=NF;i++) if($i ~ /^http/) {print $i; exit}}')
ES_KEY=$(echo "$ES_INFO" | awk '/API Key:/{getline; gsub(/[[:space:]]/,""); print; exit}')
NAME_LOWER=$(echo "$NAME" | tr '[:upper:]' '[:lower:]')
INDEX="telegraf-ant-nodes-${NAME_LOWER}-*"

echo ""
echo "=== Pipeline verification ==="
curl -sS -H "Authorization: ApiKey $ES_KEY" \
  "$ES_URL/$INDEX/_search?size=0&track_total_hits=true&ignore_unavailable=true&allow_no_indices=true" \
  -H 'Content-Type: application/json' \
  -d '{"query":{"range":{"@timestamp":{"gte":"now-10m"}}},"aggs":{"hosts":{"cardinality":{"field":"tag.host.keyword"}},"latest":{"max":{"field":"@timestamp"}}}}' \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
if 'error' in d:
    print(f'  ES error: {d[\"error\"].get(\"type\", \"unknown\")}: {d[\"error\"].get(\"reason\", \"\")}')
    sys.exit(1)
hits = d.get('hits', {})
total = hits.get('total', {}).get('value', 0)
aggs = d.get('aggregations', {})
hosts = aggs.get('hosts', {}).get('value', 0)
latest = aggs.get('latest', {}).get('value_as_string', '(none)')
print(f'  docs (last 10m): {total:,}')
print(f'  hosts seen:      {hosts}')
print(f'  latest doc:      {latest}')
if total == 0:
    print('  WARNING: the permanent pipeline shipped nothing in the last 10 minutes.')
    print('  That is unexpected — investigate before relying on the tracker.')
    sys.exit(1)
"
{{/if}}

echo ""
echo "Track-upgrades tracker for $NAME is seeded against the permanent pipeline."
echo "Nothing to tear down afterwards — the pipeline is shared production monitoring."
echo "Next steps:"
echo "  uv run saorsa-deploy track-upgrades sync    --db ~/.saorsa-deploy/upgrades/${NAME}.db --watch --interval 5m"
echo "  uv run saorsa-deploy track-upgrades summary --db ~/.saorsa-deploy/upgrades/${NAME}.db"
echo "  uv run saorsa-deploy track-upgrades details --db ~/.saorsa-deploy/upgrades/${NAME}.db"
```

## Setup runbook template — ad-hoc mode

Use this structure when no permanent pipeline exists. Fill the `{{...}}` placeholders with interview answers. Omit blocks wholesale when a feature is not selected — don't emit dead `if false; then ... fi` blocks.

```bash
#!/usr/bin/env bash
# Runbook: track-upgrades setup for {{NAME}}
# Generated: {{TIMESTAMP_HUMAN}}
# Plan:
#   Testnet:        {{NAME}}
#   Inventory:      {{INVENTORY_PATH}}
#   From version:   {{FROM_VERSION}}
#   To version:     {{TO_VERSION}}
#   SSH key:        {{SSH_KEY_PATH}}
#   Cursor:         {{SINCE_OR_DEFAULT_NOW}}
#   Log filter:     {{LOG_LEVEL_FILTER_OR_DEFAULT}}
#   ES sizing:      {{ES_REGION}} / {{ES_DROPLET_SIZE}} / {{ES_VOLUME_GB}}GB / shards={{ES_SHARDS}} / retention={{ES_RETENTION_DAYS}}d

set -euo pipefail

NAME="{{NAME}}"
INVENTORY="{{INVENTORY_PATH}}"
SSH_KEY="{{SSH_KEY_PATH}}"
FROM_VERSION="{{FROM_VERSION}}"
TO_VERSION="{{TO_VERSION}}"

cd "$(dirname "$0")/.."

# --- Preflight ---
if [[ ! -r "$INVENTORY" ]]; then
  echo "ERROR: inventory not readable: $INVENTORY" >&2
  exit 1
fi

# --- 1. Stand up Elasticsearch ---
uv run saorsa-deploy elastic infra \
  --name "$NAME" \
  --region {{ES_REGION}} \
  --droplet-size {{ES_DROPLET_SIZE}} \
  --volume-size {{ES_VOLUME_GB}}

uv run saorsa-deploy elastic provision \
  --name "$NAME" \
  --ssh-key-path "$SSH_KEY" \
  --shards {{ES_SHARDS}} \
  --retention-days {{ES_RETENTION_DAYS}}

# --- 2. Provision Telegraf log forwarders on every node VM ---
# `--from-start` makes telegraf re-read each log file from byte 0, so the
# tracker can pick up upgrade events that were already written before this
# runbook ran. Volume filtering relies on the provision-telegraf default
# (`--drop-log-levels DEBUG,TRACE`) — without it, ES saturates at prod scale
# within an hour. Only emit an explicit --drop-log-levels here if the user
# asked to override the default in §6.
uv run saorsa-deploy elastic provision-telegraf \
  --name "$NAME" \
  --inventory-path "$INVENTORY" \
  --ssh-key-path "$SSH_KEY" \
  --from-start \
  {{#if drop_log_levels_override}}--drop-log-levels '{{DROP_LOG_LEVELS}}' \
  {{/if}}--node-version "$FROM_VERSION"

# --- 3. Seed the tracker SQLite DB ---
# init wipes any prior tracking state for this name and re-inserts one row
# per (host, service_number). The cursor controls which ES docs sync will
# ever look at — anything emitted before the cursor is invisible.
uv run saorsa-deploy track-upgrades init \
  --name "$NAME" \
  --inventory-path "$INVENTORY" \
  --from-version "$FROM_VERSION" \
  --to-version "$TO_VERSION" \
  {{#if since}}--since "{{SINCE_ISO}}" \
  {{/if}}

{{#if verify}}
# --- 4. Verify the pipeline is end-to-end ---
# Pulls credentials from elastic state and queries ES directly. Failures
# here usually mean: telegraf wedged on some/all hosts, wrong testnet_name
# tag, or wrong index pattern.
# `elastic info` prints a Rich box with │ borders, so strip those first.
# URL: pick the http(s) token on the URL line.
# API Key: value lives on the line *after* "API Key:".
ES_INFO=$(uv run saorsa-deploy elastic info --name "$NAME" 2>&1 | sed 's/│//g')
ES_URL=$(echo "$ES_INFO" | awk '/URL:/{for(i=1;i<=NF;i++) if($i ~ /^http/) {print $i; exit}}')
ES_KEY=$(echo "$ES_INFO" | awk '/API Key:/{getline; gsub(/[[:space:]]/,""); print; exit}')
NAME_LOWER=$(echo "$NAME" | tr '[:upper:]' '[:lower:]')
INDEX="telegraf-ant-nodes-${NAME_LOWER}-*"

echo ""
echo "=== Pipeline verification ==="
# Telegraf typically takes 60-120s after provisioning to install, start, and
# ship its first batch. Until then the index doesn't exist and ES returns
# 404 — handled below.
sleep 90
# `ignore_unavailable=true&allow_no_indices=true` makes ES return an empty
# hits result instead of a 404 when the index pattern hasn't matched yet.
curl -sS -H "Authorization: ApiKey $ES_KEY" \
  "$ES_URL/$INDEX/_search?size=0&track_total_hits=true&ignore_unavailable=true&allow_no_indices=true" \
  -H 'Content-Type: application/json' \
  -d '{"aggs":{"hosts":{"cardinality":{"field":"tag.host.keyword"}},"latest":{"max":{"field":"@timestamp"}}}}' \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
if 'error' in d:
    print(f'  ES error: {d[\"error\"].get(\"type\", \"unknown\")}: {d[\"error\"].get(\"reason\", \"\")}')
    print('  Telegraf may not have shipped yet — re-check in a few minutes with:')
    print(f'  curl -sS -H \"Authorization: ApiKey \$ES_KEY\" \"\$ES_URL/$INDEX/_count\"')
    sys.exit(0)
hits = d.get('hits', {})
total = hits.get('total', {}).get('value', 0)
aggs = d.get('aggregations', {})
hosts = aggs.get('hosts', {}).get('value', 0)
latest = aggs.get('latest', {}).get('value_as_string', '(none)')
print(f'  docs indexed:  {total:,}')
print(f'  hosts seen:    {hosts}')
print(f'  latest doc:    {latest}')
if total == 0:
    print('  NOTE: no docs yet. Telegraf often needs 2-3 minutes after provisioning')
    print('  to install and start shipping. Re-run the curl above to recheck.')
"
{{/if}}

echo ""
echo "Track-upgrades pipeline for $NAME is set up."
echo "Next steps:"
echo "  uv run saorsa-deploy track-upgrades sync    --db ~/.saorsa-deploy/upgrades/${NAME}.db --watch --interval 5m"
echo "  uv run saorsa-deploy track-upgrades summary --db ~/.saorsa-deploy/upgrades/${NAME}.db"
echo "  uv run saorsa-deploy track-upgrades details --db ~/.saorsa-deploy/upgrades/${NAME}.db"
```

## Teardown runbook template — ad-hoc mode only

**Never generate this in permanent-pipeline mode** — `elastic clean` would strip the always-on log forwarders from the whole fleet and `elastic destroy` would delete shared production monitoring. In permanent mode there is nothing to tear down; the tracker SQLite DB is the only per-rollout artifact.

The ad-hoc teardown is deliberately minimal — it removes the telegraf log-forwarders from the node VMs, then destroys the ES VM. It does **not** delete the tracker SQLite DB; that's the user's record of the rollout and they can keep or remove it themselves.

```bash
#!/usr/bin/env bash
# Runbook: track-upgrades teardown for {{NAME}}
# Generated: {{TIMESTAMP_HUMAN}}

set -euo pipefail

NAME="{{NAME}}"
INVENTORY="{{INVENTORY_PATH}}"
SSH_KEY="{{SSH_KEY_PATH}}"

cd "$(dirname "$0")/.."

# --- 1. Remove telegraf log-forwarder from node VMs ---
# Done first so the agents stop sending bulk requests before ES is destroyed
# (otherwise they'd queue, fill buffers, and evict any in-flight metrics).
uv run saorsa-deploy elastic clean \
  --inventory-path "$INVENTORY" \
  --ssh-key-path "$SSH_KEY"

# --- 2. Destroy the Elasticsearch VM ---
uv run saorsa-deploy elastic destroy --name "$NAME"

echo ""
echo "Track-upgrades pipeline for $NAME has been torn down."
echo "Tracker DB at ~/.saorsa-deploy/upgrades/${NAME}.db is untouched; remove it manually if you don't want it."
```

## Common pitfalls

- **Cursor placed after the rollout fires.** This is the most common silent failure — sync looks like it's running fine but always finds zero events because `init` defaulted the cursor to a time after the upgrade-detection log lines were emitted. Always ask explicitly when the rollout fired and back-date `--since` if it's already in the past.
- **Back-dating beyond the permanent instance's retention.** The permanent pipeline ages indices out via ILM (7 days on ant-prod-01) and only has data from its go-live (2026-06-03 ~17:40 UTC) onward. A `--since` outside that window silently finds nothing for the missing range — initialise tracking within the retention window of the rollout, or use the `--from-start` re-ship escape hatch described in §5.
- **Tearing down the permanent pipeline.** Do not emit (or run) `elastic clean` / `elastic destroy` against a deployment with a permanent pipeline. It is shared production monitoring (logs + the paired InfluxDB/Grafana stack's companion), not rollout scaffolding.
- **Bootstrap docs in the shared index.** On the permanent pipeline the bootstrap VMs forward into the same index (`tag.host_role=BOOTSTRAP`, non-numeric service identifiers). The tracker only seeds rows for `node_vms[]`, so sync counts bootstrap docs as `skipped_unknown` — that counter being non-zero is expected, not a bug.
- **Don't emit `--drop-targets`.** That flag was removed from `provision-telegraf` upstream. Calling it is an instant `unrecognized arguments` failure. Volume filtering now relies on `--drop-log-levels DEBUG,TRACE` (the default) — emit no flag at all, and only pass `--drop-log-levels '<value>'` if the user explicitly overrode the default in §6. Skipping log-level filtering at prod scale silently breaks tracking: ES looks fine for the first hour, then telegraf buffers fill, upgrade events get evicted, and `summary` shows everything stuck on `pending`.
- **Verification index may not exist yet.** Telegraf typically needs 60–120s after provisioning to install, start, and ship its first batch. Until then, ES returns 404 for the index pattern and the verification JSON has no `hits` key. The runbook handles this by passing `ignore_unavailable=true&allow_no_indices=true` and defending the python parser; don't strip these.
- **Wrong inventory file.** This skill targets `~/.autonomi/testnets/<name>/inventory.json` (the regular node inventory). The bootstrap inventory is a different file (`bootstrap-inventory.json`) and bootstrap nodes are tracked separately via SSH, not via the ES pipeline. Don't conflate them.
- **`--node-version` is a static tag.** It gets stamped onto every log line as `tag.node_version` for ad-hoc queries; it has no effect on Telegraf or the tracker. The tracker reads the actual running version from `tail.version` on the `"ant-node starting"` log line, which the binary self-reports. Don't worry about updating it mid-rollout.
- **Don't add retry loops or sleeps to the runbook.** If a command fails the user wants to see it and decide what to do — `set -e` is the contract. The verification block has one `sleep 90` to let telegraf flush its first batch; that's the only timing-related thing in the script.
- **Don't run the script for the user.** Generate, `chmod +x`, report the path. Running it is the user's call.
