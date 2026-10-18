---
name: implement-issue
description: Implement a Linear issue against this repository unattended — branch, implement, test, commit, push, open a PR on the fork
argument-hint: <linear-issue-id>
arguments: [issue_number]
disable-model-invocation: true
---

If "$issue_number" is empty or doesn't look like a Linear issue ID (e.g. ABC-123), stop immediately.
Reply only with:
"Usage: /implement-issue <linear-issue-id>". Do not guess an issue, search Linear, or ask which
one to use.

## Rules

* This run is **unattended** (`claude -p`). You must NEVER block on a question. If you reach a
  decision you cannot make from the issue, the code or these rules, take the **Blocked** path
  below and record the question there.
* **This skill is the authorisation for git.** Creating the branch, committing to it, pushing it
  and opening the PR are what the operator asked for by invoking it. Nothing else is authorised:
  never commit to or push `main`, never force-push, never rewrite history, never touch another
  branch, never merge.
* **Scope is the issue.** Implement what it asks and what that requires (tests, docs, a migration).
  Do not refactor, tidy or fix things you notice on the way — note them in the PR body instead.
* Match the surrounding code: its idioms, naming, comment density, test style. Read CLAUDE.md for
  the architecture and the repo's conventions before writing anything.
* Never commit secrets (`.env`, credentials), build artefacts, or anything under git-ignored paths.
* The PR goes to **this fork** (`jacderida/saorsa-testnet-registry`), base `main`. It is for the
  operator's own review; do not request reviewers or add labels.

## Step 1 — Preconditions (stop and name the item if any fails; nothing has been changed yet)

* `git status --porcelain` is empty. A dirty tree means a previous run left something behind;
  do not stash or discard it.
* `git fetch origin` succeeds and `origin/main` exists.
* `gh auth status` succeeds.
* `uv run saorsa-deploy --help` runs (the environment is synced).
* Read the issue with the Linear tools. It must contain, in some form:
  * **what to change** — concrete enough that you can name the files or commands involved;
  * **how to verify it** — tests to add or run, or an observable behaviour to check.

  If either is missing, post a comment on the issue saying exactly what is missing, leave its
  status alone, and stop — your final message being `IMPLEMENT COMPLETE <issue> FAILED -` and
  nothing else, under the rules in Step 8.
* Note the issue's `gitBranchName` (Linear supplies it; use it verbatim — it keeps Linear's branch
  linking working). If that branch already exists on `origin`, or an open PR already references
  the issue, a previous run got here first: comment on the issue with what you found and stop as
  above. Do not build on it.

**If your turn was resumed** (the first message says your previous turn ended unexpectedly): the
calling script re-attached you to the same session after an API error. Check `git status` and
`git log` for where you got to and carry on from there — do not re-create the branch or redo
completed steps.

## Step 2 — Branch

```bash
git checkout -b <gitBranchName> origin/main
```

Set the issue to **In Progress**. Create a task list covering implement → verify → version bump →
commit → PR → Linear, and keep it updated.

## Step 3 — Implement

Do what the issue says. Read the code you are changing and the tests around it before editing.
New behaviour gets tests in the package's existing test directory (`deploy/tests/` for
`saorsa-deploy`), in the existing style. A new command, flag or manifest key also gets its
`deploy/README.md` argument-table entry and, if it changes how the system is described, the
matching CLAUDE.md paragraph — the issue will usually say. A model change gets an Alembic
migration under `db/alembic/` (`cd db && uv run alembic revision --autogenerate -m "..."`).

If the issue turns out to be wrong about the code (a function it names does not exist, a
behaviour it describes is not what the code does), implement what the issue *means* if that is
unambiguous and say so in the PR; if it is ambiguous, go to Blocked.

## Step 4 — Verify

Run exactly what CI runs, from the repo root:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

