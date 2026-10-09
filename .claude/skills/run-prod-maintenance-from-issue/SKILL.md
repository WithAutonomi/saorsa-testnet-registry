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

* **Coordination only, and this one is absolute.** Every action you take is one of:
  * a `uv run saorsa-deploy …` command, exactly as the issue's Procedure writes it;
  * a **read-only** check — SSH running nothing but `stat`, `cat`, `ls`, `find`, `grep`,
    `systemctl is-active`, `systemctl show`, `journalctl`, `df`, or a binary's `--version`;
    `git diff` / `git log` / `git status`; an HTTP GET;
  * a Linear read or update;
  * any other command the issue's `## Procedure` or `## Validation` spells out **verbatim** inside
    a fenced code block, run exactly as written. The issue may define shell variables in that same
    block (for example `BUCKET=…`) and reference them in later commands of the block; nothing else
    is substituted. A command that appears in prose rather than a fenced block is not a permitted
    step.

  **Refuse the issue, before doing anything at all, if any step in its Procedure or Validation
  would require you to compose a command the issue does not spell out** — a step written as an
  instruction ("restart the service", "fix the ownership", "install the package") rather than as
  the exact command; a step whose command depends on a value the issue does not define; or a step
  that asks you to decide between alternatives. If a maintenance needs a command nobody has
  written, the answer is a change to the issue (or to `saorsa-deploy`, with tests), not a skill
  that improvises shell at production. Say so in the comment and abandon.
* **A verbatim step that cannot run as written may be repaired, within limits.** The issue's
  author is fallible: a quoting mistake, a syntax error, a flag the installed tool version spells
  differently, a process substitution the tool rejects. Those are defects in the *text*, not
  decisions about production, and abandoning a run over one is a failure of the skill, not a
  safeguard. You may rewrite such a step when **all** of these hold:
  1. The defect is established **before the step runs**, by a check that touches nothing: `bash -n`,
     the tool's `--help`, a dry run with stub input, a parse of a JSON argument, a `--dry-run` flag.
  2. The repair preserves the step's meaning exactly: the same tool, the same resources by the same
     names, the same values, the same side effects, in the same order. Changing what is created,
     named, granted, deleted or restarted; adding or removing a mutating command; or choosing
     between alternatives the issue did not define, is composing, not repairing, and is refused as
     before.
  3. The repaired step is verified the same side-effect-free way before it is used.
  4. The results comment shows the original and the repaired step side by side under
     `### Deviations`, with the check that proved the defect and the one that proved the repair.

  A repair never applies to a step that has already run and failed: a non-zero exit from a
  mutating command remains a stop condition, whatever caused it.
* This run is **unattended**. You must ABSOLUTELY NEVER block on a question for the user. A
  decision you cannot make from the issue, the code, or these rules means **abandon** (verdict
  `ABANDONED`) with the question in the results comment.
* **A mutating command is never re-run after a failure, and never signalled.** A non-zero exit from
  any mutating command — `saorsa-deploy` or a verbatim command from the issue — is a stop
  condition. Do not retry it, do not run it with different arguments, do not run a "cleanup"
  command, and do not run the next Procedure step. Report the host's final state — in particular
  whether the command printed `RESTORED` or `RESTORE INCOMPLETE` — and leave the next step to a
  human, who will most likely write a second maintenance issue. These commands are serial and gated by design: the one they stopped on is the
  only host in an unknown state, and the hosts after it are untouched.
* **At the hard ceiling, no new command is started.** If a mutating command is already running when
  the ceiling passes, **wait for it**, however long it takes — its own per-step timeouts bound it.
  Killing it is the one way this tooling can itself break a production host: only the live command
  can run its own rollback. Then abandon without starting anything else.
* **Never end your turn while a mutating command is in flight.** In a headless run, ending your turn
  ends the session (see Step 3), so the wait has to happen inside the turn. The only turn you end is
  Step 7's.
