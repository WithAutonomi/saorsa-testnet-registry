---
name: upload-runbook
description: Interview the user to gather requirements for a production uploaders run and then generate a self-contained bash runbook that wraps `saorsa-deploy prod uploaders deploy` (and the matching destroy). Use this skill whenever the user asks to "plan a prod upload run", "compose an uploaders runbook", "generate a prod uploaders script", "script an uploaders deployment", or otherwise wants the saorsa-deploy command sequence for a production-mainnet upload run laid out as an executable script — even if they don't say the word "runbook". Prefer this skill over hand-rolling the command yourself; the interview catches the easily-forgotten preflight (Arbitrum One funded wallet, per-uploader URL files, monitoring sizing for big runs).
---

# upload-runbook

Generate an executable bash runbook that runs a production uploaders deployment end-to-end via `saorsa-deploy prod uploaders deploy`, plus a matching destroy runbook, based on a short interview.

## Why this skill exists

`prod uploaders deploy` is a single high-level command, but every run requires a small constellation of preconditions that are easy to forget:

- A funded **Arbitrum One mainnet** wallet whose secret is in `ARBITRUM_ONE_SECRET_KEY` (deliberately distinct from the testnet `ANT_DEPLOY_SECRET_KEY` because uploads cost real ETH).
- One **URL list file per uploader VM**, each readable on the box that runs the runbook (the file contents are slurped at deploy time and embedded into provisioning).
- A name that matches the convention used by the companion analysis commands (`prod print-upload-results`, `prod record-upload-results`).

Rather than re-deriving the spec every time, this skill interviews the user once and emits a runbook that bakes the whole plan in.

## Output contract

Two files are generated per run, both placed in `runbooks/`:

- `runbooks/<NAME>-<YYYYMMDD-HHMMSS>-prod-uploaders-deploy.sh`
- `runbooks/<NAME>-<YYYYMMDD-HHMMSS>-prod-uploaders-destroy.sh`

The timestamp suffix is computed at **generation time**, not runtime. The same name gets reused across runs and the user wants a linear record of attempts.

Both files are executable (`chmod +x`). Neither is committed — if `runbooks/` isn't in `.gitignore` already, remind the user.

Both scripts start with `set -euo pipefail` so the first failing command halts execution. Do not add `|| true` or swallow errors anywhere.

## The interview

Ask questions **in the order below**. Ask them one at a time so the user can answer conversationally, but it's fine to batch tightly related sub-questions (e.g. all per-uploader details for one VM in a single turn). After each answer, echo back your understanding in one short line before moving on.

If the user gives you a partial spec upfront, extract what you can, confirm it, and only ask about the gaps.

### 1. Deployment name

Required on every command. Used to name both output files. Conventional form is `PROD-UP-NN` (e.g. `PROD-UP-01`, `PROD-UP-02`). The same name is also used for the paired monitoring stack created in-flight, so anything reasonable for both works.

### 1b. Purpose

A short free-text description of *why* this run exists — e.g. "smoke-test 0.2.1-rc.2 against panama racing dataset". Emitted as `--purpose "..."` on the deploy command and recorded in the runbook header. Always ask; never invent.

### 2. `ant` (client) binary

Pick one of:

- **Version** (GitHub release, e.g. `0.3.0`) → emits `--ant-version X` on `prod uploaders deploy`. No build step needed — the release tarball is fetched from GitHub.
- **Branch + repo owner** → emits `--branch-name X --repo-owner Y` on `prod uploaders deploy`, **and requires a separate `saorsa-deploy build ant` step up front** (see §2b). `prod uploaders deploy` looks up the pre-built artifact in S3 keyed by `name + repo_owner + branch`, and refuses to start if it isn't there ("Run 'build-ant' first.").

These two binary-source modes are mutually exclusive. Branch and version flags must not both be set.

### 2b. Validation string (branch path only)

When the user picked the branch path in §2, also collect a **validation string**: a substring expected in the compiled binary's `strings` output. `saorsa-deploy build ant` runs `strings` on the build artifact and refuses to upload it unless this string is present — it's the cheap "did my patch actually make it into the binary" check.

- Always ask for it when the branch path is chosen. Don't invent one.
- Emit it as `--validation-string "..."` on the `build ant` command.
- If the user can't supply one, flag it: the build will succeed, but they lose the safety check. Only proceed if they explicitly accept that.

