---
name: run-client-prod-tests
description: Run the release train's client production tests — the STG-UL-01 upload and STG-DL-01 download deployments, both driving the RC `ant` binary against the real production Autonomi network. Reads the current Release Train issue from Linear for the ant-cli RC version, preflights the upload URL list against files already uploaded to production, updates both manifests, deploys them in parallel, waits for the transfers to finish, records results to Postgres, tears down the passing side, and closes the "Production compat — upload/download (cloud VM)" sub-issues. Use when asked to "run the client production tests", "do the prod client tests", "run the production compat tests", "run the client tests for rc.N", or "deploy STG-UL-01 and STG-DL-01".
---

# run-client-prod-tests

Runs the current release train's **client production tests**: two small, independent client
deployments that exercise the release-candidate `ant` binary against the **real production
Autonomi network**.

| Deployment | Kind | Manifest | What it does |
|---|---|---|---|
| `STG-UL-01` | `prod-uploaders` | `deploy/manifests/runs/staging/STG-UL-01.yaml` | Uploads the files in a URL list to production with `ant --json file upload --public`, then exits |
| `STG-DL-01` | `prod-downloaders` | `deploy/manifests/runs/staging/STG-DL-01.yaml` | Downloads a fixed address list once with `ant -v file download`, then exits |

They are separate tests with separate deployments, separate results and separate Linear issues.
Neither depends on the other.

Rules:

- **This runs against production and costs real money.** The uploader spends real ETH from a
  funded Arbitrum One wallet. Do not improvise with the configuration and do not re-run an upload
  "to see if it works this time".
- **Coordination only.** Every action is a `saorsa-deploy` CLI command, a read-only probe, or a
  Linear update.
- **Never pass `--allow-duplicate-files`, and never suggest it.** Re-uploading a file that is
  already stored does not test the production upload path — it tests the already-stored fast path,
  which is a different thing. If the preflight in Step 3 fails, the answer is fresh URLs from a
  human, not an override.
- **A failed test is a finding, not something to repair.** Do not restart the service, re-run the
  upload, or redeploy to make a result look better. Record it, report it, leave that deployment
  standing, and hand back.
- **Unattended after Step 3.** The preflight is the one place this skill stops to ask. Past it,
  report progress and keep going; stop only on a failure or an unmet precondition.
- **This skill covers two of six planned client tests.** The low-bandwidth and canary-corpus tests
  are not automated yet and are out of scope — see Known gaps.

## Step 1 — Read the release train issue

Find the current train:

1. `list_issues` with `project: "Releases"`, `team: "v2.0"`, `state: "In Progress"`.
2. Expect exactly one issue titled `Release Train — YYYY-MM-DD`. If there are none or more than
   one, stop and ask.
3. `get_issue` on it for the full description.

Extract:

| What | Where | Example |
|---|---|---|
| Train date | Issue title, `Release Train — <date>` | `2026-07-28` |
| `ant` RC version | `## Cut record` → versions table, **`ant-cli`** row, after the arrow | `0.3.2-rc.1` |
| Risks to watch | `## Risks to watch in staging/canaries` | — |

Notes:

- **Only the `ant-cli` version applies.** Both manifests are client-only kinds — there is no
  `ant-node` binary in either. `ant-cli` is the crate; `ant` is the binary it produces, and the
  version goes into each manifest's `binary.version`.
- **Read the RC carefully — the table's shape is not what you might expect.** The `## Cut record`
  table has columns `Crate | Version | rc-branch head`, and the `Version` cell holds *both*
  versions in one field as `<previous> → **<RC>**`:

  | Crate | Version | rc-branch head |
  | -- | -- | -- |
  | ant-cli | 0.3.1 → **0.3.2-rc.1** | `48cffdb…` |

  You want the **bolded right-hand side**. There is no separate "New" column. Cross-check against
  the `**RC tags**` bullets immediately below the table, which name it unambiguously — if the two
  disagree, stop and ask.
- The `## Cut record` table is authoritative over the earlier `### Final versions` table, which
  lists *release* versions rather than the RC and can be superseded at cut time.
- If there is no `## Cut record` section, the cut has not happened. Stop.

