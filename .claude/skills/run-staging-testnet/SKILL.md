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
| Train date | Issue title, `Release Train — <date>` | `2026-07-28` |
| `ant-node` RC version | `## Cut record` → versions table, `ant-node` row, **after** the arrow | `0.16.0-rc.1` |
| `ant` RC version | Same table, **`ant-cli`** row, after the arrow | `0.3.2-rc.1` |
| Previous `ant-node` release | Same table, `ant-node` row, **before** the arrow | `0.15.0` |
| Risks to watch | `## Risks to watch in staging/canaries` | — |

Notes:

- **The table has no Old/New columns.** Its columns are `Crate | Version |
  rc-branch head`, and the old and new versions share the single `Version` cell
  as `0.15.0 → **0.16.0-rc.1**`. Read both sides of the arrow out of that one
  cell: before it is the previous release, after it is the RC.
- `ant-cli` is the crate; `ant` is the binary. The `ant-cli` version goes into the
  manifest's `testnet.binaries.ant`, and also names the tag the WASM client is
  built from (`testnet.binaries.ant_wasm`, `ant-cli-v<version>` — Step 3).
- The Cut record table is authoritative over the earlier "Final versions" table in
  the manifest section — that one lists the *planned* release versions (`0.16.0`),
  not the RC tags, and can be superseded at cut time. Read the Cut record.
- The previous `ant-node` release (before the arrow) is the binary the
  mixed-version phase runs.
- If there is no `## Cut record` section, the cut has not happened. Stop.
- **A crate with no row in the versions table had no PRs on this train.** A
  single-crate RC is normal (e.g. an ant-node-only train leaves `ant-cli` out
  entirely). That crate has no RC, so the run uses its **current shipped
  release** — confirm which that is with
  `gh release list --repo WithAutonomi/<repo> --limit 5`. Do **not** carry
  forward the `-rc.N` pin already sitting in the manifest: it belongs to the
  previous train and has almost certainly shipped since, so it is a superseded
  prerelease.

Then list the train's sub-issues (`list_issues` with `parentId`) and note the
`Staging run — rc.N` one. Move it to **In Progress** now — the run occupies it
for the next five hours or so, and leaving it in Todo makes a train that is
actively running look untouched. You will update it again at the end.

**Stop here and confirm the versions with the user before continuing.** Report the
`ant-node` RC, the `ant` version, the WASM client tag (`ant-cli-v<that same ant
version>`), and the previous `ant-node` release, and wait for an answer. Do not edit a manifest or deploy anything until you have one.

This is the one hard stop in the runbook. If the versions are wrong the whole
five-hour run is invalid — it costs real money, it is not comparable to the
baseline, and nothing downstream detects it. Everything else here is a standard,
well-known configuration that runs unattended; the versions are the only input
read out of prose, and they change every train.

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

Edit these four keys and nothing else:

- `testnet.binaries.ant_node.version` → the `ant-node` RC version.
- `testnet.binaries.ant.version` → the `ant-cli` RC version.
- `testnet.binaries.ant_wasm` → `branch: ant-cli-v<version>` and
  `repo_owner: WithAutonomi`.
- `purpose` → `"Release Train <date>: staging run"`.

For `ant_wasm`, `<version>` is the **same** version as `testnet.binaries.ant`: the
ant-cli RC from the Cut record when the train has one, otherwise the current shipped
ant-cli release, resolved and confirmed at Step 1 by the same rule. Never carry
forward an old `-rc` tag. Two things about the key that differ from `ant`:

- `branch:` takes a **tag** as well as a branch — `ant-cli-v0.3.8-rc.1` is a valid
  value. The WASM client is built from source; no WASM package is published as a
  release, so there is no `version:` form.
- `repo_owner: WithAutonomi` is **required**. The tool defaults to `maidsafe`, which
  does not carry the tag, and the build fails there.

Result for the 2026-07-21 train:

```yaml
extends: ../../profiles/staging.yaml

name: STG-01
purpose: "Release Train 2026-07-21: staging run"
slack_post: true

testnet:
  binaries:
    ant_node:
      version: 0.15.0-rc.1
    ant:
      version: 0.3.1-rc.1
    ant_wasm:
      branch: ant-cli-v0.3.1-rc.1
      repo_owner: WithAutonomi
```

Leave `extends`, `name` and `slack_post` alone. The profile
(`deploy/manifests/profiles/staging.yaml`) holds the network shape and is not
edited here — a change to it is a re-baselining event and needs the RM.

Record the profile version. Comparisons are only valid within one profile version:

```bash
git log -1 --format=%h -- deploy/manifests/profiles/staging.yaml
```

If the profile file has uncommitted changes, stop. The run would not be attributable
to a profile version and could not be compared to a baseline.

Validate the edit:

```bash
uv run saorsa-deploy deployments config validate --config deploy/manifests/runs/staging/STG-01.yaml
```

Once the versions are confirmed at Step 1, the rest of the run is unattended. Do not
stop to show the plan or ask for approval — the network shape is a standard,
well-known configuration and the profile is not edited here. This applies to the
plan and config only; it does not license skipping the Step 1 version confirmation.

## Step 3b — Gas preflight (blocking)

Uploader wallets are funded with a **fixed** amount of native ETH, so their runway is
inversely proportional to the gas price. Check that the profile's `eth:` figures will
survive a full run at *today's* price, before spending five hours finding out:

```bash
uv run saorsa-deploy clients estimate-gas \
  --config deploy/manifests/runs/staging/STG-01.yaml \
  --baseline-id <previous STG-01 run id>
```

`--baseline-id` is the **previous STG-01 run**, whatever profile it ran on. This is
funding sizing, not gating, so the previous run is a valid input even across the
staging rebaseline — for the first run on `staging.yaml` it is #579. (Contrast the
verification, where runs before the rebaseline are never a baseline.)

It reads the current base fee from the chain, takes each uploader's throughput from
the baseline run, and prints the ETH each one needs plus whether the funding wallet
can cover the total. It sizes `wasm_uploaders` too, so **`[wasm]` rows are
expected**. A WASM row labelled `approx: native twin <host>` was sized from the
matching native uploader's throughput because the baseline has no WASM records; that
is normal until a baseline run with WASM records exists, not a fault. **Exit 2 means something is short** — either an uploader's `eth:`
or the funding wallet itself.

On exit 2, stop and hand back to the RM with the command's output. Two different
fixes, and neither is yours to make unilaterally:

- **An uploader is under-funded.** The command prints the exact `eth:` value for each
  uploader, in manifest order. Raising them edits `deploy/manifests/profiles/staging.yaml`,
  which changes the profile SHA — a re-baselining event that needs the RM, exactly as
  a shape change would.
- **The funding wallet is short.** No profile edit helps; the wallet needs topping up
  before the run. Top-up is per-wallet and sequential, so an under-funded funder does
  **not** fail cleanly up front — it funds the first few uploaders and then runs dry,
  leaving a half-provisioned run.

Do not proceed on a exit 2 by reducing the run's scope or by hoping. This exists
because staging run #566 lost six of ten uploaders from ~T0+3h57m: whole-run upload
success came out at 96.02% and the mixed-version phase at 79.35%, both below their
hard-gate floors, and the verification could not close as a result. Nothing warned
beforehand, and mid-run the failure looks exactly like a network fault.

Two notes on reading the output:

- The estimate uses each baseline client's **rate over its own active window**, not
  its raw upload count. A client that exhausted its wallet mid-run stopped early, so
  its raw count understates a funded client's work — sizing from it would reproduce
  the shortfall. The command says which clients it corrected.
- It sizes against the base fee **right now**. That fee moved 0.025 → 0.050 → 0.124 →
  0.437 gwei on four consecutive days around this feature's calibration, so an
  estimate taken a day early can already be stale. Run it immediately before the
  deploy, not the evening before.

