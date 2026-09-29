---
name: evaluate-staging-run
description: Produce the release train's staging verification by comparing the rc.N staging run against the previous train's accepted staging run (or, for the first run on the staging.yaml profile, establishing a new baseline). Runs the deterministic `saorsa-deploy deployments compare` command for every metric gate, collects the two observational hard gates the tool cannot see (crashes, mixed-version chunk storage) plus the advisory WASM production compat read-out, emits a fixed-format report, posts it on the "Staging verification — rc.N" sub-issue and closes it. Use when asked to "evaluate the staging run", "verify the staging run", "run the staging comparison", "compare staging against the baseline", "do the staging verification for rc.N", or "close Staging verification".
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
- **You supply only the three observational hard gates, plus one advisory read-out.** Data
  integrity, crashes and mixed-version chunk storage are not derivable from anything the registry
  stores. Those are yours to gather and state, with evidence. The one-line **WASM production compat**
  read-out is copied from the client production tests' recorded results and is advisory. Everything
  else is the tool's.
- **Canaries are not part of this report.** They are a separate production deployment with their
  own sub-issue, assessed by `evaluate-canary`, and they feed the gate verdict directly. Do not
  report, summarise or re-derive a canary result here, and do not let one change this report's
  verdict — a canary failure blocks the ship, but it is not a staging finding.
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
| **hard** | Upload success floor (< 99%) `[native]` | `deployment_upload_records` |
| **hard** | Download success floor (< 99.5%) `[native]` | `deployment_download_records` |
| advisory | Upload / download success floors `[wasm]` — rendered `… (advisory)`, never in the verdict | per-op records, `client_kind = 'wasm'` |
| soft | Upload / download success rate vs baseline, `[native]` and `[wasm]` | per-op records |
| soft | Transfer duration p50 / p95, **per file-size bucket**, `[native]` and `[wasm]` | per-op records (true percentiles) |
| soft | Node CPU | host metric buckets / rollup |
| soft | Node RSS (p95 across services) | per-service `rss_bytes` |
| soft | Memory leak (median RSS slope, final 2h) | RSS buckets, fitted per service |
| soft | Outbound bytes per GB of traffic | `deployment_derived_metrics` |
| soft | Disk bytes per GB stored | `deployment_derived_metrics` |
| soft | Connection errors, one gate per group | `deployment_log_event_counts` |
| advisory | Pointer write / read success floors `[pointer]` — rendered `… (advisory)`, never in the verdict | `deployment_pointer_records` |
| soft | Pointer stale-read rate, lost-write rate, write/read latency p50/p95, ANT cost per write — all `[pointer]`, all vs baseline | `deployment_pointer_records` |

Gate names carry a `[native]` or `[wasm]` suffix whenever either run has WASM records. The WASM
(browser-path, WebRTC-Direct) client is a test instrument, but its results are not optional: every
`[wasm]` row appears in full in the report.

- **`[wasm]` floor gates are advisory.** NAT'd nodes are unreachable over WebRTC-Direct until
  relayed WebRTC exists, so a WASM floor breach is expected and never fails a train. Report the
  figures; never let them change the verdict.
- **`[wasm]` relative gates are ordinary soft gates** and count exactly like native ones.

`[pointer]` gates appear only when the run carried pointer clients (V2-1339). They are the pointer
tier: paid, owner-signed mutable references.

- **`[pointer]` floor gates are advisory.** No run has ever measured a pointer tier, and ADR-0016
  says a non-zero stale-read rate is expected without saying how large — so the floors are a guess
  until a few trains have moved them to numbers with history. Report the figures; never let them
  change the verdict.
- **`[pointer]` relative gates count** as ordinary soft gates: they ask only whether this release
  made the tier worse than the baseline did, which needs no calibration.
- **A non-zero stale-read rate is EXPECTED.** Report it as a figure with its trend, never as a
  fault. The gate is on the RISE against the baseline, not on the level.
