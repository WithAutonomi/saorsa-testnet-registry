---
name: run-prod-maintenance-from-issue
description: Run an unattended production maintenance procedure as specified in a given Linear issue
argument-hint: <linear-issue-id>
arguments: [issue_number]
disable-model-invocation: true
---

If "$issue_number" is empty or doesn't look like a Linear issue ID (e.g. ABC-123), stop immediately.
Reply only with:
"Usage: /run-prod-maintenance-from-issue <linear-issue-id>". Do not guess an issue, search Linear,
or ask which one to use.

## Rules

**The issue states a goal; your job is to reach it.** `## Purpose` says what the maintenance is for
and `## Validation` says what "done" looks like. `## Procedure` is the author's intended path to
it — the best-known commands in the best-known order — not a script to be executed blindly and
abandoned at the first non-zero exit. Issue text is written by fallible people against systems
they could not fully inspect: a quoting mistake, a missing package on the target, a URL whose
driver resolves differently on the VM, a flag the installed tool spells differently. None of
those is a reason to stop. When a step fails or cannot run as written, **diagnose it, fix it and
carry on**, inside the boundaries below. A run that ends `ABANDONED` over something you could
have fixed within the boundaries is a failure of this skill, not a safeguard.

### Boundaries — the lines that are never crossed

These are what keep "apply intelligence" from becoming "go rogue". Every one is absolute, and a
goal that cannot be reached without crossing one is a goal to abandon, with the reason stated.

1. **Scope.** Touch only the targets the issue names in `## Target` (hosts, cloud resources,
   database objects, local files) and the resources the Procedure itself creates. Never a host the
   issue does not name; `--limit-to` is how a command is confined to the named ones.
2. **No destruction the issue did not sanction.** Never delete data, drop a table, destroy a VM or
   a bucket, remove a credential, or stop a production service, unless the issue's Procedure or
   Rollback contains that exact action. Restarting a service the Procedure itself installs or
   restarts is within scope; restarting one it does not mention is not.
3. **No new spend.** Never create billable infrastructure the issue does not call for.
4. **Secrets.** Never print, log or post a password, token, key or connection URL. Diagnose around
   them (lengths, prefixes, `has_*` checks).
5. **`retry: never` steps.** A step the issue marks `retry: never` is never re-run after a
   non-zero exit and never signalled — these are the serial, one-host-at-a-time commands with their
   own built-in rollback (`prod bootstrap upgrade` and its kin), where the host it stopped on is
   in a state only that command, or a human, can judge. For such a step a non-zero exit is a stop
   condition exactly as before: report the host's final state, whether it printed `RESTORED` or
   `RESTORE INCOMPLETE`, and leave the next move to a human. A `saorsa-deploy prod` command that
   is running is never signalled, whatever its marking.
6. **Unattended.** You must ABSOLUTELY NEVER block on a question for the user. A decision the
   issue, the code, the target's own state and these rules cannot settle means abandon, with the
   question in the results comment.
7. **The hard ceiling.** At or past it, start nothing new. A mutating command already running is
   waited for, however long it takes; then abandon.
8. **Repo changes go through a branch.** If the fix is to code in this checkout, make it on a
   branch named for the issue, commit it, push it and open a PR against `main` — and say so in
   the results comment. Running the fixed tree for this maintenance is fine; leaving a silent
   edit on `main` is not.

### Inside the boundaries, use your judgment

Nobody can list in advance what will go wrong on a live system, so this skill does not try.
Anything the boundaries do not forbid is yours to decide: read the actual failure, read the
target's actual state, work out what reaching the goal needs, and do it. Prefer the smallest
change that gets there, and prove it worked before moving on. The boundaries are the whole
constraint; a step the issue did not write, a package the issue did not mention, a value the issue
derived wrongly, a leftover from an earlier attempt — all of it is ordinary work, not a reason to
stop.

### Every fix is reported, and a fixed run is never `PASS`

Each fix goes in the results comment under `### Fixes applied`: what failed (the exact error),
what you changed (the exact command or diff), why that was inside the boundaries, and what proved
it worked. A run that needed any fix or any retry ends `WARN` at best, never `PASS`, so a human
reads what happened. The point of the leeway is to finish the job; the point of the report is that
nobody has to trust that you finished it well.

### The rest

* **Never end your turn while a mutating command is in flight.** In a headless run, ending your
  turn ends the session (see Step 3), so the wait has to happen inside the turn. The only turn you
  end is Step 7's.
