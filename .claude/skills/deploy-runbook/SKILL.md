---
name: deploy-runbook
description: Interview the user to gather testnet requirements and then generate a self-contained bash runbook that chains the correct saorsa-deploy commands to deploy (and destroy) a testnet. Use this skill whenever the user asks to "plan a testnet run", "compose a runbook", "generate a deploy script", "script a testnet deployment", or otherwise wants the set of saorsa-deploy commands for a specific testnet spec laid out as an executable script — even if they don't say the word "runbook". Prefer this skill over hand-rolling the command sequence yourself; the sequencing rules, binary-build gating, client/elastic/monitoring interplay, and destroy ordering are easy to get subtly wrong.
---

# deploy-runbook

Generate an executable bash runbook that deploys a saorsa testnet end-to-end, plus a matching destroy runbook, based on a short interview.

## Why this skill exists

Deploying a testnet requires roughly 5–15 `saorsa-deploy` subcommands executed in a strict order, with a bunch of optional stages (elastic, monitoring, clients) that mutate the rest of the sequence. The ordering rules are documented in `CLAUDE.md` and `deploy/README.md`, but they're easy to get wrong by hand: for example, `provision-bootstraps` must only run when bootstrap-count > 1, `provision-telegraf-logs` requires elastic state to have been written first, and `destroy-bootstraps` refuses to run while nodes still exist.

Rather than re-deriving that order every time, this skill interviews the user once, then emits a correct, idempotent-ish bash script that bakes the whole plan in.

## Output contract

Two files are generated per run, both placed in `runbooks/`:

- `runbooks/<TESTNET_NAME>-<YYYYMMDD-HHMMSS>-deploy.sh`
- `runbooks/<TESTNET_NAME>-<YYYYMMDD-HHMMSS>-destroy.sh`

The timestamp suffix is computed at **generation time**, not runtime. It exists because the same testnet name gets reused across runs and the user wants a linear record of attempts.

Both files are executable (`chmod +x`). Neither is committed — if `runbooks/` isn't in `.gitignore` already, remind the user.

Both scripts start with `set -euo pipefail` so the first failing command halts execution. Do not add `|| true` or swallow errors anywhere.

## The interview

Ask questions **in the order below**. Ask them one at a time so the user can answer conversationally, but it's fine to batch tightly related sub-questions (e.g. all per-client details for one client in a single turn). After each answer, echo back your understanding in one short line before moving on.

The user will sometimes give you a partial spec upfront ("deploy DEV-01 with 15 nodes, no clients"). In that case, extract what you can, confirm it, and only ask about the gaps.

### 1. Testnet name

Required on every command. Used to name both output files. Conventional form is `UPPERCASE-NN` (e.g. `DEV-01`, `STAGING-03`).

### 2. Binaries: `ant-node` and `ant` (client)

Ask about each binary separately. For each:

- **Version** (GitHub release, e.g. `0.2.0`) — uses a prebuilt release binary, and
- **Branch** — requires building from source first; ask for `--branch-name`, `--repo-owner`, and optionally a `--validation-string` (a literal string expected inside the resulting binary, used as a build sanity check — e.g. a log message only present on that branch).

These are **mutually exclusive per binary** but can mix across the two (e.g. ant-node from branch, ant client from release). If the user picks a branch, the runbook must include the matching `build-ant-node-binary` or `build-ant-binary` step before any provisioning.

If the user opts for "no clients" in question 6, skip the `ant` client binary question entirely — it's not needed.

### 3. Bootstrap count

Integer ≥ 1. If `> 1`, the runbook needs both `provision-genesis` AND `provision-bootstraps`. If `= 1`, only `provision-genesis` runs — do not emit `provision-bootstraps`.

### 4. Node count and provider distribution

Ask for the **total non-bootstrap node count** as a single number. Then **propose a distribution** across DigitalOcean, Hetzner, and Vultr and let the user accept or adjust.

Guidelines for the suggestion:

