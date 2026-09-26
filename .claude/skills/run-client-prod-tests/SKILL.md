---
name: run-client-prod-tests
description: Run the release train's client production tests — the STG-UL-01 / STG-DL-01 native pair and the STG-WUL-01 / STG-WDL-01 browser-path (WASM) pair, all four driving the release candidate against the real production Autonomi network. Reads the current Release Train issue from Linear for the ant-cli RC version, resolves the matching ant-client tag for the WASM bundle, preflights the upload file sources, updates the four manifests, deploys them in parallel, waits for the transfers to finish, records results to Postgres, tears down the passing sides, and closes the four "Production compat — …(cloud VM)" sub-issues. Use when asked to "run the client production tests", "do the prod client tests", "run the production compat tests", "run the client tests for rc.N", "deploy STG-UL-01 and STG-DL-01", or "run the WASM production compat tests".
---

# run-client-prod-tests

Runs the current release train's **client production tests**: four small, independent client
deployments that exercise the release candidate against the **real production Autonomi network** —
two over the native `ant` binary and two over the **browser path** (`ant-core` compiled to WASM,
dialling nodes over WebRTC-Direct).

| Deployment | Kind | Manifest | Transport | What it does |
|---|---|---|---|---|
| `STG-UL-01` | `prod-uploaders` | `deploy/manifests/runs/staging/STG-UL-01.yaml` | native | Uploads a pool draw to production with `ant --json file upload --public`, then exits |
| `STG-DL-01` | `prod-downloaders` | `deploy/manifests/runs/staging/STG-DL-01.yaml` | native | Downloads a fixed address list once with `ant -v file download`, then exits |
| `STG-WUL-01` | `prod-uploaders` | `deploy/manifests/runs/staging/STG-WUL-01.yaml` | **wasm** | Same, over `ant-wasm` — one `wasm_uploaders[]` entry, `uploaders: []` |
| `STG-WDL-01` | `prod-downloaders` | `deploy/manifests/runs/staging/STG-WDL-01.yaml` | **wasm** | Same, over `ant-wasm` — `client_kind: wasm` |

They are four separate tests with separate deployments, separate results and separate Linear
issues. None depends on another.

**Why the browser path is a separate pair of deployments rather than WASM entries bolted onto
STG-UL-01 / STG-DL-01.** Below the data layer the two transports share almost nothing — different
transport, different admission handshake, a payment route through a JS callback instead of evmlib,
a 1,000,000,000-byte file cap, ~4x the file held in memory. Keeping them apart means a browser-path
failure can never block or alter the native tests' recording or teardown, and each keeps its own
registry id so a later `compare`-style history has one series per transport.

Rules:

- **This runs against production and costs real money.** Both uploaders spend real ETH from the
  same funded Arbitrum One wallet, and a browser-path upload sends **two** transactions per file
  (ERC-20 `approve`, then `payForQuotes`) where a native one sends one. Do not improvise with the
  configuration and do not re-run an upload "to see if it works this time".
- **Coordination only.** Every action is a `saorsa-deploy` CLI command, a read-only probe, or a
  Linear update.
- **Never pass `--allow-duplicate-files`, and never suggest it.** Re-uploading a file that is
  already stored does not test the production upload path — it tests the already-stored fast path,
  which is a different thing. If the preflight in Step 3 fails, the answer is a topped-up pool or
  fresh URLs from a human, not an override.
- **A failed test is a finding, not something to repair.** Do not restart the service, re-run the
  upload, or redeploy to make a result look better. Record it, report it, leave that deployment
  standing, and hand back.
- **The WASM pair is ADVISORY, and independently so.** Report its counts, its failure modes and its
  over-cap skips; never let either WASM result change the native verdict, gate the train, or delay
  the native side's recording or teardown. This mirrors `wasm_floor_gates_advisory: true` in the
  testnet gate thresholds: the browser path has no calibrated floors against production yet, and the
  first trains are what establish them. Floors land in
  `deploy/saorsa_deploy/resources/gates/staging-thresholds.yaml` in a follow-up.
- **A WASM failure never stops the native half, and vice versa.** Handle each of the four
  deployments independently at every step — deploy, wait, record, tear down, report. If the WASM
  build or deploy cannot be made to work at all, say so, run the native pair to completion, and
  record the WASM pair as **SKIPPED with the reason** (Step 10).
- **Unattended after Step 3.** The preflight is the one place this skill stops to ask. Past it,
  report progress and keep going; stop only on a failure or an unmet precondition.
- **This skill covers four of eight planned client tests.** The low-bandwidth and canary-corpus
  tests are not automated yet and are out of scope — see Known gaps.

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
| `ant-client` RC tag | derived from the above: `ant-cli-v<RC>` | `ant-cli-v0.3.2-rc.1` |
| Risks to watch | `## Risks to watch in staging/canaries` | — |

Notes:

- **Only the `ant-cli` version applies.** All four manifests are client-only kinds — there is no
  `ant-node` binary in any of them. `ant-cli` is the crate; `ant` is the binary it produces, and the
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

### Resolve the WASM bundle's source ref from the same RC

There is **no published WASM artifact**: no ant-core WASM package ships as a release, so
`wasm_binary` has only a `branch:` form and the bundle is built from source by `build ant-wasm` (a
resumable step `deployments up` emits before the deploy). `git clone --branch` accepts a **tag** as
readily as a branch, and that is how the RC is pinned.

So the WASM pair's bundle ref is the ant-client **release tag for the same `ant-cli` RC**:

| `ant-cli` RC | `wasm_binary.branch` |
|---|---|
| `0.3.8-rc.1` | `ant-cli-v0.3.8-rc.1` |

with `repo_owner: WithAutonomi`. **Confirm the tag exists before editing anything** — a typo here
burns a ~10-minute build VM and then fails:

