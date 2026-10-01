# Rollout-watch verification scripts

Checks a stable rollout watch runs at its checkpoints, written for the
2026-09-23 train (V2-1297, ant-node 0.20.0 / ant 0.3.8). Each script prints one
markdown block to stdout, exits `0` when every expectation held and `1` when any
did not, and takes `--window` (default `1h`), `--to-version` and `--json-out`.
Nothing here writes to the fleet: every query is read-only, and the one SSH path
reads a file and sends a UDP packet.

**Always pass `--to-version`.** Its default is `0.20.0`, the release these were
written for; on any later rollout the default measures the wrong arm.

The scripts have two different lifetimes:

- **`webrtc_coverage.py` is standing.** Every stable rollout watch runs it at
  A2, A3 and A4, and gate 1 breaches on its listening coverage (the watch body
  written by the `train-manifest` skill in the `releases` repo). Keep it working
  across releases.
- **`traffic_targets.py` and `migration_signal.py` are per-train.** They answer
  one release's questions (V2-834's counters, saorsa-core #163) and are not
  expected to outlive it. A later train's Phase B adds its own scripts here.
- **`audit_protocol_v2.py`, `pointer_signals.py` and
  `sctp_association_errors.py` are the 2026-09-29 train's** (V2-1364 Phase B,
  pointers and the subtree-audit protocol id). The first two are tied to that
  release; the SCTP one measures a signal that predates it and will likely be
  wanted again.

What is expected to outlive every train lives in the package (`prod
rollout-watch` for adoption and per-arm health, `saorsa_deploy.webrtc_probe` for
the ICE probe).

## Environment

```bash
export ANT_PROD_01_ES_URL=...        # production Elasticsearch
export ANT_PROD_01_ES_API_KEY=...
export ANT_PROD_01_INFLUX_URL=...    # production InfluxDB
export ANT_PROD_01_INFLUX_API_KEY=...
```

ES index `telegraf-ant-nodes-ant-prod-01-*`; Influx org `37300ec1978aff3e`,
bucket `testnet-metrics`. The advertised-address sample in `webrtc_coverage.py`
additionally needs `~/.autonomi/testnets/ant-prod-01/inventory.json` and the
`~/.ssh/ant_deploy` key it names.

## The arm split, and why every script has a control