- **On the first pointer-carrying train every relative `[pointer]` gate reads NOT ASSESSED**, with
  the reason `baseline has no pointer records`. That is correct and is not a rebaseline: the file
  and node-tier gates compare against the previous train as normal, since a few hundred 5 KB writes
  an hour is below run-to-run noise. Say so in the report rather than reaching for `--new-baseline`.
- **No `[pointer]` gate reaches the exit code**, advisory or not.

With `--new-baseline` in place of `--baseline-id`, every relative (soft) gate reads NOT ASSESSED
with the reason `new baseline`; only the floors and the observational hard gates are assessed. The
command exits 2 only on a native hard-floor FAIL.

It does **not** cover the following. The command's report says nothing about them at all, so it is
**your** report's Observational hard gates table (Step 7) that must account for each one — silence
there would read as a pass:

- **Crashes / panics / OOM / unexplained restarts** — panics reach stderr and journald, not the
  JSON logs Telegraf forwards, and the run's Elasticsearch is destroyed at teardown. If the
  staging run session did not capture this while the testnet was up, it cannot be recovered.
- **Mixed-version phase** — whether in-phase transfers met the success floors (>99% upload, >99.5%
  download) is derivable from the per-op records if you know the phase boundary; "previous-version
  nodes demonstrably storing chunks" is an SSH check on `chunks.mdb` growth and must come from the
  run session.
- **Canaries** — a separate production deployment, assessed by the `evaluate-canary` skill on its
  own sub-issue. Out of scope for this report entirely: do not reference, summarise or re-derive
  it. The gate verdict combines the two.

## Step 1 — Read the release train issue

1. `list_issues` with `project: "Releases"`, `team: "v2.0"`, `state: "In Progress"`.
2. Expect exactly one issue titled `Release Train — YYYY-MM-DD`. If there are none or more than
   one, stop and ask.
3. `get_issue` on it for the full description.

Extract the `ant-node` RC version and the previous release from the `## Cut record` versions
table. **That table has no Old/New columns** — the old and new versions share one `Version` cell
as `0.15.0 → **0.16.0-rc.1**`. The value *after* the arrow is the RC; the value *before* it is the
previous release. The baseline is **not** chosen from that version — see Step 2.

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
- **Baseline**: the **previous train's accepted staging run** — the last build *shipped to beta*,
  not the last one *promoted to stable* (the same definition `gate-verdict` and `train-manifest`
  use). Not the previous rc.N of this same train: chaining train to train is what stops several
  "small" regressions inside one train compounding into a large unremarked one. It must also be a
  valid link in the baseline chain (below); if no valid baseline exists, this is the new-baseline
  run.

**Confirm both ids with the user before running anything.** Nothing in the registry records which
run a train was accepted on, so the baseline is a judgment you must have confirmed. State the two
ids you believe are right, with their dates and names — or, for the new-baseline run, the candidate
id and the statement that there is no valid baseline — and ask.

### Baseline chain

The staging baseline chain **breaks at the first STG-01 run on `staging.yaml`**, which added the
WASM client tier and moved node NAT from 30% to 10%. Nothing either side of that break is
comparable.

- A valid baseline must be an STG-01 run whose `record-analysis` summary says
  `Profile staging @ <SHA>`. Check it with `print-recorded-results --id <id>`.
- Runs recorded with `Profile staging-short @ …` — STG-01 #579 and earlier — are **never** valid
  baselines, for any gate, however recent.
- If no valid baseline exists, this is the **new-baseline run**: the verification uses
  `compare --new-baseline` (Step 5), and the decision rests on the native hard floors and the
  observational hard gates.

Baseline chain start: STG-01 #596 (2026-09-23), first run on staging.yaml

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

Re-derive the phase on **both** runs, with the same label, **using `--id`**. For a
**new-baseline** verification there is no baseline side: run `derive-metrics` on the **candidate
only**, with `--label steady-state`.

