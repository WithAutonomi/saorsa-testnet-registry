---
name: extend-runbook
description: Interview the user to gather the spec for an _extension_ testnet (one that reuses another testnet's bootstraps) and then generate a self-contained bash runbook that chains the correct saorsa-deploy commands to deploy (and unextend) it. Use this skill whenever the user asks to "plan an extension", "extend an existing testnet", "add a sibling testnet", "generate an extend script", "script a compatibility testnet", or otherwise wants the set of saorsa-deploy commands for a testnet that shares bootstraps with another — even if they don't say the word "runbook". Prefer this skill over hand-rolling the command sequence yourself; it correctly skips the bootstraps infra / nodes provision-genesis / bootstraps provision stages and wires in `deployments extend` / `deployments unextend` at the right points.
---

# extend-runbook

Generate an executable bash runbook that deploys an _extension_ testnet — a second testnet that peers with an existing testnet's bootstraps but has its own fresh node VMs, typically running a different `ant-node` binary for compatibility testing. Also emits a matching unextend runbook.

## Why this skill exists

An extension testnet is deployed with a subset of the full saorsa-deploy pipeline:

- **Skipped entirely:** `bootstraps infra`, `nodes provision-genesis`, `bootstraps provision` (the source testnet already has these running).
- **Replaced by:** a single `saorsa-deploy deployments extend --name <NEW> --source <EXISTING>` at the very start, which seeds the new testnet's state from the source's bootstraps.
- **Everything else is the same as a normal deploy:** `nodes infra`, `nodes provision`, `nodes start`, optional clients / elastic / monitoring, and `deployments generate-inventory` at the end.

Destroy is also different: no `bootstraps destroy`, because the extension doesn't own any — instead, `deployments unextend` removes only the extension's state record. The source's bootstraps are untouched.

The sequencing is easy to get subtly wrong by hand, especially around "is `--testnet` still needed for nodes provision" (yes), "is `elastic provision` still needed" (yes, if you enabled elastic for the extension), and "do I need to build the ant-node binary" (yes, if the extension uses a branch — that's the whole point of running an extension in most cases).

## Output contract

Two files per run, both placed in `runbooks/`:

- `runbooks/<NAME>-<YYYYMMDD-HHMMSS>-extend.sh`
- `runbooks/<NAME>-<YYYYMMDD-HHMMSS>-unextend.sh`

Where `<NAME>` is the **extension** testnet name (not the source). Timestamp is computed at generation time.

Both files are executable (`chmod +x`). Both start with `set -euo pipefail`. No `|| true` anywhere.

## The interview

Ask questions in the order below. Batch tightly related sub-questions into a single turn. Echo understanding back after each answer. If the user gives a partial spec upfront ("extend DEV-01 into DEV-02 with 5 DO nodes on the chore-foo branch"), extract what you can and only ask about gaps.

### 1. Source testnet name

The **existing** testnet that already has bootstraps deployed. Required. Example: `DEV-01`.

If the user can't remember the name, tell them `saorsa-deploy utils list-regions` won't help but they can check their own `~/.autonomi/testnets/` directory or the MCP `list_deployments` tool.

### 2. Extension testnet name

The **new** testnet being created. Required. Must be different from the source. Convention: `UPPERCASE-NN`, e.g. `DEV-02`. This is the `<NAME>` used for both output filenames.

### 3. ant-node binary

Same as deploy-runbook question 2, but only for `ant-node` — not for the `ant` client binary (that's only relevant if clients are enabled; ask then if needed).

Ask:
- **Version** (GitHub release, e.g. `0.2.0`), or
- **Branch** (requires `build ant-node` step; collect `--branch-name`, `--repo-owner`, optional `--validation-string`).

In practice an extension almost always uses a branch — the common case is testing a new binary against the source's stable one.

### 4. Node count and provider distribution

Same as deploy-runbook question 4:

- Ask for **total** non-bootstrap node count.
- Propose DO/Hetzner/Vultr distribution. Guidelines: Hetzner 2–4 max (or 0 for small totals), DO ~50–60%, Vultr fills the rest.
- Translate to per-region × regions math: `--<provider>-vm-count` is per-region, default 3 regions per provider. If provider total doesn't divide cleanly by 3, pick the closest region count.
- Confirm **VM sizes** per provider (defaults: DO `s-2vcpu-4gb`, Hetzner `cpx22`, Vultr `vc2-2c-4gb`; for 4/8 tier: DO `s-4vcpu-8gb`, Hetzner `cpx32`, Vultr `vc2-4c-8gb`).
- If a provider total is 0, omit its `--*-vm-count` flag entirely.

### 5. Testnet mode

Boolean yes/no. If yes, pass `--testnet` to `nodes provision` **only** for extensions — there is no `nodes provision-genesis` / `bootstraps provision` in this pipeline.

### 5a. Upgrade channel

Same choice as deploy-runbook: `beta`, `stable`, or none. If `beta` or `stable`, pass `--upgrade-channel {beta|stable}` to `nodes provision` only. If none, omit.

### 5b. Port-restricted NAT simulation

Same as deploy-runbook: boolean + percent. If yes, `--nat-percent N` goes on `nodes provision`.

### 6. Clients for the extension

Ask: "Do you want clients for the extension?" If no, skip.

If yes: same per-client spec as deploy-runbook (region, file size, store concurrency; DO-only; `ANT_DEPLOY_SECRET_KEY` preflight). Clients are independent per extension — they don't share anything with the source's clients.

If clients are enabled, you must also ask about the `ant` client binary (same version-or-branch choice as the node binary); emit a `build ant` step if the user picks a branch.

### 7. Elasticsearch for the extension

Boolean yes/no. If yes, the extension gets its own independent Elasticsearch VM (no sharing with the source). Emit `elastic infra` / `elastic provision` / `elastic provision-telegraf` / `elastic destroy` as usual.

In practice, extensions often reuse nothing beyond the bootstraps and it's common to leave elastic off. Confirm with the user.

**If elastic is enabled**, follow the same sizing interview as `deploy-runbook` section 7 (7a large-testnet warning + 7b sizing knobs). The **extension's own node count** is what drives the sizing — it's independent from the source testnet. The warning fires when the extension has ≥ 500 nodes:

> Heads up — with ~{TOTAL_NODES} nodes on this extension you're past the default ES sizing envelope. A 1000-node testnet generates roughly **500 GB of index data per day** at the default `--log-level info`. Recommend:
>
> - **Droplet size:** `s-8vcpu-16gb` (from `s-4vcpu-8gb`)
> - **Volume size:** `500` GB for 1 day, `1000` GB for 2 days (from `100`)
> - **Shards:** `8` to match the 8 vCPU on the bigger droplet (from `4`)

Knobs (default / override):

| Knob | Default | Flag |
|---|---|---|
| ES droplet size | `s-4vcpu-8gb` | `elastic infra --droplet-size` |
| ES volume size (GB) | `100` | `elastic infra --volume-size` |
| Primary shards per index | `4` | `elastic provision --shards` |
| ILM retention days | `7` | `elastic provision --retention-days` |
| Telegraf drop log levels | `DEBUG,TRACE` | `elastic provision-telegraf --drop-log-levels` |
| Telegraf output buffer lines | `2000000` | `elastic provision-telegraf --buffer-limit` |

Only emit a flag when the user picks a non-default value — keeps small-extension scripts tidy.

### 8. Monitoring for the extension

Same as question 7 but for InfluxDB + Grafana.

### 9. SSH key path

Same as deploy-runbook question 9. Default `~/.ssh/id_rsa`; almost always override. Emit as top-level `SSH_KEY="..."` and pass `--ssh-key-path "$SSH_KEY"` to every provisioning command that accepts it. Infra/destroy commands (`nodes infra`, `clients infra`, `elastic infra`, `monitoring infra`, `destroy-*`) do not take `--ssh-key-path`. `deployments extend` and `deployments unextend` also do **not** take `--ssh-key-path` — they're pure S3 state operations.

## Generation rules

Once the interview is done:

1. **Recap the plan** as a bullet list. Wait for "go" before generating files.
2. **Compute timestamp once** (`date +%Y%m%d-%H%M%S`).
3. **`mkdir -p runbooks`**.
4. Write both files, `chmod +x` them.
5. **Do not run the generated script.** Report the paths and say what's next.
6. **If `runbooks/` isn't in `.gitignore`**, tell the user — don't edit `.gitignore` yourself.

### Command-ordering invariants

These are non-negotiable:

- `deployments extend` is the **first** saorsa-deploy command in the extend script.
- `build ant-node` / `build ant` (if a branch) must come after `deployments extend` but before any `provision-*` step that consumes them.
- `nodes infra` before `nodes provision`.
- `elastic provision` before `elastic provision-telegraf`.
- `monitoring provision` before `monitoring provision-telegraf`.
- `nodes provision` before `nodes start` (nodes provision enables; nodes start runs them).
- `nodes start` before `provision-telegraf-*`.
- `nodes provision` before `provision-telegraf-*`.
- `clients infra` before `clients provision` before `clients start`.
- `deployments generate-inventory` is the **final** saorsa-deploy command in the extend script.
- Unextend order: `clients destroy` → `nodes destroy` → `elastic destroy` → `monitoring destroy` → `deployments unextend`. **Do not emit `bootstraps destroy`** — the extension doesn't own any. `deployments unextend` is the last command.

### Flag rules

- `--testnet` goes on `nodes provision` only (there is no genesis/bootstraps stage). Emit or omit uniformly.
- `--upgrade-channel {beta|stable}` goes on `nodes provision` only. Never emit with any other value. Client binaries do not take it.
- Binary flags on each command are mutually exclusive per binary (`--node-version X` XOR `--branch-name Y --repo-owner Z`).
- Always include `--name "$NAME"` on every command.
- Use `--force` on `nodes destroy`, `clients destroy`, `elastic destroy`, `monitoring destroy`, and `deployments unextend` (user already went through the interview).
- Do NOT use `--force` on `deployments extend` unless you are explicitly repointing an existing extension at a different source — that's outside this skill's scope; if the user asks for that, tell them to run `deployments extend --force` by hand.
- Do NOT use `--force` on `infra-*` commands (`nodes infra`, `clients infra`, `elastic infra`, `monitoring infra`) — same rule as deploy-runbook; `--force` there only wipes stale tfstate and leaves cloud orphans.

## Extend runbook template

```bash
#!/usr/bin/env bash
# Runbook: extend {{SOURCE}} → {{NAME}}
# Generated: {{TIMESTAMP_HUMAN}}
# Spec:
#   Source:      {{SOURCE}}
#   Extension:   {{NAME}}
#   Nodes:       {{TOTAL_NODES}} total
#                  DO      = {{DO_PER_REGION}} × {{DO_REGIONS}} region(s) = {{DO_TOTAL}}
#                  Hetzner = {{HETZNER_PER_REGION}} × {{HETZNER_REGIONS}} region(s) = {{HETZNER_TOTAL}}
#                  Vultr   = {{VULTR_PER_REGION}} × {{VULTR_REGIONS}} region(s) = {{VULTR_TOTAL}}
#   Testnet:     {{YES_OR_NO}}
#   Upgrade chan: {{UPGRADE_CHANNEL_OR_NONE}}
#   NAT sim:     {{NAT_YES_NO_WITH_PERCENT}}
#   Clients:     {{CLIENT_COUNT_OR_NONE}}
#   Elastic:     {{YES_OR_NO}}{{#if elastic}}
#                  droplet={{ES_DROPLET_SIZE}} volume={{ES_VOLUME_SIZE}}GB shards={{ES_SHARDS}}
#                  retention={{ES_RETENTION_DAYS}}d drop_levels={{ES_DROP_LEVELS}} buffer={{ES_BUFFER_LIMIT}}{{/if}}
#   Monitoring:  {{YES_OR_NO}}
#   ant-node:    {{VERSION_OR_BRANCH_DESC}}
#   ant:         {{VERSION_OR_BRANCH_DESC_OR_NA}}
#   SSH key:     {{SSH_KEY_PATH}}

set -euo pipefail

NAME="{{NAME}}"
SOURCE="{{SOURCE}}"
SSH_KEY="{{SSH_KEY_PATH}}"

cd "$(dirname "$0")/.."

# --- Load .env ---
# `uv run` already picks up .env for the python subprocesses, but the preflight
# checks below are plain bash and would otherwise miss vars defined only there.
set -a
[ -f .env ] && . .env
set +a

# --- Preflight ---
{{#if clients}}
if [[ -z "${ANT_DEPLOY_SECRET_KEY:-}" ]]; then
  echo "ERROR: ANT_DEPLOY_SECRET_KEY must be set in the environment for clients provision" >&2
  exit 1
fi
if [[ -z "${ANT_CLIENT_AWS_ACCESS_KEY_ID:-}" || -z "${ANT_CLIENT_AWS_SECRET_ACCESS_KEY:-}" ]]; then
  echo "ERROR: ANT_CLIENT_AWS_ACCESS_KEY_ID and ANT_CLIENT_AWS_SECRET_ACCESS_KEY must be set for clients provision" >&2
  echo "Generate these once via: terraform -chdir=deploy/saorsa_deploy/resources/aws-client-s3 apply" >&2
  exit 1
fi
{{/if}}

# --- Phase 1: Extend state from source ---
uv run saorsa-deploy deployments extend --name "$NAME" --source "$SOURCE"

# --- Phase 2: Build binaries (if branch) ---
{{#if ant_node_from_branch}}
uv run saorsa-deploy build ant-node \
  --name "$NAME" \
  --branch-name "{{ANT_NODE_BRANCH}}" \
  --repo-owner "{{ANT_NODE_REPO_OWNER}}" \
  --ssh-key-path "$SSH_KEY" \
  {{#if ant_node_validation}}--validation-string "{{ANT_NODE_VALIDATION}}"{{/if}}
{{/if}}
{{#if ant_from_branch}}
uv run saorsa-deploy build ant \
  --name "$NAME" \
  --branch-name "{{ANT_BRANCH}}" \
  --repo-owner "{{ANT_REPO_OWNER}}" \
  --ssh-key-path "$SSH_KEY" \
  {{#if ant_validation}}--validation-string "{{ANT_VALIDATION}}"{{/if}}
{{/if}}

# --- Phase 3: Infrastructure (no bootstraps — reused from source) ---
uv run saorsa-deploy nodes infra \
  --name "$NAME" \
  {{#if do_used}}--do-vm-count {{DO_PER_REGION}} --do-regions {{DO_REGIONS}} --do-droplet-size "{{DO_SIZE}}"{{/if}} \
  {{#if hetzner_used}}--hetzner-vm-count {{HETZNER_PER_REGION}} --hetzner-regions {{HETZNER_REGIONS}} --hetzner-server-type "{{HETZNER_SIZE}}"{{/if}} \
  {{#if vultr_used}}--vultr-vm-count {{VULTR_PER_REGION}} --vultr-regions {{VULTR_REGIONS}} --vultr-plan "{{VULTR_SIZE}}"{{/if}}

{{#if elastic}}
uv run saorsa-deploy elastic infra \
  --name "$NAME" \
  {{#if es_droplet_size_override}}--droplet-size "{{ES_DROPLET_SIZE}}"{{/if}} \
  {{#if es_volume_size_override}}--volume-size {{ES_VOLUME_SIZE}}{{/if}}
{{/if}}
{{#if monitoring}}
uv run saorsa-deploy monitoring infra --name "$NAME"
{{/if}}
{{#if clients}}
uv run saorsa-deploy clients infra \
  --name "$NAME" \
  {{#each uploader}}--uploader '{{region}}:{{file_size_mb}}{{#if store_concurrency_overridden}}:{{vm_size}}:{{store_concurrency}}{{else}}{{#if vm_size_overridden}}:{{vm_size}}{{/if}}{{/if}}' \
  {{/each}}{{#each downloader}}--downloader '{{region}}{{#if vm_size_overridden}}:{{vm_size}}{{/if}}' \
  {{/each}}
{{/if}}

# --- Phase 4: Provisioning ---
{{#if elastic}}
uv run saorsa-deploy elastic provision \
  --name "$NAME" \
  {{#if es_shards_override}}--shards {{ES_SHARDS}}{{/if}} \
  {{#if es_retention_override}}--retention-days {{ES_RETENTION_DAYS}}{{/if}} \
  --ssh-key-path "$SSH_KEY"
{{/if}}
{{#if monitoring}}
uv run saorsa-deploy monitoring provision \
  --name "$NAME" \
  --ssh-key-path "$SSH_KEY"
{{/if}}

uv run saorsa-deploy nodes provision \
  --name "$NAME" \
  {{BINARY_FLAGS_NODE}} \
  {{#if testnet}}--testnet{{/if}} \
  {{#if upgrade_channel}}--upgrade-channel {{UPGRADE_CHANNEL}}{{/if}} \
  {{#if nat_enabled}}--nat-percent {{NAT_PERCENT}}{{/if}} \
  --ssh-key-path "$SSH_KEY"

uv run saorsa-deploy nodes start \
  --name "$NAME" \
  --ssh-key-path "$SSH_KEY"

{{#if elastic}}
uv run saorsa-deploy elastic provision-telegraf \
  --name "$NAME" \
  {{#if es_drop_levels_override}}--drop-log-levels "{{ES_DROP_LEVELS}}"{{/if}} \
  {{#if es_buffer_limit_override}}--buffer-limit {{ES_BUFFER_LIMIT}}{{/if}} \
  --ssh-key-path "$SSH_KEY"
{{/if}}
{{#if monitoring}}
uv run saorsa-deploy monitoring provision-telegraf \
  --name "$NAME" \
  --ssh-key-path "$SSH_KEY"
{{/if}}

{{#if clients}}
uv run saorsa-deploy clients provision \
  --name "$NAME" \
  {{BINARY_FLAGS_ANT}} \
  {{#if monitoring}}--monitoring-name "$NAME"{{/if}} \
  --ssh-key-path "$SSH_KEY"

uv run saorsa-deploy clients start \
  --name "$NAME" \
  --ssh-key-path "$SSH_KEY"
{{/if}}

# --- Phase 5: Inventory snapshot ---
uv run saorsa-deploy deployments generate-inventory \
  --name "$NAME" \
  --ssh-key-path "$SSH_KEY"

echo ""
echo "Extension $NAME (source=$SOURCE) is up."
```

`{{BINARY_FLAGS_NODE}}` expands to either `--node-version {{ANT_NODE_VERSION}}` or `--branch-name {{ANT_NODE_BRANCH}} --repo-owner {{ANT_NODE_REPO_OWNER}}`.

`{{BINARY_FLAGS_ANT}}` expands to either `--ant-version {{ANT_VERSION}}` or `--branch-name {{ANT_BRANCH}} --repo-owner {{ANT_REPO_OWNER}}`.

## Unextend runbook template

```bash
#!/usr/bin/env bash
# Runbook: unextend {{NAME}}
# Generated: {{TIMESTAMP_HUMAN}}
# Source testnet ({{SOURCE}}) is NOT touched — its bootstraps stay up.

set -euo pipefail

NAME="{{NAME}}"

cd "$(dirname "$0")/.."

{{#if clients}}
uv run saorsa-deploy clients destroy --name "$NAME" --force
{{/if}}

uv run saorsa-deploy nodes destroy --name "$NAME" --force

{{#if elastic}}
uv run saorsa-deploy elastic destroy --name "$NAME" --force
{{/if}}
{{#if monitoring}}
uv run saorsa-deploy monitoring destroy --name "$NAME" --force
{{/if}}

uv run saorsa-deploy deployments unextend --name "$NAME" --force

echo ""
echo "Extension $NAME has been removed. Source testnet bootstraps are untouched."
```

Do **not** emit `clients stop` in the unextend script (same reason as deploy-runbook's destroy script). Do **not** emit `bootstraps destroy` — extensions don't own bootstraps.

## Common pitfalls

- **Don't emit `bootstraps infra`, `nodes provision-genesis`, or `bootstraps provision` for an extension.** The source already has those; running them for the extension would create orphaned bootstrap VMs and confuse state.
- **Don't emit `bootstraps destroy` in the unextend script.** There are no bootstraps to destroy for an extension.
- **`deployments extend` must be the first saorsa-deploy command.** All subsequent commands rely on the state file it creates.
- **`build-ant-*-binary` still goes _after_ `deployments extend` but _before_ the first `provision-*` that uses the branch.** Same timing rule as deploy-runbook — builds are independent of bootstrap/genesis stages.
- **`--do-vm-count` (and Hetzner/Vultr equivalents) are still per-region, not total.** Same as deploy-runbook; confirm the math with the user before generating.
- **Don't emit a provider's `--*-vm-count` flag if its total is 0.**
- **Don't emit `elastic provision-telegraf` without elastic** (nor `monitoring provision-telegraf` without monitoring). Same as deploy-runbook.
- **Pass `--monitoring-name "$NAME"` to `clients provision` when monitoring is enabled on the extension.** Without it, client metrics don't reach the extension's InfluxDB.
- **Don't parameterize the timestamp.** Baked at generation for traceability.
- **`--force` on `deployments extend`** repoints an existing extension at a different source. Don't emit it by default; tell the user to run it by hand if they really want that.
- **`deployments unextend` does not destroy cloud resources.** It only removes the extension's S3 state record (and marks the DB row destroyed). That's the whole point — the source's bootstrap VMs stay up.