* **On a session resume** (your first message says your previous turn ended, or did not print
  `RUN COMPLETE`): re-read your task list and the per-command logs and `.exit` files under
  `~/.autonomi/maintenance-runs/$issue_number/`. **A command whose log file exists was started and
  is not started again until you know how it ended.** Then, in this order:
  * **An `.exit` file exists** → that is the command's exit status. Carry on from there, fixing
    and re-running as the rules above allow.
  * **No `.exit` file, but the process is still alive** (`pgrep -af 'saorsa-deploy prod' | grep -v pgrep`)
    → it was launched detached and survived. **Wait for it**, exactly as Step 3 says. Never
    signal it.
  * **No `.exit` file and no live process** → it was in flight when the session died. Read the
    log and the target's state. A `retry: never` step in this condition is a stop condition;
    any other step is treated as "failed after a partial change" above.
* Keep a task list and update it at every step, every fix and every deviation.
* **The issue always ends in `In Review`, never `Done` or `Canceled`** — whatever happened. A human
  reads every results comment; a status that says "finished" has let a useless run go unnoticed.
  The outcome is carried by the verdict and by the warnings that open the comment, not by the issue
  status. If the issue's own text says to set `Done` or `Cancelled`, this rule wins.

### The verdict

Exactly one. It heads the results comment and is the third word of the final line (Step 7), which
the calling script reads to decide whether the run counted.

| verdict | when |
| -- | -- |
| `PASS` | the Procedure ran as written, every Validation check matched, no fix, no retry, no warning |
| `WARN` | every Validation check matched, but something a human should read happened: a fix, a retry, a precondition that made the run unnecessary, a NOT ASSESSED check |
| `FAIL` | at least one Validation check did not match after everything inside the boundaries had been tried |
| `ABANDONED` | the goal could not be reached inside the boundaries or the ceiling, or a `retry: never` step failed |

When in doubt between `PASS` and `WARN`, it is `WARN`.

### Abandoning the run

Abandon means, in this order:

1. If the Procedure changed anything and the issue has a `## Rollback`, run it — **unless** a
   `retry: never` step is the reason you are abandoning, in which case the host it stopped on is in
   a state only a human may touch, and nothing is rolled back. Say which of the two applied.
2. Run whatever **read-only** Validation checks still make sense, so the comment records the state
   the targets were actually left in. Skip any that cannot run, and say so.
3. Post the results comment (Step 5 form, verdict `ABANDONED`) describing what ran, what did not,
   every fix attempted, the exact failure or question that ended the run, and every target's final
   state.
4. Set `$issue_number` to `In Review`.
5. Go to Step 7 with verdict `ABANDONED`.

## Step 1 — Read and validate the issue

**First, before reading anything else, set `$issue_number` to `In Progress`.** The status is the
only signal a human watching Linear has that a run is under way; it goes on the moment the run
starts, not after the preconditions pass.

**A maintenance issue runs once**, as a testnet-run issue does. If the issue was not in `Todo` or
`Backlog` when you picked it up, or it already carries a results comment (one whose first line
begins `## Results —`), it has been run before: refuse it. Comment that the issue has already been
run and that the next attempt needs a new issue (the human duplicates it and cancels this one),
then go to Step 7 with verdict `ABANDONED`. Never re-run a Procedure under an issue that has a
results comment, whatever that comment's verdict.

Read Linear issue `$issue_number`. It must contain **all** of these sections, by these headings.
Stop and name the missing one if any is absent:

| heading | what it holds |
| -- | -- |
| `## Purpose` | why this maintenance is being done |
| `## Target` | the tier, the exact hosts, the inventory path, the SSH key |
| `## Procedure` | the exact commands, in order |
| `## Preconditions` | what must be true before the Procedure starts |
| `## Validation` | the checks, each with an **expected value** |
| `## Stop conditions` | what ends the run early |
| `## Hard ceiling` | a duration from the start of the Procedure |
| `## Rollback` | what the tool does on a failure, and what you must report |

An issue may also carry `## Boundaries` — extra limits specific to it, which add to the boundaries
above and never loosen them — and may mark individual Procedure steps `retry: never`.