A service's version is the `tag.node_version` on its most recent document
(ant-node #225 stamps it; V2-1263 promoted it to a Telegraf tag), so the new and
old arms are resolved fresh on every run with nothing remembered in between. A
build older than #225 stamps nothing and telegraf writes `unknown`, which counts
as the old arm.

Before the rollout starts there are no new-version services, and every measure
then reports `not applicable: 0 new-version services` rather than a vacuous
pass. That is the expected output of a pre-rollout baseline run.

Each script also names a **control** — a query that is non-zero whenever the
fleet is alive. A broken query and a quiet fleet both read zero, so if the
control reads zero every check that passed *because* it counted nothing is
downgraded to `UNTRUSTED` and the script fails. Checks reported as
`NOT APPLICABLE` are unaffected: they claimed nothing.

## `webrtc_coverage.py`

Run at each checkpoint once the new arm is non-empty. Verifies ant-node #220
fleet-side, in three measures of increasing strength: every new-version service
logged `ADR-0015 WebRTC Direct listening` since its most recent `ant-node
starting`; no WebRTC/browser WARN family touches more than 5% of the new arm;
and a `--sample 10` spread of hosts (at least three each from hetzner, ovh and
contabo) advertises its own public IP in
`/mnt/ant-node-storage/data/<service>/webrtc-direct.multiaddr` and answers an
ICE connectivity check on the port in that file.

**Pass** means all three. The sample is the measure that matters: the first two
only prove the listener bound and did not complain. Node services take a random
WebRTC port — unlike the seven bootstraps, which V2-1307 pins — so the port is
read out of the file every time. Pass `--sample 0` for an ES-only run.

Control: `ant-node starting` lines in the window. Zero means the fleet has not
restarted in the window, which is normal outside a rollout — coverage is then
reported as unassessed rather than as a miss, so a quiet fleet does not read as
a failure.

## `traffic_targets.py`

Run at each checkpoint. Verifies V2-834's instrumentation is *present* in both
stores (whether it reconciles is `scripts/v2_834_reconcile.py`, section 5 of
V2-1308). Checks that each of `ant_node::{storage,upgrade,payment}::traffic` is
emitted by ≥95% of the new-version services up longer than ten minutes; that the
new cumulative fields and summary lines exist on the new arm and on *no* old-arm
service; and that the three targets reach the `tail` measurement in InfluxDB on
as many hosts as run a new-version service.

**Pass** means all four. The ten-minute exclusion matters: the first traffic
summary lands five minutes after start, so a checkpoint taken just after a
restart wave would otherwise read as a fleet-wide failure. A zero on the Influx
side while ES holds the lines points at the forwarder, not the release —
V2-1275 rolled out a `*::traffic` glob tagpass so a new target needs no Telegraf
change — and the report says so explicitly.

Control: `ant_node::replication::traffic` documents, which every version emits.

## `migration_signal.py`

Run while the fleet is still **mixed**. Verifies saorsa-core #163 (V2-1260): a
peer that disconnects mid-tally must stop being recorded as `agent = none` on
every subsequent report. The measure is therefore persistence, not presence — a
single `agent = none` observation is legitimate — so it counts `(observer, peer)`
pairs that recur across two consecutive 15-minute report intervals.

**Pass** means zero such pairs on the new arm while the old arm has some: the
bug reproducing on the unfixed half in the same window is what gives the new
arm's zero meaning. Zero on both arms is reported as `UNTRUSTED`, not as a pass.
Once the old arm is under 5% of running services the comparison is suspended and
the new arm's zero stands alone — which is why this one must be run before the
rollout completes.

Control: total `peer_state` documents in the window.

## `audit_protocol_v2.py`

ADR-0016 moves the subtree audit's protocol id to `v2`, so old and new releases
**do not audit each other across the upgrade**. The receiving side of a mismatch
logs nothing at any level, so the only signal is the auditor-side INFO
cumulative summary (`ant_node::replication::traffic`, group 4, `"audit outcome
summary (cumulative)"`).

Two traps are handled here, and both produced wrong numbers before they were:

- **`tail.group` does not identify a summary.** Group 4 carries *both* the audit
  outcome summary and `"replication responder admission summary (cumulative)"`,
  emitted at the same millisecond, and only the first has the `subtree_audit_*`
  fields. Filtering on the group alone lets a first/last sort land on the
  admission summary, where the audit fields are absent — which a delta then
  reads as a reset from zero and reports the service's entire lifetime as one
  window. Both the group and the message must be filtered.
- **A counter reset is decided per field, never per service.** The counters are
  cumulative, so a windowed delta is `last - first`; on a restart the last value
  *is* the delta. Deciding that per service lets one field going backwards make
  the other seven fall back to their absolute totals.

**Pass** is two separate rules, because the two failure counters behave nothing
alike on a real fleet:

- `subtree_audit_fail_key_absent` is **zero-tolerance**. Measured at zero on a
  uniform 0.20.0 fleet over both 1h and 24h, and ADR-0016 calls it a confirmed
  failure: a node committed a key it cannot produce.
- `subtree_audit_fail_digest_mismatch` is **a ratio, not zero**. A pointer-free
  0.20.0 fleet already ran ~51 of these a day fleet-wide against ~83,000 passes
  (≈0.0027 per service-hour, 2026-09-30), and the rate roughly tripled over the
  preceding week. Every one of those is a *chunk* mismatch, so the measure is the
  new arm's per-service rate against the old arm's **in the same window**, which
  cancels that background exactly. `--digest-baseline` takes over once the old
  arm has drained below 5%.

`subtree_audit_fail_timeout` climbing on **both** arms is the expected pause and
is reported, not gated. `responsible_audit_*` rides the unchanged protocol id and
should not pause; if it does, the protocol id change is not the cause. Do not
substitute the `KeyAbsent` or `DigestMismatch` log phrases for these counters —
those phrases also fire on the responsible-audit path (~1,800 `KeyAbsent` a week)
and conflate the two audit kinds.

Control: group 4 audit-summary documents in the window.

## `pointer_signals.py`

Proves the pointer feature is live and clean, and deliberately does **not** try
to prove replication: the entire success path of pointer replication is `debug!`,
which production's `info` level and two independent filters drop, and the
per-variant replication counters were never wired into `log_traffic_summary()`
(the enum went 17 → 23 variants, only 0–16 are emitted). Replication is proved
out of band instead, by a client read — a read returns a state only when two
peers name it.

What it checks, all at `info`:

- the chunk rpc traffic summary carries `pointer_get_rx_count`, a field that
  exists on 0.21.0 and no earlier build, so its presence is proof of the new code
  path independent of any pointer existing;
- `"Replication engine started with N background tasks"` reports 18 — pointers
  add the verification loop and the fresh drainer to 0.20.0's 16
  (`--expected-tasks`);
- `"Storage commitment rotated"` carries `pointers=`. The whole line is one
  unparsed `tail.message` string (`hash=` and `key_count=` are not fields
  either), so this is matched on the message;
- the impossible-state warnings are all at zero — both cross-kind address
  refusals, `"Subtree audit: committed pointer ... is not held"`,
  `"Commitment retention: corrupt pointer leaves"`, and any WARN/ERROR on
  `ant_node::pointer::store` (that target has no info-level line at all).
  Pointer and chunk addresses derive from different BLAKE3 modes, so a collision
  takes a cross-mode preimage break and any occurrence is a real alarm;
- no `outcome=error` on `pointer_put_rpc` / `pointer_get_rpc`. Production has no
  pointer client, so with no pointer RPC at all this reads `NOT APPLICABLE`
  rather than passing vacuously. `not_found` is an answer, not an error.

Control: chunk rpc traffic summary documents in the window.

## `sctp_association_errors.py`

`webrtc_sctp::association::association_internal` logs ERROR only, with two
distinct messages, and was **already climbing before the 2026-09-29 train**
(1,035 → 2,934 errors a day and 38 → 60 hosts over 2026-09-25 → 09-29; 181.04
per 1k service-hours on 2026-09-30). Nothing on that train fixes it, so this is
not asking whether it is fixed — it asks whether the release makes it worse,
which matters because pointers newly cross the WebRTC-direct boundary and so add
traffic to the erroring path.

Not read off `prod rollout-watch` §4 on purpose: §4's per-target rows cover only
the top 15 targets and this one sits at the edge of that cut, so it can silently
drop out of a run; and §4 is suspended whenever either arm is under 5%, which is
the state at A4.

**Pass** is the new arm within 1.5x the old arm's rate per 1k service-hours,
taken while the fleet is still mixed. `--baseline-rate` takes over once an arm
drains. There is no pre-0.20.0 baseline to be had — the index retains about seven
days — so before-and-after and old-versus-new are the same comparison here.
Service-hours are the arm's running-service count times the window, the same
simplification on both arms, so the ratio is unbiased.

Control: chunk rpc traffic summary documents in the window.

## `rss_slope.py`

Per-service RSS slope from InfluxDB (`ant_node_memory.vm_rss`), **record-only** —
it asserts nothing and always exits 0. Raised because the staging memory-leak
gate passed at 4.239 %/h against a 5 %/h limit, close enough to be worth
watching; but production's median over 6h measured 0.572 %/h, an order of
magnitude below, so applying staging's threshold here would be false precision.
The figures accumulate across trains until there is enough history for a real
one.

`ant_node_memory` carries no version tag, so unlike every other script here this
cannot split by rollout arm — run it once the fleet is uniform. Influx host names
are also mixed-case where Elasticsearch's are upper-case, so the two are not
directly joinable.

## Not in this directory

`scripts/client_artefact_smoke.py` lives one level up: it proves a shipped `ant`
CLI release is the same build from the GitHub release and from npm, and applies
to every train that ships the CLI, not just this one.
