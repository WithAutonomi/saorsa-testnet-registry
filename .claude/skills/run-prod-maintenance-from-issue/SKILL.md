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
  * a Linear read or update.

  **Refuse the issue, before doing anything at all, if any step in its Procedure or Validation is
  anything else** — a raw mutating SSH command, a `systemctl restart`, an editor, a `chown`, a
  `cp`, a package install, a `terraform` invocation. If a maintenance needs something no command
  can do, the answer is a change to `saorsa-deploy` (with tests), not a skill that types shell at
  production. Say so in the comment and abandon.
* This run is **unattended**. You must ABSOLUTELY NEVER block on a question for the user. A
  decision you cannot make from the issue, the code, or these rules means **abandon** (verdict
  `ABANDONED`) with the question in the results comment.
* **A mutating command is never re-run after a failure, and never signalled.** A non-zero exit from
  a mutating `saorsa-deploy` command is a stop condition. Do not retry it, do not run it with
  different arguments, do not run a "cleanup" command, and do not run the next Procedure step.
  Report the host's final state — in particular whether the command printed `RESTORED` or
  `RESTORE INCOMPLETE` — and leave the next step to a human, who will most likely write a second
  maintenance issue. These commands are serial and gated by design: the one they stopped on is the
  only host in an unknown state, and the hosts after it are untouched.
* **At the hard ceiling, no new command is started.** If a mutating command is already running when
  the ceiling passes, **wait for it**, however long it takes — its own per-step timeouts bound it.
  Killing it is the one way this tooling can itself break a production host: only the live command
  can run its own rollback. Then abandon without starting anything else.
* **On a session resume** (your first message says your previous turn ended unexpectedly): re-read
  your task list and the per-command logs under `~/.autonomi/maintenance-runs/$issue_number/`. **A
  command whose log file exists was started and is NEVER started again** — read its exit status out
  of the log instead. If a log exists with no recorded exit status, the command was in flight when
  the session died: treat it as a stop condition, report what the log shows, and abandon.
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
rule above. A single step that is not a `saorsa-deploy` command, a read-only check or a Linear
update means the issue is refused: comment naming the step verbatim and why, leave the issue's
status alone, and go to Step 7 with verdict `ABANDONED`.

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

1. Set `$issue_number` to **In Progress**.
2. Compute the absolute ceiling: now + the `## Hard ceiling` duration. Record it in the task list
   as a UTC timestamp, and check the clock against it before starting each command.
3. Create `~/.autonomi/maintenance-runs/$issue_number/` and write the Procedure's commands, in
   order, one per line, to `commands.sh` in it — **before running any of them**. That file is what
   a human reads to see what this run intended, including when the session dies mid-way.

## Step 3 — Run the Procedure

One command at a time, in the issue's order. For each, in this order:

1. Check the clock against the ceiling. At or past it, start nothing: go to Step 4 and abandon.
2. Record the UTC start time in the task list.
3. Run it with stdout **and** stderr teed to `~/.autonomi/maintenance-runs/$issue_number/<n>-<verb>.log`
   (`<n>` is the step number, `<verb>` the command's own verb, e.g. `1-bootstrap-upgrade.log`).
   Never pipe a command through `tee` alone and read `$?` — the pipeline's status is the pipe's.
   Use `… > >(tee log) 2>&1` or redirect and `cat`, then read the exit code of the command itself.
4. Record the UTC end time and the **exit code**, appended to the log as a final line, so a resumed
   session can tell a finished command from one that was in flight.
5. A non-zero exit is a stop condition — see the never-re-run rule. Go to Step 4 and abandon.

These commands are slow by design (`HEALTH_TIMEOUT_SECS` is 1200 s, so a bootstrap health gate
allows up to 20 minutes per host, plus a 60 s settle between hosts), so give each a timeout generous
enough that **you** never time out before the command's own gate does: the ceiling is the bound that
matters, not your tool call's.

Watch the output for the rollback lines the `## Rollback` section names. `RESTORED` means the tool
put the host back; `RESTORE INCOMPLETE` means it could not, and that host needs a human before
anything else happens on the tier.

## Step 4 — Validation

Run every check in `## Validation` and compare what you got against the **expected value the issue
wrote**, not against what you would like to see. Each check is PASS, FAIL or NOT ASSESSED.

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