### Find the two test issues — they are two levels down

The issues are **not** direct children of the train. The tree is:

```
Release Train — <date>
└── Client production tests
    ├── Production compat — upload (cloud VM)      ← STG-UL-01
    ├── Production compat — download (cloud VM)    ← STG-DL-01
    ├── Low-bandwidth — upload / download          (usually Canceled)
    └── Canary corpus — upload / download          (usually Canceled)
```

So: `list_issues` with `parentId: <train id>` → find **`Client production tests`** → `list_issues`
with `parentId: <that id>` → find the two whose titles start with **`Production compat —`**.

- Match on the `Production compat —` prefix. The real titles carry a `(cloud VM)` suffix.
- **Ignore any child whose status is `Canceled`.** Finding six children instead of two is normal,
  not an error. If either `Production compat` child is missing or already `Done`, stop and ask.
- **`get_issue` on both children and read their descriptions.** They carry per-train instructions
  that the parent does not. For the 2026-07-28 train, the upload issue asks for a batch of 64+
  chunks to exercise ant-client #161's 3x merkle settlement path, and for actual spend to be
  recorded against the quoted estimate. Carry anything you find there into the Step 9 report, and
  see Known gaps for what the tooling can and cannot show.

Leave all three issues in Todo for now — Step 6 moves the two children to In Progress once the
deployments are actually up.

Report the extracted RC version, the two issue ids, and any per-train asks before continuing.

## Step 2 — Check preconditions

Stop and name the item that failed if any of these is not met:

- `DATABASE_URL` is set and reaches the registry. Check with `uv run saorsa-deploy deployments ls`.
  Without it the preflight in Step 3 has nothing to compare against and would silently skip.
- `ARBITRUM_ONE_SECRET_KEY` is set and the wallet is funded. This is **mainnet ETH** — a
  deliberately different variable from the testnet `ANT_DEPLOY_SECRET_KEY`. An unfunded wallet
  fails the upload partway through, after infrastructure has been paid for.
- `DO_TOKEN` — both client VMs and both InfluxDB monitoring VMs are DigitalOcean.
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — S3 deployment state.
- `~/.ssh/ant_deploy` exists locally. Both manifests name it as `ssh_key_path`, and it is the key
  that actually works on the VMs.

**Check the previous runs.** Both names are reused every train:

```bash
uv run saorsa-deploy deployments ls | grep -E 'STG-UL-01|STG-DL-01'
```

- **Note the highest existing id for each name before you start.** This is how you prove at Step 8
  that the rows you record are *this* train's. A reused deployment name has silently no-op'd a
  recording command and lost a whole run's data before.
- If the most recent row for either name is anything other than `destroyed` — `running`, `failed`,
  or stuck at `destroying` — **stop and ask**. Do not auto-destroy it and do not deploy over it. A
  previous run's results may not have been extracted yet, and a half-destroyed one means live
  cloud resources a new deploy would collide with.

## Step 3 — Preflight the upload URL list

**This is the one step that stops and asks a human.** Do it before touching either manifest and
before creating any infrastructure.

Production uploads must be of files that have **never been uploaded to the network before**.
A file that is already stored exercises the already-stored path, not the upload path, so the test
would silently measure the wrong thing.

1. Read the URL list path out of the manifest — `prod_uploaders.uploaders[0].urls` in
   `deploy/manifests/runs/staging/STG-UL-01.yaml`. It is an absolute path.

2. **Confirm the file exists and is non-empty.** `deploy/manifests/runs/` is git-ignored, so this
   file is not in version control and is frequently simply absent. A missing or empty list is the
   same problem as a stale one: the human has not supplied this train's links.

   Nothing else catches this. `deployments config validate` checks the manifest's *schema*, not
   whether the paths inside it resolve — it reports `OK` for an `STG-UL-01.yaml` whose `urls:` file
   does not exist. The failure would otherwise surface only once the VM was already provisioned.

3. Extract the **basename** of each URL — the last path segment, which is what the uploader
   service records as the file name:

   ```bash
   grep -v '^\s*#' <urls-file> | grep -v '^\s*$' | sed 's|.*/||'
   ```