Skip §2b entirely on the version path — `build ant` does not run.

### 3. Per-uploader specs

`prod uploaders deploy` is multi-VM. Each `--uploader` flag describes one VM, with the format `REGION:VM_SIZE:urls=PATH[:payment=MODE]` — region, vm_size, and urls are required; payment is optional and per-uploader.

For each uploader, collect:

- **Region** — a DigitalOcean region slug (`lon1`, `nyc1`, `ams3`, `sfo3`, `sgp1`, `blr1`, `fra1`, `tor1`). Uploader VMs are DO-only.
- **VM size** — DO droplet slug. Sensible defaults: `s-2vcpu-4gb` for light loads, `s-4vcpu-8gb` for heavier file sizes / higher concurrency.
- **URLs file path** — a local path to a newline-separated file of public source URLs that this VM should download then re-upload to mainnet. The path must be readable on the host running the runbook because `saorsa-deploy` slurps the file contents at deploy time and embeds them into provisioning.
- **Payment mode** (optional) — `merkle`, `no-merkle`, or default. See §3b below; ask per uploader, not globally.

Ask first: "How many uploader VMs?" then collect the four fields per VM.

Then **confirm the full spec list** back to the user before moving on. A typo in a region slug or URL path won't be caught until provisioning is well underway.

### 3b. Payment mode (per uploader)

Each uploader spec optionally takes `:payment=merkle|no-merkle`, which forces the payment mode on `ant file upload` for that VM only:

- **`merkle`** → adds `--merkle` to every `ant file upload` (single-tx batch payment; cheaper gas for batches of 2+ chunks).
- **`no-merkle`** → adds `--no-merkle` (per-chunk payments).
- **default** (omit `payment=`) → ant picks its own default per upload.

Ask per uploader: "Force a payment mode (merkle / no-merkle) or leave at ant's default?" Different uploaders in the same run can have different modes, which is the point — back-to-back A/B comparisons of merkle vs no-merkle on the same dataset across regions are a primary use case.

- Only emit `payment=X` in the spec when the user picks `merkle` or `no-merkle`. Omit the token entirely for ant's default.
- There is no global `--payment-type` flag; payment is per-uploader.

### 4. Wallet (Arbitrum One mainnet)

The runbook must preflight-check `ARBITRUM_ONE_SECRET_KEY`. Fail fast if missing.

- **Do not** prompt the user to paste the key — it's a funded mainnet wallet secret. They'll have it in `.env` already, or they'll set it themselves before running.
- **Do not** emit `--secret-key "$..."` on the saorsa-deploy command — the CLI falls back to the env var on its own. Keeping the key out of the command line means it doesn't leak into shell history or process listings.
- The preflight in the runbook is just `[[ -n "${ARBITRUM_ONE_SECRET_KEY:-}" ]]`.

Tell the user, in your interview recap, that the runbook expects this env var. If they don't already have a funded wallet, **stop here and flag it** — do not generate a runbook against an empty wallet.

### 5. Monitoring volume size

`prod uploaders deploy` always provisions a paired monitoring VM (InfluxDB + Grafana, always in `lon1`). The default attached volume is **50 GB**, which suffices for short runs.

Ask once: "Default 50 GB monitoring volume, or larger?"

- For runs expected to stretch beyond a few hours, or with many VMs / high upload volume, suggest `100`–`250` GB.
- Only emit `--monitoring-volume-size N` when the user picks a value other than 50.

### 6. SSH key path

Ask for the **absolute path to the SSH private key** used for provisioning (e.g. `/home/chris/.ssh/ant_deploy`). The public half must be registered with DigitalOcean — if it isn't, Terraform fails before any VM is created.

- Default is `~/.ssh/id_rsa`; the user will almost always want to override this with a project-specific key, so always ask.
- Emit the path as a top-level `SSH_KEY="..."` variable in the runbook and pass `--ssh-key-path "$SSH_KEY"` to **both** the deploy and the destroy commands. (Unlike testnet destroy, prod destroy does take `--ssh-key-path`.)

### 7. Destroy behaviour

Default destroy tears down both the uploader VMs and the paired monitoring VM. Ask: "When you destroy, keep the monitoring VM alive afterwards (so you can keep querying recorded metrics) or tear it down too?"

