---
name: run-testnet-from-issue
description: Run an unattended end-to-end testnet as specified in a given Linear issue
argument-hint: <linear-issue-id>
arguments: [issue_number]
disable-model-invocation: true
---

If "$issue_number" is empty or doesn't look like a Linear issue ID (e.g. ABC-123), stop immediately.
Reply only with:
"Usage: /run-testnet-from-issue <linear-issue-id>". Do not guess an issue, search Linear, or ask
which one to use.

## Rules

* Coordination only. Every action is a `saorsa-deploy` CLI command, a read-only query (SSH `df`,
  ES/Influx/Grafana), or a Linear update. A host is modified only when the issue's phase
  commands direct it through a committed `saorsa-deploy` command (for example `analysis
  isolate`, `analysis restart-nodes`, `analysis rot-chunks`); never on the runner's own
  initiative, and the time and targets go in the results comment.
* This run is **unattended**. You must ABSOLUTELY NEVER block on a question for the user. If you
  reach a decision you cannot make from the issue, the code, or these rules, **abandon the run**
  (see "Abandoning the run") and report the question in the results comment.
* We NEVER exit the run with billable **compute** left around — clients, nodes, bootstraps, any
  extension fleet. The one exception is the observability stack (Elasticsearch and/or monitoring),
  and only when the issue explicitly says to retain it (see "Retained observability stacks").
* Never destroy anything a recording reads from before that recording has exited: the Elasticsearch
  and monitoring stacks (`record-log-counts`, `record-analysis`, `derive-metrics`), and the **S3
  client state** that `clients destroy` deletes (`record-results` resolves `client_kind` from it;
  V2-1453 and V2-1441 lost the column by destroying first). Nodes and bootstraps are read by no
  recording command and go the moment the last phase's SSH evidence is collected (Step 6).
* Once Step 5 has started, the ceiling no longer causes a teardown. Recording runs to completion,
  bounded by its commands exiting, not by the clock: a fleet that has finished measuring has already
  paid for its data (V2-1542 started the destroy chain under ceiling pressure while `record-results`
  was still writing).
* Every wall-clock boundary gets its own wake (Step 4). "I armed a monitor" is not a schedule.
* Keep a task list and update it at every phase boundary, every wake, and every deviation.
* Do not work around infrastructure or permission problems. Diagnose, record, abandon if needed.
* **The issue always ends in `In Review`, never `Done` or `Canceled`** — whatever happened. A human
  reads every results comment; a status that says "finished" has let a useless run go unnoticed.
  The outcome is carried by the verdict (see "The verdict") and by the warnings that open the
  comment, not by the issue status. If the issue's own text says to set `Done` or `Cancelled`, this
  rule wins.

### The run directory

Everything this run writes outside the repository — the `up` log, the teardown logs, any evidence
file — goes in one directory, so that a run's evidence sits in one place and whatever started the
session can print a single path for it.

Create it as the **first thing you do**, before reading the issue, and say in those first lines
which of the two paths you got:

```bash
mkdir -p "${ANTOPS_RUN_DIR:-$HOME/.autonomi/testnet-runs/<issue>}"
echo "run directory: ${ANTOPS_RUN_DIR:-$HOME/.autonomi/testnet-runs/<issue>}"
```

`ANTOPS_RUN_DIR` is exported by `antops`, which owns one directory per run, creates it before
starting the session, keeps its own session log in it, and expects this run's files beside that.
When it is **unset** the session came from `scripts/run-testnet-issues.sh` /
`run-testnet-chain.sh`, which know nothing about it, so the fallback
`~/.autonomi/testnet-runs/<issue>` applies. Those drivers stay in use until antops has completed a
live run, so both paths are live — and a reader looking for a log has no way to tell which it was
unless the run says so.

**Write the whole expansion out in every command that touches the directory. Never carry it in a
shell variable.** Each Bash tool call gets a **fresh shell**: only the working directory survives,
and a variable does not, `export` or no `export`. A `RUN_DIR=…` set here is already gone by Step 3,
where `> "$RUN_DIR/up.log"` becomes `> "/up.log"`, fails with permission denied, and the run never
starts; the same thing hours later silently costs the teardown redirect that exists so an exit code
is not a pipe's. `ANTOPS_RUN_DIR` itself is safe — antops puts it in the `claude` process's
environment, so every Bash call inherits it. It is only a variable *you* assign that evaporates.
`<issue>` is this skill's own argument and is **not** in the shell's environment either, so write
the id itself (`V2-1234`) into the command — a `$issue_number` left in a redirect expands to
nothing and puts the log one directory up, where the next run's log of the same name overwrites it.

