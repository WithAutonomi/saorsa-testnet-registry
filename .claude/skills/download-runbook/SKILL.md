---
name: download-runbook
description: Interview the user to gather requirements for a production continuous-downloader run and then generate a self-contained bash runbook that wraps `saorsa-deploy prod continuous-downloader deploy` (and the matching destroy). Use this skill whenever the user asks to "plan a prod download run", "compose a continuous-downloader runbook", "generate a prod download script", "script a continuous-downloader deployment", or otherwise wants the saorsa-deploy command sequence for a production-mainnet continuous download laid out as an executable script — even if they don't say the word "runbook". Prefer this skill over hand-rolling the command yourself; the interview catches the easily-forgotten bits (single-VM constraint, addresses-file override, monitoring volume sizing for long runs).
---

# download-runbook

Generate an executable bash runbook that runs a production continuous-downloader deployment end-to-end via `saorsa-deploy prod continuous-downloader deploy`, plus a matching destroy runbook, based on a short interview.

## Why this skill exists

`prod continuous-downloader deploy` is a single high-level command, but every run still requires a few decisions that are easy to forget:

- It's **single-VM by design** — exactly one `--spec region[:vm_size]`, no list, no provider mix. The download service then loops indefinitely against a fixed list of public mainnet addresses.
- The address list defaults to a curated file shipped inside the deploy package; the user may want to override it with their own list (e.g. to test a specific corpus).
- A paired monitoring stack is created in-flight; for runs longer than a day, the default 50 GB volume needs bumping.
- The companion command `deployments record-results --name X --monitoring-name X --window -1h` is the standard way to roll the InfluxDB measurements into Postgres for after-the-fact analysis, but it runs on the user's own cadence, not from this runbook.

Rather than re-deriving the spec every time, this skill interviews the user once and emits a runbook that bakes the whole plan in.

## Output contract

Two files are generated per run, both placed in `runbooks/`:

- `runbooks/<NAME>-<YYYYMMDD-HHMMSS>-prod-continuous-downloader-deploy.sh`
- `runbooks/<NAME>-<YYYYMMDD-HHMMSS>-prod-continuous-downloader-destroy.sh`

The timestamp suffix is computed at **generation time**, not runtime. The same name gets reused across runs and the user wants a linear record of attempts.

Both files are executable (`chmod +x`). Neither is committed — if `runbooks/` isn't in `.gitignore` already, remind the user.

Both scripts start with `set -euo pipefail` so the first failing command halts execution. Do not add `|| true` or swallow errors anywhere.

## The interview

Ask questions **in the order below**. Ask them one at a time so the user can answer conversationally, but it's fine to batch tightly related sub-questions in a single turn. After each answer, echo back your understanding in one short line before moving on.

If the user gives you a partial spec upfront, extract what you can, confirm it, and only ask about the gaps.

### 1. Deployment name

Required on every command. Used to name both output files. Conventional form is `PROD-DL-NN` (e.g. `PROD-DL-01`, `PROD-DL-02`). The same name is also used for the paired monitoring stack created in-flight, so anything reasonable for both works.

### 2. `ant` (client) binary

Pick one of:

- **Version** (GitHub release, e.g. `0.3.0`) → emits `--ant-version X`, or
- **Branch + repo owner** → emits `--branch-name X --repo-owner Y`. This builds from source on the downloader VM at provisioning time.

These are mutually exclusive. Branch and version flags must not both be set. There is no separate `build ant` step for prod downloads — the binary is fetched / built directly during `prod continuous-downloader deploy`.

### 3. Single VM spec

`prod continuous-downloader deploy` takes exactly one `--spec` of the form `region[:vm_size]`. No multi-VM mode, no provider mix. Collect:

- **Region** — a DigitalOcean region slug (`lon1`, `nyc1`, `ams3`, `sfo3`, `sgp1`, `blr1`, `fra1`, `tor1`). Downloader VMs are DO-only.
- **VM size** — DO droplet slug. Sensible default: `s-2vcpu-4gb`. Bigger sizes generally don't help unless the run is bandwidth-bound.