- **Tear down** (default): no flag.
- **Keep monitoring**: emit `--keep-monitoring` on the destroy command. Note in the recap that the user is then on the hook for cleaning up monitoring later (manual `monitoring destroy` against the same name, if such a path even applies — the prod commands manage this VM directly).

## Generation rules

Once the interview is done, before writing the files:

1. **Recap the plan** back to the user as a bullet list, including the per-uploader spec list and the env-var preconditions. Let them correct anything. Do not generate files until they say go.
2. **Compute the timestamp once** (shell: `date +%Y%m%d-%H%M%S`) and use it in both filenames.
3. **Create `runbooks/` if it doesn't exist.** Use `mkdir -p runbooks`.
4. **Write both files**, then `chmod +x` them.
5. **Do not run the generated script.** Just report the two paths and tell the user what to do next, including a reminder to set `ARBITRUM_ONE_SECRET_KEY`.
6. **If `runbooks/` is not already in `.gitignore`**, tell the user — don't edit `.gitignore` yourself.
7. **Mention the companion commands** in the closing message (not in the script): once uploads finish, `uv run saorsa-deploy prod print-upload-results --name $NAME` and `uv run saorsa-deploy prod record-upload-results --name $NAME` give per-file Rich tables and Postgres upserts respectively.

### Flag rules

- Always include `--name "$NAME"` on every command.
- Binary selection is mutually exclusive: either `--ant-version X` or `--branch-name Y --repo-owner Z`. Never both.
- On the branch path, emit a `saorsa-deploy build ant` step before `prod uploaders deploy`. It must use the same `--name`, `--branch-name`, `--repo-owner`, and `--ssh-key-path`. If the user supplied a validation string, pass it as `--validation-string "..."`.
- Each uploader spec must include `urls=PATH`; refuse to generate without it.
- Each uploader spec optionally appends `:payment=merkle|no-merkle`; only emit when the user explicitly picked one — omit for ant's default. Different uploaders in the same run may use different payment modes.
- Only emit `--monitoring-volume-size N` when the user picked a non-default value.
- Only emit `--keep-monitoring` on destroy when the user explicitly asked for it.
- **Never emit `--secret-key`**; the env var fallback handles it.
- There is no global `--payment-type` flag — payment is per-uploader via `:payment=` in the spec.

## Deploy runbook template

Use this structure. Fill the `{{...}}` placeholders with interview answers. Omit blocks wholesale when a feature is not selected — don't emit dead `if false; then ... fi` blocks.

```bash
#!/usr/bin/env bash
# Runbook: prod uploaders deploy {{NAME}}
# Generated: {{TIMESTAMP_HUMAN}}
# Purpose: {{PURPOSE}}
# Spec:
#   Uploader VMs: {{UPLOADER_COUNT}}
{{#each uploader}}
#     - {{region}} / {{vm_size}} / urls={{urls_path}} / payment={{payment_or_default}}
{{/each}}
#   ant binary:  {{VERSION_OR_BRANCH_DESC}}
#   Monitoring:  always on (paired); volume={{MONITORING_VOLUME_SIZE_OR_DEFAULT}}GB
#   SSH key:     {{SSH_KEY_PATH}}

set -euo pipefail

NAME="{{NAME}}"
SSH_KEY="{{SSH_KEY_PATH}}"

cd "$(dirname "$0")/.."

# --- Load .env ---
# `uv run` already picks up .env for the python subprocesses, but the preflight
# checks below are plain bash and would otherwise miss vars defined only there.
set -a
[ -f .env ] && . .env
set +a

# --- Preflight ---
if [[ -z "${ARBITRUM_ONE_SECRET_KEY:-}" ]]; then
  echo "ERROR: ARBITRUM_ONE_SECRET_KEY must be set — prod uploads spend real ETH on Arbitrum One." >&2
  echo "       Put it in .env or export it before running this runbook." >&2
  exit 1
fi

{{#each uploader}}
if [[ ! -r "{{urls_path}}" ]]; then
  echo "ERROR: uploader URL list not readable: {{urls_path}}" >&2
  exit 1
fi
{{/each}}

{{#if branch_path}}
# --- Build ant binary from source ---
# Required because we're using --branch-name; prod uploaders deploy will
# refuse to start without an existing custom build for this name + branch.
uv run saorsa-deploy build ant \
  --name "$NAME" \
  --branch-name {{ANT_BRANCH}} \
  --repo-owner {{ANT_REPO_OWNER}} \
  {{#if validation_string}}--validation-string "{{VALIDATION_STRING}}" \
  {{/if}}--ssh-key-path "$SSH_KEY"

{{/if}}# --- Deploy uploaders + paired monitoring ---
uv run saorsa-deploy prod uploaders deploy \
  --name "$NAME" \
  --purpose "{{PURPOSE}}" \
  {{#each uploader}}--uploader '{{region}}:{{vm_size}}:urls={{urls_path}}{{#if payment_override}}:payment={{payment}}{{/if}}' \
  {{/each}}{{BINARY_FLAGS_ANT}} \
  {{#if monitoring_volume_override}}--monitoring-volume-size {{MONITORING_VOLUME_SIZE}} \
  {{/if}}--ssh-key-path "$SSH_KEY"

echo ""
echo "Prod uploaders run $NAME has been deployed."
echo "Each uploader VM exits when its URL list is exhausted; teardown is via the destroy runbook."
```