Only the directory's location changes with the variable. Name each file after the command that
produced it (`up.log`, `clients-destroy.log`), and any sentinel file beside its own log, exactly as
before.

### The verdict

Every run ends with exactly one verdict. It heads the results comment and is the third word of the
final line (Step 8), which the calling script reads to decide whether to start the next run.

| verdict | when |
| -- | -- |
| `PASS` | the full procedure ran, every acceptance criterion passed, and there is **no** warning |
| `WARN` | no criterion failed, but there is at least one warning (see Step 7) |
| `FAIL` | at least one acceptance criterion failed |
| `ABANDONED` | a precondition or stop condition ended the run before its procedure completed |

`PASS` is a claim that a later run may safely build on this one. When in doubt between `PASS` and
`WARN`, it is `WARN`.

### Abandoning the run

Abandon means, in this order:

1. Record whatever extraction is still possible (Step 5 — Postgres is cheap, the run cost money).
   Skip any recording that cannot succeed, and say so.
2. Tear down compute (Step 6), verified.
3. Tear down the observability stacks too, unless the issue names them as retained.
4. Post a comment on `$issue_number` (in the Step 7 form, verdict `ABANDONED`) describing what
   happened, what was recorded, what was skipped, and the exact question or failure that ended the
   run. Set `$issue_number` to `In Review`.

Precondition failures (Steps 1–2) happen before any infrastructure exists, so there is nothing to
tear down: post a short comment naming the item that failed (verdict `ABANDONED`), set the issue to
`In Review`, and go to Step 8 with `-` as the registry id.

### Retained observability stacks

A stack may be left running only if **all** of these hold:

* It is the Elasticsearch or monitoring stack — never compute.
* The issue explicitly says to retain it (e.g. `keep_on_destroy: true` in the manifest plus a
  sentence saying a human will look at it later).
* The results comment names each retained stack, its endpoint(s), and the exact destroy command
  (`elastic destroy --name <n> --force` / `monitoring destroy --name <n> --force`).

If the issue is silent, the stacks come down with everything else.

## Step 1 — Read the testnet run issue

Read Linear issue `$issue_number`. It defines the deployment manifest, test procedure and
acceptance criteria for a testnet run.

Confirm the issue contains:

* The name of the deployment
* The purpose of the run
* A deployment manifest
* A phase table with T0-relative boundaries
* A **hard ceiling** (a T0-relative time). The ceiling bounds the **measurement**: by that time the
  last phase must have ended and Step 5 (recording) must have started. It is not a time by which
  compute must be gone. If the issue has phases but no ceiling, use the last phase boundary + 30m
  and record that you did so. An issue whose ceiling text says compute must be torn down by it
  regardless of the run's state is read as this rule anyway — the ceiling fires on an unfinished
  measurement, never on a recording in progress — and the results comment says the issue's wording
  was superseded.

If any of the first three are missing, stop immediately and say which.

Read the acceptance criteria and note any that cannot be decided by a command you can run (e.g.
"panels look right"). Those are reported NOT ASSESSED — do not try to eyeball them.

## Step 2 — Check preconditions

Stop and say which item failed if any of these is not met:

* `DATABASE_URL` is set and reaches the registry. Check with `uv run saorsa-deploy deployments ls`.
  Note the highest existing id for the deployment name — the run's own id must be greater.
* Cloud credentials are present for every provider in the manifest: `DO_TOKEN`, `VULTR_API_KEY`,
  `HCLOUD_TOKEN`, the four `OVH_*` vars as applicable. Also `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` for S3 state and `ANT_DEPLOY_SECRET_KEY` for client wallets. OVH
  credentials are needed at destroy time too.
* No other `up` or `down` is running for the deployment name
  (`ps -eo pid,args | grep 'deployments \(up\|down\)' | grep -v grep`). They share S3 and
  Terraform state and will race.