* **On a session resume** (your first message says your previous turn ended, or did not print
  `RUN COMPLETE`): re-read your task list and the per-command logs and `.exit` files under
  `~/.autonomi/maintenance-runs/$issue_number/`. **A command whose log file exists was started and
  is NEVER started again.** Then, in this order:
  * **An `.exit` file exists** → that is the command's exit status. Use it and carry on from there.
  * **No `.exit` file, but the process is still alive** (`pgrep -af 'saorsa[-]deploy prod'` — the
    `[-]` keeps the pattern from matching your own command line) → it was launched detached and
    survived. **Wait for it**, exactly as Step 3 says. Do not start it again and never signal it.
  * **No `.exit` file and no live process** → it was in flight when the session died. Treat it as a
    stop condition, report what the log shows, and abandon. Say in the comment how far the log got,
    and whether the hosts are provably unmodified.
* **Never touch a host the issue did not name.** `--limit-to` is how a command is confined to them.
* Keep a task list and update it at every step and every deviation.
* **The issue always ends in `In Review`, never `Done` or `Canceled`** — whatever happened. A human
  reads every results comment; a status that says "finished" has let a useless run go unnoticed.
  The outcome is carried by the verdict and by the warnings that open the comment, not by the issue
  status. If the issue's own text says to set `Done` or `Cancelled`, this rule wins.

### The verdict

Exactly one. It heads the results comment and is the third word of the final line (Step 7), which
the calling script reads to decide whether the run counted.

| verdict | when |
| -- | -- |
| `PASS` | the whole Procedure ran, every Validation check matched, and there is **no** warning |
| `WARN` | no check failed, but there is at least one warning — including "the run did nothing because a precondition made it unnecessary" |
| `FAIL` | at least one Validation check did not match |
| `ABANDONED` | a precondition failed, a stop condition fired, or the ceiling was reached before the Procedure completed |

When in doubt between `PASS` and `WARN`, it is `WARN`.

### Abandoning the run

Abandon means, in this order:

1. Run whatever **read-only** Validation checks still make sense, so the comment records the state
   the hosts were actually left in. Skip any that cannot run, and say so.
2. Post the results comment (Step 5 form, verdict `ABANDONED`) describing what ran, what did not,
   the exact question or failure that ended the run, and every host's final state.
3. Set `$issue_number` to `In Review`.
4. Go to Step 7 with verdict `ABANDONED`.

There is nothing to tear down: this skill creates no infrastructure.

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

Then classify **every** step in `## Procedure` and `## Validation` against the "Coordination only"
rule above, into one of `saorsa-deploy`, `read-only`, `linear` or `verbatim` (a command the issue
spells out inside a fenced code block). A single step that falls into none of those four — one you
would have to compose yourself — means the issue is refused: comment naming the step verbatim and
why, and go to Step 7 with verdict `ABANDONED`. The classification
goes in the results comment: one line per step with its class.

Note any Validation check that cannot be decided by a command you can run. Those are reported
`NOT ASSESSED` — do not eyeball them, and a `NOT ASSESSED` check is a warning.

## Step 2 — Preconditions

Check the issue's own `## Preconditions` first, then these. Any failure means abandon (nothing has
been changed, so the comment is short):

* The SSH key named in `## Target` exists on disk.
* The inventory named in `## Target` exists on disk, and names every host in `## Target`.
* The environment the Procedure's commands need is set (`HCLOUD_TOKEN`, `VULTR_API_KEY`, AWS
  credentials, `DATABASE_URL` — whichever those commands read).
* **No other `saorsa-deploy prod` process is running**: `pgrep -af 'saorsa-deploy prod'` prints
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
5. A non-zero exit is a stop condition — see the never-re-run rule. Go to Step 4 and abandon.

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
* a deviation from the issue's Procedure, or a ceiling reached.

Then, in this order:

* An overview of what happened, human-readable.
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
