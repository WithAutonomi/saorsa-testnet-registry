# `analysis browser-reads` fixtures

Two synthetic row sets for the hot-read report (V2-1618), plus the hot-file
selection they were read against. Synthetic on purpose, unlike the
`browser-trace/` fixture beside them: that one is a REAL ant-client capture
because the question there is "what does `setBrowserTrace` actually emit", which
cannot be answered by writing down an assumption. Here the question is
arithmetic over rows whose shape is already fixed by the table, so the useful
fixture is one whose every expected figure can be worked out on paper — which
these are, below.

Rows are the `deployment_browser_download_records` columns as plain JSON, with
`status` as its string value and `started_at` as ISO-8601. The tests feed them
to `saorsa_deploy.browser_reads` as dicts.

**Each row set has a matching `*-trace-docs.json`** (V2-1650), because the
per-chunk section and G3 no longer read the rows' `first_reads` — they read the
Elasticsearch trace documents, and count the holder that ANSWERED `found` rather
than the peer the first GET was sent to. The documents are generated FROM the
rows' own `first_reads`, so the two files cannot disagree about which holder
served a read; `mkfixture`-style regeneration is described at the bottom. Each
document is an ES `_source`: fields nested under `browser_trace`, tags under
`tag`, exactly as telegraf writes them.

Per chunk the generated trace gives each download a deliberately different
shape, so all four new columns have distinct hand-checkable values:

| chunk | records per download | `gets` | `not_found` | `1st→holder` | `pre-disc` |
| --- | --- | --: | --: | --: | --: |
| chunk-a | seed GET answered `NotFound`, then the holder, then `discovery finish "7 peers"` | 2 | 1 | 0% | 100% |
| chunk-b | straight to the holder, no discovery at all | 1 | 0 | 100% | 100% |
| chunk-c | `discovery finish "3 peers"` FIRST, then the holder | 1 | 0 | 100% | 0% |

So `spread-trace-docs.json` is 24 successful downloads x (5 chunk-a records +
2 chunk-b) + 6 stalled downloads x 3 chunk-c = **186** documents. The pre-window
round contributes none, because the query names the window's round ids — which
is also what keeps its `not_found` row out of the per-chunk section.

All of it is built on **period 600 s** and round **R0 = 2987190**, whose boundary
is exactly `2026-10-18T09:00:00Z` (2987190 x 600 = 1792314000).

## `hot-files.json`

Two files of 52,428,800 bytes (50 MiB) each, indices 0 and 1. A round reads
`files[round_id mod 2]`, so the fixtures set `hot_file_index = round_id % 2`.
R0 is even, so the odd rounds read file 1 and the even ones file 0 — except
that R0's rows are all `skipped` and a skip carries NO index, having never
chosen a file. File 0 is therefore read by round 2987192 alone, and file 1 by
2987191 and 2987193.

## `spread-rows.json` — every gate PASSES

32 in-window rows plus 8 that must be excluded.

| rounds | boundary | rows | what |
| --- | --- | --: | --- |
| 2987189 | 08:50:00Z | 8 | **before the window.** `failure` / `not_found`, 5 refused dials each |
| 2987190 | 09:00:00Z | 8 | all `skipped` / `no_hot_files` — the rounds before a selection |
| 2987191–3 | 09:10–09:30Z | 24 | all `success`, 2 hosts (lon1, nyc3) x 4 instances x 3 rounds |

Window `[2026-10-18T09:00:00Z, 2026-10-18T09:40:00Z)` takes rounds 2987190–3 and
leaves 2987189 out. That exclusion is itself a test: the excluded round would
fail G2 (`not_found`) and G4 (40 refused dials) if the window selection leaked.

Hand-checked figures:

- **Totals** — 32 downloads: 24 success, 0 failure, 8 skipped. Completion
  `24/24 = 100%` (skips are not in the denominator). 24 traces archived.
- **Rounds** — 4. Round 2987190 has no completion at all (every row skipped), so
  3 rounds are assessed by G1, each at 100%.
- **Desynchronisation** — round 2987192's nyc3 instance 4 has
  `started_late_ms = 12000` against 500 for the other seven, so the spread is
  `12000 - 500 = 11,500 ms`, over the 10,000 ms flag. The other rounds are flat
  at 0 ms.
- **Per region** — lon1 and nyc3, 1 host and 16 rows each (4 skipped + 12
  success).
- **chunk-a** — one first read per successful download = 24, dealt round-robin
  over 6 holders, so 4 each: busiest share `4/24 = 16.67%`. 24 >= 20, so it is
  GATED, and 16.67% <= 25% passes. Every read reports `known_holders = 7`. The
  bootstrap seed that answered `NotFound` first is **not** the first read, which
  is the whole point of the holder definition.