* **No state exists for the deployment name.** Check all five S3 keys:
  `saorsa-deploy/deployments/<name>.json` (NOT `saorsa-deploy/<name>.json` — that key never
  exists, so checking it tests nothing), `saorsa-deploy/clients/<name>.json`,
  `saorsa-deploy/elastic/<name>.json`, `saorsa-deploy/monitoring/<name>.json`,
  `saorsa-deploy/journals/<name>.json`. Any of them present means something went wrong on a
  previous run — `monitoring infra` / `elastic infra` silently **reuse** same-name stacks. Do not
  destroy them; fail the precondition and report which key exists.
* **No stale local inventory for any name the run will create.** For each name, if
  `~/.autonomi/testnets/<name>/inventory.json` exists while the S3 state is absent (just
  verified), it is a snapshot of a destroyed deployment of the same name: deployment names are
  reused and provider IPs are recycled, so its addresses now belong to other tenants — or, worse,
  to another deployment of ours. Rename it before `up`:
  `mv ~/.autonomi/testnets/<name>/inventory.json{,.stale-$(date -u +%Y%m%dT%H%M%SZ)}` (and the
  `devnet-manifest.json` beside it). This is a **note** in the results comment, not a deviation.
  V2-1530 ran `df` against 14 such addresses at T0+30m. `up` writes a fresh snapshot at the end,
  and the destroy commands now retire it themselves, so this precondition only ever catches
  deployments destroyed before that landed.
* Any extra preconditions the issue names (branch, SHA, latest release tag, etc.).

Then:

* Create the manifest in `deploy/manifests/runs/` from the manifest in the issue.
* Run `deployments config validate` and `deployments plan` on it; confirm the plan matches the
  shape the issue describes (VM counts, NAT count, volume sizes).
* Compute the absolute schedule you will arm at T0: every phase boundary, every recurring check
  (e.g. every 30 min), the extraction start, and the hard ceiling. Write the T0-parameterised
  phase commands to a file (`deploy/manifests/runs/<name>.phases.sh` or the scratchpad) **before**
  launching `up`. Nothing is authored mid-run.
* Set `$issue_number` to `In Progress`.

## Step 3 — Deploy

Create a task list for the whole procedure from deployment to teardown, then launch the deploy
**in the background**, logging to a file in the run directory:

```bash
uv run saorsa-deploy deployments up --config <manifest-path> \
  > "${ANTOPS_RUN_DIR:-$HOME/.autonomi/testnet-runs/<issue>}/up.log" 2>&1 &
```

Immediately after launch, in the same tool call: find the **`.venv/bin/python3` PID** (not the
`uv` wrapper — that exits early and looks like a finished run), assert it is alive with
`kill -0 $PID`, and tail the log to confirm the first step has started. A command that dies on
arrival must be caught here, not discovered later.

Wait on the PID, then print exit status and log tail unconditionally:

```bash
until ! kill -0 $PID 2>/dev/null; do sleep 30; done; echo "up ended"
tail -40 "${ANTOPS_RUN_DIR:-$HOME/.autonomi/testnet-runs/<issue>}/up.log"
```

Never `pgrep -f` a pattern that appears in the waiter's own command line (it self-matches).
Do **not** `nohup`, `setsid` or `disown` the deploy: it must die with this session, so that an
operator's Ctrl-C on the calling script stops the deploy too instead of orphaning a half-built
fleet.

The last step is `clients start`. When it completes, record the wall-clock time from the client
units' `ActiveEnterTimestamp` (not from the log). That is **T0**, and every phase is measured from
it. Immediately arm the schedule (Step 4).

### When a deploy step fails

There are two classes of failure and they are handled differently.

**Infrastructure creation** (`bootstraps infra`, `nodes infra`, `clients infra`,
`monitoring infra`, `elastic infra` — anything creating VMs or volumes at a cloud provider). Read
the failing lane's error and classify it before deciding; the class, not the step, decides.

* **Terminal — the provider refused for a reason that will not change in the next few minutes.** A
  quota or limit exceeded, 401/403/"invalid token", billing, an unknown region or image, or the OVH
  ~60-minute silent hang in `nodes infra` (the per-region 40-vCPU quota). Do **not** retry. Abandon
  the run: `deployments down --name <deployment-name>`, verify (Step 6), report which provider and
  step failed and the error.