```bash
uv run saorsa-deploy deployments derive-metrics --id 474 \
  --window-start 2026-07-28T20:14:12 --window-end 2026-07-28T23:14:12 --label steady-state
uv run saorsa-deploy deployments derive-metrics --id 470 \
  --window-start 2026-07-25T17:00:55 --window-end 2026-07-25T20:00:55 --label steady-state
```

`--name` resolves to the most recent run bearing it, so it cannot address a baseline of a reused
name. Take each run's T0 and phase boundaries from its `Staging run — rc.N` sub-issue.

### The mixed-version phase has its own label

The run session records `mixed-version` alongside `steady-state` (see
`run-staging-testnet` Step 7). Compare on **`steady-state`** as normal — that is
the measurement phase and the phase the baseline is comparable on.

Then read the mixed-version window SEPARATELY, and report it as an observation
rather than folding it into the verdict:

```bash
uv run saorsa-deploy deployments print-pointers --id <candidate> \
  --window-start <mixed start> --window-end <mixed end>
```

A **pre-pointer ant-node answers a protocol error to every pointer message**, so
the pointer tier is expected to degrade during the mixed-version hour. That is why
the phase gets its own label: the degradation is MEASURED and stated, rather than
averaged into steady-state and lost. Do not compare the mixed-version window
against a baseline's steady-state one — different phases are not comparable, and
the command refuses on a label mismatch anyway.

Two rules the command enforces and you should not try to work around:

- **Both sides must carry the same `window_label` and the same `source`.** A whole-run capture sums
  raw counter deltas and is exact; a re-derived window integrates bucketed averages and is
  approximate. Comparing one against the other measures the method, not the release.
- **Window lengths must be within 1.25x.** Replication continues after an upload completes, so a
  shorter window truncates the disk numerator while still counting the full denominator.

The connection-error counts are looked up under the **same label**, so a steady-state comparison
finds none unless the run session recorded that slice as well. Check what each run actually holds
before choosing the window:

```sql
SELECT deployment_id, window_label, count(*) FROM deployment_log_event_counts
WHERE deployment_id IN (<candidate>, <baseline>) GROUP BY 1, 2;
```

If both runs' Elasticsearch instances are still up, record the missing slice on each and compare on
steady-state as normal:

```bash
uv run saorsa-deploy deployments record-log-counts --id <id> \
  --window-start <start> --window-end <end> --label steady-state
```

**If they are not — and after teardown they never are — a `full-run` comparison is a legitimate
route, not a workaround.** What you must not do is pair a steady-state metric window with whole-run
counts inside one comparison; that mixes two windows in a single verdict. Running the whole
comparison on `full-run`, where both runs hold counts, is internally consistent and assesses every
gate. Prefer it over accepting five NOT ASSESSED rows when the windows are comparable — confirm
first that the two `full-run` windows are close on span, total forwarded lines, host count and
activity coverage, and say in the report that the train was gated on `full-run` and why.

`full-run` is in any case the **sounder** window for these particular gates. They are raw counts
normalised by total lines, so none of the reasons steady-state is preferred for the metric gates —
clock skew dragging the window, idle time inflating per-GB numerators — applies to them. The
opposite does: the command warns that a *time-filtered* count is applied against each host's own
deliberately-skewed clock, which is an argument against the steady-state slice, not for it.

Be aware the two windows can disagree on a metric gate. On the 2026-08-24 train, outbound bytes per
GB read +12.08% (FAIL) on steady-state and +2.13% (PASS) on full-run. Where that happens, report
both figures — a gate whose verdict flips with the window is not a settled finding, and quoting
only the passing one misrepresents it.

### The clock-skewed host will corrupt a steady-state connection-error count. Always subtract it.

**This is not a theoretical caveat — it inverted a published verdict on the 2026-09-08 train.**
Before quoting *any* windowed connection-error delta, decompose it by host and recompute with the
skewed host removed.

