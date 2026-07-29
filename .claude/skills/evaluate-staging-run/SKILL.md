---
name: evaluate-staging-run
description: Produce the release train's staging verification by comparing the rc.N staging run against the baseline run of the last shipped release. Runs the deterministic `saorsa-deploy deployments compare` command for every metric gate, collects the four observational hard gates the tool cannot see (data integrity, crashes, mixed-version chunk storage, canaries), emits a fixed-format report, posts it on the "Staging verification — rc.N" sub-issue and closes it. Use when asked to "evaluate the staging run", "verify the staging run", "run the staging comparison", "compare staging against the baseline", "do the staging verification for rc.N", or "close Staging verification".
---

# evaluate-staging-run

Produces the **staging verification** for the current release train: the judgment-and-record
counterpart to the staging *run* (the `run-staging-testnet` skill, which deploys the testnet,
drives the phases, extracts to Postgres and tears down — a separate sub-issue, usually a separate
session). This skill reads what that run recorded, compares it against a baseline, and writes the
verification.

Rules:

- **The numbers come from the tool, never from you.** Every metric gate verdict is produced by
  `saorsa-deploy deployments compare`, which is deterministic: same inputs, identical report. Do
  not recompute a delta by hand, do not round a figure differently, and do not overrule a verdict.
  If a number looks wrong, say so in the report and hand back — do not adjust it.
- **You supply only the four observational hard gates.** Data integrity, crashes, mixed-version
  chunk storage and the production canaries are not derivable from anything the registry stores.
  Those are yours to gather and state, with evidence. Everything else is the tool's.
- **You do not issue the gate verdict.** That is the `gate-verdict` skill in the releases repo,
  which *consumes* this report alongside the canary verification and the client production tests.
  This skill stops at "staging verification recorded".
- **Hard-gate FAIL never auto-closes.** On any hard-gate failure, post the report and hand back to
  the RM with the sub-issue left In Progress. Only an all-hard-gates-PASS run moves
  `Staging verification — rc.N` to Done.
- **Read-only.** Every action is a `saorsa-deploy` command, a database read, or a Linear update.

## What the command does and does not cover

`deployments compare --id <candidate> --baseline-id <baseline>` evaluates:

| | Gate | Source |
|---|---|---|
| **hard** | Upload success floor (< 99%) | `deployment_upload_records` |
| **hard** | Download success floor (< 99.5%) | `deployment_download_records` |
| soft | Upload / download success rate vs baseline | per-op records |
| soft | Transfer duration p50 / p95, **per file-size bucket** | per-op records (true percentiles) |
| soft | Node CPU | host metric buckets / rollup |
| soft | Node RSS (p95 across services) | per-service `rss_bytes` |
| soft | Memory leak (median RSS slope, final 2h) | RSS buckets, fitted per service |
| soft | Outbound bytes per GB of traffic | `deployment_derived_metrics` |
| soft | Disk bytes per GB stored | `deployment_derived_metrics` |

It does **not** cover the following. The command's report says nothing about them at all, so it is
**your** report's Observational hard gates table (Step 7) that must account for each one — silence
there would read as a pass:

- **Data integrity** — no content verification exists anywhere in the system. There is nothing to
  query. Unless the run did something bespoke, this is NOT ASSESSED and should be stated as a
  known gap, not as a pass.
- **Crashes / panics / OOM / unexplained restarts** — panics reach stderr and journald, not the
  JSON logs Telegraf forwards, and the run's Elasticsearch is destroyed at teardown. If the
  staging run session did not capture this while the testnet was up, it cannot be recovered.
- **Mixed-version phase** — zero failed transfers while the mixed fleet was live is derivable from
  the per-op records if you know the phase boundary; "previous-version nodes demonstrably storing
  chunks" is an SSH check on `chunks.mdb` growth and must come from the run session.
- **Canaries** — a separate production deployment, assessed by the `evaluate-canary` skill.
  Reference its verification; do not re-derive it here.

## Step 1 — Read the release train issue

1. `list_issues` with `project: "Releases"`, `team: "v2.0"`, `state: "In Progress"`.
2. Expect exactly one issue titled `Release Train — YYYY-MM-DD`. If there are none or more than
   one, stop and ask.
3. `get_issue` on it for the full description.

Extract the `ant-node` RC version and the previous release from the `## Cut record` versions
table. **That table has no Old/New columns** — the old and new versions share one `Version` cell
as `0.15.0 → **0.16.0-rc.1**`. The value *after* the arrow is the RC; the value *before* it is the
last shipped release, which is what the baseline run must correspond to.