Only emit `:vm_size` in the spec when the user picks a non-default. If they accept the default, just emit the bare region (e.g. `--spec lon1`).

### 4. Address list

The download service loops over a fixed list of public mainnet addresses. The default list is the one shipped in `deploy/saorsa_deploy/resources/prod_addresses.txt`.

Ask: "Use the shipped default address list, or a custom file?"

- **Default**: omit `--addresses-file` entirely.
- **Custom**: collect the **absolute path** to a local newline-separated file of addresses. The path must be readable on the host running the runbook because saorsa-deploy slurps the file at deploy time and embeds it into provisioning.

If the user picks custom, preflight-check the file's readability in the runbook.

### 5. Monitoring volume size

`prod continuous-downloader deploy` always provisions a paired monitoring VM (InfluxDB + Grafana, always in `lon1`). The default attached volume is **50 GB**, which suffices for runs of a few hours.

Ask once: "Default 50 GB monitoring volume, or larger?"

- For runs expected to stretch beyond a day, suggest `100`–`250` GB. Continuous downloads emit one Influx record per file per cycle, so the measurement table grows quickly on long runs.
- Only emit `--monitoring-volume-size N` when the user picks a value other than 50.

### 6. SSH key path

Ask for the **absolute path to the SSH private key** used for provisioning (e.g. `/home/chris/.ssh/ant_deploy`). The public half must be registered with DigitalOcean — if it isn't, Terraform fails before any VM is created.

- Default is `~/.ssh/id_rsa`; the user will almost always want to override this with a project-specific key, so always ask.
- Emit the path as a top-level `SSH_KEY="..."` variable in the runbook and pass `--ssh-key-path "$SSH_KEY"` to **both** the deploy and the destroy commands. (Unlike testnet destroy, prod destroy does take `--ssh-key-path`.)

### 7. Destroy behaviour

Default destroy tears down both the downloader VM and the paired monitoring VM. Ask: "When you destroy, keep the monitoring VM alive afterwards (so you can keep querying recorded metrics) or tear it down too?"

- **Tear down** (default): no flag.
- **Keep monitoring**: emit `--keep-monitoring` on the destroy command. Note in the recap that the user is then responsible for cleaning up the monitoring VM later.

## Generation rules

Once the interview is done, before writing the files:

1. **Recap the plan** back to the user as a bullet list, including the spec, the address-list choice, and any volume override. Let them correct anything. Do not generate files until they say go.
2. **Compute the timestamp once** (shell: `date +%Y%m%d-%H%M%S`) and use it in both filenames.
3. **Create `runbooks/` if it doesn't exist.** Use `mkdir -p runbooks`.
4. **Write both files**, then `chmod +x` them.
5. **Do not run the generated script.** Just report the two paths and tell the user what to do next.
6. **If `runbooks/` is not already in `.gitignore`**, tell the user — don't edit `.gitignore` yourself.
7. **Mention the companion command** in the closing message (not in the script): periodic `uv run saorsa-deploy deployments record-results --name $NAME --monitoring-name $NAME --window -1h` will upsert recent download aggregates into Postgres for after-the-fact analysis. The cadence is the user's call.

### Flag rules

- Always include `--name "$NAME"` on every command.
- Binary selection is mutually exclusive: either `--ant-version X` or `--branch-name Y --repo-owner Z`. Never both.
- Only emit `--addresses-file PATH` when the user picked a custom list.
- Only emit `--monitoring-volume-size N` when the user picked a non-default value.
- Only emit `--keep-monitoring` on destroy when the user explicitly asked for it.
- Spec is a single string; no repeatable form. Refuse to generate a runbook for more than one VM.

## Deploy runbook template

Use this structure. Fill the `{{...}}` placeholders with interview answers. Omit blocks wholesale when a feature is not selected — don't emit dead `if false; then ... fi` blocks.