4. List what production already holds:

   ```bash
   uv run saorsa-deploy prod files
   ```

   Output is one section per upload run (`=== NAME (id=N)  kind  started TS ===`) with each file's
   name on its own unindented line, followed by indented `address:` / `size:` / `uploaded:` lines.
   The set of already-uploaded names is therefore the unindented, non-`===`, non-blank lines:

   ```bash
   uv run saorsa-deploy prod files | grep -v '^ ' | grep -v '^===' | grep -v '^$'
   ```

5. Compare **basename against basename**. That is exactly what the tool itself compares (see
   below), so any other comparison would let this preflight pass where the deploy then aborts.

**If every name is new**, say so and go on to Step 4.

**If any name collides — or the file is missing or empty — stop.** Report:

- which URLs collide, and when each was previously uploaded (`prod files` prints `uploaded:`), or
  that the list file is missing/empty;
- that the fix is fresh public URLs in that file, which only a human can supply today;
- that you are not proceeding with either deployment.

Do not deploy the download side either. Keeping the pair in step means a re-run after the human
edits the list is a clean single run rather than a half-finished one.

> **Why check at all when the tool already does?** `prod uploaders deploy` — and therefore
> `deployments up` — runs its own duplicate-file gate (`_check_for_duplicate_files` in
> `deploy/saorsa_deploy/cmd/prod_uploaders.py`) before creating infrastructure, and exits 1 on a
> collision. Doing it here first buys three things: the download side is not left running alone
> after the upload aborts; the human gets a report naming the stale URLs and their upload dates
> rather than a bare list of file names; and the missing-file case, which the tool does not treat
> as a distinct error, gets caught. It is a pre-check, not a replacement — the tool's gate stays
> the backstop.

## Step 4 — Update the manifests

Always use these two manifests, paths relative to the repo root:

```
deploy/manifests/runs/staging/STG-UL-01.yaml
deploy/manifests/runs/staging/STG-DL-01.yaml
```

Edit **two things in each and nothing else**:

| Manifest | Field | New value |
|---|---|---|
| `STG-UL-01.yaml` | `prod_uploaders.binary.version` | the `ant-cli` RC from Step 1 |
| `STG-UL-01.yaml` | `purpose` | `"Release Train <date>: production compat upload (cloud VM)"` |
| `STG-DL-01.yaml` | `prod_downloaders.binary.version` | the same `ant-cli` RC |
| `STG-DL-01.yaml` | `purpose` | `"Release Train <date>: production compat download (cloud VM)"` |

**Everything else in both files is load-bearing.** In particular:

- `payment: merkle` on the uploader selects the batch payment path. When the train carries
  ant-client payment changes, that is the path under test — do not switch it.
- `clock_skew: 2h` on the downloader is deliberate: it deploys the client VM with its system clock
  displaced, which is a real-world condition the client has broken under before. It also makes the
  print output look odd (see Step 7). Do not remove it to tidy the output.
- `ssh_key_path: ~/.ssh/ant_deploy` is the key that works on these VMs.
- `slack_post: true` posts the deployment summary; `secret_key_env: ARBITRUM_ONE_SECRET_KEY` names
  the wallet variable rather than embedding a key.
- The uploader's `urls:` and the downloader's `addresses_path:` are the test inputs. The URL list
  is the human's to edit (Step 3); the address list is fixed.

Changing anything else is a re-baselining decision and needs the RM.

Validate both edits:

```bash
uv run saorsa-deploy deployments config validate --config deploy/manifests/runs/staging/STG-UL-01.yaml
uv run saorsa-deploy deployments config validate --config deploy/manifests/runs/staging/STG-DL-01.yaml
```

Then proceed straight to the deploy. Do not stop to show a plan or ask for approval.

## Step 5 — Deploy both, in parallel

```bash
uv run saorsa-deploy deployments up --config deploy/manifests/runs/staging/STG-UL-01.yaml
uv run saorsa-deploy deployments up --config deploy/manifests/runs/staging/STG-DL-01.yaml
```

Run these **concurrently** — as two background commands, not one after the other. Each is a
single-step sequence (`prod uploaders deploy` / `prod downloaders deploy`) that creates one client
VM plus a paired InfluxDB monitoring VM, provisions the service, and starts it.