Then list the train's sub-issues (`list_issues` with `parentId`) and find
`Staging verification — rc.N`. Move it to **In Progress** now.

Also read the `Staging run — rc.N` sub-issue. The run session records the registry deployment id,
the phase boundary timestamps, and the mixed-version observations there — you need all three.

## Step 2 — Identify the two runs

Both runs are named by **deployment id**, never by name. Staging names are reused every train, so
a name resolves to whichever run was most recent — the failure mode that once made
`record-analysis` a silent no-op.

```bash
uv run saorsa-deploy deployments ls
```

- **Candidate**: the `STG-01` deployment for this train. Take the id from the `Staging run — rc.N`
  sub-issue; confirm the date matches this train.
- **Baseline**: by the process, *the staging run the last shipped release passed on*. Not the
  previous rc.N of this same train — comparing against the last shipped release is what stops
  several "small" regressions inside one train chaining into a large unremarked one.

**Confirm both ids with the user before running anything.** Nothing in the registry records which
run a release shipped on, so the baseline is a judgment you must have confirmed. State the two
candidates you believe are right, with their dates and names, and ask.

Sanity-check that both have data before comparing:

```bash
uv run saorsa-deploy deployments print-recorded-results --id <id>
```

A run with no per-op records or no derived metrics will produce a report full of NOT ASSESSED.
That is a finding about the run, not about the release — say so plainly if it happens.

## Step 3 — Choose the window

**Prefer the steady-state phase. `full-run` is the fallback, not the default choice.**

A `full-run` window is the span of each run's observed *metrics*, not the shape of the run. It
opens when the first host started reporting — which the deliberate −3h node clock skew drags well
before T0 — and closes when the last one stopped, sweeping in the mixed-version extension the
baseline never ran. Measured on STG-01 #474 against #470: the candidate window carried 3h05m of
pre-run data plus the mixed-version hour, and **both comparability guards still passed** (length
1.13x, coverage 1.12x) because they check size and idleness, not phase composition. The command
warns whenever `--window-label full-run` is used, for exactly this reason.

The difference is not cosmetic. On that pair, moving to steady-state changed node CPU from +19.28%
to +15.29%, disk bytes/GB from −2.35% to −1.07%, and brought both runs' absolute disk ratio to
~8.7 GB/GB — much closer to the network's physical replication factor than the full-run figures.

Re-derive the phase on **both** runs, with the same label, **using `--id`**:

```bash
uv run saorsa-deploy deployments derive-metrics --id 474 \
  --window-start 2026-07-28T20:14:12 --window-end 2026-07-28T23:14:12 --label steady-state
uv run saorsa-deploy deployments derive-metrics --id 470 \
  --window-start 2026-07-25T17:00:55 --window-end 2026-07-25T20:00:55 --label steady-state
```

`--name` resolves to the most recent run bearing it, so it cannot address a baseline of a reused
name. Take each run's T0 and phase boundaries from its `Staging run — rc.N` sub-issue.

Two rules the command enforces and you should not try to work around:

- **Both sides must carry the same `window_label` and the same `source`.** A whole-run capture sums
  raw counter deltas and is exact; a re-derived window integrates bucketed averages and is
  approximate. Comparing one against the other measures the method, not the release.
- **Window lengths must be within 1.25x.** Replication continues after an upload completes, so a
  shorter window truncates the disk numerator while still counting the full denominator.

## Step 4 — Gather the accepted-run history (optional)

The 2σ noise criterion needs roughly five prior **accepted** runs per metric. Which runs count as
accepted is a judgment call — nothing in the registry records it — so it is yours, not the tool's.
The tool does only the arithmetic.

Find candidates (same profile, has derived metrics, completed cleanly), **list them for the user
with their dates and ask for confirmation**, then pass the confirmed ids:

```bash
uv run saorsa-deploy deployments compare --id <candidate> --baseline-id <baseline> \
  --history 462,464,468,470,472
```

Do not include the candidate or the baseline — the command refuses that, because including either
makes the variance band partly a function of the run being judged. Below the configured minimum
the percentage threshold stands alone and the report says so; that is a perfectly normal outcome
and not something to work around.

## Step 5 — Run the comparison

```bash
uv run saorsa-deploy deployments compare \
  --id <candidate> --baseline-id <baseline> \
  --window-label full-run \
  --markdown-out /tmp/staging-verification-rc<N>.md
```