## Step 4 — Deploy

```bash
uv run saorsa-deploy deployments up --config deploy/manifests/runs/staging/STG-01.yaml
```

The last step is `clients start`. When it completes, record the wall-clock time.
That is **T0**, and every phase is measured from it.

### When a deploy step fails

**The WASM client build** (`build ant-wasm`, step 1 of the plan). The WASM client is
built from the `ant_wasm` tag on a build VM, before any testnet infrastructure is
created. It is the only check that the WASM harness still compiles against the RC's
browser API — there is no local pre-check, deliberately: building Rust locally is
slow and takes over the operator's machine. If it fails, `up` halts there with
nothing to tear down. Do not retry and do not work around it: post the failing
output on the `Staging run — rc.N` sub-issue and hand back to the RM. A harness that
has drifted from the RC's API is a harness fix.

For every other step there are two classes of failure, handled differently.

**Infrastructure creation** (`bootstraps infra`, `nodes infra`, `clients infra`,
`monitoring infra`, `elastic infra` — anything creating VMs or volumes at a cloud
provider). Read the failing lane's error and classify it; the class, not the step,
decides.

* **Terminal — the provider refused.** Capacity or "not available" / "out of stock",
  a quota or limit exceeded, a plan or size not offered in that region,
  401/403/"invalid token", billing, or the OVH ~60-minute silent hang in `nodes
  infra` (the per-region 40-vCPU quota). These last a while. Do **not** retry:
  1. Stop the run.
  2. Clear whatever was created:
     ```bash
     uv run saorsa-deploy deployments down --name STG-01
     ```
  3. Report to the user: which provider and step failed, and the error.
* **Transient — the provider's API misbehaved on a call that normally works.** A 5xx,
  a 429, a timeout or connection reset, a TLS error, or a 404 / "Invalid instance-id"
  on the **read-back** of a resource that was just created (Vultr does this; it ended
  DEV-01 run 613 for nothing). Wait 3 minutes, confirm no other `up` or `down` is
  alive for `STG-01`, then retry:
  ```bash
  uv run saorsa-deploy deployments up --config deploy/manifests/runs/staging/STG-01.yaml --resume
  ```
  The journal records a parallel group only when every lane succeeded, so the resume
  re-runs the **whole** infrastructure group. That is safe: `bootstraps infra` reuses
  the bootstrap VMs already in state, `nodes infra` persists its regions before
  `terraform apply` so a re-apply reconciles the half-created region rather than
  duplicating it, `elastic infra` / `monitoring infra` reuse a same-name stack, and
  `clients infra` matches its specs. The builds are their own journaled group and are
  not repeated. Up to **three** resume attempts for the infrastructure group in
  total; a fourth failure is terminal, and so is the same lane failing with a
  different error class → treat as terminal above.
* **Never pass `--force`** to `up` or to any `infra` command: it wipes state and leaves
  orphans. If a resume attempt itself errors with "Terraform state exists … orphaned …
  Use --force", that is not retryable: terminal.
* Record every attempt — time, lane, error, wait — in the sub-issue's results comment.

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
- **Native** upload success below 99%, or native download success below 99.5%.

**Report the WASM tier separately.** The profile runs WASM (browser-path,
WebRTC-Direct) uploaders and downloaders alongside the native `ant` clients. In each
status line give WASM upload and download success/failure counts as their own figures,
never folded into the native ones. `print-uploads` / `print-downloads` list both;
tell WASM hosts apart by the role in the hostname (`…-wasm-upload…` /
`…-wasm-download…`). After Step 7's extraction the same split is available from
Postgres by `client_kind`.

- **WASM failures are recorded, never a reason to halt the run.** WASM floors are
  advisory: NAT'd nodes are unreachable over WebRTC-Direct until relayed WebRTC
  exists, so a WASM success rate below the native floors is expected, not a hard-gate
  event.
