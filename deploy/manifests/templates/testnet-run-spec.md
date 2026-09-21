# Testnet run specification — template

This is the body of a Linear "Testnet run" issue, to be executed **unattended** by the
`run-testnet-from-issue` skill. Every `<...>` is filled in from the interview; every
`[choose]` is a decision the interview must obtain. Delete the guidance in `> ` quote
blocks before posting.

The runner reads only what is here. If a value is not in the issue, the runner does not
have it — it will not ask, it will abandon. So: literal SHAs, literal timestamps relative
to T0, literal commands, literal thresholds.

---

## Purpose

<One paragraph: what question this run answers, and why now. Name the change under test
and the issue(s) it belongs to.>

> Keep the *question* separate from the *procedure*. A reader should be able to tell from
> this paragraph alone whether the acceptance criteria below are the right ones.

**This run is executed unattended by the `run-testnet-from-issue` skill.** Everything the
runner needs is literal in this issue.

**Under test:** <the thing(s) whose behaviour decides pass/fail>

**Not under test:** <things observed but not gated on — e.g. node health, transfer rates,
resource usage. "The usual no-crash / no-intervention criteria apply.">

## Prior runs and what changed because of them

| Prior finding (<issue>) | Change here |
| -- | -- |
| <what went wrong or was learned> | <what this issue does differently> |

> One row per lesson that changed this spec. If there is no prior run, delete the section.

## Build under test

| binary | source | version / branch @ SHA |
| -- | -- | -- |
| `ant-node` | [choose: release / branch] | <`0.x.y` or `owner/branch @ sha`> |
| `ant` | [choose: release / branch] | <`0.x.y` or `owner/branch @ sha`> |

Crate pins (`[patch.crates-io]` on the branch), if any:

| crate | pin | PR |
| -- | -- | -- |
| <crate> | `<owner/repo> @ <full sha>` | <PR link> |

> A branch build must name the **commit**, not just the branch — the runner records the
> head before and after the build and they must match. If a PR is still moving, the
> issue is not ready. Say whether the branch is ever merged or is a composition branch.

Latest published ant-node release expected at deploy time: **<vX.Y.Z>** (a newer tag → do
not deploy; nodes would auto-upgrade out from under the run).

### What the build emits

> Only if the run measures something the build logs or exports (new log targets, metrics,
> counters). Table of target / message / fields, and the cadence. Delete otherwise.

## Deployment manifest

`deploy/manifests/runs/<NAME>-<slug>.yaml` (git-ignored; the runner writes it from this
block). Extends `<profile>.yaml`. **Do not edit the profile.**

```yaml
extends: ../profiles/<profile>.yaml

name: <NAME>
purpose: "<one line — lands in the registry and the Slack post>"
slack_post: [choose: true / false]

testnet:
  binaries:
    ant_node: { <version: X | branch: B, repo_owner: O> }
    ant:      { <version: X | branch: B, repo_owner: O> }

  nodes:
    per_vm: <n>
    nat_percent: <0-100>
    clock_skew: <0 | count>
    providers:
      <provider>:
        vm_count: <n>            # PER REGION — compute the fleet total below
        regions_list: [<...>]
        size: <slug>
        attached_volume_size: <GB>   # size from fill rate × runway, not the default

  clients:
    uploaders:
      - { provider: <p>, region: <r>, file_size_mb: <n>, payment: <merkle|no-merkle>, ant: <n>, eth: <n> }
    downloaders:
      - { provider: <p>, region: <r> }

  elastic:
    enabled: [choose]
    keep_on_destroy: [choose]      # must match the Teardown section

  monitoring:
    enabled: [choose]
    grafana: [choose]
    keep_on_destroy: [choose]      # must match the Teardown section
```