Each staging run skews **one** node VM's clock by ±3h. The direction is
`blake2b(vm_ip)[0] % 2` (`skew_direction_for_ip` in `provisioning/clock_skew.py`) — a hash of the
**VM's IP address**. Nothing in the profile controls it, and because every run provisions fresh VMs
with fresh IPs, **the direction flips between runs at random**. Do not go looking for a config
change to explain a flip; there isn't one.

A windowed count is applied against each host's own clock, so that host contributes a *different
stretch of its run* than everyone else. Which stretch depends on the sign, and the two stretches are
nothing like each other in volume:

- **+3h** → the steady-state filter captures that host's **startup burst**, when the fleet is forming
  and connection errors run 10–50x their settled rate.
- **−3h** → it captures a quiet late-run or post-teardown stretch.

Measured on #579 vs #566, where the sign happened to differ between the two runs:

| | Baseline #566 (−3h) | Candidate #579 (+3h) | Delta |
|---|---:|---:|---:|
| Steady-state total | 4,299 | 8,611 | +100.3% |
| of which `BLR1-6` | 302 (7.0%) | 2,395 (27.8%) | — |
| **Excluding `BLR1-6`** | **3,997** | **6,216** | **+55.5%** |

One host produced **48.5% of the entire apparent increase**. The first published report quoted
+100.3% as the settled-network figure; the true figure is +55.5%.

**How to do it.** The per-host breakdown is already stored — no Elasticsearch needed, so this works
after teardown:

```sql
SELECT deployment_id, count, details->'top_hosts'
FROM deployment_log_event_counts
WHERE deployment_id IN (<candidate>, <baseline>)
  AND window_label = 'steady-state' AND event_key = 'connection_attempt_failed';
```

Identify the skewed host from the bucket data (it needs no Elasticsearch either) — any host whose
final bucket is >15 min off the fleet median:

```sql
SELECT host, max(bucket_start) FROM deployment_host_metric_buckets
WHERE deployment_id = <id> AND metric = 'rss_bytes' GROUP BY 1 ORDER BY 2;
```

Expect **exactly one** such host per run. Then subtract it from both sides and report the corrected
delta, saying which host was excluded and why.

**Cross-check against `full-run`, which is immune.** It applies no time filter, so every host
contributes its whole run regardless of skew. On #579 vs #566 the skewed host was 3.7–3.8% of each
run and excluding it moved the figure from +23.4% to +23.5% — no effect. If removing the skewed host
changes the steady-state figure a lot and the full-run figure barely at all, that confirms the
mechanism rather than merely being consistent with it.

### Host cardinality tracks event volume, not coverage

The `hosts` column counts hosts that logged **at least one** matching line in the window. A low
number does **not** mean hosts were dropped from the window, and must never be used to argue that a
slice is unrepresentative — that argument is circular, because low cardinality is a *consequence* of
the run having fewer events.

Demonstrated within a single run: #579's steady state shows 65 hosts at 8,611 events, but one of its
own hours shows **52 hosts at 1,756 events** — fewer hosts than the baseline's 62, on the same fleet,
in the same run. Only one host per run is clock-skewed, so skew can never account for a shortfall of
more than one.

## Step 4 — Gather the accepted-run history (optional)

**Skip this step for a new-baseline verification** — `--history` does not apply to
`--new-baseline`. Only runs on the current chain (`Profile staging @ …`) are ever history
candidates.

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

For the **new-baseline run**, replace `--baseline-id` with `--new-baseline` (the two are mutually
exclusive) and use `steady-state`; the `full-run` fallback rules in Step 3 are unchanged:

```bash
uv run saorsa-deploy deployments compare \
  --id <candidate> --new-baseline \
  --window-label steady-state \
  --markdown-out /tmp/staging-verification-rc<N>.md
```