- **Hetzner is constrained** — capacity/availability issues are common. Keep Hetzner at roughly 2–4 VMs regardless of total, unless the user explicitly says to push more there. For small totals (≤ 5), it's fine to leave Hetzner at 0.
- **DigitalOcean is the default primary** — aim for ~50–60% of total there.
- **Vultr fills the rest** — ~25–35%, whatever remains after DO and Hetzner.
- Present the suggestion concretely: e.g. for 20 total → `12 DO / 3 Hetzner / 5 Vultr`. Let the user override any of the three.

**Important — the CLI counts are per-region, not total.** `--do-vm-count` means "VMs per region on DO", and `--do-regions` defaults to 3 (same for Hetzner and Vultr). So to achieve a provider total of 12 DO VMs, you typically emit `--do-vm-count 4 --do-regions 3` (4 per region × 3 regions = 12).

When translating the confirmed provider totals into CLI flags:

- Default to 3 regions per provider — aligns with "distribute across geographies".
- If the provider total divides cleanly by 3, use `--do-regions 3` (or equivalent) and `--<provider>-vm-count = total / 3`.
- If it doesn't divide cleanly, pick the closest region count that does (e.g. 10 DO → 5 per region × 2 regions, or 10 per region × 1 region). Show the user the resulting `per-region × regions` math and let them confirm or adjust.
- If a provider total is 0, **omit that provider's `--vm-count` flag entirely** (CLI default is 0 = provider not used). Don't emit `--hetzner-vm-count 0`.
- For Hetzner specifically, since the total is usually small (2–4), a single region is fine: `--hetzner-vm-count N --hetzner-regions 1`.

Then confirm **VM sizes per provider**. Sensible defaults if the user shrugs:

| Provider | Default slug | Equivalent |
|---|---|---|
| DigitalOcean | `s-2vcpu-4gb` | 2 vCPU / 4 GB |
| Hetzner | `cpx22` | 2 vCPU / 4 GB |
| Vultr | `vc2-2c-4gb` | 2 vCPU / 4 GB |

If the user wants a single "tier" (e.g. "4/8"), map to equivalents: DO `s-4vcpu-8gb`, Hetzner `cpx32`, Vultr `vc2-4c-8gb`. Always surface the chosen slugs back to the user so a typo is caught early.

### 5. Testnet mode

Boolean yes/no. If yes, pass `--testnet` to **all three** of `provision-genesis`, `provision-bootstraps` (if emitted), and `provision-nodes`.

### 5a. Upgrade channel

