---
name: run-staging-testnet
description: Run the release train's staging testnet end-to-end. Reads the current Release Train issue from Linear for the RC versions, updates the STG-01 manifest, deploys, drives the 1h warm-up / 3h steady-state / 1h mixed-version phases, extracts results to Postgres, tears down, and closes the "Staging run — rc.N" sub-issue. Use when asked to "run the staging testnet", "do the staging run", "run staging for rc.N", or "kick off the staging run".
---

# run-staging-testnet

Runs the staging testnet for the current release train.

Rules:

- Coordination only. Every action is a `saorsa-deploy` CLI command or a Linear update.
- Never tear down before the extraction to Postgres has landed.
- Do not add load, kill nodes, or otherwise improvise. The run must stay comparable
  to the baseline. Anything the profile does not do, it does not do on purpose.
- You do not issue the gate verdict. That is a separate step on Wednesday.

## Step 1 — Read the release train issue

Find the current train:

1. `list_issues` with `project: "Releases"`, `team: "v2.0"`, `state: "In Progress"`.
2. Expect exactly one issue titled `Release Train — YYYY-MM-DD`. If there are none
   or more than one, stop and ask.
3. `get_issue` on it for the full description.

Extract from the description:

| What | Where | Example |
|---|---|---|
| Train date | Issue title, `Release Train — <date>` | `2026-07-21` |
| `ant-node` RC version | `## Cut record` → Versions table, `ant-node` row, **New** column | `0.15.0-rc.1` |
| `ant` RC version | Same table, **`ant-cli`** row, **New** column | `0.3.1-rc.1` |
| Previous `ant-node` release | Same table, `ant-node` row, **Old** column | `0.14.4` |
| Risks to watch | `## Risks to watch in staging/canaries` | — |

Notes:

- `ant-cli` is the crate; `ant` is the binary. The `ant-cli` version goes into the
  manifest's `testnet.binaries.ant`.
- The Versions table is authoritative over the earlier "Final versions" bullets in
  the manifest section — those can be superseded at cut time. Read the table.
- The previous `ant-node` release (Old column) is the binary the mixed-version phase
  runs.
- If there is no `## Cut record` section, the cut has not happened. Stop.

Then list the train's sub-issues (`list_issues` with `parentId`) and note the
`Staging run — rc.N` one. You will update it at the end.

Report the extracted values back before continuing.

## Step 2 — Check preconditions

Stop and say which item failed if any of these is not met:

- `DATABASE_URL` is set and reaches the registry. Check with
  `uv run saorsa-deploy deployments ls`.
- Cloud credentials are present for every provider in the profile: `DO_TOKEN`,
  `VULTR_API_KEY`, and the four `OVH_*` vars. Also `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` for S3 state and `ANT_DEPLOY_SECRET_KEY` for client
  wallets. OVH credentials are needed at destroy time too.
- No other `up` or `down` is running for `STG-01`. They share S3 and Terraform
  state and will race.
- The previous `STG-01` and `STG-02` runs are destroyed. Check `deployments ls`, and
  note the id of the most recent existing `STG-01` row before you start.

The names are reused every train, so `deployments ls` will show rows from previous
runs. That has caused a whole run's metrics to be lost before — recording against a
reused name exited 0 and wrote nothing. Knowing the previous `STG-01` row id up
front is how you confirm at Step 7 that the new row is yours.

## Step 3 — Update the manifest

Always use this manifest, path relative to the repo root:

```
deploy/manifests/runs/staging/STG-01.yaml
```

Edit two things and nothing else:

- `testnet.binaries.ant_node.version` → the `ant-node` RC version.
- `testnet.binaries.ant.version` → the `ant-cli` RC version.
- `purpose` → `"Release Train <date>: staging run"`.

Result for the 2026-07-21 train:

```yaml
extends: ../../profiles/staging-short.yaml

name: STG-01
purpose: "Release Train 2026-07-21: staging run"
slack_post: true

testnet:
  binaries:
    ant_node:
      version: 0.15.0-rc.1
    ant:
      version: 0.3.1-rc.1
```

Leave `extends`, `name` and `slack_post` alone. The profile
(`deploy/manifests/profiles/staging-short.yaml`) holds the network shape and is not
edited here — a change to it is a re-baselining event and needs the RM.

Record the profile version. Comparisons are only valid within one profile version:

```bash
git log -1 --format=%h -- deploy/manifests/profiles/staging-short.yaml
```

If the profile file has uncommitted changes, stop. The run would not be attributable
to a profile version and could not be compared to a baseline.

Validate the edit:

```bash
uv run saorsa-deploy deployments config validate --config deploy/manifests/runs/staging/STG-01.yaml
```

The run is unattended. Do not stop to show the plan or ask for approval — this is a
standard, well-known configuration.

## Step 4 — Deploy

```bash
uv run saorsa-deploy deployments up --config deploy/manifests/runs/staging/STG-01.yaml
```

The last step is `clients start`. When it completes, record the wall-clock time.
That is **T0**, and every phase is measured from it.

### When a deploy step fails

There are two classes of failure and they are handled differently.

**Infrastructure creation** (`bootstraps infra`, `nodes infra`, `clients infra`,
`monitoring infra`, `elastic infra` — anything creating VMs or volumes at a cloud
provider). Do **not** retry. These failures are usually provider-side maintenance or
capacity problems and last a while.

1. Stop the run.
2. Clear whatever was created:
   ```bash
   uv run saorsa-deploy deployments down --name STG-01
   ```
3. Report to the user: which provider and step failed, and the error.

**Provisioning** (`nodes provision`, `provision-genesis`, `bootstraps provision`,
`clients provision`, `monitoring provision` — anything installing software over
SSH). These are usually transient SSH failures. Retry:

```bash
uv run saorsa-deploy deployments up --config deploy/manifests/runs/staging/STG-01.yaml --resume
```

`--resume` skips the already-completed steps using the journal, so a retry does not
repeat the binary builds. Without it a non-TTY journal hit refuses rather than
hanging.

Retry a few times. It usually passes. Occasionally a VM's SSH never becomes
available at all — this happens sometimes on OVH and Vultr. After about three
attempts with no progress, treat it as terminal: stop, `deployments down --name
STG-01`, and report which host was stuck.

One provisioning failure looks different: a node provision that hangs silently
rather than erroring is usually a stalled S3 binary download on a throttled VM. The
partial binary is then skipped by the idempotency check on retry, so it must be
deleted on that VM before `--resume` will help.

Do not work around infrastructure or permission problems in any other way.

## Step 5 — Phases

| Phase | Window | Notes |
|---|---|---|
| Warm-up | T0 → T0+1h | Not part of the measurement |
| Steady state | T0+1h → T0+4h | The measurement window |
| Mixed version | T0+4h → T0+5h | Pass/fail criteria only |

Record the wall-clock time of each boundary and report it. Results are extracted
once, for the whole run, at the end — the boundaries are recorded so the data can be
narrowed to a window in post-processing.

Use background waits for the phase clocks rather than tight polling. Report at each
boundary and at each check.

### Warm-up (T0 → T0+1h)

Check once at ~T0+30m and once at the boundary:

```bash
uv run saorsa-deploy deployments print-uploads   --name STG-01
uv run saorsa-deploy deployments print-downloads --name STG-01
```

Only looking for total breakage. If uploads are ~100% failing at the end of warm-up,
say so and ask the RM whether to extract and tear down early.

### Steady state (T0+1h → T0+4h)

Run the same two commands every ~30 minutes and report a one-line status each time.

Report immediately if any of these appears. They are hard gates, so early notice
matters:

- Any node panic, OOM, or unexplained restart. The deployment's Elasticsearch has
  the node logs; query it rather than SSH-ing around.
- Upload success below 99%, or download success below 99.5%.

Also watch whatever the train issue's "Risks to watch in staging/canaries" section
named.

Do not intervene to improve the numbers. No restarts, no re-uploads, no client
changes. A degraded steady state is a finding.

## Step 6 — Mixed-version phase (T0+4h → T0+5h)

Add ~100 nodes (~10%) running the previous `ant-node` release. This is a
`testnet-extension`: it creates no bootstraps and reuses STG-01's.

Always use this manifest:

```
deploy/manifests/runs/staging/STG-02.yaml
```

Do not create a manifest on the fly. Edit two things in the existing one:

- `extension.binaries.ant_node.version` → the previous `ant-node` release (the
  **Old** column from the Cut record table).
- `purpose` → `"Release Train <date>: mixed-version phase, ant-node <previous release>."`

Leave everything else alone. In particular the node distribution is deliberate:

- **No OVH.** `staging-short` already runs ~36 OVH VMs and OVH enforces a per-region
  40-vCPU quota. Exhausting it shows up as a ~60-minute silent hang in node infra,
  not an error.
- **No `upgrade_channel`.** Omitting it means the stable channel. At staging time
  the RC has not shipped, so stable is the previous release and the fleet stays put.
  `beta` would upgrade the previous-version half and defeat the phase.

Deploy it directly. No validate, no plan.