Every relative gate then reads NOT ASSESSED with the reason `new baseline`, and the verdict is
stored with a NULL baseline. It exits 2 only on a native hard-floor FAIL.

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
- **Connection-error gates.** One per group, on the counts the run session recorded with
  `deployments record-log-counts`. Three things to read before quoting a delta:
  - **A NOT ASSESSED here is expected on the first train that uses them.** No run before the
    gate existed recorded these counts, and none can be backfilled — the run's Elasticsearch is
    destroyed at teardown. It becomes a real gate once a baseline carries counts too. Say so
    plainly rather than treating it as a run failure.
  - **The gates compare raw counts**, which scale with how long a run logged for. Each gate's
    detail carries the per-hour rate and the host spread; the command warns when the two runs'
    logs span materially different periods. A count concentrated on a couple of hosts of seventy
    is usually a service restart, not a fleet-wide regression — the detail says which.
  - **`Connection attempt failed` and `connect_happy_eyeballs failed` are one gate**, not two.
    They are the same failure logged at two layers and tracked each other to within 0.2% across
    a 50x swing, so the group takes the larger rather than their sum. Do not report them as two
    corroborating signals.

  A **zero** count is not self-explaining: "fixed", "renamed" and "emitted at a level the node
  forwarder drops" are indistinguishable from Elasticsearch alone. If a group fell to zero,
  either confirm the literal is still in the pinned source or report it as unconfirmed.

## Step 6 — Gather the two observational hard gates

For each, state PASS / FAIL / NOT ASSESSED **with its evidence or with the reason no evidence
exists**. Do not infer a pass from the absence of a report.

Data integrity is **no longer one of them**: `compare` emits a
`Data integrity (download content verification)` hard gate from the per-download SHA-256 checks the
native client tier records (V2-757). Take that row from the command's own report like any other
hard gate. It reads NOT ASSESSED with the reason `no verified downloads recorded` only when the
run's uploads predate content verification (or every download failed) — which is a real absence of
evidence, not the permanent structural gap it used to be.

| Gate | Where it comes from |
|---|---|
| Crashes | The `Staging run — rc.N` sub-issue's steady-state observations. If the run session did not check `systemctl --state=failed` while the testnet was up, it cannot be recovered — ES died at teardown. NOT ASSESSED, not PASS. |
| Mixed-version phase | The run sub-issue records both criteria: in-phase **native** transfers meeting the success floors (>99% upload, >99.5% download), and previous-version nodes storing chunks (`chunks.mdb` growth over SSH). |
| Mixed-version WASM transfers (advisory) | The run sub-issue's mixed-phase WASM row. Reported, never in the verdict. |

Note that `deployment_upload_records` / `deployment_download_records` carry per-op `finished_at`,
so if the run sub-issue records the mixed-version phase boundary you can confirm the transfer half
of that gate directly from Postgres rather than taking it on trust. Do check it there rather than
from the run's own in-flight counts: `print-uploads` truncates its output, so a single failing row
can be missing from what the run session saw. Split by transport: `client_kind` NULL or `native` is
the native tier and decides the gate; `client_kind = 'wasm'` is the advisory WASM row. Where WASM
ops failed in-phase, carry the run session's attribution (old-version peers — ant-node 0.19.x and
earlier have no WebRTC-Direct listener — or NAT'd peers).

The transfer criterion was **zero failed transfers** until the 2026-08-24 train, when the RM
replaced it with the main-phase success floors. A transfer can fail for reasons unrelated to
running mixed versions, and an absolute-zero bar made the gate fire on that noise — on run #544 one
upload of 866 failed and every peer the client could not reach was running the **new** version. If
a transfer did fail in-phase, still say in the report whether an older-version node was involved;
that attribution is the difference between a compatibility finding and a coincidence.

### The WASM production compat line

