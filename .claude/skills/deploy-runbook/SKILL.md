---
name: deploy-runbook
description: Interview the user to gather testnet requirements and then generate a declarative YAML deployment manifest (for `saorsa-deploy up`/`plan`), not a bash script. Use this skill whenever the user asks to "plan a testnet run", "compose a runbook", "generate a deploy script/manifest", "script a testnet deployment", or otherwise wants the configuration for a specific testnet spec laid out as a runnable artifact — even if they say "runbook". Prefer this skill over hand-rolling a manifest; the interview catches the provider-distribution heuristics, the ES large-testnet sizing envelope, and the clients-need-both-roles rule.
---

# deploy-runbook

Generate a declarative **YAML manifest** that deploys a saorsa testnet end-to-end via
`saorsa-deploy up`, based on a short interview.

> **This skill no longer emits bash.** The command ordering and flag-coupling rules
> that used to live here as prose now live in the `saorsa-deploy` orchestrator
> (`deploy/saorsa_deploy/orchestrator/`). You produce a manifest; the orchestrator
> produces the correct command sequence. Run `saorsa-deploy deployments plan --config
> <file>` to preview that sequence and `saorsa-deploy deployments up --config <file>` to
> execute it. The old bash runbooks under `runbooks/` still work but are no longer the
> recommended path.

## Output contract

Write **one** manifest to the git-ignored `deploy/manifests/runs/<NAME>.yaml` (e.g.
`deploy/manifests/runs/STG-05.yaml`) — concrete run manifests are not committed (the
committed profiles live in `deploy/manifests/profiles/`). No separate destroy file —
`saorsa-deploy deployments down --config <file>` derives the teardown sequence from the
same manifest.

If a suitable base profile already exists under `deploy/manifests/profiles/` (e.g.
`staging.yaml`), prefer `extends:` it and override only what changes
(name, purpose, binaries). That is the whole point — reuse the shape, edit the run.

After writing, **do not run it**. Validate it (`saorsa-deploy deployments config validate
--config <file>`), show the user the path, and suggest they run `deployments plan` to
preview.

## The interview

Ask in this order; batch tightly-related sub-questions. If the user gives a partial
spec upfront, extract what you can, confirm, and only ask about gaps. Each answer maps
to a manifest field — see the schema in `deploy/README.md` §"Declarative manifests".

1. **Name** — `name:` (conventional `UPPERCASE-NN`, e.g. `DEV-01`).
2. **Purpose** — `purpose:` (free text; required; surfaced by `deployments slack-post`).
3. **Binaries** — `testnet.binaries.ant_node` and `.ant`. Each is `{ version: X }`
   (a release) **or** `{ branch: Y, repo_owner: Z, validation: W }` (a branch build).
   The orchestrator emits the `build ant-node`/`build ant` step automatically for
   branch builds. Skip `ant` if the user wants no clients.
4. **Bootstraps** — `testnet.bootstraps.count` (≥1). The orchestrator emits
   `bootstraps provision` only when count > 1; you don't manage that.
5. **Nodes** — `testnet.nodes.per_vm` (services per VM) and `.providers`. Ask for the
   total non-bootstrap node count, then **propose a distribution**:
   - Hetzner is capacity-constrained — keep it ~2–4 VMs (0 for small totals).
   - DigitalOcean is the default primary (~50–60%).
   - Vultr fills the rest (~25–35%).
   - **`vm_count` is PER REGION**: provider total = `vm_count × len(regions_list)`
     (or `× regions`). Show the user the math. Omit a provider entirely to not use it.
   - `size:` per provider maps to the right flag (DO droplet-size / Vultr plan /
     Hetzner server-type). Defaults if the user shrugs: DO `s-2vcpu-4gb`,
     Vultr `vc2-2c-4gb`, Hetzner `cpx22`.
6. **Testnet mode / upgrade channel / NAT** — `testnet.mode: true|false`,
   `testnet.upgrade_channel: beta|stable` (omit for default),
   `testnet.nodes.nat_percent: N` (omit/0 to disable).
7. **Clients** — `testnet.clients`. If none, omit the block. Otherwise collect **≥1
   uploader AND ≥1 downloader** (validation enforces both):
   - uploader: `{ provider, region, file_size_mb, vm_size?, payment?(merkle|no-merkle), ant?, eth? }`
   - downloader: `{ provider, region, vm_size? }`
   Requires `ANT_DEPLOY_SECRET_KEY` + `ANT_CLIENT_AWS_*` in the env at `up` time.
8. **Elasticsearch** — `testnet.elastic`. `{ enabled: true, droplet_size, volume_size,
   shards, retention_days, drop_log_levels, buffer_limit }`. **Large-testnet warning:**
   if total nodes ≥ 500, tell the user the defaults (`s-4vcpu-8gb` / 100 GB / 4 shards)
   will silently drop logs at scale, and recommend `s-8vcpu-16gb` / 500–1000 GB / 8
   shards. Only set fields that differ from defaults.
9. **Monitoring** — `testnet.monitoring.enabled: true|false`.
10. **SSH key** — `ssh_key_path:` (absolute path or `~/...`; public half registered with
    every provider in use).

## Reuse mechanisms (mention to the user)

- `extends: ../profiles/<base>.yaml` — deep-merge a base; **lists replace** (redefine
  `clients` to swap it wholesale). Path is relative to the run manifest in `runs/`.
- `saorsa-deploy deployments up --config <file> --set testnet.nodes.nat_percent=0` — one-off override.
- `saorsa-deploy deployments up --config <file> --name STG-09` — reuse a manifest under a new name.

## Don'ts

- Don't emit bash, command sequences, or ordering rules — the orchestrator owns those.
- Don't put secrets in the manifest.
- Don't set fields that equal the CLI defaults — keep manifests tidy.
- Don't run `up` yourself; generate + validate, then hand off.

## For non-testnet kinds

For production runs, the manifest `kind:` selects the shape: `prod-uploaders`,
`prod-canary`, `prod-continuous-downloader`, `prod-downloaders`, `prod-local-uploads`,
`prod-local-downloads`. See `deploy/manifests/` for `PROD-UL-01.yaml`, `PROD-DL-01.yaml`,
`CAN-01.yaml` examples. The `upload-runbook` / `download-runbook` / `extend-runbook`
skills cover those flows; prefer producing a manifest there too.