* **Capacity-shaped — "not available", "out of stock", "Size is not available in this region", "no
  capacity", "insufficient capacity". Never classify these from the error string: ask the
  provider's catalogue.** Providers reuse one message both for a size that is *structurally* absent
  from a region and for one that is merely *momentarily* exhausted there, and only the second is
  worth retrying — so the string alone cannot decide the class. DigitalOcean's
  `422 Size is not available in this region` is the known case: it ended DEV-06 run 645 for
  nothing, having refused `s-2vcpu-4gb` in `nyc1` for a bootstrap droplet and granted the identical
  slug in the identical region 70 seconds later, in the same `up`, for a pointer client.

  Make one read-only catalogue call for the failing provider, size and region:

  ```bash
  # DigitalOcean
  curl -s -H "Authorization: Bearer $DO_TOKEN" 'https://api.digitalocean.com/v2/sizes?per_page=200' \
    | jq -r --arg s '<size>' --arg r '<region>' '.sizes[]
        | select(.slug==$s)
        | "available=\(.available) offers_region=\(.regions|index($r)!=null)"'
  ```

  Vultr: `/v2/plans`, match the plan's `locations[]`. OVH: the region's
  `data.ovh_cloud_project_flavors`. Then:

  * Size catalogued for that region **and** `available=true` → it was **momentary capacity**.
    Treat as **transient**: follow the transient rule below, same 3-attempt budget.
  * Size **not** offered in that region, or the region unavailable → **structural**, therefore
    **terminal**: abandon, and say in the results comment that the manifest — or the tool's default
    region list, which is where the bootstrap tier gets its regions — names a region that cannot
    serve that size.
  * The check prints **nothing** (unknown slug), or the call itself fails → **terminal**. Never
    default a capacity-shaped failure to transient because the evidence was unobtainable; an
    unknown slug is a manifest error, and an unanswerable catalogue is its own finding.

  Record the check's output either way, under the deviation. It is one read-only call, and it is
  the difference between discarding a fully-preflighted multi-hour run and losing three minutes.
* **Transient — the provider's API misbehaved on a call that normally works.** A 5xx, a 429, a
  timeout or connection reset, a TLS error, or a 404 / "Invalid instance-id" on the **read-back**
  of a resource that was just created (Vultr does this; it ended DEV-01 run 613 for nothing).
  Wait 3 minutes, confirm no other `up` or `down` is alive for the name, then retry:

  ```bash
  uv run saorsa-deploy deployments up --config <manifest-path> --resume
  ```

  The journal records a parallel group only when every lane succeeded, so the resume re-runs the
  **whole** infrastructure group. That is safe: `bootstraps infra` reuses the bootstrap VMs already
  in state, `nodes infra` persists its regions before `terraform apply` so a re-apply reconciles
  the half-created region rather than duplicating it, `elastic infra` / `monitoring infra` reuse a
  same-name stack, and `clients infra` matches its specs. The builds are their own journaled group
  and are not repeated. Up to **three** resume attempts for the infrastructure group in total; a
  fourth failure is terminal, and so is the same lane failing with a different error class →
  abandon as above.
* **Never pass `--force`** to `up` or to any `infra` command: it wipes state and leaves orphans. If
  a resume attempt itself errors with "Terraform state exists … orphaned … Use --force", that is
  not retryable: abandon.
* Record every attempt — time, lane, error, wait — as a deviation in the results comment.

**Provisioning** (`nodes provision`, `provision-genesis`, `bootstraps provision`,
`clients provision`, `monitoring provision` — anything installing software over SSH). These are
usually transient SSH failures. Confirm no other `up` is alive, then retry:

```bash
uv run saorsa-deploy deployments up --config <manifest-path> --resume
```

`--resume` skips the already-completed steps using the journal, so a retry does not repeat the
binary builds. Without it a non-TTY journal hit refuses rather than hanging.

Retry up to three times. Occasionally a VM's SSH never becomes available at all (OVH and Vultr do
this), and a node provision that hangs silently is usually a stalled S3 binary download on a
throttled VM. After three attempts with no progress, treat it as terminal: abandon the run and
report which host was stuck. Do not SSH in to fix it.

## Step 4 — Phases

