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
  GATED, and 16.67% <= 25% passes. Every read reports `known_holders = 7`.
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

## Regenerating

Both files were written by a throwaway generator and are committed as data. The
rules above are the specification: `chunk-a` holder `i` is
`12D3KooWA{n mod 6}` for the n-th successful download in order, `chunk-b` is
`12D3KooWB{n mod 4}`, and a download stalls when `n mod 4 == 0`. Edit the JSON
directly rather than re-deriving it — the numbers in this README are what the
tests assert, so a change to the data is a change to them.