> **Why parallel is safe here**, when the usual rule is one `up`/`down` at a time: that rule is
> **per deployment name**. These are two different names, so they touch different S3 state keys
> (`saorsa-deploy/{name}.json`, `saorsa-deploy/monitoring/{name}.json`) and different Terraform
> state keys (which embed the name). Nothing is shared. Two `up` runs against the *same* name
> would still race.

These deployments are small and come up quickly — minutes, not the hours a testnet takes.

When each one's last step completes, record its wall-clock time. Those are **T0(UL)** and
**T0(DL)**; they go in the report and they are what the elapsed-time judgements in Step 7 are
measured from.

### When a deploy step fails

Two classes, handled differently. Handle each deployment independently — one failing does not
mean stopping the other.

**Infrastructure creation** (VM or volume creation at DigitalOcean). Do **not** retry. These are
usually provider-side capacity or maintenance problems and they last a while.

1. Stop that deployment's run.
2. Clear whatever was created:
   ```bash
   uv run saorsa-deploy deployments down --config deploy/manifests/runs/staging/STG-UL-01.yaml
   ```
3. Report which step failed and the error.

**Provisioning** (anything installing software over SSH). Usually transient SSH failures. Retry:

```bash
uv run saorsa-deploy deployments up --config deploy/manifests/runs/staging/STG-UL-01.yaml --resume
```

`--resume` skips already-completed steps using the journal. Without it a non-TTY journal hit
refuses rather than hanging.

Retry a few times. It usually passes. After about three attempts with no progress, treat it as
terminal: stop, `deployments down --config <that manifest>`, and report.

Do not work around infrastructure or permission problems in any other way.

## Step 6 — Mark the tests in progress

Once **both** deployments are up, move both `Production compat` child issues to **In Progress**.
The transfers occupy them for a while, and leaving them in Todo makes a train that is actively
running client tests look untouched.

Leave `Client production tests` (the parent) alone for now — Step 10 handles it.

## Step 7 — Wait for the transfers to finish

Both services are `Type=simple Restart=no`: they walk their list once and exit. There is no
completion event to subscribe to, so completion is inferred from **record counts** — a record only
appears once its operation has finished.

Work out the expected counts from the input files rather than assuming:

- **Uploads expected** = number of non-blank, non-comment URLs in the uploader's `urls:` file
  (currently 1).
- **Downloads expected** = number of addresses in the downloader's `addresses_path:` file
  (currently 5, from `addresses/5_large_addresses.txt`).

Poll both, roughly every 10 minutes, and report a one-line status each time:

```bash
uv run saorsa-deploy deployments print-uploads   --name STG-UL-01
uv run saorsa-deploy deployments print-downloads --name STG-DL-01 --summary
```

Use the matching command for each deployment. `print-downloads` against the uploader (or vice
versa) prints a warning and no data — the command dispatches on the deployment's kind.

**Reading the upload output.** One table row per completed upload, with columns
`# / File / Size / Status / Chunks Uploaded (stored/total) / Failed / Duration`. Status is `ok`,
`partial` (some chunks failed) or `dl-fail` (the source URL could not be fetched — a harness
problem, not a network one). Before the first upload lands you get
`No prod_upload records yet for 'STG-UL-01'` — that is the normal in-progress state, not an error.
**Done = the expected number of rows.**

**Reading the download output.** `--summary` prints a run window (Started UTC, Elapsed) and a
per-host block of Total ops / Successes / Failures / Avg duration. **Done = Total ops equals the
expected number.** Drop `--summary` for the per-operation detail.

> **The download timestamps will look wrong, and that is correct.** `clock_skew: 2h` displaces the
> client VM's system clock, so its recorded start times — and therefore the "Started"/"Elapsed"
> figures — are shifted by that amount. The tables label the skew where they detect it. Judge
> download progress by the op count, not by elapsed time, and do not report the skew as a fault.

### Expected durations, and when to escalate

- The **download** side is 5 large files and typically finishes well before the upload.
- The **upload** is the long pole: **20–30 minutes is normal**, and longer is not automatically
  wrong. It is chunking a large file, quoting, paying on-chain and storing.