```bash
git ls-remote --tags https://github.com/WithAutonomi/ant-client "refs/tags/ant-cli-v<RC>"
```

An empty result means the RC tag has not been pushed. That is a hand-back, not something to work
around: do **not** substitute the released tag, a branch name, or the previous RC — the whole point
is that the browser path is exercised on *this* candidate's code.

The tag is a moving target in only one direction: it names a commit, and the commit is what
identifies the code. `build ant-wasm` writes that commit into an S3 sidecar and the deploy reads it
into `deploy_args.wasm_build`, which is what Step 10 records on the sub-issue — not the tag.

### Find the test issues — they are two levels down

There are **four**, two native and two WASM. The two WASM ones were added to the release-train
template by V2-1338; on a train created before that they will be absent, which is handled below.

The issues are **not** direct children of the train. The tree is:

```
Release Train — <date>
└── Client production tests
    ├── Production compat — upload (cloud VM)           ← STG-UL-01
    ├── Production compat — download (cloud VM)         ← STG-DL-01
    ├── Production compat — WASM upload (cloud VM)      ← STG-WUL-01   (V2-1338 onwards)
    ├── Production compat — WASM download (cloud VM)    ← STG-WDL-01   (V2-1338 onwards)
    ├── Low-bandwidth — upload / download               (usually Canceled)
    └── Canary corpus — upload / download               (usually Canceled)
```

So: `list_issues` with `parentId: <train id>` → find **`Client production tests`** → `list_issues`
with `parentId: <that id>` → find the children whose titles start with **`Production compat —`**.

- Match on the `Production compat —` prefix, then on whether the remainder contains **`WASM`**.
  The real titles carry a `(cloud VM)` suffix. Never match on position in the list.
- **Ignore any child whose status is `Canceled`.** Finding eight children instead of four is
  normal, not an error. If either **native** `Production compat` child is missing or already `Done`,
  stop and ask.
- **A train that predates V2-1338 has no WASM children, and that is not an error.** Run the WASM
  pair anyway and post its results **on the train issue itself** as one comment, clearly headed
  `Production compat — WASM (no sub-issue on this train)`. Do not create the sub-issues yourself —
  the template owns them — and do not skip the tests for want of somewhere to file them.
- **`get_issue` on every non-Canceled child and read its description.** They carry per-train
  instructions that the parent does not. For the 2026-07-28 train, the upload issue asks for a batch
  of 64+ chunks to exercise ant-client #161's 3x merkle settlement path, and for actual spend to be
  recorded against the quoted estimate. Carry anything you find there into the Step 10 report, and
  see Known gaps for what the tooling can and cannot show.

Leave every issue in Todo for now — Step 6 moves the children to In Progress once the deployments
are actually up.

Report the extracted RC version, the resolved `ant-client` tag, the issue ids you found (and which
WASM ones are absent), and any per-train asks before continuing.

## Step 2 — Check preconditions

Stop and name the item that failed if any of these is not met:

- `DATABASE_URL` is set and reaches the registry. Check with `uv run saorsa-deploy deployments ls`.
  Without it the preflight in Step 3 has nothing to compare against and would silently skip.
- `ARBITRUM_ONE_SECRET_KEY` is set and the wallet is funded. This is **mainnet ETH** — a
  deliberately different variable from the testnet `ANT_DEPLOY_SECRET_KEY`. An unfunded wallet
  fails the upload partway through, after infrastructure has been paid for.
- `DO_TOKEN` — all four client VMs and all four InfluxDB monitoring VMs are DigitalOcean.
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — S3 deployment state, and the WASM bundle's build
  artifact and its `build-info.json` sidecar.
- `~/.ssh/ant_deploy` exists locally. All four manifests name it as `ssh_key_path`, and it is the key
  that actually works on the VMs.
- **The wallet is sized for all three uploads, not one.** STG-UL-01 draws four pool files and
  STG-WUL-01 three, and every browser-path upload sends two transactions (`approve` +
  `payForQuotes`) where a native one sends one. An exhausted wallet fails an upload partway through,
  after infrastructure has been paid for, and the failure is indistinguishable in the result rows
  from a network fault.

**Check the previous runs.** All four names are reused every train:

```bash
uv run saorsa-deploy deployments ls | grep -E 'STG-UL-01|STG-DL-01|STG-WUL-01|STG-WDL-01'
```

- **Note the highest existing id for each of the four names before you start.** This is how you
  prove at Step 8 that the rows you record are *this* train's. A reused deployment name has silently
  no-op'd a recording command and lost a whole run's data before.
- If the most recent row for any of the four is anything other than `destroyed` — `running`,
  `failed`, or stuck at `destroying` — **stop and ask** for that name. Do not auto-destroy it and do
  not deploy over it. A previous run's results may not have been extracted yet, and a
  half-destroyed one means live cloud resources a new deploy would collide with.
- A name with **no** rows at all is the normal first-train state for `STG-WUL-01` / `STG-WDL-01`.
  Record "no previous run" rather than treating it as a failure.

**Check the production bootstrap WebRTC list is answering.** The browser path's only way in is
`deploy/saorsa_deploy/resources/prod_bootstrap_webrtc_multiaddrs.txt`, and every entry is ICE-probed
before any infrastructure is created: zero answering **refuses** the deploy, a partial answer warns
and names the dead entries. That probe runs inside the deploy, so nothing needs doing here — but if
it refuses, the fix is `prod bootstrap webrtc-addresses` to refresh the list (an operator action
against the production bootstrap tier, **not** something to bypass with
`--skip-bootstrap-probe`). Record the probe's `total`/`answered` in the Step 10 report; it comes back
in `deploy_args.wasm_bootstrap`.

## Step 3 — Preflight the upload file sources

**This is the one step that stops and asks a human.** Do it before touching any manifest and
before creating any infrastructure.