- A WASM downloader failure with `error_kind=crash` is the browser process dying
  mid-operation — an **OOM**. Call it out explicitly in the status line, with the
  host and time, rather than counting it as an ordinary failure.

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
  version **before the arrow** in the Cut record table's `ant-node` row).
- `purpose` → `"Release Train <date>: mixed-version phase, ant-node <previous release>."`

Leave everything else alone. The extension's nodes carry `nat_percent: 10`, matching
the `staging.yaml` main fleet (it was 30 before the staging rebaseline). In
particular the node distribution is deliberate:

- **No OVH.** `staging` already runs ~36 OVH VMs and OVH enforces a per-region
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

1. **Transfers meet the main-phase success floors while the mixed fleet is live** —
   upload success **>99%**, download success **>99.5%**, measured on the delta during
   the phase, not the run total. Check every ~15 minutes with `print-uploads` /
   `print-downloads` on **STG-01** (the clients live there), and confirm the final
   figures against the per-op Postgres records at extraction — `print-uploads`
   truncates its output, so a single failing row can be absent from it entirely.

   This was **zero failed transfers** until the 2026-08-24 train, and that bar was
   wrong: an upload or download can fail for reasons that have nothing to do with
   running mixed versions, so an absolute zero made the gate fire on unrelated
   noise. It did exactly that on run #544, where one upload of 866 failed 12
   minutes into the phase and every peer the client could not reach was running the
   **new** version — no older-version node was involved at all. Same floors as the
   main phase, so the phase is judged by the standard the run is already held to.

   **Compute this criterion separately per transport**, from the Postgres per-op
   records (`deployment_upload_records` / `deployment_download_records`) after
   Step 7's `record-results`, taking ops whose `finished_at` falls inside the phase
   window:

   ```sql
   SELECT 'upload' AS op, coalesce(client_kind, 'native') AS kind,
          count(*) FILTER (WHERE status = 'success') AS ok, count(*) AS total
   FROM deployment_upload_records
   WHERE deployment_id = <STG-01 id> AND finished_at >= '<phase start>' AND finished_at < '<phase end>'
   GROUP BY 2
   UNION ALL
   SELECT 'download', coalesce(client_kind, 'native'),
          count(*) FILTER (WHERE status = 'success'), count(*)
   FROM deployment_download_records
   WHERE deployment_id = <STG-01 id> AND finished_at >= '<phase start>' AND finished_at < '<phase end>'
   GROUP BY 2;
   ```

   `client_kind` NULL or `native` is the native tier; `wasm` is the WASM tier.

   - The **native** figures decide pass/fail, against the floors above, exactly as
     before.
   - The **WASM** figures are reported as their own row, marked **advisory**. They
     never fail the phase. Where the logs allow it, attribute WASM failures in this
     phase to old-version peers (ant-node 0.19.x and earlier have no WebRTC-Direct
     listener, so a browser client cannot reach them at all) or to NAT'd peers.

   **A failure is still worth explaining, not just counting.** If any transfer
   fails, establish whether an older-version node was involved before writing it
   off: the client's forwarded logs name the peers it could not reach, and the
   deployment inventories say which fleet each IP belongs to. That attribution is
   what distinguishes a compatibility problem from a coincidence, and it must be
   done while the run's Elasticsearch is alive.