The verdict is upserted to `deployment_gate_verdicts` keyed on
`(candidate, baseline, window_label)`, so re-running replaces its previous answer rather than
leaving two. Pass `--no-persist` only for an exploratory run.

Read the output for:

- **Warnings.** A `full-run` warning means the windows may differ in phase composition — re-run on
  a labelled phase if you can. An activity-coverage warning means one run's window was materially
  idler than the other's, which inflates the per-GB ratios on the idler side. Quote either in the
  report; do not quietly drop the affected gates.
- **`[mean]` markers.** The process spec names node CPU *median* and node RSS *p50*. Runs recorded
  before percentile capture landed have no percentile and the tool substitutes a sample-weighted
  mean, marked in the report. Say which statistic was used. `[p50~]` means a windowed estimate
  rather than an exact whole-run percentile.
- **NOT ASSESSED rows.** Each names why. These are not passes and must survive into your report
  with their reasons intact.

## Step 6 — Gather the four observational hard gates

For each, state PASS / FAIL / NOT ASSESSED **with its evidence or with the reason no evidence
exists**. Do not infer a pass from the absence of a report.

| Gate | Where it comes from |
|---|---|
| Data integrity | Nothing verifies downloaded content. Expect NOT ASSESSED and say why. |
| Crashes | The `Staging run — rc.N` sub-issue's steady-state observations. If the run session did not check `systemctl --state=failed` while the testnet was up, it cannot be recovered — ES died at teardown. NOT ASSESSED, not PASS. |
| Mixed-version phase | The run sub-issue records both criteria: zero failed transfers while the mixed fleet was live, and previous-version nodes storing chunks (`chunks.mdb` growth over SSH). |
| Canaries | The `Canary verification` sub-issue, produced by `evaluate-canary`. Reference it; do not re-derive. |

Note that `deployment_upload_records` / `deployment_download_records` carry per-op `finished_at`,
so if the run sub-issue records the mixed-version phase boundary you can confirm the zero-failed
-transfers half of that gate directly from Postgres rather than taking it on trust.

## Step 7 — Write the verification

Post as a comment on `Staging verification — rc.N`, in this order:

```markdown
# Staging verification — rc.N

**Runs compared**: STG-01 #<candidate> (rc.N, <date>) against STG-01 #<baseline> (<last shipped
release>, <date>)
**Window**: <label>
**Threshold config**: v<N>

## Metric gates (deterministic — `deployments compare`)

<the command's markdown report, unedited>

## Observational hard gates (not covered by the tool)

| Gate | Verdict | Evidence |
|---|---|---|
| Data integrity | NOT ASSESSED | No content verification exists in the system. |
| Crashes | … | … |
| Mixed-version phase | … | … |
| Canaries | … | Canary verification sub-issue <link> |

## Summary

<counts> — <what blocks the ship, if anything>
```

Paste the tool's report **unedited**. Reformatting it defeats the point of a fixed-format
deterministic report: two trains' reports must be diffable.

On a **soft-gate FAIL**, state the override path requirements — RM written justification, one
engineer co-sign, `gate-override` label, and an auto-created P1 debt issue linked as blocking the
next train's cut — but **do not make the decision and do not write the Decisions section**. That
is the RM's, recorded by the `gate-verdict` skill.

## Step 8 — Close, or hand back

- All hard gates PASS → move `Staging verification — rc.N` to **Done**.
- Any hard gate FAIL, or any hard gate you could not assess → leave it **In Progress**, post the
  report, and hand back to the RM naming exactly what is unresolved.

## Known gaps

State these in the final report rather than working around them:

- **No content verification** anywhere in the system, so data integrity has no mechanism.
- **Panics never reach Elasticsearch** — they go to stderr and journald, and ES dies at teardown.
  A crash check must happen during the run or not at all.
- **Percentiles cannot be backfilled.** Any run recorded before percentile capture landed compares
  CPU and RSS as a sample-weighted mean, marked `[mean]`.
- **Disk occupancy is absent from every run recorded before 2026-07-25**, so
  `disk_bytes_per_gb_stored` is NOT ASSESSED against an older baseline. No backfill is possible.
- **Nothing records which staging run a release shipped on**, so the baseline is chosen by the
  operator and confirmed by hand every time.
- **The memory-leak threshold is calibrated against the noise floor, not against a known leak.**
  It was set from four runs, none of which is known to contain one. A PASS there means "no
  fleet-wide RSS trend", not "no leak".