Production uploads must be of files that have **never been uploaded to the network before**.
A file that is already stored exercises the already-stored path, not the upload path, so the test
would silently measure the wrong thing.

**Which preflight applies depends on the uploader's file source, and each uploader entry names
exactly one** — `pool:` or `urls:`. Read them out of the manifests rather than assuming:

| Manifest | Where | Today |
|---|---|---|
| `STG-UL-01.yaml` | `prod_uploaders.uploaders[0]` | `pool:` — small 2, medium 1, large 1 |
| `STG-WUL-01.yaml` | `prod_uploaders.wasm_uploaders[0]` | `pool:` — small 2, medium 1, bounded ≤300MB |

### 3a — Pool draws (`pool:`)

A pool draw satisfies the duplicate-file requirement **by construction**: the managed pool's `url`
inventory only ever hands out a file with no blocking claim, so nothing a draw returns has been
uploaded to production before. There is no URL list for a human to curate, and the deploy's own
duplicate-file gate deliberately skips a pool-sourced uploader — it checks a `urls:` list only. (It
briefly did name-check pool draws, which cost run #598 an abort when the pool's generic
`VTS_01_1.VOB` collided with seven unrelated uploads; the gate now keys on the uploader's
`pool_request` instead.)

Two things *can* go wrong, and both are cheap to check:

**A shortfall.** Selection runs before any infrastructure exists, so a shortfall aborts with no VMs
and no mainnet spend, naming which class fell short — it costs nothing but it does stop the run.
Check availability for every class both uploaders request, honouring each entry's
`pool_min_size` / `pool_max_size` bounds, because those narrow a class without changing its count:

```bash
uv run saorsa-deploy prod pool stats
uv run saorsa-deploy prod pool list --kind url --status available \
    --size <class> [--min-size <min>] [--max-size <max>] --limit 5
```

**Claims a previous train left open.** Every file behind an unresolved claim is withheld from the
pool, so a run whose results were never recorded silently shrinks the inventory the next one draws
from:

```bash
uv run saorsa-deploy prod pool claims
```

Report any open claim against `STG-UL-01` or `STG-WUL-01` from an earlier train. Resolving one is a
human decision — `prod pool mark-uploaded` if the file did reach production, `prod pool release` if
it did not — and never a sweep: a claim that might correspond to a real upload must not be freed
automatically.

- **If every requested class has at least the requested count available**, say so with the numbers
  and go on to Step 4.
- **If any class is short**, stop and report which class, the bound that narrowed it, how many are
  available and how many are needed. The fix is `prod pool add` — cataloguing more files, which
  needs a human's judgment about sources. Do **not** reduce a manifest's counts to fit the pool: the
  draw's shape is what makes one train's upload test comparable to the last one's.
- **`large` is not in the WASM draw and must not be added.** Every file in the class is above
  ant-core's 1,000,000,000-byte browser cap, so the manifest validator refuses it outright — a drawn
  one would be a permanently-spent pool entry that could never be uploaded.
- A **stale persisted draw** is worth one look. The draw is persisted to S3 under the deployment
  **name**, which every train reuses, so a previous train's selection sits under this train's key.
  `up --resume` only reuses it while every file still carries the exact `CLAIMED` claim it recorded;
  a resolved, released or replaced claim discards it and draws again. You do not need to intervene —
  but if `plan`/`up` reports reusing a selection, say so in the report, and never reach for
  `--allow-duplicate-files` to get past a complaint about it.

### 3b — URL lists (`urls:`)

Only if an uploader entry names `urls:` instead. Then:

1. Read the URL list path out of that entry. It is an absolute path.

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
- that you are not proceeding with any of the four deployments.

Do not deploy the download sides either. Keeping all four in step means a re-run after the human
edits the list is a clean single run rather than a half-finished one.

> **Why check at all when the tool already does?** `prod uploaders deploy` — and therefore
> `deployments up` — runs its own duplicate-file gate (`_check_for_duplicate_files` in
> `deploy/saorsa_deploy/cmd/prod_uploaders.py`) before creating infrastructure, and exits 1 on a
> collision. Doing it here first buys three things: the download side is not left running alone
> after the upload aborts; the human gets a report naming the stale URLs and their upload dates
> rather than a bare list of file names; and the missing-file case, which the tool does not treat
> as a distinct error, gets caught. It is a pre-check, not a replacement — the tool's gate stays
> the backstop.

### 3c — The download address lists (always)

Whatever the uploaders draw from, confirm both `addresses_path:` files exist and count their
non-blank, non-comment lines:

```bash
grep -cvE '^[[:space:]]*(#|$)' <addresses-file>
```

Those counts are the expected op counts Step 7 waits on, and nothing else catches a missing file —
`deployments config validate` reports `OK` for a manifest whose `addresses_path:` does not resolve,
and the failure then surfaces only once the VM has been provisioned.


## Step 4 — Update the manifests

Always use these four manifests, paths relative to the repo root:

```
deploy/manifests/runs/staging/STG-UL-01.yaml
deploy/manifests/runs/staging/STG-DL-01.yaml
deploy/manifests/runs/staging/STG-WUL-01.yaml
deploy/manifests/runs/staging/STG-WDL-01.yaml
```

Edit **only the fields below, and nothing else**:

| Manifest | Field | New value |
|---|---|---|
| `STG-UL-01.yaml` | `prod_uploaders.binary.version` | the `ant-cli` RC from Step 1 |
| `STG-UL-01.yaml` | `purpose` | `"Release Train <date>: production compat upload (cloud VM)"` |
| `STG-DL-01.yaml` | `prod_downloaders.binary.version` | the same `ant-cli` RC |
| `STG-DL-01.yaml` | `purpose` | `"Release Train <date>: production compat download (cloud VM)"` |
| `STG-WUL-01.yaml` | `prod_uploaders.binary.version` | the same `ant-cli` RC |
| `STG-WUL-01.yaml` | `prod_uploaders.wasm_binary.branch` | the `ant-client` tag from Step 1, e.g. `ant-cli-v0.3.8-rc.1` |
| `STG-WUL-01.yaml` | `purpose` | `"Release Train <date>: production compat WASM upload (cloud VM)"` |
| `STG-WDL-01.yaml` | `prod_downloaders.binary.version` | the same `ant-cli` RC |
| `STG-WDL-01.yaml` | `prod_downloaders.wasm_binary.branch` | the same `ant-client` tag |
| `STG-WDL-01.yaml` | `purpose` | `"Release Train <date>: production compat WASM download (cloud VM)"` |

`binary.version` on the two WASM manifests still names the native `ant` release, and still has to be
updated: nothing on those VMs runs `ant` (it is not even installed on the WASM uploader), but the
version is what the registry row and the Slack post label the run with, and a stale one mislabels
this train's results as the last one's.

**Everything else in all four files is load-bearing.** In particular:

- `payment: merkle` on both uploaders selects the batch payment path. When the train carries
  ant-client payment changes, that is the path under test — do not switch it. On the WASM uploader it
  is `ant-wasm file upload --public --merkle`, settled through the JS payment callback.
- `clock_skew: 2h` on STG-DL-01 is deliberate: it deploys the client VM with its system clock
  displaced, which is a real-world condition the client has broken under before. It also makes the
  print output look odd (see Step 7). Do not remove it to tidy the output. **STG-WDL-01 deliberately
  has no `clock_skew`** — that condition is a property of `ant`'s own quote and SigV4 handling, which
  STG-DL-01 covers, and adding it here would put two unmeasured variables on one run with no
  browser-path baseline to separate them. Do not add it.
- `uploaders: []` on STG-WUL-01 is deliberate: STG-UL-01 **is** the native control, and a second
  native uploader here would spend real ETH measuring the same thing twice.
- `pool_max_size: 300MB` on the WASM uploader is a schedule bound, not a correctness one. The browser
  path measured roughly five times slower than native on upload, so an unbounded `medium` draw (up to
  ~950 MB under the 1 GB cap) is a multi-hour upload holding the train open. Raising it is an RM
  decision about how long the train may wait.
- `forward_logs` is off on both WASM manifests. Turning it on stands up a dedicated Elasticsearch per
  deployment and tears it down again; the failure text this test reports comes from `journalctl` and
  the per-op logs over SSH, which a failed run keeps because a failed run is not torn down.
- `ssh_key_path: ~/.ssh/ant_deploy` is the key that works on these VMs.
- `slack_post: true` posts the deployment summary; `secret_key_env: ARBITRUM_ONE_SECRET_KEY` names
  the wallet variable rather than embedding a key. **Both uploaders share that one wallet** — that is
  intended, so both transports pay on the same chain from the same funded address.
- The uploaders' `pool:` (or `urls:`) and the downloaders' `addresses_path:` are the test inputs; see
  Step 3. The address lists are fixed.
- **STG-WDL-01's address list is a superset of STG-DL-01's, on purpose, and the extra entries are the
  measurement.** `addresses/wasm_addresses.txt` is STG-DL-01's five addresses verbatim followed by
  the ten `10_medium_addresses.txt` ones. The five are 2.5–2.9 GB, every one above ant-core's
  1,000,000,000-byte browser cap, so every one comes back as a **skip** rather than a failure: the
  content is stored and healthy, the transport refuses it. That is the over-cap count this test
  reports — and it is also why the two transports cannot fetch the same bytes for that corpus, which
  is a fact about the browser path, not a defect in the test. The ten medium entries (16–97 MB) are
  inside the cap and are what the browser path actually measures. Do not "fix" this by pointing
  STG-WDL-01 at the five-address list alone: that yields five skips, zero measured downloads, and a
  test that reads green having transferred nothing.

  **Attribute a failure on those ten before reading it as a browser-path finding.** They are the
  `10_medium_addresses.txt` corpus, last verified natively by `STG-LOCAL-DL-01` (#370, 2026-06-17,
  10/10) — old enough that a failure could be chunk loss on months-old content rather than anything
  about the transport. The cheap check is a native fetch of the same address from this machine
  (`prod local-downloads --addresses-path <just that address>`): if native fails too, it is the
  content, not the browser path, and the report must say so.

Changing anything else is a re-baselining decision and needs the RM.

Validate all four edits:

```bash
for m in STG-UL-01 STG-DL-01 STG-WUL-01 STG-WDL-01; do
  uv run saorsa-deploy deployments config validate --config deploy/manifests/runs/staging/$m.yaml
done
```

Then proceed straight to the deploy. Do not stop to show a plan or ask for approval.

## Step 5 — Deploy all four, in parallel

```bash
uv run saorsa-deploy deployments up --config deploy/manifests/runs/staging/STG-UL-01.yaml
uv run saorsa-deploy deployments up --config deploy/manifests/runs/staging/STG-DL-01.yaml
uv run saorsa-deploy deployments up --config deploy/manifests/runs/staging/STG-WUL-01.yaml
uv run saorsa-deploy deployments up --config deploy/manifests/runs/staging/STG-WDL-01.yaml
```

Run these **concurrently** — as four background commands, not one after another. Each native one is a
single-step sequence (`prod uploaders deploy` / `prod downloaders deploy`) that creates one client VM
plus a paired InfluxDB monitoring VM, provisions the service, and starts it.

> **Why parallel is safe here**, when the usual rule is one `up`/`down` at a time: that rule is
> **per deployment name**. These are four different names, so they touch different S3 state keys
> (`saorsa-deploy/{name}.json`, `saorsa-deploy/monitoring/{name}.json`) and different Terraform
> state keys (which embed the name). Nothing is shared. Two `up` runs against the *same* name
> would still race.