2. **Previous-version nodes are storing chunks.** Check on the extension fleet. No
   CLI reports record-store counts yet, so:

   ```bash
   uv run saorsa-deploy deployments generate-inventory --name STG-02
   ```

   The inventory's node VM IPs are under `node_vms[].ip` (`ssh_user` is `root`,
   key `~/.ssh/ant_deploy`).

   Chunks are stored in **LMDB**, not in a `record_store/` directory:

   ```
   /mnt/ant-node-storage/data/ant-node-<N>/chunks.mdb      # the chunk store
   /mnt/ant-node-storage/data/ant-node-<N>/paid_list.mdb   # paid-store records
   ```

   There is **no `record_store/` directory** — an earlier version of this skill said
   to count files under `/mnt/ant-node-storage/*/record_store`, which matches nothing
   and returns 0 on every host. That zero is indistinguishable from "the
   previous-version nodes are storing nothing", i.e. it looks exactly like the
   hard-gate failure this criterion exists to catch. Do not use that path.

   Instead measure the store's **allocated size**, per host, at phase start and again
   at phase end:

   ```bash
   ssh -i ~/.ssh/ant_deploy root@<ip> \
     'du -cb /mnt/ant-node-storage/data/*/chunks.mdb    | tail -1 | cut -f1
      du -cb /mnt/ant-node-storage/data/*/paid_list.mdb | tail -1 | cut -f1'
   ```

   The criterion is that the size **grew on every host**. Two caveats:

   - LMDB can preallocate, so a single absolute number proves nothing. Always take
     two spaced measurements and require growth. (Take the first ~15 min after
     `nodes start` — at the exact phase start the fleet is legitimately at zero.)
   - `paid_list.mdb` growing alongside `chunks.mdb` is the stronger evidence: it
     shows the nodes are accepting *paid* stores rather than only inbound
     replication. Report both.

   There is no metric path for this: `mdb_stat` is not installed on the VMs, and an
   extension deployment's own InfluxDB carries host metrics only
   (cpu/disk/diskio/mem/net/procstat) — no `prometheus` measurement, so no app-level
   record count. SSH is the only option today.

   This is the one step outside the CLI — flag it in the report. If you cannot verify
   it, that is a failure, not a pass.

## Step 7 — Extraction (blocking)

Run this once, at the end of the run, covering the whole run. No extraction, no
teardown. The testnet is scratch; Postgres is the record.

**`record-results --name STG-01` runs before any `clients destroy`. This ordering is
not negotiable.** Each op's `client_kind` (native vs WASM) is resolved from the client
state that `clients destroy` deletes; record after it and the transports can no longer
be told apart.

```bash
uv run saorsa-deploy deployments record-results    --name STG-01
uv run saorsa-deploy deployments record-analysis   --name STG-01 \
  --summary "ant-node <RC>, ant <RC>. Profile staging @ <SHA>. T0=<ts>; steady <ts>..<ts>; mixed <ts>..<ts>. Mixed-phase criteria: <pass/fail + evidence>."
uv run saorsa-deploy deployments record-log-counts --name STG-01
```

`record-results` picks the **pointer** operations up in the same pass when the run
carried pointer clients — they ride an ordinary testnet, so there is no separate
command to remember. Check what landed before moving on:

```bash
uv run saorsa-deploy deployments print-pointers --id <id>
uv run saorsa-deploy deployments print-pointers --id <id> --by-age
```

A **non-zero stale-read rate is EXPECTED** (ADR-0016: an eclipsed reader can be
served an older, validly signed state and cannot tell). Report the figure; do not
report it as a fault. The `--by-age` table is the one to read for decay: a stale
or not-found rate that climbs with pointer age is the finding this tier exists to
produce.

### The mixed-version phase is recorded under its own label

Run this as part of the same extraction, once the mixed-version hour has ended:

```bash
uv run saorsa-deploy deployments derive-metrics --id <id> \
  --window-start <mixed start> --window-end <mixed end> --label mixed-version
uv run saorsa-deploy deployments record-log-counts --id <id> \
  --window-start <mixed start> --window-end <mixed end> --label mixed-version
uv run saorsa-deploy deployments print-pointers --id <id> \
  --window-start <mixed start> --window-end <mixed end>
```

The mixed-version hour is where a **pre-pointer node answers a protocol error to
every pointer message**, so pointer operations against it are expected to degrade.
Recording the phase under its own label is what makes that degradation a MEASURED
figure rather than something averaged into the steady-state window and discarded.
It is a third label alongside `full-run` and `steady-state`, not a replacement for
either — the comparison looks every stored measurement up under the label it is
comparing on.

STG-01 only. STG-02 has no clients of its own — the transfers all run from STG-01,
so there is nothing to record for it.

### `record-log-counts`