**Shape:** <N bootstraps + N node VMs × per_vm = N nodes (N plain, N NAT'd) + N clients>.
<One sentence on why this shape answers the question.>

> The interview must compute the fleet from the YAML (vm_count is per region) and read the
> total back. It must also confirm: disk sizing against the expected fill rate for the run
> length; `eth`/`ant` funding against `clients estimate-gas`; and that every profile
> override the prose promises is actually in the YAML.

## Tooling prep (done before this issue is handed to the runner)

> Anything the run depends on that had to be built or fixed first: scripts, snapshot
> coverage, dashboard changes. Each item states the registry commit it landed in. Nothing
> here may be "to do" — the runner does not build tooling. Delete the section if empty.

1. <item> — registry `<sha>`.

## Preconditions (stop and name the item if any fails — nothing exists yet, so nothing to tear down)

* Registry checkout on branch `<branch>` at a commit ≥ `<sha>`; clean tree; record the SHA.
* <Build precondition: branch head = the SHA above; pins match; no newer commit on <PR>.>
* `gh api repos/WithAutonomi/ant-node/releases/latest --jq .tag_name` = **<vX.Y.Z>**.
* `DATABASE_URL`, <`DO_TOKEN` / `VULTR_API_KEY` / `HCLOUD_TOKEN` / the four `OVH_*`>,
  `AWS_*`, `ANT_DEPLOY_SECRET_KEY` present. Note the highest existing `<NAME>` registry id.
* **No `<NAME>` state in S3**: `saorsa-deploy/<NAME>.json`, `elastic/<NAME>.json`,
  `monitoring/<NAME>.json`, `journals/<NAME>.json` all absent. Any present → the run fails
  here, naming the key. **Do not destroy it.**
* No other `deployments up` / `down` process alive for `<NAME>`.
* <Any run-specific precondition.>

```bash
uv run saorsa-deploy deployments config validate --config deploy/manifests/runs/<NAME>-<slug>.yaml
uv run saorsa-deploy deployments plan            --config deploy/manifests/runs/<NAME>-<slug>.yaml
uv run saorsa-deploy clients estimate-gas        --config deploy/manifests/runs/<NAME>-<slug>.yaml [--baseline-id <id> | --uploads-per-hour <n> --duration-hours <h>]
```

Exit 2 from `estimate-gas` → raise `eth:` (only ever raise) and record it.

## Operator rules

1. **Once `up` is launched, nothing blocks.** A tooling defect found after launch is a
   recorded deviation; a criterion it prevents is NOT ASSESSED.
2. **Stop conditions mean abandon.** The only ones are: <list them — e.g. a Phase 0 check
   fails; no <signal> by T0+<m>; a pending or applied upgrade; a node VM at 100% disk with
   services dying>. Abandon = collect the evidence the phase asks for, run whatever of
   the extraction still works, tear down compute, [choose: destroy / leave] the
   observability stacks (must match Teardown), post the results comment, set **Cancelled**.
3. **Hard ceiling: compute is torn down by T0+<H> regardless of state.** <Last phase
   boundary + time for win-end evidence + margin, typically +1h30m.> If the ceiling wake
   fires and compute is still up, the run is over: abandon as above.
4. **The phase clock is driven by verified wall-clock wakes**, one per boundary (<list
   every T0+… that has an action>) plus the ceiling, armed at T0. Each wake re-reads the
   clock; a late wake is recorded with its actual time.
5. `up` **runs in the background and is waited on by PID**; T0 is read from the client
   units' `ActiveEnterTimestamp`.
6. **No host is modified during the run.** Reads only: <snapshot / `df` / ES / Influx>.
7. <Recurring health read and its thresholds — e.g. disk every 30 min; >90% fails
   criterion N and continues; 100% with failing units is a stop condition.>

## Procedure

```bash
uv run saorsa-deploy deployments up --config deploy/manifests/runs/<NAME>-<slug>.yaml
```

**T0 =** `clients start` **completes.** All boundaries UTC.

| Phase | Window | Purpose |
| -- | -- | -- |
| 0 — deploy + pipeline gate | `up` → T0+<m> | <what must be proven before measuring> |
| 1 — warm-up | T0 → T0+<m> | <not measured> |
| 2 — measurement | T0+<m> → T0+<h> | <the window the criteria are computed over> |
| <N — further phases, e.g. mixed-version> | … | … |
| 3a — win-end evidence + compute teardown | T0+<h> → ~T0+<h+15m> | evidence needing SSH, then clients/nodes/bootstraps destroyed |
| 3b — extraction | after 3a, against ES/Influx | recordings, analysis scripts |
| **Ceiling** | **T0+<H>** | **compute must be gone; if not, abandon** |

### Phase 0 — pipeline gate (BLOCKING, by T0+<m>)

1. <Check, with the command and the expected value.>
2. …

Any failure → stop condition.

### Phase 1 — warm-up (T0 → T0+<m>)

<Checks at T0+<m>, with commands and expected values. Name the signal whose absence is a
stop condition, and the deadline.>

**At exactly T0+<m>** (wake-driven): <window-start evidence command>.

### Phase 2 — measurement (T0+<m> → T0+<h>)

<Every <interval>, wake-driven: the health read. Report, never intervene. State what a
service restart inside the window does to the criteria.>

**At exactly T0+<h>**: <window-end evidence command>.

### Phase 3a — evidence, then compute teardown

<Everything that needs the fleet alive: crash sweep (failed units, `NRestarts`,
`(deleted)` exe), final `df`, final upgrade counts, any snapshot.> Then, with no further
wait:

```bash
uv run saorsa-deploy clients destroy    --name <NAME> --force
uv run saorsa-deploy nodes destroy      --name <NAME> --force
uv run saorsa-deploy bootstraps destroy --name <NAME> --force
```

Verify: `saorsa-deploy/<NAME>.json` gone from S3; no <provider> VMs or volumes named
`<NAME>-*`. Record the time.

### Phase 3b — extraction (against the retained ES/Influx)

```bash
uv run saorsa-deploy deployments record-results  --name <NAME>
uv run saorsa-deploy deployments record-analysis --name <NAME> \
  --summary "<purpose>. <binaries @ shas>. registry <branch> @ <sha>. T0=<ts>; window <ts>..<ts>."
uv run saorsa-deploy deployments derive-metrics --id <id> --window-start <T0+m> --window-end <T0+h> --label steady-state
uv run saorsa-deploy deployments record-log-counts --name <NAME>
uv run saorsa-deploy deployments record-log-counts --name <NAME> --window-start <T0+m> --window-end <T0+h> --label steady-state
<run-specific analysis script, at its committed path, with its exact arguments>
```

> `record-results` before `record-analysis`. Any analysis script is a committed
> deliverable at a named SHA — the runner never reimplements it; if it errors, the
> traceback is a deviation and its criteria are NOT ASSESSED.

## Phase commands (literal; parameterised on `T0` only)

Written to `deploy/manifests/runs/<NAME>-<slug>.phases.sh` by the runner before `up`.

```bash
# health read — every wake
uv run saorsa-deploy deployments print-uploads   --name <NAME>
uv run saorsa-deploy deployments print-downloads --name <NAME>
uv run saorsa-deploy analysis log-count --name <NAME> --phrase "New version detected"        --start "$T0" --end "$(date -u +%FT%TZ)"
uv run saorsa-deploy analysis log-count --name <NAME> --phrase "Starting auto-apply upgrade" --start "$T0" --end "$(date -u +%FT%TZ)"
for ip in $NODE_IPS $BOOTSTRAP_IPS; do ssh -o BatchMode=yes root@$ip 'hostname; df -h /mnt/ant-node-storage | tail -1'; done

# T0+<m> — <boundary name>
<command>

# T0+<h> — window end
<command>
# crash sweep, all VMs
for ip in $NODE_IPS $BOOTSTRAP_IPS $CLIENT_IPS; do ssh -o BatchMode=yes root@$ip 'hostname; systemctl --failed --no-legend; for u in $(systemctl list-units "ant-*" --no-legend | awk "{print \$1}"); do echo "$u NRestarts=$(systemctl show -p NRestarts --value $u) exe=$(readlink /proc/$(systemctl show -p MainPID --value $u)/exe 2>/dev/null)"; done'; done
```

> Every command a wake runs appears here, verbatim, checked against `--help`. The runner
> substitutes `T0` and nothing else.

## Acceptance criteria

> One row per criterion. Each must be decidable by a command the runner can run, with a
> numeric threshold or an exact expected value. Anything a human must look at is split out
> as **(human, post-run)** and the runner reports it NOT ASSESSED.

1. **<Build confirmed>** — <how: version on a sample of nodes; expected log line from all
   N VMs in the first cadence; no newer commit on <PR> at deploy time>.
2. **<Primary criterion>** — <metric, threshold, scope (all N VMs / per host / fleet)>.
3. …
N-2. **No crashes, no upgrade, no intervention** — 0 failed `ant-*`, 0 restarts, 0
   `(deleted)`, 0 `New version detected`.
N-1. **Every recording landed** — <the extraction commands, any evidence files>.
N. **The window was measured on a healthy fleet** — <disk threshold> before T0+<h>;
   evidence taken within ±2 min of its boundary; no phase command skipped; every wake
   fired within 5 min of its boundary; compute gone before the T0+<H> ceiling.

<Which criteria are the verdict on the change under test, and what a failure means — a
bug filed against the crate, a rerun, or a decision for the RM.>

## Results comment (posted on this issue)

1. Registry id; highest prior `<NAME>` id; <baseline/reference run ids>.
2. Provenance: <binaries and SHAs before/after build, pins, registry SHA, profile SHA,
   latest ant-node tag at deploy>.
3. T0, every boundary and every wake's actual fire time, UTC. Time compute was destroyed.
4. Phase 0 results.
5. <The primary result table(s).>
6. <Observations: client success/durations, node CPU/RSS, disk per VM per read.>
7. Crash sweep; upgrade counts.
8. Deviations; provider incidents.
9. Teardown verification (S3 key, provider API listing) and — if anything is retained —
   each stack's endpoint and destroy command.

## Known gaps and traps

* <Anything that has bitten a previous run of this shape and is not otherwise encoded
  above: query traps, name reuse, flags that look right and aren't.>

## Teardown

Compute goes at Phase 3a (clients → nodes → bootstraps, `--force`), before extraction,
and is verified against S3 and the provider APIs.

[choose one]

**Nothing retained.** After Phase 3b:

```bash
uv run saorsa-deploy elastic destroy    --name <NAME> --force
uv run saorsa-deploy monitoring destroy --name <NAME> --force
```

**Retained, deliberately (authorised by this issue):** `<NAME>`'s <Elasticsearch and/or
monitoring> stack(s) stay up after the run, for <reason — e.g. a human reads Grafana for
criterion N; a follow-up issue>. They bill hourly; the results comment names their
endpoints and these commands:

```bash
uv run saorsa-deploy elastic destroy    --name <NAME> --force
uv run saorsa-deploy monitoring destroy --name <NAME> --force
```

> The manifest's `keep_on_destroy` flags must agree with this choice. Retaining is only
> ever the observability stacks — never compute.

## Workflow

1. Preconditions pass. Manifest and phases file written. Set **In Progress**.
2. Launch `up` in the background; at T0 arm the wakes and the ceiling; execute the phases;
   no blocking questions; stop conditions mean abandon.
3. Win-end evidence, then destroy compute and verify.
4. Extract against the retained ES/Influx.
5. Post the results comment; <file bugs / hand to the RM>; set **Done** (or **Cancelled**
   if abandoned).
6. <Human follow-up, if anything is retained.>