One extra line the comparison cannot produce, because it is not about this testnet at all: the
train's **browser-path production** tests, `STG-WUL-01` (upload) and `STG-WDL-01` (download), run by
the `run-client-prod-tests` skill against the real network. It belongs in this report because the
staging comparison's `[wasm]` rows measure the browser path against a *testnet* fleet whose NAT'd
nodes it cannot reach, and a reader asking "does the browser path work on this release" needs the
production answer beside that one.

Read it from the registry — **read-only, and never re-derive or re-run anything**:

```bash
uv run saorsa-deploy deployments ls | grep -E 'STG-WUL-01|STG-WDL-01'
uv run saorsa-deploy deployments print-recorded-results --id <the ids from this train>
```

Report it as **exactly one line** in the hard-gates table, carrying **four numbers** — the two
transports' upload and download results:

```
| WASM production compat (advisory — not in verdict) | ADVISORY | upload <ok>/<attempted>, download <ok>/<attempted>, <n> over-cap skips, bundle <commit-short> — STG-WUL-01 #<id> / STG-WDL-01 #<id> |
```

- **ADVISORY, never PASS or FAIL, and never in the verdict.** There are no calibrated production
  floors for the browser path yet; those land in `resources/gates/staging-thresholds.yaml` in a
  follow-up, mirroring `wasm_floor_gates_advisory: true` for the testnet side. A verdict here would be
  an uncalibrated number deciding whether a release ships.
- **The download denominator is *attempted*, not the address count.** Over-cap entries are recorded
  but excluded from every success-rate denominator, which is why the skip count is reported beside the
  rate rather than folded into it.
- **If the client production tests have not run yet, the line is `NOT ASSESSED — client production
  tests not yet run`.** Emit it anyway: a line missing from the report reads as a gate nobody thought
  about. Do not wait for them, do not run them, and never block this verification on them — they are
  a separate sub-issue with its own session.
- Do not reproduce the client tests' failure text, spend or per-file detail here. One line, then the
  sub-issue link.

## Step 7 — Write the verification

Post as **exactly one** comment on `Staging verification — rc.N`, in this order. One comment, every
time — if a comment already exists (a superseded window, an earlier attempt), edit or delete it
rather than adding another. A reader should never have to reconcile two reports.

```markdown
# Staging verification — rc.N

**Result: PASS / FAIL**

| | |
|---|---|
| Candidate | STG-01 #<id> — ant-node <RC>, <date> |
| Baseline | STG-01 #<id> — ant-node <previous train's RC>, <date> — or `none — new baseline (first run on staging.yaml)` |
| Profile | `<profile> @ <sha>` (both runs) |
| Window | <label> — candidate <start→end>, baseline <start→end> |
| Threshold config | v<N> |
| Metric gates | <n> pass, <n> fail, <n> gray, <n> not assessed |

## What happened

<Prose. Three or four short paragraphs, no bullets of raw numbers. See below.>

## Verdict

<One or two lines: what passed, what failed. If the RM has ruled, record the ruling and their
stated reasoning, and note the stored verdict is unchanged.>

## Hard gates

| Gate | Verdict | Detail |
|---|---|---|
| Upload success floor [native] | PASS | 100.000% (2130/2130), floor >99% |
| Download success floor [native] | PASS | 100.000% (429/429), floor >99.5% |
| Upload success floor [wasm] (advisory — not in verdict) | … | …, floor >99% |
| Download success floor [wasm] (advisory — not in verdict) | … | …, floor >99.5% |
| Data integrity (download content verification) | … | … ok / … mismatch / … unavailable, coverage …% — fails at > 0 |
| Crashes (staging fleet) | … | … |
| Mixed-version phase | … | native in-phase transfers + chunk storage |
| Mixed-version WASM transfers (advisory — not in verdict) | … | in-phase WASM up/down + attribution |
| WASM production compat (advisory — not in verdict) | ADVISORY | upload …/…, download …/…, … over-cap skips, bundle … — STG-WUL-01 #… / STG-WDL-01 #… |

## Soft gates

<new-baseline run only: "Relative gates NOT ASSESSED: this run establishes the new baseline (WASM
tier added, NAT 30%→10%).">

<the tool's soft-gates table, verbatim — every [native] and [wasm] row>
```

