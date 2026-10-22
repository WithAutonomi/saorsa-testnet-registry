---
name: run-three-hour-dev-testnet
description: Run any testnet for four hours — 1h warm-up then a 3h measurement window — and record the results to the registry Postgres so the run can be compared against another. Takes a deployment manifest from deploy/manifests/runs. Use when asked to "run a three hour testnet", "gather three hours of data", "run a testnet for comparison", "do a comparison run", or "run a dev testnet and record the results".
---

# run-three-hour-dev-testnet

Runs a testnet for four hours to gather a comparable set of results: 1 hour of
warm-up, then a 3 hour measurement window. Deploys from a manifest the user
supplies, records the results, and tears down.

This is the general-purpose sibling of the release train's staging run, which
`compose-staging-run` in `~/dev/github.com/WithAutonomi/autonomi-release-ops`
composes as its own run issue. Same phase structure and same recording, but no
release train, no mixed-version phase, and no manifest editing — the manifest is
taken as given.

Rules:

- Coordination only. Every action is a `saorsa-deploy` CLI command.
- Never tear down before the extraction to Postgres has landed.
- Do not add load, kill nodes, restart services, or re-run failed transfers. The
  point of the run is comparability, and intervening destroys it.

## Step 1 — Get the manifest

Ask the user for the deployment manifest if they have not already given one. It must
be in `deploy/manifests/runs`, path relative to the current directory. Show them
what is there if it helps:

```bash
ls deploy/manifests/runs deploy/manifests/runs/staging
```

Read the manifest and pull out:

| What | Field |
|---|---|
| Deployment name | `name` |
| Kind | `kind` |
| `ant-node` version or branch | `testnet.binaries.ant_node` |
| `ant` version or branch | `testnet.binaries.ant` |
| Base profile, if any | `extends` |

If the manifest `extends` a profile, read that too — it holds the network shape.

Stop and say why if any of these is true:

- `kind` is not `testnet`. The prod kinds and `testnet-extension` do not have this
  phase structure and are not what this skill runs.
- There are no clients (`testnet.clients.uploaders` / `.downloaders`). With no
  uploads or downloads there is nothing to measure.
- `testnet.monitoring.enabled` is `false`. Monitoring is where the metrics come
  from; without it the run records nothing. It defaults on, so this only bites if
  the manifest explicitly disables it.

Report the name and binary versions back before continuing.

**For the comparison to mean anything, the two runs must have the same network
shape.** Only the binaries should differ. If the user is comparing against an
earlier run, say which profile or manifest this one uses so they can confirm it
matches. The profiles ship inside the saorsa-deploy wheel, so if the manifest
extends one (`extends: profile:<name>`) record the tool version and the profile's
own last commit:

```bash
saorsa-deploy --version
git log -1 --format=%h -- deploy/saorsa_deploy/resources/profiles/<profile>.yaml
```

## Step 2 — Check preconditions

Stop and say which item failed if any of these is not met:

- `DATABASE_URL` is set and reaches the registry. Check with
  `uv run saorsa-deploy deployments ls`.
- Cloud credentials are present for every provider the manifest uses:
  `DO_TOKEN` (DigitalOcean), `VULTR_API_KEY` (Vultr), `HCLOUD_TOKEN` (Hetzner), the
  four `OVH_*` vars (OVH), the four `CNTB_OAUTH2_*` vars (Contabo). Also
  `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` for S3 state and
  `ANT_DEPLOY_SECRET_KEY` for client wallets. OVH and Contabo credentials are needed
  at destroy time too, not just at create time.
- No other `up` or `down` is running for this deployment name. They share S3 and
  Terraform state and will race.
- Any previous run under this name is destroyed. Check `deployments ls`, and note
  the id of the most recent existing row for the name before you start.

If the name has been used before, `deployments ls` will show the older rows.
Recording against a reused name has silently written nothing and lost a whole run's
metrics before. Knowing the previous row id up front is how you confirm at Step 5
that the new row is yours.

## Step 3 — Deploy

Validate first, then deploy:

```bash
uv run saorsa-deploy deployments config validate --config <manifest>
uv run saorsa-deploy deployments up --config <manifest>
```

The last step is `clients start`. When it completes, record the wall-clock time.
That is **T0**, and both phases are measured from it.

### When a deploy step fails

There are two classes of failure and they are handled differently.

**Infrastructure creation** (`bootstraps infra`, `nodes infra`, `clients infra`,
`monitoring infra`, `elastic infra` — anything creating VMs or volumes at a cloud
provider). Do **not** retry. These failures are usually provider-side maintenance or
capacity problems and last a while.

1. Stop the run.
2. Clear whatever was created:
   ```bash
   uv run saorsa-deploy deployments down --name <name>
   ```
3. Report to the user: which provider and step failed, and the error.

**Provisioning** (`nodes provision`, `provision-genesis`, `bootstraps provision`,
`clients provision`, `monitoring provision` — anything installing software over
SSH). These are usually transient SSH failures. Retry:

```bash
uv run saorsa-deploy deployments up --config <manifest> --resume
```

`--resume` skips the already-completed steps using the journal, so a retry does not
repeat the binary builds. Without it a non-TTY journal hit refuses rather than
hanging.

Retry a few times. It usually passes. Occasionally a VM's SSH never becomes
available at all — this happens sometimes on OVH and Vultr. After about three
attempts with no progress, treat it as terminal: stop, `deployments down --name
<name>`, and report which host was stuck.

One provisioning failure looks different: a node provision that hangs silently
rather than erroring is usually a stalled S3 binary download on a throttled VM. The
partial binary is then skipped by the idempotency check on retry, so it must be
deleted on that VM before `--resume` will help.

Do not work around infrastructure or permission problems in any other way.

## Step 4 — Phases

| Phase | Window | Notes |
|---|---|---|
| Warm-up | T0 → T0+1h | Not part of the measurement |
| Measurement | T0+1h → T0+4h | The three hours being compared |

Record the wall-clock time of each boundary and report it. Results are extracted
once, for the whole run, at the end — the boundaries are recorded so the data can be
narrowed to the measurement window in post-processing.

Use background waits for the phase clocks rather than tight polling. Report at each
boundary and at each check.

### Warm-up (T0 → T0+1h)

Check once at ~T0+30m and once at the boundary:

```bash
uv run saorsa-deploy deployments print-uploads   --name <name>
uv run saorsa-deploy deployments print-downloads --name <name>
```

Only looking for total breakage. If uploads are ~100% failing at the end of warm-up,
say so and ask the user whether to extract and tear down early rather than spend
three more hours on a dead run.

### Measurement (T0+1h → T0+4h)

Run the same two commands every ~30 minutes and report a one-line status each time.

Report immediately if either of these appears:

- Any node panic, OOM, or unexplained restart. If the manifest enables
  Elasticsearch, the node logs are there; query it rather than SSH-ing around.
- A large drop in upload or download success rate.

Report them, but keep the clock running. Do not intervene to improve the numbers —
no restarts, no re-uploads, no client changes. A degraded measurement window is a
finding, and doctoring it makes the run uncomparable.

## Step 5 — Extraction (blocking)

Run this once, at T0+4h, covering the whole run. No extraction, no teardown. The
testnet is scratch; Postgres is the record.

```bash
uv run saorsa-deploy deployments record-results  --name <name>
uv run saorsa-deploy deployments record-analysis --name <name> \
  --summary "<purpose>. ant-node <version>, ant <version>. Profile <profile> @ <SHA>. T0=<ts>; measurement <ts>..<ts>."
```

Do not pass `--window`. The defaults cover the whole run, which is what we want —
narrowing to the measurement window is done later in post-processing, using the
boundary timestamps recorded in the summary.

**`record-results` can take several minutes.** Give it a timeout of at least 10
minutes (600000 ms). A short timeout will kill it mid-write and look like a failure.

Verify, and only continue if rows are actually present:

```bash
uv run saorsa-deploy deployments print-recorded-results --name <name>
```

Check the rows, not the exit code, and check that they belong to **this** run.
Compare the deployment id against the one you noted in Step 2.

If extraction fails or returns nothing, stop. Leave the testnet up and hand back to
the user. The data is still in InfluxDB while the monitoring stack is alive, and
gone once it is destroyed.

## Step 6 — Teardown

```bash
uv run saorsa-deploy deployments down --config <manifest>
```

OVH and Contabo credentials must still be in the environment. Confirm the
deployment shows `destroyed` in `deployments ls` before reporting done.

## Step 7 — Report

Give the user:

- The deployment name and registry row id.
- The binary versions, and the profile version if the manifest extends one.
- T0 and both phase boundary timestamps, so the measurement window can be isolated.
- Anything observed during the run: node crashes, success-rate drops, or "none
  observed".
- Any deviation from the 1h/3h structure.

If they named a run to compare against, also give them that run's name so both are
in one place. This skill does not do the comparison itself.

## Known gaps

State these in the report rather than working around them:

- No comparison tool. There is nothing that reads two runs out of Postgres and
  diffs them; this skill stops at "run recorded".
- No `profile_version` field. The profile SHA goes in the summary as free text.
- No single extraction command — `record-results` covers per-host file-op
  aggregates, `record-analysis` covers per-host CPU/mem/net/disk-IO aggregates, and
  neither stores the binary versions or profile version as a column.
- Content verification covers the NATIVE client tier only: its downloads are checked
  against the SHA-256 their uploader published and the verdict is recorded per op
  (V2-757). The WASM tier publishes no hash (V2-1360), so its downloads carry none.
- Phase clocks are wall-clock and driven by you. Nothing in `saorsa-deploy` enforces
  the 1h/3h structure or stamps the boundaries.
