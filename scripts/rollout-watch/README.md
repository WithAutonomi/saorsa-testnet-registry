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

## Not in this directory

`scripts/client_artefact_smoke.py` lives one level up: it proves a shipped `ant`
CLI release is the same build from the GitHub release and from npm, and applies
to every train that ships the CLI, not just this one.