- At **~120 minutes with no completed upload**, something is probably wrong. Investigate — do not
  restart anything.

```bash
uv run saorsa-deploy deployments generate-inventory --name STG-UL-01 --ssh-key-path ~/.ssh/ant_deploy
```

> **SSH key gotcha.** `generate-inventory` defaults `--ssh-key-path` to `~/.ssh/id_rsa`, which does
> **not** work on these VMs — pass `~/.ssh/ant_deploy` as above. If you still hit
> `Permission denied (publickey)`, confirm with a one-liner first:
> `ssh -i ~/.ssh/ant_deploy -o BatchMode=yes root@<ip> 'hostname'`.

Then, on the client VM:

```bash
ssh -i ~/.ssh/ant_deploy -o StrictHostKeyChecking=no -o BatchMode=yes root@<ip> '
  systemctl status ant-prod-uploader --no-pager | head -20
  echo "== recent service output =="
  journalctl -u ant-prod-uploader --no-pager -n 50
  echo "== per-file logs =="
  ls -la /var/log/ant-prod-uploader/
  tail -40 /var/log/ant-prod-uploader/*.stderr 2>/dev/null
'
```

The service keeps the full `ant` stdout **and** stderr for every file under
`/var/log/ant-prod-uploader/` (the downloader's equivalent is
`/var/log/ant-prod-downloader/{idx}-{addr_prefix}.{stdout,stderr}`). `ant --json` puts the machine
-readable result on stdout, so progress, quotes and errors are in the **stderr** log. That is where
a stall shows itself — a stuck chunk batch, a payment that never settled, an unreachable peer set.

- **Service still running with the log advancing** → it is slow, not stuck. Keep waiting and say so.
- **Service exited** (`Result=exit-code`, or active=inactive with no record) → the run is over and
  failed. Go to Step 8 and record what there is.
- **Service running but the log has not advanced in a long while** → report it as a stall with the
  last log lines as evidence. Do not restart it.

Report a failure the moment you see it. Do not intervene to improve the numbers.

## Step 8 — Record results (blocking, and unconditional)

Run this for **both** deployments as soon as each has finished, **whether it passed or failed**,
and **before any teardown**. A failed run's records are the whole point of having run it.

```bash
uv run saorsa-deploy deployments record-results --name STG-UL-01
uv run saorsa-deploy deployments record-results --name STG-DL-01
```

**Ordering is not negotiable: record before you destroy.** `deployments down` tears down each
deployment's InfluxDB monitoring VM along with the client (neither manifest sets
`keep_monitoring`), and that InfluxDB is where the per-operation records live until this command
copies them into Postgres. Destroy first and the results are gone for good.

**`record-results` can take several minutes.** Give it a timeout of at least 10 minutes
(600000 ms). A short timeout kills it mid-write and looks like a failure.

Do not pass `--window`. For `prod-uploaders` it is ignored (the run is bounded); for
`prod-downloaders` the default `-30d` more than covers a same-day run.

Verify, and only continue if rows are actually present:

```bash
uv run saorsa-deploy deployments ls        # find this run's STG-UL-01 / STG-DL-01 ids
uv run saorsa-deploy deployments print-recorded-results --id <id>
```

This command takes **`--id`, not `--name`** — `--name` exits 2 with a usage error. That is the
right interface here anyway: the id is what proves the rows belong to **this** run. Check each id
is greater than the one you noted in Step 2.

For the upload, `print-recorded-results` is also where the **on-chain spend** appears — the
`ANT cost` and gas columns, and per-record `Storage cost` / `Gas cost` lines in the detail view.
The live `print-uploads` does not show cost, so this is the only place to read it.

If recording fails or returns nothing, stop. Leave both deployments up and hand back — the data is
still in InfluxDB while the monitoring stacks are alive, and gone once they are destroyed.

## Step 9 — Tear down the passing side only

Decide **per deployment**, not for the pair:

- **Passed** — every expected operation completed with success status:
  ```bash
  uv run saorsa-deploy deployments down --config deploy/manifests/runs/staging/STG-UL-01.yaml
  ```