Counts the catalogue's log strings (`resources/log_patterns/connection-errors.yaml`:
the connection, dial and identity-announce failures, the pointer and subtree-audit
groups, and from v5 the **payment-RPC failures**) in STG-01's Elasticsearch and
stores the counts in `deployment_log_event_counts`. The verification then gates on
them — all but the payment-RPC group, which is **advisory**: it measures whether the
public Arbitrum Sepolia RPC was rate-limiting the nodes' payment verification, which
is the weather the run happened in rather than a property of the release, and the
verification reads the upload-duration gates against it (`evaluate-staging-run`,
**Payment RPC health**).

Run it here, in the same extraction step, and **before any teardown**. It reads
the deployment's own Elasticsearch, which is per-run scratch: once that instance
is destroyed the logs are gone and there is no backfill. This is the same
constraint as `record-analysis` against InfluxDB, and it is the reason Step 8
keeps both stacks alive.

It refuses rather than recording zeros if the index turns out to be empty — a
zero from an empty index is indistinguishable from a real absence and would read
as a total elimination of every connection error on the next comparison. If that
happens, check that log forwarding was actually enabled for the run and say so;
do not work around it.

Read the table it prints before moving on:

- A **zero count** is not self-explaining. "Fixed", "renamed" and "emitted at
  DEBUG, which the node forwarder drops by default" all look identical from
  Elasticsearch. Note it in the Step 9 report as an observation, not a
  conclusion.
- The **Hosts** column matters as much as the count. A large count spread over
  two hosts of seventy is usually a service restart, not a fleet-wide change.

It also counts ant-node's `pointer_put_rpc` / `pointer_get_rpc` events per
outcome with latency p50/p95 whenever the run had pointer clients, and stores them
in the same table keyed `<event>:<outcome>`. Read `outcome=error` as a fraction of
the event total. That table is the NODE's view: `stale` there is a node refusing a
write that lost the merge, which is a different event from a client being served a
stale state — that one is in `deployments print-pointers`.

**Record BOTH labels. This is two invocations, not one.** The bare command lands
its counts under `full-run`, which is everything the index holds. Then record the
steady-state slice as well:

```bash
uv run saorsa-deploy deployments record-log-counts --name STG-01 \
  --window-start <steady-state start> --window-end <steady-state end> \
  --label steady-state
```

Both are needed because `deployments compare` looks the counts up under **the same
window label it is comparing on**, and the baseline carries both. Record only
`full-run` and a steady-state comparison reports every connection-error gate as NOT
ASSESSED; record only `steady-state` and a full-run comparison does the same. There
is no backfill — the run's Elasticsearch is destroyed at teardown — so a label you
skip here is a gate that cannot be assessed for this train. This was skipped on the
2026-08-24 train because the slice was written up as optional; the verification had
to fall back to a full-run comparison to assess those gates at all.

### Payment RPC health: pull the per-hour shape while Elasticsearch is alive

The counts above give the verification a total and a per-hour rate. They do not
give it the **shape** — whether the rate limiting ran all day or hit one hour, and
whether it was fleet-wide — and by the time anyone asks, the index may be gone. So
pull it here, in the same extraction step, and paste it into the Step 9 report.
URL and API key from `uv run saorsa-deploy elastic info --name STG-01`; use T0 and
the run end for the range:

```bash
curl -s -H "Authorization: ApiKey $ES_KEY" -H 'Content-Type: application/json' \
  "$ES_URL/telegraf-ant-nodes-stg-01-*/_search" -d '{
  "size": 0,
  "query": {"bool": {"must": [{"match_phrase": {"tail.message": "HTTP error 429"}}],
            "filter": [{"range": {"@timestamp": {"gte": "<T0>", "lt": "<run end>"}}}]}},
  "aggs": {"per_hour": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1h"}},
           "hosts": {"terms": {"field": "tag.host.keyword", "size": 40}}}}'
```