The native deployments are small and come up quickly — minutes, not the hours a testnet takes.

**The WASM pair has one extra step in front: `build ant-wasm`, and it is the long pole.** It stands
up a build VM, clones the ant-client tag, runs `npm ci` against the committed lockfile and
`wasm-pack`, and uploads `ant-wasm-client.tar.gz` plus a `build-info.json` sidecar to S3 — call it
**~10 minutes**. Two consequences worth knowing:

- Both WASM deployments build **separately**, because the S3 build key is namespaced by deployment
  name. That is two build VMs, in parallel, not one shared artifact. Do not try to share one.
- The step is journalled, so a `--resume` after a later failure **skips the rebuild** rather than
  paying that ten minutes again. Always retry a WASM deploy with `--resume` (see below).

The bootstrap ICE probe also runs in the WASM deploy step, before any infrastructure: **zero
answering refuses the deploy** and a partial answer warns and names the dead entries. A refusal is a
hand-back (refresh the list with `prod bootstrap webrtc-addresses`); a partial answer is a note for
the report, not a stop — ant-core cools a failed endpoint down for 30 minutes and a live subset is a
working deployment.

When each one's last step completes, record its wall-clock time. Those are **T0(UL)**, **T0(DL)**,
**T0(WUL)** and **T0(WDL)**; they go in the report and they are what the elapsed-time judgements in
Step 7 are measured from.

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

**Always retry a WASM deploy with `--resume`** so the journalled `build ant-wasm` step is skipped
rather than rebuilt. Without it you pay ~10 minutes per attempt for nothing.

**Two WASM-specific terminal failures, neither of which is retryable:**

- **The bootstrap probe refused** (zero of the production WebRTC multiaddrs answered). Nothing was
  created, so there is nothing to tear down. Hand back: the list needs refreshing with
  `prod bootstrap webrtc-addresses` against the production bootstrap tier. Do **not** pass
  `--skip-bootstrap-probe` to get past it — that flag exists for the known ant-node 0.20.0 window
  where the listener opens 2–15 minutes after a restart, and using it here would deploy a VM that
  bills from creation and fails every operation.
- **`build ant-wasm` failed on an ant-client API drift.** That is a finding about the candidate, not
  an infrastructure problem: record the compiler/bundler error verbatim and report the WASM pair as
  **FAILED at build** with that text. It does not stop, delay or alter the native pair.

Do not work around infrastructure or permission problems in any other way.

## Step 6 — Mark the tests in progress

Once a deployment is up, move **its own** `Production compat` child issue to **In Progress** — per
deployment, not once for the set, so a train whose WASM build is still running still shows its native
tests as live. The transfers occupy the issues for a while, and leaving them in Todo makes a train
that is actively running client tests look untouched.

On a train with no WASM sub-issues (pre-V2-1338), there is nothing to move for the WASM pair; note it
and carry on.

Leave `Client production tests` (the parent) alone for now — Step 10 handles it.

## Step 7 — Wait for the transfers to finish

All four services are `Type=simple Restart=no`: they walk their list once and exit. There is no
completion event to subscribe to, so completion is inferred from **record counts** — a record only
appears once its operation has finished. **The WASM pair's wait conditions are exactly the same** —
the transport changes which binary the service spawns and nothing about the service, its unit, its
per-op logs or its restart policy.

Work out the expected counts from the input files rather than assuming:

| Deployment | Expected = | Today |
|---|---|---|
| `STG-UL-01` | files in the uploader's `pool:` draw (or non-blank, non-comment lines in its `urls:` file) | 4 |
| `STG-WUL-01` | same, for the `wasm_uploaders[0]` entry | 3 |
| `STG-DL-01` | non-blank, non-comment lines in `addresses_path:` | 5 |
| `STG-WDL-01` | same | 15 — of which **5 are expected to be skipped as over-cap** |

The WASM download count is the whole list, skips included: a skip is a recorded row, so
`Total ops == 15` is what "finished" looks like. Judge it against 15, never against 10 — waiting for
10 successes would report a clean run as a stall.

Poll all four, roughly every 10 minutes, and report a one-line status each time:

```bash
uv run saorsa-deploy deployments print-uploads   --name STG-UL-01
uv run saorsa-deploy deployments print-uploads   --name STG-WUL-01
uv run saorsa-deploy deployments print-downloads --name STG-DL-01  --summary
uv run saorsa-deploy deployments print-downloads --name STG-WDL-01 --summary
```

Use the matching command for each deployment. `print-downloads` against an uploader (or vice
versa) prints a warning and no data — the command dispatches on the deployment's kind.

**Reading the upload output.** One table row per completed upload, with columns
`# / File / Size / Status / Chunks Uploaded (stored/total) / Failed / Duration`. Status is `ok`,
`partial` (some chunks failed) or `dl-fail` (the source URL could not be fetched — a harness
problem, not a network one). Before the first upload lands you get
`No prod_upload records yet for 'STG-UL-01'` — that is the normal in-progress state, not an error.
**Done = the expected number of rows.** On a run with WASM entries the output is split under two
headings, one per transport; STG-WUL-01 has only the WASM one.

**Reading the download output.** `--summary` prints a run window (Started UTC, Elapsed) and a
per-host block of Total ops / Successes / Failures / Avg duration. **Done = Total ops equals the
expected number.** Drop `--summary` for the per-operation detail. On STG-WDL-01 the output is
labelled with the `wasm` transport and carries the **skip** and **crash** counts alongside successes
and failures — read the skips as their own number, never folded into either.