- **Failed** — any operation failed, `partial`, `dl-fail`, or never completed: **leave it up.**
  The VM, its per-file logs under `/var/log/ant-prod-*`, and its InfluxDB are the only forensic
  surface, and `down` destroys all three. Say plainly in the report that it is still running, name
  its VM IP, and say it will keep costing money until someone destroys it.

Confirm anything you tore down shows `destroyed` in `deployments ls` before reporting done.

## Step 10 — Report and update Linear

Emit this report, then post the relevant half as a comment on each `Production compat` child issue:

```
# Client production tests — Release Train <date> (rc.N)

ant (ant-cli) RC: <version>
URL preflight:    PASS — <N> URL(s), none previously uploaded

## Production compat — upload (cloud VM)

Deployment: STG-UL-01 (registry id <id>) — DigitalOcean <region> <vm_size>, payment=merkle
T0:         <timestamp, UTC>
Result:     PASS | FAIL
Files:      <ok>/<expected> ok, <partial> partial, <dl-fail> dl-fail
Detail:     <file name> — <size> — <chunks stored>/<total> chunks — <duration>
Spend:      <ANT storage cost> / <ETH gas> (from print-recorded-results)
Teardown:   destroyed | STILL RUNNING at <ip> (failed — kept for investigation)

## Production compat — download (cloud VM)

Deployment: STG-DL-01 (registry id <id>) — DigitalOcean <region> <vm_size>, clock_skew=2h
T0:         <timestamp, UTC>
Result:     PASS | FAIL
Ops:        <successes>/<expected> succeeded, <failures> failed — avg <duration>
Teardown:   destroyed | STILL RUNNING at <ip> (failed — kept for investigation)

## Notes

<per-train asks from the child issues and whether they were met>
<risks to watch from the train issue>
<anything off-band; any check that could not run and why>
```

Fill every field — an unfillable field is itself a finding, so say why.

Then update the issues:

- **`Production compat — upload (cloud VM)`**: PASS → **Done**. FAIL → **In Review**, with the
  failure and the still-running deployment stated plainly.
- **`Production compat — download (cloud VM)`**: same rule, judged independently.
- **`Client production tests`** (the parent): move to **Done** only when **both** children are
  Done. If either went to In Review, leave the parent **In Progress** — its own definition is that
  it closes once all child tests are recorded, and a test in review is not yet settled. The
  Canceled children do not block it.

Finally, if either deployment is still running, say so as the last line of your reply, with the
name, the VM IP and the fact that it is billing.

## Known gaps

State these in the report rather than working around them:

- **No content verification on downloads.** A download counts as a success if `ant` exits 0; the
  bytes are never compared against the source. The data-integrity gate is therefore not actually
  covered by this test. Tracked as V2-757.
- **No completion signal.** Neither service reports "finished" anywhere the CLI can read. Completion
  is inferred by counting records against the input-file length, so a service that dies after
  emitting its last record is indistinguishable from one that finished cleanly. The systemd state
  over SSH is the tiebreaker.
- **The quoted cost estimate is not recorded anywhere queryable.** Actual spend
  (`storage_cost_atto` / `gas_cost_wei`) lands in Postgres and prints via `print-recorded-results`,
  but the client's *pre-upload quote* only ever appears in the `ant` stderr log on the VM
  (`/var/log/ant-prod-uploader/*.stderr`). When a train asks for estimate-versus-actual, capture
  that log over SSH **before Step 9 tears the VM down** — after teardown it is unrecoverable.
- **The URL list is a human input.** There is no pool of known-unused public URLs to draw from, so
  the Step 3 preflight can only ever stop and ask. Tracked as V2-802.
- **Only two of six client tests are automated.** Low-bandwidth upload/download need a
  bandwidth-limit option that does not exist yet (V2-733); canary-corpus upload/download need
  corpus tooling that does not exist yet. Both pairs are normally `Canceled` on the train and are
  out of scope here. Overall skill scope is tracked as V2-734.
- **A single upload of a single file is a thin test.** It exercises the path, not its reliability;
  one `partial` result is the difference between PASS and FAIL with no sample size behind it.