Carry out the phases as defined in the issue. This will typically take many hours.

### The schedule

This skill runs **headless** (`claude -p`): the process exits the moment you end your turn, and
nothing scheduled for later — `ScheduleWakeup`, `CronCreate`, a Monitor, a background `sleep` —
can fire after that. So the phase clock is driven by **foreground, bounded waits** inside the
turn, and you do not end the turn until Step 7 is done.

At T0, write the absolute timestamp (epoch seconds, UTC) of **every** timed action plus the
ceiling into the task list. If the issue has N timed actions there are N+1 boundaries. Then wait
for the next one with a foreground loop, at most 10 minutes per tool call:

```bash
until [ "$(date +%s)" -ge "$TARGET" ]; do sleep 30; done; date -u +%FT%TZ
```

Call it repeatedly (each call returns within its `timeout`, 600000 ms) until it prints a time at
or past the boundary. Never one long sleep, never a backgrounded sleep — a single backgrounded
sleep has returned 8 hours late and cost a whole run. Never `pgrep` for your own waiter.

**If your turn was resumed** (the first message says your previous turn ended unexpectedly): the
calling script has re-attached you to the same session after an API error. Everything you knew is
still in context. Re-read the task list and the clock, work out which boundary is next or overdue,
and continue — never restart from Step 1, never re-run `up`.

At **every** boundary:

1. Re-read the clock. Compute where you actually are relative to T0 before doing anything. If you
   are late, run the action that is due *now*, record the actual time, and never run an action
   whose window has passed as if it were on time.
2. Run the pre-written command for that boundary.
3. Run the health read (transfer counts, `df` on node VMs, failed units) and record it.
4. Update the task list with the fire time and outcome.
5. Start the wait for the next boundary.

A health check is a different job from the schedule and does not replace it — the fleet being
healthy is not evidence that an action is not due.

### Never gate a host list on a file existing

A phases script that reads an extension's (or any deployment's) IP list from
`~/.autonomi/testnets/<name>/inventory.json` must **not** gate on `[ -f "$INV" ]`. That is true
for every name ever deployed from this machine, including one whose deployment was destroyed weeks
ago — which is how V2-1530's script came to run `df` against 14 reassigned addresses. Gate on the
snapshot being **this** run's:

```bash
# The snapshot is only this run's if it carries the registry id we deployed.
[ "$(jq -r '.deployment_id // empty' "$EXT_INV" 2>/dev/null)" = "$EXT_ID" ] || { ... skip ... }
```

`deployment_id` is written by `generate-inventory` whenever the registry is reachable; a snapshot
predating that feature has no such key, and so is correctly treated as not ours. Checking the S3
state key (`saorsa-deploy/deployments/<name>.json`) is the equivalent gate when no id is to hand.
Say in the generated script's comment which of the two it uses and why.

### The ceiling

The ceiling is the last entry in the boundary list and is checked at every wake, not only its own.
If the current time is past the ceiling and the **last phase has not ended** (Step 5 has not
started), the run is over regardless of phase — run Step 5 for whatever can be recorded, then
Step 6, then report; the results comment says which phases were incomplete and why. If the current
time is past the ceiling and Step 5 **has** started, nothing happens: finish every recording
command, then run Step 6 in the order it gives. Record in the results comment that recording ran
past the ceiling and by how much; that is a note, not a warning, unless something else made it one.

### Acceptance criteria and stop conditions

Evaluate the acceptance criteria as the run proceeds. A failed criterion does **not** stop the run
by itself — continue the procedure and report it.

The issue may name explicit stop conditions ("no relay reservations by T0+30m", "pending upgrade",
"node VM at 100% disk with services dying"). A stop condition means **abandon the run** — collect
the evidence the issue asks for, extract, tear down, report as `ABANDONED`. It never means "hand
back with the deployment up"; there is no one to hand back to.

## Step 5 — Record results

Run the issue's full extraction sequence once, after the last phase, **before destroying the
observability stacks**. The commands below are the minimum for any issue; the issue may add more
(`derive-metrics`, analysis scripts, snapshots). The testnet is scratch; Postgres is the record.

Run `record-results` **before** `clients destroy` — it is the one recording command that reads
compute-tier state (the S3 client state, for `client_kind`). Nodes and bootstraps may already be
gone; `record-results` does not touch them.