> **The download timestamps will look wrong, and that is correct.** `clock_skew: 2h` displaces the
> client VM's system clock, so its recorded start times — and therefore the "Started"/"Elapsed"
> figures — are shifted by that amount. The tables label the skew where they detect it. Judge
> download progress by the op count, not by elapsed time, and do not report the skew as a fault.

### Expected durations, and when to escalate

- **STG-DL-01** is 5 large files and typically finishes well before the uploads.
- **STG-UL-01** is the native long pole: **20–30 minutes is normal**, and longer is not
  automatically wrong. It is chunking a large file, quoting, paying on-chain and storing.
- At **~120 minutes with no completed native upload**, something is probably wrong. Investigate — do
  not restart anything.
- **The WASM pair is slower, and by enough that native thresholds would misread it as a stall.**
  Measured against production: a browser-path upload runs roughly **five times** slower than native,
  and a browser-path download took **~60 s per file across 512 KB to 30 MB** — the browser path's
  cost is dominated by the WebRTC-Direct handshake and chunk fan-out rather than by file size. So:
  - `STG-WDL-01`: the 5 over-cap entries resolve a data map and are refused quickly; budget a few
    minutes each for the 10 medium ones. Escalate at **~180 minutes** with fewer than 15 rows.
  - `STG-WUL-01`: budget about **an hour per 100 MB**. With the current draw (2 small ≈ 25 MB each
    plus 1 medium ≤300 MB) escalate at **~360 minutes** with no completed upload.
  Quote the measured figures in the report rather than calling the browser path slow — "slower" is
  the expected finding, and the number is what a later train compares against.
- **An OOM on the WASM side lands on the child, not the service**, and is recorded rather than lost:
  the `ant-wasm` process is the largest RSS on the VM, so a kill reaches it first, the Python parent
  sees a signal exit and records `error_kind=crash`, and the walk continues. A non-zero crash count
  is therefore a finding to report, not a reason to intervene — and on the download side it is the
  first thing to check against the VM size, because an under-sized box turns every transfer into an
  OOM kill that reads as a network fault.

To investigate, you need the client VM's IP. On the **download** kinds it comes from the inventory
snapshot (`deployments up` already emits the step, so it is usually on disk already):

```bash
uv run saorsa-deploy deployments generate-inventory --name STG-DL-01 --ssh-key-path ~/.ssh/ant_deploy
```

> **SSH key gotcha.** `generate-inventory` defaults `--ssh-key-path` to `~/.ssh/id_rsa`, which does
> **not** work on these VMs — pass `~/.ssh/ant_deploy` as above. If you still hit
> `Permission denied (publickey)`, confirm with a one-liner first:
> `ssh -i ~/.ssh/ant_deploy -o BatchMode=yes root@<ip> 'hostname'`.

**`generate-inventory` has no `prod-uploaders` path**, so for STG-UL-01 / STG-WUL-01 take the IP from
the deploy output or the Slack post instead of expecting a snapshot. Both WASM deployments' inventory
(download side) additionally carries `client_kind`, `wasm_build`, `wasm_bootstrap` and the
`ant_wasm_*` paths — useful for the report, and the only record of where the per-op artefacts live
before the VM is destroyed.

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

**The WASM pair uses the same unit names and the same log paths** — `ant-prod-uploader` /
`ant-prod-downloader` under `/var/log/ant-prod-{uploader,downloader}/` — because only the spawned
binary differs. `ant-wasm` follows `ant --json`'s contract: a final JSON line on **stdout**, progress
and errors on **stderr**. Two WASM-only things to look for in the stderr log, and both belong in the
report verbatim:

- `error_kind` — `over_cap` (the transport refused the file; a skip, not a failure), `crash` (the
  child took a signal, usually an OOM) or a transport/handshake error.
- On the uploader, whether a **payment went out**: the result JSON's `payment_sent`, or the existence
  of the `--payment-marker` file. That is what decides whether a failed upload spent its pool file
  (chunks are in production → `partial`) or returned it untouched (nothing reached the network →
  released). You are not deciding this — the service already has — but it is the difference between
  two very different findings and it belongs in the quoted failure text.

`ant-wasm` **always emits an `error` key**, set to `null` on success, where `ant --json` omits it.
Read its **value**, never its presence: treating the key's presence as failure reports every healthy
browser-path upload as a partial.

- **Service still running with the log advancing** → it is slow, not stuck. Keep waiting and say so.
- **Service exited** (`Result=exit-code`, or active=inactive with no record) → the run is over and
  failed. Go to Step 8 and record what there is.
- **Service running but the log has not advanced in a long while** → report it as a stall with the
  last log lines as evidence. Do not restart it.

Report a failure the moment you see it. Do not intervene to improve the numbers.

## Step 8 — Record results (blocking, and unconditional)

Run this for **each** deployment as soon as it has finished, **whether it passed or failed**,
and **before any teardown**. A failed run's records are the whole point of having run it.

```bash
uv run saorsa-deploy deployments record-results --name STG-UL-01
uv run saorsa-deploy deployments record-results --name STG-DL-01
uv run saorsa-deploy deployments record-results --name STG-WUL-01
uv run saorsa-deploy deployments record-results --name STG-WDL-01
```

**Ordering is not negotiable: record before you destroy.** `deployments down` tears down each
deployment's InfluxDB monitoring VM along with the client (no manifest sets `keep_monitoring`),
and that InfluxDB is where the per-operation records live until this command copies them into
Postgres. Destroy first and the results are gone for good.

**Do each one independently and do not let one failure hold the others.** A WASM recording that
errors must not stop STG-UL-01's and STG-DL-01's from running — their monitoring VMs are billing and
their teardown waits on this.

