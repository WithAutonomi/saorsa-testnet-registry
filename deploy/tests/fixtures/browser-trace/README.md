# Browser-trace fixture

`read-startup.jsonl.gz` is a **real `setBrowserTrace` capture**, not a
hand-written one. It is one `downloadPublicFile` call's worth of trace records,
which is exactly what one round of the browser-downloader harness writes to
`/var/lib/ant-browser-downloader/trace/<round>-<instance>.jsonl`.

## Provenance

| | |
|--|--|
| Source | ant-client `docs/investigations/2026-09-25-read-startup/traces/baseline-single-3.jsonl.gz` |
| Commit | `a205d06d` on `mickvandijke/v2-1508-hot-chunk-read-spreading` (the branch V2-1617's first consumers build) |
| Produced by | `ant-core/browser-tests/read-startup.mjs` against a live network, with the bundle built `--features browser-wasm,test-utils` |
| Edit applied | **one**: `read-startup.mjs` wraps each record with its own `run`, `mode`, `kind`, `ms` and `download_ms` keys, so the records were filtered to `kind == "trace"` and projected back to the seven keys `Trace::event` actually emits (`id`, `operation`, `target`, `peer`, `phase`, `elapsed_ms`, `detail`). No value was changed, added or reordered. |

Reproduce the projection with:

```bash
gunzip -c baseline-single-3.jsonl.gz \
  | jq -c 'select(.kind=="trace") | {id,operation,target,peer,phase,elapsed_ms,detail}' \
  | gzip -9 > read-startup.jsonl.gz
```

Gzipped because that is how ant-client commits these same traces, and because
1,058 records is 308 KB raw against 26 KB compressed. Both `derive.test.mjs` and
`test_browser_downloaders.py` decompress it in-process (`node:zlib` / `gzip`),
so neither needs a dependency.

## What it contains

| | count |
|--|--|
| trace records | 1,058 |
| `association` / `connect` / `discovery` / `lookup-progress` / `rpc` | 437 / 251 / 17 / 202 / 105 |
| dials attempted that never opened a channel | 43 |
| distinct chunks read | 10 |
| `discovery` records that reported a peer count | 2 |

## The expected values, and how each one was checked

`expected.json` holds what `lib/derive.mjs` produces from this capture. Every
number in it was **independently reproduced with `jq` over the raw records**,
not just read back out of the implementation — the queries are below, and
`test_browser_downloaders.py::TestTraceFixture` pins the same values from the
Python side so the pair cannot quietly move together.

### `stalls: 42`

A stall is a dial that was attempted (`dial` phase present), never opened a
channel, ended in `cancelled-or-error`, and died on one of the transport's two
setup timeouts.

```bash
jq -r 'select(.operation=="association") | [.id,.phase,.elapsed_ms] | @tsv' trace.jsonl \
  | awk -F'\t' '{ph[$1]=ph[$1]" "$2; if($3+0>m[$1]) m[$1]=$3+0}
      END {for(i in ph) if (ph[i]~/dial/ && ph[i]~/cancelled-or-error/ && ph[i]!~/channel-open/) print m[i]}' \
  | sort -n
```

gives 43 values: **41** between 10,003.5 and 10,008 ms, **one** at 30,002.5 ms,
and one at 4,082.6 ms. 41 + 1 = **42**.

The two clusters are the two timeouts, and this is why both are counted:
`REQUEST_TIMEOUT_MS = 10_000` on the data-channel open select, and
`CONNECTION_SETUP_TIMEOUT_MS = 30_000` wrapped by `ensure_connected` around the
whole of `Connection::open`, which fires when the dial hung in ICE gathering or
SDP before the channel-open select was armed. The 4,082.6 ms failure is neither
and is deliberately counted as neither.

### `refused_dials: 0`

No trace record ever carries a refusal: an association emits `start`, `dial`,
`channel-open`, then `finish("authenticated")` or, on drop,
`cancelled-or-error` with an **empty** detail. `DialOutcome::Refused` is set by
`note_refusal` into the dial-failure cache and never reaches the trace. So the
count is a **timing heuristic** reconstructing `note_refusal`'s own condition
from what the trace does carry — dialled, no channel ever opened, dead inside
the client's `REFUSAL_DETECTION_WINDOW` of 3 s.

The `sort -n` above shows no failed dial under 4,082 ms, so **0** here is the
right answer for this capture rather than a vacuous one.

### `first_reads`: 10 entries

```bash
jq -r 'select(.operation=="rpc" and .target!="" and .peer!="") | "\(.target) \(.peer)"' trace.jsonl \
  | awk '!seen[$1]++'
```

gives exactly the 10 `(chunk, holder)` pairs in `expected.json`, in that order.
A chunk read is marked by a **non-empty `target`** (`Trace::new("rpc", <chunk
hex if GetRequest else "">, <peer hex>)`); the detail is empty on `start` and
`found N bytes` / `NotFound { .. }` on `finish`, and the word "GET" appears
nowhere in a real trace.

`known_holders` is non-null for exactly two chunks, both `7`:

```bash
jq -r 'select(.operation=="discovery" and .phase=="finish") | "\(.target) \(.detail)"' trace.jsonl
# 69443b7fecbc… 7 peers
# 7f4b3e83ed0b… 7 peers
```

The other eight are `null` because their `discovery` ended in
`cancelled-or-error` with an empty detail and never reported a count.

**Note on `known_holders`' meaning.** It is the count for the whole download,
not the count known at the moment of the read, and that is forced by the data.
In **all fourteen** committed ant-client captures there is not one target whose
`discovery` finish precedes the first read of that same target — the client
reads peers it already holds while discovery is still running:

```
discovery start → rpc start → discovery finish "7 peers" → rpc finish
```

A strictly-preceding reading would therefore be `null` on every real trace and
the field would say nothing at all.