- **chunk-b** — 24 first reads over 4 holders, 6 each: `6/24 = 25.0%` exactly,
  which passes because the gate is `<=`. Every read reports
  `known_holders = null`, which is its own bucket and not 0.
- **chunk-c** — read by the 6 downloads that stalled (`n % 4 == 0` of 24), all
  from one holder: `6/6 = 100%`. 6 < 20, so it is LISTED and NOT gated — which
  is why a 100% share here still leaves G3 passing.
- **Stalls** — those 6 downloads carry `stalls = 1`; the other 26 in-window rows
  (18 successes + 8 skips) carry 0. Total 6 over 32 rows carrying a count, so
  the share with at least one stall is `6/32 = 18.75%`.
- **Refusals** — 0 in the window.
- **Bootstrap load** — downloads per round: 0 (all-skipped round), then 8, 8, 8.
  A skip launches no browser and dials nothing, so it is not a session. Peak 8
  against the 256 ceiling, 248 of headroom, over 2 seeds.
- **Verdict** — PASS, exit 0.

## `nearest-first-rows.json` — every gate FAILS

One host (lon1), 4 instances, 6 rounds 2987190–2987195 = 24 rows. The last
instance of the last round is `failure` / `not_found` and carries 2 refused
dials; the other 23 are successes reading **chunk-a from the same holder**.

Window `[2026-10-18T09:00:00Z, 2026-10-18T10:00:00Z)` takes all six rounds.

- **G1** fails: round 2987195 is `3 ok / 1 fail = 75%`, below the 99% floor.
  The other five rounds are at 100%, which is the point — a window-wide average
  would be `23/24 = 95.8%` and a per-round gate catches the one bad round.
- **G2** fails: 1 `not_found` row against a threshold of 0.
- **G3** fails: chunk-a has 23 first reads, all to `12D3KooWHot`, so the busiest
  share is `23/23 = 100%` — nearest-first behaviour — and 23 >= 20 gates it.
- **G4** fails: 2 refused dials against a threshold of 0.
- **Verdict** — FAIL, exit 1.

## `id685-trace-docs.jsonl.gz` — the REAL capture

1,738 Elasticsearch documents from the archived traces of **DEV-01 registry id
685** (V2-1621), gzipped: round **2987404**, the first twenty downloads in
`(host, instance)` order, narrowed to two of that round's 17 chunks. Real, not
written down, for the same reason `../browser-trace/read-startup.jsonl.gz` is:
the question these figures answer is what ant-core's read path actually does,
which an assumption cannot answer. Host names are shortened to
`DEV-01-BROWSER-LON1-<n>` — the real ones are 60 characters of deployment name
repeated — and nothing else is altered.

| | file-address chunk `486d9615…` | content chunk `08b6b3ee…` |
| --- | --: | --: |
| downloads | 20 | 20 |
| first reads | 20 | 20 |
| holders serving first reads | 3 | 5 |
| holders answering `found` | 3 | 6 |
| busiest holder's share | **70.0%** (14/20) | **45.0%** (9/20) |
| `gets_per_download` | 9.25 | 11.65 |
| `not_found_per_download` | 7.75 | 9.40 |
| `first_get_to_holder` | **0%** | **0%** |
| `decided_before_discovery` | 100% | 100% |

Both breach the 25% G3 ceiling. Over the whole measured window (R1–R9, 1,260
traces, 892,174 records) the same definitions give: file-address chunks 6–7
holders and 29% / 59% / 63%; content chunks 4–9 holders, median 34%, max 90%,
38 of 48 over 25%; **41 of 51** chunks over the ceiling against **3 of 51** by
the old first-GET definition; 10.04 GETs and 7.94 `NotFound` per chunk per
download, against the ADR's own validation figures of 3.45 and 1.39. Those are
in `deploy/README.md` §"The report" and in V2-1621's follow-up comment.

`test_browser_trace_id685.py` asserts every figure in the table. They were
computed from the raw JSONL by a separate script, not by the module under test.

## Regenerating

The row files were written by a throwaway generator and are committed as data.
The rules above are the specification: `chunk-a` holder `i` is
`12D3KooWA{n mod 6}` for the n-th successful download in order, `chunk-b` is
`12D3KooWB{n mod 4}`, and a download stalls when `n mod 4 == 0`. Edit the JSON
directly rather than re-deriving it — the numbers in this README are what the
tests assert, so a change to the data is a change to them.

The two `*-trace-docs.json` files are derived from the row files by the rules in
the shape table above, so a change to a row's `first_reads` means regenerating
them; the `id685` capture is extracted from the archived traces and is not
regenerable without them (`s3://saorsa-testnet-addresses/saorsa-deploy/results/DEV-01/browser-traces/`
held them at the time, and that archive path no longer exists for new runs).