**WASM records live in a different Influx bucket, and the command already knows.** Browser-path
operations land in `wasm-client-metrics` rather than `client-metrics`, kept apart so nothing can
average two transports measured several times apart; `record-results` reads both and deduplicates.
Each row is labelled with its own `client_kind`, resolved from the **deployment's `deploy_args`**
rather than from S3 client state — which teardown deletes — so a re-run after destroy still labels
the transport correctly instead of relabelling every WASM operation as native.

**`record-results` can take several minutes.** Give it a timeout of at least 10 minutes
(600000 ms). A short timeout kills it mid-write and looks like a failure.

Do not pass `--window`. For `prod-uploaders` it is ignored (the run is bounded); for
`prod-downloaders` the default `-30d` more than covers a same-day run.

Verify, and only continue if rows are actually present:

```bash
uv run saorsa-deploy deployments ls        # find this run's four ids
uv run saorsa-deploy deployments print-recorded-results --id <id>
```

This command takes **`--id`, not `--name`** — `--name` exits 2 with a usage error. That is the
right interface here anyway: the id is what proves the rows belong to **this** run. Check each id
is greater than the one you noted in Step 2.

For the uploads, `print-recorded-results` is also where the **on-chain spend** appears — the
`ANT cost` and gas columns, and per-record `Storage cost` / `Gas cost` lines in the detail view.
The live `print-uploads` does not show cost, so this is the only place to read it.

**This is the command the WASM results are read from, and it labels them by transport.** On
STG-WUL-01 / STG-WDL-01 every row carries `client_kind = wasm`, rendered under its own transport
heading, with the download side's **skip** and **crash** counts shown separately from successes and
failures. Over-cap skips are **stored but excluded from every success-rate denominator** — that is
deliberate, and it is why the WASM download's rate is out of 10 rather than out of 15. Report both
numbers: the rate over what was attempted, and the skip count.

If recording fails or returns nothing for a deployment, leave **that** deployment up and say so — the
data is still in InfluxDB while its monitoring stack is alive, and gone once it is destroyed. Carry
on with the others.

## Step 9 — Tear down the passing sides only

Decide **per deployment**, one of four independent judgements. A WASM verdict never changes a native
one, and a native one never changes a WASM one:

- **Passed** — every expected operation completed with success status (on STG-WDL-01, every
  *attempted* one — the 5 over-cap skips are expected and are not failures):
  ```bash
  uv run saorsa-deploy deployments down --config deploy/manifests/runs/staging/STG-UL-01.yaml
  ```
- **Failed** — any operation failed, `partial`, `dl-fail`, crashed, or never completed: **leave it
  up.** The VM, its per-file logs under `/var/log/ant-prod-*`, and its InfluxDB are the only forensic
  surface, and `down` destroys all three. Say plainly in the report that it is still running, name
  its VM IP, and say it will keep costing money until someone destroys it.

**Tear down a passing native deployment even if the WASM pair is still running or has failed**, and
vice versa. Four names, four independent teardowns; nothing here waits on anything else.

> A WASM uploader's teardown also runs the **pool results guard**, which compares recorded rows
> against the deployment's pool claims and **warns** if they disagree rather than blocking. If it
> warns, it means Step 8 did not capture everything the run drew — quote the warning in the report and
> check the claims before accepting the result. Do not silence it with `--skip-results-check`.

Confirm anything you tore down shows `destroyed` in `deployments ls` before reporting done.

## Step 10 — Report and update Linear

Emit this report, then post the relevant section as a comment on each `Production compat` child
issue. On a train with no WASM sub-issues, post the two WASM sections as one comment on the **train
issue** instead, headed `Production compat — WASM (no sub-issue on this train)`:

```
# Client production tests — Release Train <date> (rc.N)

ant (ant-cli) RC:   <version>
ant-client WASM ref: <tag> @ <deploy_args.wasm_build.commit>  (node-datachannel <version>, wasm-pack <version>)
Upload preflight:   PASS — pool draw, <class>:<n> … available; or <N> URL(s), none previously uploaded
WASM bootstraps:    <answered>/<total> production WebRTC multiaddrs answered the ICE probe

## Production compat — upload (cloud VM)

Deployment: STG-UL-01 (registry id <id>) — DigitalOcean <region> <vm_size>, native, payment=merkle
T0:         <timestamp, UTC>
Result:     PASS | FAIL
Files:      <ok>/<expected> ok, <partial> partial, <dl-fail> dl-fail
Detail:     <file name> — <size> — <chunks stored>/<total> chunks — <duration>
Spend:      <ANT storage cost> / <ETH gas> (from print-recorded-results)
Teardown:   destroyed | STILL RUNNING at <ip> (failed — kept for investigation)

## Production compat — download (cloud VM)

Deployment: STG-DL-01 (registry id <id>) — DigitalOcean <region> <vm_size>, native, clock_skew=2h
T0:         <timestamp, UTC>
Result:     PASS | FAIL
Ops:        <successes>/<expected> succeeded, <failures> failed — avg <duration>
Teardown:   destroyed | STILL RUNNING at <ip> (failed — kept for investigation)

## Production compat — WASM upload (cloud VM)   [ADVISORY]

Deployment: STG-WUL-01 (registry id <id>) — DigitalOcean <region> <vm_size>, transport=wasm, payment=merkle
Bundle:     <tag> @ <commit>
T0:         <timestamp, UTC>
Result:     ADVISORY — <n>/<expected> ok, <partial> partial, <over-cap> over-cap, <crash> crashed
Detail:     <file name> — <size> — <chunks stored>/<total> chunks — <duration> — payment_sent=<bool>
Spend:      <ANT storage cost> ANT; ETH gas NOT RECORDED on this transport (see below)
Failures:   <verbatim error text per failed file, from /var/log/ant-prod-uploader/*.stderr>
            <or "none">
Pool:       <files drawn>, claims resolved <uploaded>/<partial>/<released>; results guard <clean|warning text>
vs native:  <wall-clock ratio against STG-UL-01 for a comparable size, if the draws allow it>
Teardown:   destroyed | STILL RUNNING at <ip> (failed — kept for investigation)

## Production compat — WASM download (cloud VM)   [ADVISORY]

Deployment: STG-WDL-01 (registry id <id>) — DigitalOcean <region> <vm_size>, transport=wasm
Bundle:     <tag> @ <commit>
T0:         <timestamp, UTC>
Result:     ADVISORY — <successes>/<attempted> succeeded, <failures> failed, <crash> crashed
Over-cap:   <n> of <total> addresses skipped — above ant-core's 1,000,000,000-byte browser cap
            (the <n> shared with STG-DL-01 are 2.5–2.9 GB and are expected to skip)
Ops:        avg <duration> over the <attempted> attempted — <size range>
Failures:   <verbatim error text per failed address, from /var/log/ant-prod-downloader/*.stderr>
            <or "none">
Teardown:   destroyed | STILL RUNNING at <ip> (failed — kept for investigation)

## Notes

<per-train asks from the child issues and whether they were met>
<risks to watch from the train issue>
<anything off-band; any check that could not run and why>
<whether the WASM pair ran, was advisory, or was SKIPPED — and if skipped, the exact reason>
```