A recording command can run longer than one tool call (V2-1542's `record-results` took 42 minutes
on 9,187 pointer rows, and its first attempt was killed by the 600 s limit after the file tiers and
before the pointer tier). Launch each recording command detached (`setsid`), redirected to its own
log in the run directory, and wait on its PID with the bounded foreground loop from "The schedule".
The `up` rule against `setsid` does not apply here: a recording is an idempotent upsert into
Postgres, and one that outlives the session does no harm. A recording command that exits non-zero
is re-run once in full; if it fails again, record what landed, name the failure in the warnings, and
proceed to Step 6 — a recording that cannot complete does not hold the fleet up indefinitely.

```bash
uv run saorsa-deploy deployments record-results    --name <deployment-name>
uv run saorsa-deploy deployments record-analysis   --name <deployment-name> \
  --summary "<summary of this run: build, T0, windows, notable findings>"
uv run saorsa-deploy deployments record-log-counts --name <deployment-name>
uv run saorsa-deploy deployments record-log-counts --name <deployment-name> \
  --window-start <first phase after warm-up start> --window-end <last phase end> \
  --label steady-state
```

Run `record-results` **before** `record-analysis` (the derived per-GB ratios need its rows).

### `record-log-counts`

Counts the connection-error strings in the deployment's own Elasticsearch and stores them in
`deployment_log_event_counts`. ES is per-run scratch — once destroyed there is no backfill, which is
why Step 6 keeps ES and monitoring up until this step is done.

It refuses rather than recording zeros if the index is empty. If that happens, check that log
forwarding was enabled for the run and say so; do not work around it.