### The "What happened" section

This is the part anyone actually reads. Write it as **plain prose for someone who was not
watching the run** — an engineer or the RM skimming on a Tuesday, not a replay of the tables
underneath.

Cover, in this order:

1. **What the run did** — how it behaved overall, in a sentence or two. Did it hold up? Include
   one sentence on the WASM tier's upload and download results (and any WASM downloader crash/OOM).
2. **What improved** against the baseline, in plain terms, with the few numbers that matter.
   Group them ("every transfer measurement got faster, the largest uploads by about half")
   rather than listing all thirty.
3. **Where there could be concerns** — every failing gate, anything that passed but moved the
   wrong way, and anything a reader should keep an eye on next train even though it passed.
   A gate passing at 14% against a 15% threshold is a concern; say so.
4. **What this run could not tell us**, if it matters to a decision — e.g. a change that ships
   dormant and so was never exercised.

**Keep methodology out of it.** No `source=buckets` / `source=capture`, no `[mean]` vs `[p50]`
markers, no explanation of what `--history` does or why the 2σ band did or did not run, no
command lines, no notes about which flags the tool takes or what a past version could not do.
Those are properties of the measuring apparatus, not findings about the release, and they
drown the signal. The reader wants to know whether the release is good.

The **one** exception: when a measurement limitation would change how someone acts on a number,
say it in a single plain-language clause — "the CPU figure is an average across the fleet rather
than a typical host, so a few busy machines can pull it up". Never more than a clause, never a
section, and only where it changes the reading.

Do not include a "Method notes" or "Known gaps" section. Do not explain the window choice; the
Window row in the table is the whole of what a reader needs.

### The gate tables

**One hard-gates table, covering all of them**, sitting directly above the soft-gates table. A
hard gate is a hard gate whether the tool derived it or you observed it, and splitting them across
two tables at opposite ends of the report made a reader reconcile two lists to answer one
question: did anything blocking fail? So compose a single `Gate | Verdict | Detail` table
combining the tool's success floors (`[native]` hard, `[wasm]` marked "(advisory — not in
verdict)") with the three observational gates, the advisory mixed-version WASM row and the advisory
WASM production compat row. Carry each floor's
figure and threshold into its Detail cell so nothing is lost in the reshaping. This table is the
only place hard gates appear.

Both tables sit **after** the prose and the verdict. They are reference material — the reader who
needs them will scroll, and the reader who doesn't should not have to scroll past them to reach
the answer.

**The soft-gates table is pasted verbatim** — same columns, same ordering, same values, `[mean]` /
`[p50]` markers and all, every `[native]` and `[wasm]` row. Two trains' reports must stay diffable, and the numbers are the tool's,
not yours.

Drop the report's `## Not evaluated by this tool` footer. Those gates are assessed directly in the
hard-gates table, so repeating them as "not evaluated" contradicts the report.

**Canaries are out of scope.** They are a separate production deployment with their own sub-issue
and feed the gate verdict independently. Do not add a canary row, do not summarise the canary
result, and do not let a canary failure change this report's verdict.

A `[wasm]` relative-gate FAIL is a soft FAIL, handled exactly like a native one.

On a **soft-gate FAIL**, state the override path requirements — RM written justification, one
engineer co-sign, `gate-override` label, and an auto-created P1 debt issue linked as blocking the
next train's cut — but **do not make the decision and do not write the Decisions section**. That
is the RM's, recorded by the `gate-verdict` skill. If the RM has already ruled, record their
decision plainly and note that the stored verdict is unchanged.

## Step 8 — Close, or hand back

