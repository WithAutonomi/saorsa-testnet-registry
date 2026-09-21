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
  ES/Influx/Grafana), or a Linear update. No host is modified during the run.
* This run is **unattended**. You must ABSOLUTELY NEVER block on a question for the user. If you
  reach a decision you cannot make from the issue, the code, or these rules, **abandon the run**
  (see "Abandoning the run") and report the question in the results comment.
* We NEVER exit the run with billable **compute** left around — clients, nodes, bootstraps, any
  extension fleet. The one exception is the observability stack (Elasticsearch and/or monitoring),
  and only when the issue explicitly says to retain it (see "Retained observability stacks").
* Never destroy the Elasticsearch or monitoring stack before the extraction to Postgres has landed.
  Compute is a different matter: tear it down as soon as the last phase ends (Step 6).
* Every wall-clock boundary gets its own wake (Step 4). "I armed a monitor" is not a schedule.
* Keep a task list and update it at every phase boundary, every wake, and every deviation.
* Do not work around infrastructure or permission problems. Diagnose, record, abandon if needed.

### Abandoning the run

Abandon means, in this order:

1. Record whatever extraction is still possible (Step 5 — Postgres is cheap, the run cost money).
   Skip any recording that cannot succeed, and say so.
2. Tear down compute (Step 6), verified.
3. Tear down the observability stacks too, unless the issue names them as retained.
4. Post a comment on `$issue_number` describing what happened, what was recorded, what was skipped,
   and the exact question or failure that ended the run. Set `$issue_number` to `Cancelled`.

Precondition failures (Steps 1–2) happen before any infrastructure exists: stop, report which item
failed, and leave the issue as it is. There is nothing to tear down.

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
* A **hard run ceiling** (a T0-relative time by which compute must be torn down regardless of
  state). If the issue has phases but no ceiling, use the last phase boundary + 1h30m and record
  that you did so.

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
* **No state exists for the deployment name.** Check all four S3 keys: `saorsa-deploy/<name>.json`,
  `saorsa-deploy/elastic/<name>.json`, `saorsa-deploy/monitoring/<name>.json`,
  `saorsa-deploy/journals/<name>.json`. Any of them present means something went wrong on a
  previous run — `monitoring infra` / `elastic infra` silently **reuse** same-name stacks. Do not
  destroy them; fail the precondition and report which key exists.
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
**in the background**, logging to a file:

```bash
uv run saorsa-deploy deployments up --config <manifest-path> > <log> 2>&1 &
```

Immediately after launch, in the same tool call: find the **`.venv/bin/python3` PID** (not the
`uv` wrapper — that exits early and looks like a finished run), assert it is alive with
`kill -0 $PID`, and tail the log to confirm the first step has started. A command that dies on
arrival must be caught here, not discovered later.

Wait on the PID, then print exit status and log tail unconditionally:

```bash
until ! kill -0 $PID 2>/dev/null; do sleep 30; done; echo "up ended"; tail -40 <log>
```

Never `pgrep -f` a pattern that appears in the waiter's own command line (it self-matches).

The last step is `clients start`. When it completes, record the wall-clock time from the client
units' `ActiveEnterTimestamp` (not from the log). That is **T0**, and every phase is measured from
it. Immediately arm the schedule (Step 4).

### When a deploy step fails

There are two classes of failure and they are handled differently.

**Infrastructure creation** (`bootstraps infra`, `nodes infra`, `clients infra`,
`monitoring infra`, `elastic infra` — anything creating VMs or volumes at a cloud provider). Do
**not** retry. These failures are usually provider-side maintenance or capacity problems and last a
while. Abandon the run: `deployments down --name <deployment-name>`, verify (Step 6), report which
provider and step failed and the error.

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

### The ceiling

The ceiling is the last entry in the boundary list and is checked at every wake, not only its own:
if the current time is past the ceiling and Step 6 (compute teardown) has not run, the run is over
regardless of phase — run Step 5 for whatever can be recorded, then Step 6, then report. The
results comment says which phases were incomplete and why.

### Acceptance criteria and stop conditions

Evaluate the acceptance criteria as the run proceeds. A failed criterion does **not** stop the run
by itself — continue the procedure and report it.

The issue may name explicit stop conditions ("no relay reservations by T0+30m", "pending upgrade",
"node VM at 100% disk with services dying"). A stop condition means **abandon the run** — collect
the evidence the issue asks for, extract, tear down, cancel. It never means "hand back with the
deployment up"; there is no one to hand back to.

## Step 5 — Record results

Run the issue's full extraction sequence once, after the last phase, **before destroying the
observability stacks**. The commands below are the minimum for any issue; the issue may add more
(`derive-metrics`, analysis scripts, snapshots). The testnet is scratch; Postgres is the record.

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

**`record-results` can take several minutes.** Give it a timeout of at least 10 minutes
(600000 ms).

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

### Compute — immediately after the last phase

The moment the last phase ends and the last SSH-dependent evidence is collected (crash sweep,
snapshots, `df`), destroy the compute tiers. Do not wait for extraction: it reads ES and InfluxDB,
not the nodes, so the fleet is pure cost from here.

```bash
uv run saorsa-deploy clients destroy    --name <deployment-name> --force
uv run saorsa-deploy nodes destroy      --name <deployment-name> --force
uv run saorsa-deploy bootstraps destroy --name <deployment-name> --force
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
  the command's. Redirect to a file instead.
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

Post a comment on `$issue_number` with:

* An overview of what happened, human-readable.
* The acceptance criteria as a table: one row per criterion, PASS / FAIL / NOT ASSESSED, with the
  evidence in one line.
* T0, every boundary, every wake's actual fire time (UTC), and any deviations.
* Registry id.
* Teardown verification result, and — if any stack is retained — its endpoints and destroy
  commands.

Then set `$issue_number` to `Done` (or `Cancelled` if the run was abandoned).

## Step 8 — Exit

The run is finished only when Step 7 is done. Print one final line —
`RUN COMPLETE <issue> <Done|Cancelled> <registry-id>` — and **end your turn with no pending tool
calls, waits or schedules**. In headless mode that is what ends the process; the calling script
waits on it and will not start the next issue until it exits.