Report the per-hour `doc_count` series and the number of hosts in the `hosts`
buckets. Then the client side, which the node-tier catalogue cannot see: the same
query against `telegraf-ant-clients-stg-01-*` with the phrase `payment failed`,
and beside it the uploaders' total `Failed to store chunk on` count, so the
verification can say what fraction of the clients' store failures were payment
rejections. A fleet-wide series in the thousands per hour is the public endpoint
rate-limiting the fleet; the verification treats the upload-duration gates as
confounded when it is well above the baseline's. Do not interpret it further here —
record it.

Each label has a distinct weakness, which is why both are recorded rather than one
being chosen:

- **`full-run`** covers everything the index holds, so it is unaffected by clock
  skew — but it spans the mixed-version phase and any pre-measurement period.
- **`steady-state`** isolates the measurement phase, but the command warns that a
  time-filtered count is applied against each host's own deliberately-skewed clock.

Do not pass `--window`; it is a different flag and is not how the label is set.

**`record-results` can take several minutes.** Give it a timeout of at least 10
minutes (600000 ms). A short timeout will kill it mid-write and look like a failure.

Verify, and only continue if rows are actually present:

```bash
uv run saorsa-deploy deployments ls        # find this run's STG-01 id
uv run saorsa-deploy deployments print-recorded-results --id <id>
```

This command takes **`--id`, not `--name`** — it is the one command in this runbook
that does, and `--name` exits 2 with a usage error rather than printing anything.

That is the right interface here anyway: the id is what proves the rows belong to
**this** run. Take the newest `STG-01` id from `deployments ls` and check it is
greater than the id you noted in Step 2 — a reused deployment name once made
`record-analysis` a silent exit-0 no-op and lost a whole run's metrics.

Check the rows, not the exit code. The header echoes the id back
(`Recorded results — STG-01 (id=<id>, kind=testnet)`); confirm it matches, and that
the Analysis summary is the one you just wrote rather than a previous run's.

If extraction fails or returns nothing, stop. Leave the testnet up and hand back to
the RM. The data is still in InfluxDB while the monitoring stack is alive, and gone
once it is destroyed.

## Step 8 — Teardown (compute only — leave the observability stack up)

**Do not use `deployments down`.** It tears down everything including Elasticsearch
and InfluxDB, and those are the only places the run's logs and metrics exist. The
staging *verification* happens later, usually in a separate session; if it turns up
something odd — a gate that moved unexpectedly, a figure that looks wrong — the
evidence has to still be there to look at. Postgres holds the extracted aggregates,
not the raw logs, and nothing can be recovered once the instances are gone.

So destroy the compute tiers individually and keep the observability tiers:

`record-results` for STG-01 must already have landed (Step 7) — `clients destroy`
below deletes the state `client_kind` is resolved from.

```bash
# STG-02 first — bootstraps destroy refuses while a dependent extension exists,
# and --force does not override that.
uv run saorsa-deploy nodes destroy --name STG-02
uv run saorsa-deploy deployments unextend --name STG-02 --force

# then STG-01's compute tiers
uv run saorsa-deploy clients destroy --name STG-01
uv run saorsa-deploy nodes destroy --name STG-01
uv run saorsa-deploy bootstraps destroy --name STG-01
```

Run these one at a time. OVH credentials must still be in the environment.

**Deliberately NOT destroyed:** STG-01's `elastic` and `monitoring` instances, plus
STG-02's monitoring. They stay up for the verification.

Because `deployments down` is what flips the registry row, the STG-01 row will not
read `destroyed` after this. That is expected — the deployment genuinely is not fully
destroyed yet. Verify the compute tiers are gone by their own output rather than by
the status column.

### Handing over the surviving stack

These VMs bill by the hour, so say plainly in the Step 9 report that they are still
running, name them, and give the commands that finish the job once the verification
is recorded:

```bash
uv run saorsa-deploy monitoring destroy --name STG-02
uv run saorsa-deploy elastic destroy --name STG-01
uv run saorsa-deploy monitoring destroy --name STG-01
```