Ask: "Which ant-node upgrade channel — `beta`, `stable`, or none?" Only `beta` and `stable` are valid saorsa-deploy choices; "none" means omit the flag entirely (ant-node's built-in default applies).

- If `beta` or `stable`, pass `--upgrade-channel {beta|stable}` to **all three** of `provision-genesis`, `provision-bootstraps` (if emitted), and `provision-nodes` — same value on all three, never mix channels across the commands.
- If "none", omit `--upgrade-channel` from all three.
- This setting only affects `ant-node` (nodes/bootstraps/genesis); clients do not take `--upgrade-channel`.

### 5b. Port-restricted NAT simulation

Boolean yes/no. If yes, ask for a **percentage** (1–100) of node VMs that should run behind a simulated port-restricted NAT (Linux network namespace + iptables DNAT/MASQUERADE).

- If yes, pass `--nat-percent N` to `provision-nodes`.
- If no (or 0%), omit the flag entirely — current behaviour is unchanged.
- Selection is deterministic ("one per region first, then round-robin") and is locked into deployment state on first run. `--force-nat-reselect` can reshuffle but the runbook should not emit it without being asked.
- NAT only applies to regular node VMs. Bootstrap, genesis, client, and monitoring/elastic VMs are always public.

### 6. Clients

Ask first: "Do you want clients at all?" If no, skip the rest of this section and omit `infra-clients`, `provision-clients`, and `start-clients` from the runbook.

If yes, ask for the client count, then for each client collect:

- **Region** — a DigitalOcean region slug (`lon1`, `nyc1`, `ams3`, `sfo3`, `sgp1`, `blr1`, `fra1`, `tor1`).
- **File size** — in MB, integer. This is the upload/download test size.
- **Store concurrency** — integer, default **8** (ant's own default). This becomes the `--store-concurrency` value the uploader passes to `ant file upload`. Accept the default unless the user explicitly wants to override; higher values upload more chunks in parallel.

Clients always run on DO (saorsa-deploy doesn't support other providers for clients). Each client is passed as a separate `--client` flag to `infra-clients`. The spec format is positional: `region:file_size_mb[:vm_size[:store_concurrency]]`. To override `store_concurrency` you must also state `vm_size` explicitly (the default is `s-2vcpu-4gb`) — positional slots can't be skipped.

If clients are enabled, the runbook must preflight-check `ANT_DEPLOY_SECRET_KEY` is set in the environment (see template below) — fail fast if it isn't.

### 7. Elasticsearch

Boolean yes/no. If yes, emit these in the right places:

- `elastic-infra --name $NAME` during the infra phase
- `provision-elastic --name $NAME` during the provision phase (before `provision-telegraf-logs`)
- `provision-telegraf-logs --name $NAME` **after** nodes are provisioned (it auto-loads ES creds from elastic state)
- `destroy-elastic --name $NAME --force` at the end of the destroy script

### 8. Monitoring (InfluxDB + Grafana)

Boolean yes/no. If yes, emit:

- `monitoring-infra --name $NAME` during the infra phase
- `provision-monitoring --name $NAME` during the provision phase
- `provision-telegraf-metrics --name $NAME` after nodes are provisioned (auto-loads Influx creds from monitoring state)
- `destroy-monitoring --name $NAME --force` at the end of the destroy script

### 9. SSH key path

Ask for the **absolute path to the SSH private key** that should be used for all provisioning SSH (e.g. `/home/chris/.ssh/ant_deploy`). The key's public half must be registered with every provider in use (DO, Hetzner, Vultr) — if it isn't, Terraform fails before any VM is created.

- Default is `~/.ssh/id_rsa`; the user will almost always want to override this with a project-specific key, so always ask.
- Emit the path as a top-level `SSH_KEY="..."` variable in the runbook and pass `--ssh-key-path "$SSH_KEY"` to every command that accepts it.
- Commands that accept `--ssh-key-path` (anything that SSHes into a VM): `build-ant-node-binary`, `build-ant-binary`, `provision-genesis`, `provision-bootstraps`, `provision-nodes`, `start-nodes`, `provision-monitoring`, `provision-elastic`, `provision-telegraf-metrics`, `provision-telegraf-logs`, `provision-clients`, `start-clients`.
- Infra/destroy commands (`infra-*`, `monitoring-infra`, `elastic-infra`, `destroy-*`) do **not** take `--ssh-key-path` — they're pure Terraform, no SSH. Don't emit the flag on those.

## Generation rules

Once the interview is done, before writing the files:

1. **Recap the plan** back to the user as a bullet list. Let them correct anything. Do not generate files until they say go.
2. **Compute the timestamp once** (shell: `date +%Y%m%d-%H%M%S`) and use it in both filenames.
3. **Create `runbooks/` if it doesn't exist.** Use `mkdir -p runbooks`.
4. **Write both files**, then `chmod +x` them.
5. **Do not run the generated script.** Just report the two paths and tell the user what to do next (e.g. `./runbooks/DEV-01-20260421-143022-deploy.sh`).
6. **If `runbooks/` is not already in `.gitignore`**, tell the user — don't edit `.gitignore` yourself.

### Command-ordering invariants

These are non-negotiable; the generated script must respect them:

- `infra-bootstraps` before `infra-nodes`
- Any `build-ant-*-binary` before any `provision-*` that uses that branch
- `infra-nodes` before `provision-nodes`
- `provision-genesis` before `provision-bootstraps` and `provision-nodes`
- `provision-elastic` before `provision-telegraf-logs`
- `provision-monitoring` before `provision-telegraf-metrics`
- `provision-nodes` before `start-nodes` (provision-nodes enables services; start-nodes brings them up)
- `start-nodes` before `provision-telegraf-*` (telegraf targets running nodes)
- `provision-nodes` before `provision-telegraf-*` (telegraf targets node VMs)
- `infra-clients` before `provision-clients` before `start-clients`
- Destroy order: `destroy-clients` → `destroy-nodes` → `destroy-bootstraps` → `destroy-elastic` → `destroy-monitoring` (skip `stop-clients` — the VMs are about to die anyway)

### Flag rules

- `--testnet` goes on `provision-genesis`, `provision-bootstraps`, and `provision-nodes` — all three or none.
- `--upgrade-channel {beta|stable}` goes on `provision-genesis`, `provision-bootstraps`, and `provision-nodes` — same value on all three, or omit from all three. Only accepts `beta` or `stable`; never emit the flag with any other value. The flag is unique to node provisioning — do not emit it on `provision-clients` or any other command.
- Binary selection is mutually exclusive **per binary**: either `--node-version X` or `--branch-name Y --repo-owner Z`, never both, on the same command.
- Port `10000` for `provision-genesis --port` and `provision-bootstraps --port`.
- Always include `--name "$NAME"` on every command.
- Use `--force` on all destroy commands (user already ran through the interview; no interactive confirm needed).
- **Do not** use `--force` on infra-creation commands (`infra-bootstraps`, `infra-nodes`, `infra-clients`, `monitoring-infra`, `elastic-infra`). On those commands `--force` only deletes the stale S3 tfstate — it does **not** destroy the real cloud resources that state references. The next Terraform run then creates new resources that collide with the orphans (volume/firewall name conflicts, duplicate droplets, etc.) and leaves the account in a worse state. If a stale-state error fires, tell the user to run the matching `destroy-*` command first, then retry — don't paper over it with `--force`.

## Deploy runbook template

Use this structure. Fill the `{{...}}` placeholders with interview answers. Omit sections wholesale when the corresponding feature is disabled — don't emit dead `if false; then ... fi` blocks, just leave them out.

```bash
#!/usr/bin/env bash
# Runbook: deploy {{NAME}}
# Generated: {{TIMESTAMP_HUMAN}}
# Spec:
#   Bootstraps:  {{BOOTSTRAP_COUNT}}
#   Nodes:       {{TOTAL_NODES}} total
#                  DO      = {{DO_PER_REGION}} × {{DO_REGIONS}} region(s) = {{DO_TOTAL}}
#                  Hetzner = {{HETZNER_PER_REGION}} × {{HETZNER_REGIONS}} region(s) = {{HETZNER_TOTAL}}
#                  Vultr   = {{VULTR_PER_REGION}} × {{VULTR_REGIONS}} region(s) = {{VULTR_TOTAL}}
#   Testnet:     {{YES_OR_NO}}
#   Upgrade chan: {{UPGRADE_CHANNEL_OR_NONE}}
#   NAT sim:     {{NAT_YES_NO_WITH_PERCENT}}
#   Clients:     {{CLIENT_COUNT_OR_NONE}}
#   Elastic:     {{YES_OR_NO}}
#   Monitoring:  {{YES_OR_NO}}
#   ant-node:    {{VERSION_OR_BRANCH_DESC}}
#   ant:         {{VERSION_OR_BRANCH_DESC_OR_NA}}
#   SSH key:     {{SSH_KEY_PATH}}

set -euo pipefail

NAME="{{NAME}}"
BOOTSTRAP_PORT=10000
SSH_KEY="{{SSH_KEY_PATH}}"

cd "$(dirname "$0")/.."

# --- Preflight ---
{{#if clients}}
if [[ -z "${ANT_DEPLOY_SECRET_KEY:-}" ]]; then
  echo "ERROR: ANT_DEPLOY_SECRET_KEY must be set in the environment for provision-clients" >&2
  exit 1
fi
{{/if}}

# --- Phase 1: Build binaries ---
{{#if ant_node_from_branch}}
uv run saorsa-deploy build-ant-node-binary \
  --name "$NAME" \
  --branch-name "{{ANT_NODE_BRANCH}}" \
  --repo-owner "{{ANT_NODE_REPO_OWNER}}" \
  --ssh-key-path "$SSH_KEY" \
  {{#if ant_node_validation}}--validation-string "{{ANT_NODE_VALIDATION}}"{{/if}}
{{/if}}
{{#if ant_from_branch}}
uv run saorsa-deploy build-ant-binary \
  --name "$NAME" \
  --branch-name "{{ANT_BRANCH}}" \
  --repo-owner "{{ANT_REPO_OWNER}}" \
  --ssh-key-path "$SSH_KEY" \
  {{#if ant_validation}}--validation-string "{{ANT_VALIDATION}}"{{/if}}
{{/if}}

# --- Phase 2: Infrastructure ---
uv run saorsa-deploy infra-bootstraps \
  --name "$NAME" \
  --bootstrap-count {{BOOTSTRAP_COUNT}}

uv run saorsa-deploy infra-nodes \
  --name "$NAME" \
  {{#if do_used}}--do-vm-count {{DO_PER_REGION}} --do-regions {{DO_REGIONS}} --do-droplet-size "{{DO_SIZE}}"{{/if}} \
  {{#if hetzner_used}}--hetzner-vm-count {{HETZNER_PER_REGION}} --hetzner-regions {{HETZNER_REGIONS}} --hetzner-server-type "{{HETZNER_SIZE}}"{{/if}} \
  {{#if vultr_used}}--vultr-vm-count {{VULTR_PER_REGION}} --vultr-regions {{VULTR_REGIONS}} --vultr-plan "{{VULTR_SIZE}}"{{/if}}

{{#if elastic}}
uv run saorsa-deploy elastic-infra --name "$NAME"
{{/if}}
{{#if monitoring}}
uv run saorsa-deploy monitoring-infra --name "$NAME"
{{/if}}
{{#if clients}}
uv run saorsa-deploy infra-clients \
  --name "$NAME" \
  {{#each client}}--client '{{region}}:{{file_size_mb}}{{#if store_concurrency_overridden}}:s-2vcpu-4gb:{{store_concurrency}}{{/if}}' \
  {{/each}}
{{/if}}

# --- Phase 3: Provisioning ---
{{#if elastic}}
uv run saorsa-deploy provision-elastic \
  --name "$NAME" \
  --ssh-key-path "$SSH_KEY"
{{/if}}
{{#if monitoring}}
uv run saorsa-deploy provision-monitoring \
  --name "$NAME" \
  --ssh-key-path "$SSH_KEY"
{{/if}}

uv run saorsa-deploy provision-genesis \
  --name "$NAME" \
  --port $BOOTSTRAP_PORT \
  {{BINARY_FLAGS_NODE}} \
  {{#if testnet}}--testnet{{/if}} \
  {{#if upgrade_channel}}--upgrade-channel {{UPGRADE_CHANNEL}}{{/if}} \
  --ssh-key-path "$SSH_KEY"

{{#if multi_bootstrap}}
uv run saorsa-deploy provision-bootstraps \
  --name "$NAME" \
  --port $BOOTSTRAP_PORT \
  {{BINARY_FLAGS_NODE}} \
  {{#if testnet}}--testnet{{/if}} \
  {{#if upgrade_channel}}--upgrade-channel {{UPGRADE_CHANNEL}}{{/if}} \
  --ssh-key-path "$SSH_KEY"
{{/if}}

uv run saorsa-deploy provision-nodes \
  --name "$NAME" \
  --node-count 1 \
  {{BINARY_FLAGS_NODE}} \
  {{#if testnet}}--testnet{{/if}} \
  {{#if upgrade_channel}}--upgrade-channel {{UPGRADE_CHANNEL}}{{/if}} \
  {{#if nat_enabled}}--nat-percent {{NAT_PERCENT}}{{/if}} \
  --ssh-key-path "$SSH_KEY"

uv run saorsa-deploy start-nodes \
  --name "$NAME" \
  --ssh-key-path "$SSH_KEY"

{{#if elastic}}
uv run saorsa-deploy provision-telegraf-logs \
  --name "$NAME" \
  --ssh-key-path "$SSH_KEY"
{{/if}}
{{#if monitoring}}
uv run saorsa-deploy provision-telegraf-metrics \
  --name "$NAME" \
  --ssh-key-path "$SSH_KEY"
{{/if}}

{{#if clients}}
uv run saorsa-deploy provision-clients \
  --name "$NAME" \
  {{BINARY_FLAGS_ANT}} \
  {{#if monitoring}}--monitoring-name "$NAME"{{/if}} \
  --ssh-key-path "$SSH_KEY"

uv run saorsa-deploy start-clients \
  --name "$NAME" \
  --ssh-key-path "$SSH_KEY"
{{/if}}

echo ""
echo "Testnet $NAME is up."
```

`{{BINARY_FLAGS_NODE}}` expands to either:
- `--node-version {{ANT_NODE_VERSION}}`, or
- `--branch-name {{ANT_NODE_BRANCH}} --repo-owner {{ANT_NODE_REPO_OWNER}}`

`{{BINARY_FLAGS_ANT}}` expands to either:
- `--ant-version {{ANT_VERSION}}`, or
- `--branch-name {{ANT_BRANCH}} --repo-owner {{ANT_REPO_OWNER}}`

Note that for `provision-clients`, the client binary flags use `--branch-name`/`--repo-owner` too (not a separate `--ant-branch-name`); saorsa-deploy infers which binary from the command context.

## Destroy runbook template

```bash
#!/usr/bin/env bash
# Runbook: destroy {{NAME}}
# Generated: {{TIMESTAMP_HUMAN}}

set -euo pipefail

NAME="{{NAME}}"

cd "$(dirname "$0")/.."

{{#if clients}}
uv run saorsa-deploy destroy-clients --name "$NAME" --force
{{/if}}

uv run saorsa-deploy destroy-nodes --name "$NAME" --force
uv run saorsa-deploy destroy-bootstraps --name "$NAME" --force

{{#if elastic}}
uv run saorsa-deploy destroy-elastic --name "$NAME" --force
{{/if}}
{{#if monitoring}}
uv run saorsa-deploy destroy-monitoring --name "$NAME" --force
{{/if}}

echo ""
echo "Testnet $NAME has been destroyed."
```

Do **not** emit `stop-clients` in the destroy script. The VMs are about to be torn down by `destroy-clients`, so gracefully stopping the services first is a redundant step that just adds latency (and can spuriously fail if services were never running). Destroy propagates all errors — no `|| true` anywhere.

## Common pitfalls

- **`--do-vm-count` is per-region, not total.** Same for `--hetzner-vm-count` and `--vultr-vm-count`. The provider total is `vm-count × regions`. Always confirm the math with the user before generating.
- **Don't emit a provider's `--*-vm-count` flag if its total is 0.** CLI default is 0 (provider not used); an explicit 0 still no-ops but adds noise.
- **Don't include `provision-bootstraps` when `bootstrap-count == 1`.** The genesis bootstrap is the only one; `provision-bootstraps` would no-op or error.
- **`start-nodes` is always required after `provision-nodes`.** `provision-nodes` only enables the systemd units — it no longer starts them, so the runbook must emit `start-nodes` next, regardless of whether NAT simulation is enabled. Telegraf steps go after `start-nodes` so they target running nodes.
- **Don't mix `--node-version` and `--branch-name` on the same command.** They're mutually exclusive flags in saorsa-deploy.
- **Don't emit `provision-telegraf-logs` without elastic** (nor `provision-telegraf-metrics` without monitoring). Those commands auto-load credentials from state files that only exist if the corresponding stack was provisioned.
- **Do pass `--monitoring-name "$NAME"` to `provision-clients` when monitoring is enabled.** Without it, client upload/download metrics never reach the testnet's InfluxDB, which defeats the point of running both stacks.
- **Don't parameterize the timestamp.** It's baked into the filename at generation and the header comment for traceability; runtime regeneration defeats the point.
- **Don't add retry loops or sleeps.** If a command fails, the user wants to see it and decide what to do — `set -e` is the contract.