```bash
#!/usr/bin/env bash
# Runbook: prod continuous-downloader deploy {{NAME}}
# Generated: {{TIMESTAMP_HUMAN}}
# Spec:
#   Downloader:  1 VM in {{REGION}}{{#if vm_size_overridden}} ({{VM_SIZE}}){{/if}}
#   Addresses:   {{ADDRESSES_DESC}}
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
{{#if custom_addresses}}
if [[ ! -r "{{ADDRESSES_FILE}}" ]]; then
  echo "ERROR: addresses file not readable: {{ADDRESSES_FILE}}" >&2
  exit 1
fi
{{/if}}

# --- Deploy continuous downloader + paired monitoring ---
uv run saorsa-deploy prod continuous-downloader deploy \
  --name "$NAME" \
  --spec '{{SPEC_STRING}}' \
  {{#if custom_addresses}}--addresses-file "{{ADDRESSES_FILE}}" \
  {{/if}}{{BINARY_FLAGS_ANT}} \
  {{#if monitoring_volume_override}}--monitoring-volume-size {{MONITORING_VOLUME_SIZE}} \
  {{/if}}--ssh-key-path "$SSH_KEY"

echo ""
echo "Prod continuous-downloader run $NAME has been deployed."
echo "The download service will loop until the runbook's destroy script is invoked."
```

`{{BINARY_FLAGS_ANT}}` expands to either:

- `--ant-version {{ANT_VERSION}}`, or
- `--branch-name {{ANT_BRANCH}} --repo-owner {{ANT_REPO_OWNER}}`

`{{SPEC_STRING}}` is either `{{REGION}}` or `{{REGION}}:{{VM_SIZE}}` depending on whether the VM size was overridden.

## Destroy runbook template

```bash
#!/usr/bin/env bash
# Runbook: prod continuous-downloader destroy {{NAME}}
# Generated: {{TIMESTAMP_HUMAN}}

set -euo pipefail

NAME="{{NAME}}"
SSH_KEY="{{SSH_KEY_PATH}}"

cd "$(dirname "$0")/.."

uv run saorsa-deploy prod continuous-downloader destroy \
  --name "$NAME" \
  {{#if keep_monitoring}}--keep-monitoring \
  {{/if}}--ssh-key-path "$SSH_KEY"

echo ""
echo "Prod continuous-downloader run $NAME has been destroyed."
{{#if keep_monitoring}}
echo "Monitoring VM was kept; remember to tear it down separately when you're done querying."
{{/if}}
```

## Common pitfalls

- **Single VM only.** `--spec` is not repeatable and there's no provider mix. If the user wants multiple downloader VMs, this skill is the wrong tool — the prod command itself doesn't support that shape.
- **Don't emit `--addresses-file` when using the default.** The CLI defaults to `resources/prod_addresses.txt`; emitting the same path explicitly adds noise without changing behaviour. Only pass the flag when the user picked a custom list.
- **Custom addresses paths are read at deploy time, not runtime.** The path must be readable on the host that runs the runbook. A common slip is putting it under a path that only exists on a target VM.
- **`--ant-version` and `--branch-name` are mutually exclusive.** They're separate flags in saorsa-deploy.
- **Monitoring is implicit.** Don't emit a separate `monitoring infra` / `monitoring provision` chain — `prod continuous-downloader deploy` does it in one shot. The only knob exposed at this level is `--monitoring-volume-size`.
- **Destroy takes `--ssh-key-path`.** Unlike testnet destroy commands, the prod destroy commands SSH into VMs for clean teardown, so the key is required.
- **The companion command isn't in the runbook.** `deployments record-results --name X --monitoring-name X --window -1h` runs at the user's chosen cadence (the `--window` slot maps neatly to a cron job). Mention it in the closing message; don't bake it into the script.
- **Don't add retry loops or sleeps.** If a command fails, the user wants to see it and decide what to do — `set -e` is the contract.
- **Don't confuse this with the testnet downloader.** Testnet `clients infra --downloader region[:vm_size]` is repeatable and pulls addresses uploaders just published. Prod `--spec region[:vm_size]` is single-VM and consumes a fixed mainnet address list. Different shapes; don't conflate them.