Then read `## Procedure` and `## Validation` as the plan: note which steps the issue marks
`retry: never`, which mutate something and which are read-only, and what each step is for. You are
not classifying in order to refuse; an instruction written in prose rather than as an exact
command ("install the package", "restart the unit") is one you work out from the target's state
and the repository, inside the boundaries, and report. The only reasons to refuse an issue here
are a missing section from the table above, a goal that cannot be stated from `## Purpose` and
`## Validation`, or a Procedure whose only way to the goal crosses a boundary.

Note any Validation check that cannot be decided by a command you can run. Those are reported
`NOT ASSESSED` — do not eyeball them, and a `NOT ASSESSED` check is a warning.

## Step 2 — Preconditions

Check the issue's own `## Preconditions` first, then these. Any failure means abandon (nothing has
been changed, so the comment is short):

* The SSH key named in `## Target` exists on disk.
* The inventory named in `## Target` exists on disk, and names every host in `## Target`.
* The environment the Procedure's commands need is set (`HCLOUD_TOKEN`, `VULTR_API_KEY`, AWS
  credentials, `DATABASE_URL` — whichever those commands read). The runner exports the repo's
  `.env` into this session before it starts, with any value already in the operator's shell
  taking precedence, so `DATABASE_URL` and the other keys in that file are present without the
  operator exporting them by hand; a key missing from both is a genuine failure of this check.
* **No other `saorsa-deploy prod` process is running**: `pgrep -af 'saorsa-deploy prod' | grep -v pgrep` prints
  nothing. Two commands walking the same tier at once would restart two bootstraps together, and
  the whole design of these commands is one host at a time.
* The working tree is the revision the maintenance is meant to run (`git log -1`, `git status`).
  A dirty tree is a warning, not a failure — say which files.

A precondition that makes the maintenance **unnecessary** rather than impossible (the hosts are
already on the target version, the ownership is already correct) is not a failure: do nothing, and
report it as verdict `WARN` with that as the first warning. A production command run for no reason
restarts production hosts for no reason.

Then:

1. Compute the absolute ceiling: now + the `## Hard ceiling` duration. Record it in the task list
   as a UTC timestamp, and check the clock against it before starting each command.
2. Create `~/.autonomi/maintenance-runs/$issue_number/` and write the Procedure's commands, in
   order, one per line, to `commands.sh` in it — **before running any of them**. That file is what
   a human reads to see what this run intended, including when the session dies mid-way.

## Step 3 — Run the Procedure

One command at a time, in the issue's order. For each, in this order:

1. Check the clock against the ceiling. At or past it, start nothing: go to Step 4 and abandon.
2. Record the UTC start time in the task list.
3. **Launch it detached, never as a child of this session.** Write a one-command runner to
   `~/.autonomi/maintenance-runs/$issue_number/run-<n>-<verb>.sh` that `cd`s to the repository,
   runs the command with stdout **and** stderr redirected to
   `~/.autonomi/maintenance-runs/$issue_number/<n>-<verb>.log` (`<n>` the step number, `<verb>` the
   command's own verb, e.g. `1-bootstrap-upgrade.log`), then appends `=== END <utc> exit=$rc ===`
   to that log and writes the bare exit code to `<n>-<verb>.exit`. Start it with:

   ```bash
   setsid nohup ~/.autonomi/maintenance-runs/$issue_number/run-<n>-<verb>.sh \
       >/dev/null 2>&1 </dev/null &
   ```

   Then confirm it really is detached — `ps -o pid,pgid,sid,args -C saorsa-deploy` must show it
   with its own session id, not this session's.

   Never pipe the command through `tee` and read `$?` — the pipeline's status is the pipe's. Write
   the exit code of the command itself.

   **This is not a style preference.** This skill is invoked from
   `scripts/run-maintenance-issues.sh` as a headless `claude -p` session, which **exits the moment
   you end your turn**, and a background Bash task is a child of that process and is killed at
   teardown. On 2026-10-06 that killed a `prod bootstrap upgrade` 26 seconds into a production host
   (session log: `terminal_reason: completed`, `api_error_status: null`, rc 0; task output:
   `[killed]`). A foreground Bash call is not the alternative: it is capped at 600,000 ms, which is
   shorter than this command's own 1200 s per-host gate. Detaching is the only shape that survives,
   and it is also what lets a resumed session find the command still running rather than dead.
4. **Poll for the `.exit` file inside this turn, and do not end your turn while the command is in
   flight.** A single background `until test -f <...>.exit; do sleep 10; done` gives you one
   notification when it lands; do not arm per-step monitors and do not narrate progress. Record the
   UTC end time and the exit code in the task list.
5. A non-zero exit from a `retry: never` step is a stop condition: go to Step 4 and abandon.
   From any other step it is a problem to solve: read the log and the target's state, apply a fix
   inside the boundaries, re-run (detached, as a new numbered runner — never reuse a log file),
   and record the fix. If no fix inside the boundaries reaches the step's intent, abandon.

These commands are slow by design: `HEALTH_TIMEOUT_SECS` is 1200 s, so a bootstrap health gate
allows up to 20 minutes per host, plus a 60 s settle between hosts. The ceiling is the only bound
that matters — never your own tool call's.

Watch the output for the rollback lines the `## Rollback` section names. `RESTORED` means the tool
put the host back; `RESTORE INCOMPLETE` means it could not, and that host needs a human before
anything else happens on the tier.

## Step 4 — Validation

Run every check in `## Validation` and compare what you got against the **expected value the issue
wrote**, not against what you would like to see. Each check is PASS, FAIL or NOT ASSESSED.

Where the issue states an **exemption in prose** that does not match where the data actually turns
up — it exempts a known pre-run event but names the wrong table, field or command for it — apply
the exemption's stated intent and **establish the fact independently** rather than inferring it from
the exemption. Then record both readings: score the row, and raise a warning giving the literal
reading, the verdict it would produce, and the evidence that decided it. Never silently pick the
kinder reading, and never widen an exemption past the specific event the issue named. (V2-1445
exempted the two hosts' pre-upgrade `Backup creation failed` attempt but placed it in
`rollout-watch`'s `transient` table, where it in fact lands in `failures`; the fact was settled by
querying Elasticsearch for the document's timestamp and re-running over a window starting after the
Procedure.)

Where the issue names a read-only status command (`prod bootstrap status --json` for the bootstrap
tier), run it and quote the fields. Run it **before** the Procedure too where the issue asks for a
before/after table — if you have not, say the "before" column is from the issue's own recorded
observations and name them as such.

A check you cannot run is NOT ASSESSED and a warning; it is never a PASS.

## Step 5 — Post the results comment

Post a comment on `$issue_number`. **Its first two things, before anything else, are the verdict
and the warnings** — a reader who stops after ten lines must already know whether the maintenance
can be trusted:

```
## Results — <target> — <PASS|WARN|FAIL|ABANDONED>

### Warnings
1. <the most serious first, one or two lines each, with the value that shows it>
```

If there are none, write `### Warnings` followed by `None.` — never omit the heading. A warning is
anything a human must know before relying on this run:

* **`RESTORE INCOMPLETE` on any host — this is always the first warning**, and names the host;
* a Validation check that FAILED or is NOT ASSESSED;
* a Procedure command that did not run, or ran and failed;
* the run did nothing because a precondition made it unnecessary;
* a host left in a state the issue did not intend, including one the tool rolled back;
* a deviation from the issue's Procedure, or a ceiling reached;
* any fix applied or any step re-run (the detail goes under `### Fixes applied`).

Then, in this order:

* An overview of what happened, human-readable.
* **`### Fixes applied`** — one entry per fix or retry: the exact error, the exact change, why it
  was inside the boundaries, and the evidence it worked. `None.` if there were none.
* **The Validation checks as a table**: one row per check — check, PASS / FAIL / NOT ASSESSED,
  expected, observed.
* **A per-host before/after table** from the read-only status the issue names (for the bootstrap
  tier, `prod bootstrap status --json`: on-disk version, running version, active, webrtc port,
  backup owner, foreign-owned count).
* **Every command run**, with its UTC start, UTC end and exit code, and the path of its log.
* The `RESTORED` / `RESTORE INCOMPLETE` lines **verbatim**, if any appeared.
* Deviations: anything done differently from the issue, and why.

## Step 6 — Set the issue to In Review

Always, whatever the verdict. Never `Done`, never `Canceled`.

## Step 7 — Exit

Print one final line — `RUN COMPLETE <issue> <verdict> -` — and **end your turn with no pending
tool calls, waits or schedules**. The trailing `-` keeps the field count identical to the testnet
run line, so one parser serves both. `<verdict>` is the single word from "The verdict", the same one
that heads the results comment. The calling script reads this line: any verdict other than `PASS`
is treated as a bad run. In headless mode printing it and ending the turn is what ends the process;
the calling script waits on it and will not start the next issue until it exits.