`{{BINARY_FLAGS_ANT}}` expands to either:

- `--ant-version {{ANT_VERSION}}`, or
- `--branch-name {{ANT_BRANCH}} --repo-owner {{ANT_REPO_OWNER}}`

## Destroy runbook template

```bash
#!/usr/bin/env bash
# Runbook: prod uploaders destroy {{NAME}}
# Generated: {{TIMESTAMP_HUMAN}}

set -euo pipefail

NAME="{{NAME}}"
SSH_KEY="{{SSH_KEY_PATH}}"

cd "$(dirname "$0")/.."

uv run saorsa-deploy prod uploaders destroy \
  --name "$NAME" \
  {{#if keep_monitoring}}--keep-monitoring \
  {{/if}}--ssh-key-path "$SSH_KEY"

echo ""
echo "Prod uploaders run $NAME has been destroyed."
{{#if keep_monitoring}}
echo "Monitoring VM was kept; remember to tear it down separately when you're done querying."
{{/if}}
```

## Common pitfalls

- **Three-part uploader spec.** It's `REGION:VM_SIZE:urls=PATH` — not four parts. The deprecated `concurrency=N` token was removed; never emit it.
- **`--secret-key` on the command line is a footgun.** Always rely on `ARBITRUM_ONE_SECRET_KEY` and preflight-check it instead. Putting the secret in argv leaks it into shell history and `ps`.
- **URL list paths are read at deploy time, not runtime.** They must be readable on the host that runs the runbook. A common slip is putting them under a path that only exists on a target VM.
- **Don't confuse this with the testnet uploader.** Testnet `clients infra --uploader region:file_size_mb[:vm_size]` uses a synthetic file size; prod `--uploader REGION:VM_SIZE:urls=PATH` uploads real files from the URL list. Don't conflate the spec formats.
- **`--ant-version` and `--branch-name` are mutually exclusive.** They're separate flags in saorsa-deploy.
- **The branch path needs a `build ant` step first.** `prod uploaders deploy --branch-name X` does *not* build on the uploader VMs — it pulls a pre-built artifact from S3 keyed by `name + repo_owner + branch`, and refuses to start if the artifact is missing. Always emit a `saorsa-deploy build ant` step before the deploy when the user picked the branch path. The version path (`--ant-version`) needs no build step.
- **Monitoring is implicit.** Don't emit a separate `monitoring infra` / `monitoring provision` chain — `prod uploaders deploy` does it in one shot. The only knob exposed at this level is `--monitoring-volume-size`.
- **Destroy takes `--ssh-key-path`.** Unlike testnet destroy commands, the prod destroy commands SSH into the monitoring VM (and others) for clean teardown, so the key is required.
- **Companion commands aren't in the runbook.** `prod print-upload-results` and `prod record-upload-results` run after uploads finish, at a time the runbook can't predict. Mention them in the closing message instead of baking them into the script.
- **Don't omit `--monitoring-volume-size` from preflight if user overrode it.** If the user picked a non-default value, emit the flag exactly once on the deploy command — the destroy command does not take this flag.
- **Don't add retry loops or sleeps.** If a command fails, the user wants to see it and decide what to do — `set -e` is the contract.