Read the table it prints. A **zero count** is not self-explaining ("fixed", "renamed" and "emitted
at DEBUG, which the forwarder drops" look identical from ES) — report it as an observation, not a
conclusion. The **Hosts** column matters as much as the count.

**Record BOTH labels — two invocations.** The bare command lands under `full-run`; the
`--window-start/--window-end --label steady-state` form records the steady-state slice. Do not pass
`--window`; it is a different flag.

**`record-results` can take tens of minutes** — far longer than one tool call on a run with a
pointer tier — so launch it detached and wait on its PID, as above. Never give it a foreground
timeout and hope: 600 s killed V2-1542's first attempt between the file tiers and the pointer tier.

Verify rows are present before continuing:

```bash
uv run saorsa-deploy deployments ls        # find this run's id
uv run saorsa-deploy deployments print-recorded-results --id <id>
```

This command takes **`--id`, not `--name`**. Take the newest `<deployment-name>` id from
`deployments ls` and check it is greater than the id noted in Step 2 — a reused name once made
`record-analysis` a silent exit-0 no-op and lost a run's metrics.

If extraction fails or returns nothing, still tear down (Step 6) and report the problem.

## Step 6 — Teardown

### Compute — nodes and bootstraps after the last phase, clients after `record-results`

The moment the last phase ends and the last SSH-dependent evidence is collected (crash sweep,
snapshots, `df`), destroy the node and bootstrap tiers; no recording reads them, so they are pure
cost from here. Destroy the client tier only after `record-results` has exited (Step 5):
`clients destroy` deletes the S3 client state it reads.

```bash
uv run saorsa-deploy nodes destroy      --name <deployment-name> --force
uv run saorsa-deploy bootstraps destroy --name <deployment-name> --force
uv run saorsa-deploy clients destroy    --name <deployment-name> --force  # after `record-results`
```

(For an extension: `nodes destroy` then `deployments unextend --name <n> --force`.) These three
prompt without `--force` and raise `EOFError` on a non-TTY before destroying anything.

### Observability — after Step 5

Once extraction has landed, destroy ES and monitoring unless the issue retains them:

```bash
uv run saorsa-deploy elastic destroy    --name <deployment-name> --force
uv run saorsa-deploy monitoring destroy --name <deployment-name> --force
```

Or, when nothing is retained and compute is still up (the abandon path), the single orchestrated
teardown:

```bash
uv run saorsa-deploy deployments down --name <deployment-name>
```

**`deployments down` takes NO `--force`.** Its parser has `--config/--set/--name/--dry-run/
--skip-drain-failures` only; `--force` exits 2 with `unrecognized arguments` and destroys
nothing. This has "succeeded" three times while 30–45 VMs kept billing.

### Verify — never trust the exit code

* Never pipe a teardown command through `tee`/`tail`; the pipeline's exit code is the pipe's, not
  the command's. Redirect to a file in the run directory instead, with the path written out in full
  (`> "${ANTOPS_RUN_DIR:-$HOME/.autonomi/testnet-runs/<issue>}/clients-destroy.log" 2>&1`).
* A teardown that finishes in seconds did not happen.
* After it exits, confirm the reality: the S3 deployment state is gone
  (`deployments generate-inventory --name <n>` → "No deployment state found"), and the provider
  APIs list no VMs or volumes tagged with the name (DO droplets/volumes, Vultr instances/blocks,
  OVH instances, Hetzner servers — whichever the manifest used). Terraform success only proves
  *tracked* resources are gone. The registry `status` column is not evidence.
* Vultr block-storage delete can 400 `Host error (3)` right after detach; a second `down` clears
  it. Retry once, then verify again.

If verification finds anything still running after a retry, report the exact resource ids in the
results comment as the first line — that is the one thing a human must act on.

## Step 7 — Update Linear

Post a comment on `$issue_number`. **Its first two things, before anything else, are the verdict
and the warnings** — a reader who stops after ten lines must already know whether the run can be
trusted:

```
## Results — <deployment-name>, registry id <id> — <PASS|WARN|FAIL|ABANDONED>

### Warnings
1. <the most serious first, one or two lines each, with the number that shows it>
```

If there are none, write `### Warnings` followed by `None.` — never omit the heading. A warning is
anything a reader must know before relying on this run, **whether or not a criterion gated on it**:

* a criterion that FAILED or is NOT ASSESSED;
* **the run did not do what it was for, even though every criterion passed** — the load, rate,
  scale or condition its Purpose depends on was not reached; a success rate, error count or
  resource figure the issue only "observes" came out far from what it expected or from what
  earlier runs of the same shape recorded. Compare what you measured against the issue's own sizing
  and Purpose, and say so here if they disagree (a reference run that passed all six of its criteria
  while 35% of its uploads failed is the case this rule exists for);
* anything still running or billing — retained stacks, resources teardown could not remove;
* a deviation, late wake, retry or provider incident that affects how far the numbers can be trusted
  or compared with another run;
* a recording that is missing, partial or was skipped.

Do not soften a warning, bury it in a later section, or leave it only in the observations. Details
belong further down; the fact belongs here.

Then:

* An overview of what happened, human-readable.
* The acceptance criteria as a table: one row per criterion, PASS / FAIL / NOT ASSESSED, with the
  evidence in one line.
* T0, every boundary, every wake's actual fire time (UTC), and any deviations: the time the last
  phase ended, the time recording started, the time each recording command finished, and the
  ceiling — stating whether recording ran past it.
* Registry id.
* Teardown verification result, and — if any stack is retained — its endpoints and destroy
  commands.

Then set `$issue_number` to `In Review` — always, whatever the verdict.

## Step 8 — Exit

The run is finished only when Step 7 is done. **Your final message is one line and nothing else:**

```
RUN COMPLETE <issue> <verdict> <registry-id>
```

No overview before it, no sign-off after it, no prose around it — everything you have to say is
already in the results comment, and whoever wants it reads it there. `<verdict>` is the single word
from "The verdict", the same one that heads that comment; `<registry-id>` is `-` if the run never
got one. Then **end your turn with no pending tool calls, waits or schedules**: in headless mode
that is what ends the process, and the caller waits on it and will not start the next issue until it
exits. Any verdict other than `PASS` is treated as a bad run, and in a chain of dependent runs it
stops the runs that follow.

This line is the only evidence the run finished. Nothing infers completion from the exit code,
because the process exits zero whenever you end your turn — so a run that did everything the issue
asked and then signed off with a summary instead of this line is recorded as `NO-VERDICT`, raised
with the operator as urgent, and has to be read back out of the raw log by hand. Without the line
the run is a failure as far as anything outside this session can tell, whatever it achieved.
Ending a run that went perfectly with a summary in place of this line happened on 2026-10-13 and is
the most expensive way there is to finish.