Fill every field — an unfillable field is itself a finding, so say why.

**The WASM sections say `ADVISORY`, never PASS or FAIL.** There are no calibrated production floors
for the browser path yet; these first trains are what establish them. Report the counts, the failure
text and the skips, and say explicitly that they do not gate the train. Once there is history, floors
go into `deploy/saorsa_deploy/resources/gates/staging-thresholds.yaml` and a follow-up flips them to
gating — until then, inventing a threshold here would be a number nobody calibrated deciding whether
a release ships.

**The bundle commit is the identifier, not the tag.** Read it from
`deploy_args.wasm_build.commit` (with `ref`, `repo_owner`, `node_datachannel_version` and
`wasm_pack_version` beside it) — a tag can be moved, and `node-datachannel` is the WebRTC
implementation the whole path runs on, so which version resolved is part of what was tested. A bundle
built before that sidecar existed records `null`; say so rather than filling in the tag.

Then update the issues:

- **`Production compat — upload (cloud VM)`**: PASS → **Done**. FAIL → **In Review**, with the
  failure and the still-running deployment stated plainly.
- **`Production compat — download (cloud VM)`**: same rule, judged independently.
- **`Production compat — WASM upload (cloud VM)`** and **`… WASM download (cloud VM)`**: → **Done**
  once the results are recorded and posted, **whatever the counts say**. They are advisory: the
  deliverable is the measurement, not a pass. The two exceptions are (a) the results could not be
  recorded at all, and (b) the pair never ran — then **In Review** with the reason, so nobody reads
  a closed issue as "the browser path was tested".
- **`Client production tests`** (the parent): move to **Done** only when **both native** children are
  Done and both WASM ones are Done or absent. If any went to In Review, leave the parent **In
  Progress** — its own definition is that it closes once all child tests are recorded, and a test in
  review is not yet settled. The Canceled children do not block it.

Finally, if any deployment is still running, say so as the last line of your reply, with the
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
- **A `urls:`-sourced upload list is a human input.** Both uploaders draw from the managed pool
  today, which needs no curation — but an entry switched back to `urls:` has no pool of known-unused
  public URLs behind it, so the Step 3b preflight can only ever stop and ask. Tracked as V2-802.
- **Only four of eight client tests are automated.** Low-bandwidth upload/download need a
  bandwidth-limit option that does not exist yet (V2-733); canary-corpus upload/download need
  corpus tooling that does not exist yet. Both pairs are normally `Canceled` on the train and are
  out of scope here. Overall skill scope is tracked as V2-734.
- **A handful of files is a thin test on either transport.** It exercises the path, not its
  reliability; one `partial` result is the difference between PASS and FAIL with no sample size
  behind it.
- **The WASM pair has no floors and therefore no failure condition.** Everything it produces is a
  number for the record. A browser-path regression that halves the success rate would be *reported*
  by this skill and would not stop the train. Closing that is the follow-up in
  `resources/gates/staging-thresholds.yaml`, and it needs several trains of history first.
- **A browser-path upload's ETH gas is not recorded.** `gas_cost_wei` comes back **0** on every WASM
  row — the two transactions (`approve`, `payForQuotes`) are sent by the JS payment callback inside the
  bundle, so the Python service never sees a receipt to cost. The ANT storage cost *is* recorded and is
  correct. So a WASM upload's gas spend can only be had by diffing the wallet's ETH balance across the
  run, or from Arbiscan against the paying address the deploy prints — capture it that way if a train
  asks, and otherwise report the gas as NOT RECORDED rather than as zero. Zero would read as "the
  browser path pays no gas", which is the opposite of true: it pays **two** transactions per file where
  native pays one.
- **The two transports cannot fetch the same bytes on the download side.** STG-DL-01's corpus is
  2.5–2.9 GB per file, all above ant-core's 1,000,000,000-byte browser cap, so the browser path
  measures a *different*, smaller corpus (the 10 medium addresses) and reports the five shared
  addresses as over-cap skips. There is no like-for-like large-file comparison to be had while the cap
  stands; do not manufacture one by shrinking STG-DL-01's list, which would change the native
  baseline.
- **There is no native browser-path control for the WASM download's clock-skew condition.**
  STG-WDL-01 deliberately runs with an unskewed clock, so a browser-path client's behaviour under
  clock skew is untested against production by this skill.
- **`generate-inventory` has no `prod-uploaders` path**, so neither uploader gets an inventory
  snapshot; the download kinds do. For the uploaders the VM IP comes from the deploy output or from
  S3 client state, and the per-op logs must be pulled over SSH before teardown.