`uv run pytest` takes several minutes — run it with a timeout of at least 10 minutes
(600000 ms), or in the background to a log file and wait on the PID. Then run the specific
tests the issue names, if any, and **every** verification it describes — a CLI invocation, a
`deployments plan` against a manifest, and **live end-to-end runs that stand up real
infrastructure** (testnets, production client deployments, builds). Performing the issue's live
verification is part of the job, not optional: this skill is the authorisation for the
infrastructure and spend the issue's verification section calls for. Do not stop to ask.

For a live run:

* **Use fresh deployments you create.** Name them as the issue says, or `VERIFY-<issue>-<n>` if it
  doesn't, and check the name is unused (registry + `saorsa-deploy/` S3 state) before `up`. Never
  run `up`/`down`/provision/destroy or restart services against a deployment this run did not
  create (e.g. `ant-prod-*`, `PROD-*`, `STG-*`); read-only queries against them are fine.
* Build what the issue says to build (`build …` steps run on remote build VMs — never build Rust
  locally).
* Long waits: run `up` in the background to a log file and wait on the PID; poll state on a
  sensible interval rather than sleeping blind. One `up`/`down` per deployment name at a time.
* Collect everything the issue's pass criteria need **before teardown** (ES/Influx/VM artefacts
  die with it): `record-results`, `print-recorded-results`, logs, SSH checks.
* **Tear down every deployment you created, whether the verification passed or failed**, then
  confirm nothing remains (S3 state gone, `deployments list`/registry status). Only leave one up
  if the issue says to. A teardown failure goes in the PR body with the exact resources left.
* Put the results — pass/fail per criterion, row counts, quoted output — in the PR's Test plan
  and in a comment on the issue.

If a live run fails because the change is wrong, fix it and re-run. If it fails for a reason
outside the change (provider outage, quota, missing credential), record it and go to Blocked
with what is still outstanding.

Every failure in a file you touched, or in a test you added, is yours to fix. A failure you did
not cause — check by running that one test against `origin/main` in a temporary worktree
(`git worktree add /tmp/<x> origin/main`, run, `git worktree remove /tmp/<x>`) — is
**recorded in the PR body, not fixed** (it is out of scope). Format only the files you changed
(`uv run ruff format <files>`), not the whole tree.

Do not weaken a test to make it pass. Do not skip or mark xfail. If you cannot make the gate
pass on your own changes, go to Blocked.

## Step 5 — Version bump

The four workspace packages share one version number, and a `saorsa-deploy` wheel built from it
is what an antops run installs — so a change to the tool that does not move the version is a
change no run can ask for. CI enforces it: the `version-bump` check fails any PR whose declared
bump type and version do not match what it changes, and it is a required check on `main`.

Decide the type from the issue:

| type | when |
| -- | -- |
| `breaking` | a change under `deploy/` or `db/` that a manifest, a caller or an operator's command line must adapt to |
| `feature` | a new command, flag, manifest key or model field |
| `fix` | any other change to the tool |
| `none` | the change touches neither `deploy/` nor `db/`, or the issue says not to bump. Documentation and run manifests inside those trees — `deploy/README.md`, `deploy/manifests/`, `db/README.md` — are not a change to the tool |

For anything but `none`, read the current version from `deploy/pyproject.toml`, apply the bump
(`breaking` → major+1 with minor and patch reset, `feature` → minor+1 with patch reset, `fix` →
patch+1) and set it:

```bash
uv run scripts/bump_version.py <new version> --no-commit
```

That edits `deploy/`, `db/`, `web/` and `mcp/`'s `pyproject.toml` plus `uv.lock` and stops;
include all five in the commit. Then write the line

```
Semantic version: <breaking | feature | fix | none>
```

into the PR body, under the issue link (Step 7). Without it the check fails the PR. A
`version-bump` check that fails on your PR is fixed by re-bumping or correcting the declared
type — **never** by editing the check or the workflow.

## Step 6 — Commit