- No hard gate FAILED, and nothing assessable was left unassessed → move
  `Staging verification — rc.N` to **Done**.
- Any hard gate FAILED, or a gate that *could* have been assessed was not (the run session never
  checked for crashes, say) → leave it **In Progress**, post the report, and hand back to the RM
  naming exactly what is unresolved.

**A data-integrity NOT ASSESSED DOES hold the sub-issue open**, unlike every other structural gap
here. The gate is measured now, so `no verified downloads recorded` means the run produced no
evidence either way — an uploader tier that predates content verification, or a run where every
download failed — and that is a fixable problem with the run, not a permanent property of the
system. Name which of the two it was before closing.

The one NOT ASSESSED that does **not** block is a relative gate carrying the reason
`new baseline`. On the new-baseline run every relative gate reads that way by design; close on the
native hard floors and the observational hard gates.

`[wasm]` floor gates are advisory and never block, whatever their figure.

**The WASM production compat line never blocks either**, including when it reads `NOT ASSESSED —
client production tests not yet run`. It reports a *different* test, run by a different skill against
production, on its own sub-issue and its own schedule; this verification is about the staging testnet.
Holding it open for that would couple two independent sub-issues and stall the train on ordering.

A **soft**-gate FAIL — native or `[wasm]` — does not hold the sub-issue open either. Record it, state the override path,
and close; the decision is the RM's at the gate verdict. If the RM has already ruled on it, record
that ruling and note the stored verdict is unchanged.

## Step 9 — Record the chain start (new-baseline run only)

After the **first** `--new-baseline` verification is posted, write this line — with the real
candidate id and train date — in three places, replacing the placeholder in each:

> Baseline chain start: STG-01 #<id> (<train date>), first run on staging.yaml

1. This skill's **Baseline chain** section (Step 2).
2. The equivalent **Baseline chain** entry in the `gate-verdict` skill
   (`/home/chris/dev/releases/.claude/skills/gate-verdict/SKILL.md`).
3. The memory note `project_staging_baseline_chain_break_wasm.md` in
   `/home/chris/.claude/projects/-home-chris-dev-github-com-jacderida-saorsa-testnet-registry/memory/`.

Commit and push only when the user says to. On every later train the chain start is already set:
skip this step.

## Known gaps

State these in the final report rather than working around them:

- **Content verification covers the NATIVE client tier only.** The browser (WASM) tier publishes no
  hash and verifies nothing (V2-1360), and the production client kinds do not either (V2-1361), so
  their downloads carry no verdict and contribute nothing to the data-integrity gate. Its coverage
  figure is what says how much of the run was actually checked.
- **Panics never reach Elasticsearch** — they go to stderr and journald, and ES dies at teardown.
  A crash check must happen during the run or not at all.
- **Percentiles cannot be backfilled.** Any run recorded before percentile capture landed compares
  CPU and RSS as a sample-weighted mean, marked `[mean]`.
- **Disk occupancy is absent from every run recorded before 2026-07-25**, so
  `disk_bytes_per_gb_stored` is NOT ASSESSED against an older baseline. No backfill is possible.
- **Nothing records which staging run a train was accepted on**, so the baseline is chosen by the
  operator and confirmed by hand every time.
- **Connection-error counts cannot be backfilled**, so a gate is assessable only when the
  candidate AND the baseline both hold counts under the label being compared. Which runs hold
  which label is not a cutoff date — it is a record of who ran the second invocation on the day,
  so **query it, never assume it** (the `deployment_log_event_counts` check in Step 3). Measured:
  #513 `full-run` only, #532 both, **#544 `full-run` only**, #566 both. The 10% threshold is a
  starting point with no run history behind it, not a calibrated level.
- **The memory-leak threshold is calibrated against the noise floor, not against a known leak.**
  It was set from four runs, none of which is known to contain one. A PASS there means "no
  fleet-wide RSS trend", not "no leak".