An ES instance on the staging profile is an `s-8vcpu-16gb` with a 500GB volume — not
something to leave running for a week because nobody was told it was there.

## Step 9 — Update Linear

Update the train's `Staging run — rc.N` sub-issue with:

- The registry run row: the `STG-01` deployment id from `deployments ls`.
- The RC versions and the profile SHA.
- The connection-error counts recorded at Step 7, per group, with the host
  spread. Flag any zero count as needing a source check rather than reporting it
  as an improvement.
- **Payment RPC health**: the `Payment RPC failures (node)` count under each
  label, the per-hour node-side `HTTP error 429` series and host count from Step
  7, and the client-side `payment failed` count against the uploaders' total
  `Failed to store chunk on`. Figures only; the verification does the reading.
- T0 and each phase boundary timestamp.
- Transfer results as **separate native and WASM rows** (uploads and downloads,
  success/total and rate for each), plus any WASM `error_kind=crash` (OOM) events.
  The WASM rows are marked advisory.
- Mixed-version phase: PASS or FAIL against both criteria, with the evidence,
  plus the **mixed-phase WASM transfer row** (advisory) and its failure attribution.
- Whether this run is the **first `staging.yaml` run** — the new-baseline run. It is
  when no earlier STG-01 run's `record-analysis` summary says `Profile staging @
  <SHA>`. Say so explicitly either way; the verification needs it.
- Any hard-gate observation during steady state, or "none observed".
- Any deviation from the standard phase durations.
- **Which observability instances are still running**, and the commands to destroy
  them once the verification is recorded (Step 8).

Then move the sub-issue to Done. Ask first if any criterion is unresolved.

## Known gaps

State these in the final report rather than working around them:

- No `profile_version` field. The profile SHA goes in `purpose` as free text.
- No single extraction command. The process describes one script writing the
  canonical metric set (run id, RC tag, profile version) to Postgres. Today it is
  `record-results` (per-host file-op aggregates) plus `record-analysis` (per-host
  CPU/mem/net/disk-IO aggregates) plus `record-log-counts` (connection-error
  string counts), and none of them stores the RC tag or profile version as a
  column.
- **Connection-error counts cannot be backfilled**, so a gate is only assessable
  when both the candidate and the baseline recorded counts under the label being
  compared. Do not assume a given baseline carries a label — **check it**, because
  which runs carry what is a history of who followed this prose on the day, not a
  cutoff date:

  ```sql
  SELECT deployment_id, window_label, count(*) FROM deployment_log_event_counts
  WHERE deployment_id IN (<candidate>, <baseline>) GROUP BY 1, 2;
  ```

  Measured on the registry: #513 (2026-08-11) `full-run` only; #532 (2026-08-18)
  both; **#544 (2026-08-26) `full-run` only**; #566 (2026-09-01) both. Recording
  both labels only became mandatory in this skill on 2026-08-26 at 21:01 UTC —
  about three hours after #544's extraction that same afternoon — so #544 missed
  it and, its Elasticsearch being long destroyed, never can. A gate reading NOT
  ASSESSED for that reason is expected, not a failure of the run.
- No comparison or verdict tool. This skill stops at "run recorded".
- No CLI or metric for record-store counts, so the mixed-version chunk-storage
  criterion needs an SSH check. The chunk store is LMDB, so what is actually
  measured is `chunks.mdb` byte growth, not a record count — a node storing a few
  large chunks and one storing many small ones are indistinguishable.
- Content verification covers the NATIVE client tier only. Its uploaders publish each
  file's SHA-256 on the address line and its downloaders check what they got back, so
  `deployments compare` can assess the data-integrity hard gate from the recorded run
  (V2-757). The WASM tier publishes no hash (V2-1360), so its downloads carry no
  verdict and the gate's coverage figure is what says how much of the run was checked.
- Phase clocks are wall-clock and driven by you. Nothing in `saorsa-deploy` enforces
  the 1h/3h/1h structure or stamps the boundaries.