Stage the files you changed explicitly (`git add <paths>`, never `git add .` / `-A`). Check
`git diff --cached` once before committing: nothing unrelated, no secrets, no stray files.

One commit is preferred; several are fine if they are genuinely separate changes. Message format:

* Conventional prefix, **no scope**, first line lower-case, imperative, ≤72 chars:
  `feat: add --foo to nodes provision`.
* Blank line, then **why** the change was made (the issue's motivation, in your words), wrapped
  at 100 columns. A test summary line (`3610 passed` etc.).
* End with the attribution line the session's system reminder specifies, if one is present.

## Step 7 — Push and open the PR

```bash
git push -u origin <gitBranchName>
gh pr create --repo jacderida/saorsa-testnet-registry --base main --head <gitBranchName> \
  --title "<commit first line, or a one-line summary if several commits>" \
  --body-file <file>
```

The PR body has, in this order:

1. The Linear issue id and URL on the first line (so Linear links it), then the
   `Semantic version:` line from Step 5.
2. **Summary** — bullets of what changed and why.
3. **Test plan** — the exact commands run and their results (`ruff` clean, `N passed`), the
   issue-specific verification and what it showed.
4. **Deviations** — anything the issue asked for that you did differently or not at all, and why;
   pre-existing failures found on `origin/main`; things noticed but deliberately left alone.
5. **Session** — if `$CLAUDE_RUNNER_SESSION_ID` is set in the environment, a line reading
   ``Resume the session that produced this PR: `claude --resume <that id>` `` so review feedback can
   be handled in the conversation that has the full context, rather than from scratch. Omit the
   section entirely when the variable is unset.
6. The attribution line the session's system reminder specifies, if one is present.

Then set the issue to **In Review** and post a comment with the PR URL.

## Blocked

If you cannot complete the implementation — the issue is ambiguous in a way that changes the
code, a dependency is missing, the gate cannot be made to pass on your own changes:

1. If you have made useful progress: commit it as `wip: <summary>`, push, and open the PR as a
   **draft** (`gh pr create --draft …`) whose body leads with **Blocked:** and the exact question
   or failure. If nothing useful exists, commit nothing and open no PR.
2. Set the issue to **Blocked** and post a comment with the same question or failure and the
   draft PR URL if there is one.
3. Continue to Step 8 with status `BLOCKED`.

## Step 8 — Exit

Return the checkout to `main` (`git checkout main`) so the next issue starts clean; the branch
stays pushed. `git status --porcelain` must be empty — if it is not, say what is left and why.

**Your final message is one line and nothing else:**

```
IMPLEMENT COMPLETE <issue> <COMPLETED|BLOCKED|FAILED> <pr-url or ->
```

where the verdict is one of:

| verdict | meaning | the issue's status |
| -- | -- | -- |
| `COMPLETED` | the change is written, the gate passed, the branch is pushed and the PR is open | `In Review` |
| `BLOCKED` | you could not finish; the question or failure is in a comment | `Blocked` |
| `FAILED` | a precondition failed and nothing was changed | left alone, with a comment |

`COMPLETED` is about **your** work, not about the review: the change is finished and submitted. The
operator deciding whether to accept it happens afterwards and is none of this run's business.

No summary before the line, no sign-off after it, no prose around it — what you did is already
written up in the PR body and the issue comment, and whoever wants it reads it there. Then **end
your turn with no pending tool calls**: that is what ends the headless process, and the caller waits
on it and will not start the next issue until it exits.

This line is the only evidence the run finished. Nothing infers completion from the exit code,
because the process exits zero whenever you end your turn — so a run that did everything the issue
asked and then signed off with a summary instead of this line is recorded as `NO-VERDICT`, raised
with the operator as urgent, and has to be read back out of the raw log by hand. Without the line
the run is a failure as far as anything outside this session can tell, whatever it achieved.
Ending a run that went perfectly with a summary in place of this line happened on 2026-10-13 and is
the most expensive way there is to finish.