```bash
uv run saorsa-deploy deployments up --config deploy/manifests/runs/staging/STG-02.yaml
```

STG-01 must be fully up first, and nothing else may touch STG-01 while this runs —
they share S3 and Terraform state.

The sequence is `deployments extend` → `nodes infra` → `nodes provision` →
`nodes start`. Failures are handled exactly as in Step 4, except that the teardown
on a terminal failure is `deployments down --name STG-02`. Start the 1h clock when
`nodes start` completes.

### Phase criteria

Both must hold. A failure here is a hard gate.

1. **Zero failed uploads or downloads while the mixed fleet is live.** Check every
   ~15 minutes with `print-uploads` / `print-downloads` on **STG-01** (the clients
   live there). Compare against the counts at phase start — the criterion is on the
   delta during the phase, not the run total.
2. **Previous-version nodes are storing chunks.** Check on the extension fleet. No
   CLI reports record-store counts yet, so:

   ```bash
   uv run saorsa-deploy deployments generate-inventory --name STG-02
   ```

   Then, for each host in the inventory, count records under
   `/mnt/ant-node-storage/*/record_store` at phase start and again at phase end. The
   criterion is that the count grew. This is the one step outside the CLI — flag it
   in the report. If you cannot verify it, that is a failure, not a pass.

## Step 7 — Extraction (blocking)

Run this once, at the end of the run, covering the whole run. No extraction, no
teardown. The testnet is scratch; Postgres is the record.

```bash
uv run saorsa-deploy deployments record-results  --name STG-01
uv run saorsa-deploy deployments record-analysis --name STG-01 \
  --summary "ant-node <RC>, ant <RC>. Profile staging-short @ <SHA>. T0=<ts>; steady <ts>..<ts>; mixed <ts>..<ts>. Mixed-phase criteria: <pass/fail + evidence>."
```

STG-01 only. STG-02 has no clients of its own — the transfers all run from STG-01,
so there is nothing to record for it.

Do not pass `--window`. The defaults cover the whole run, which is what we want —
narrowing to a phase is done later in post-processing, using the boundary timestamps
recorded in the summary.

**`record-results` can take several minutes.** Give it a timeout of at least 10
minutes (600000 ms). A short timeout will kill it mid-write and look like a failure.

Verify, and only continue if rows are actually present:

```bash
uv run saorsa-deploy deployments print-recorded-results --name STG-01
```

Check the rows, not the exit code, and check that they belong to **this** run — a
reused deployment name once made `record-analysis` a silent exit-0 no-op and lost a
whole run's metrics. Compare the deployment ids against the ones you noted in
Step 2.

If extraction fails or returns nothing, stop. Leave the testnet up and hand back to
the RM. The data is still in InfluxDB while the monitoring stack is alive, and gone
once it is destroyed.

## Step 8 — Teardown

Order matters. `bootstraps destroy` refuses while a dependent extension exists, and
`--force` does not override that.

```bash
uv run saorsa-deploy deployments down --config deploy/manifests/runs/staging/STG-02.yaml
uv run saorsa-deploy deployments down --config deploy/manifests/runs/staging/STG-01.yaml
```

Run these one at a time. OVH credentials must still be in the environment. Confirm
both show `destroyed` in `deployments ls` before reporting done.

## Step 9 — Update Linear

Update the train's `Staging run — rc.N` sub-issue with:

- The registry run row: the `STG-01` deployment id from `deployments ls`.
- The RC versions and the profile SHA.
- T0 and each phase boundary timestamp.
- Mixed-version phase: PASS or FAIL against both criteria, with the evidence.
- Any hard-gate observation during steady state, or "none observed".
- Any deviation from the standard phase durations.

Then move the sub-issue to Done. Ask first if any criterion is unresolved.

## Known gaps

State these in the final report rather than working around them:

- No `profile_version` field. The profile SHA goes in `purpose` as free text.
- No single extraction command. The process describes one script writing the
  canonical metric set (run id, RC tag, profile version) to Postgres. Today it is
  `record-results` (per-host file-op aggregates) plus `record-analysis` (per-host
  CPU/mem/net/disk-IO aggregates), and neither stores the RC tag or profile version
  as a column.
- No comparison or verdict tool. This skill stops at "run recorded".
- No CLI for record-store counts, so the mixed-version chunk-storage criterion needs
  an SSH check.
- No content verification on downloads. The process lists data integrity as a hard
  gate, but there is nothing to check it with, so the run does not cover it.
- Phase clocks are wall-clock and driven by you. Nothing in `saorsa-deploy` enforces
  the 1h/3h/1h structure or stamps the boundaries.
