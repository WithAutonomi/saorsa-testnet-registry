/**
 * The per-download summary fields, derived from that download's trace.
 *
 * The trace is the point of this role: an Influx point saying a download took
 * 9 seconds says nothing about *why*, while the trace says which holder each
 * chunk's first read went to, how many holders the client knew about at that
 * moment, and which dials stalled or were refused. Those are exactly the
 * quantities ADR-0023 (hot-record read spreading) moves.
 *
 * EVERYTHING HERE IS WRITTEN AGAINST THE REAL EMITTER, which is
 * `ant-core/src/browser/wasm_transport/diagnostics.rs` plus its call sites
 * (verified at ant-client `mickvandijke/v2-1508-hot-chunk-read-spreading` @
 * `a205d06d`, and against the committed capture this module's fixture comes
 * from). `Trace::event` serialises exactly
 *
 *     {id, operation, target, peer, phase, elapsed_ms, detail}
 *
 * and hands it to the callback as a JSON **string**. Four properties of it are
 * load-bearing and none of them is guessable from the field names:
 *
 * * **`detail` is always a string.** It is a `&str` at every call site, so a
 *   structured detail -- an object, an array of peers -- never arrives.
 * * **A chunk GET is marked by a non-empty `target`, not by its detail.**
 *   `Trace::new("rpc", <chunk hex if GetRequest else "">, <peer hex>)`: the
 *   `start` detail is empty and `finish` carries `found N bytes`,
 *   `NotFound { .. }` or `response`. The word "GET" appears nowhere.
 * * **`discovery` reports a COUNT, never a peer list.** Its records carry
 *   `target` with an EMPTY `peer`, and its `finish` detail is `"N peers"`
 *   (`shared.rs`, `trace.finish(&format!("{} peers", result.nodes.len()))`).
 *   `lookup-progress` has `target` AND `peer` empty with a free-text message,
 *   so it says nothing about any particular target at all.
 * * **No record ever contains the word "refused".** An association emits
 *   `start`, `dial`, `channel-open`, then `finish("authenticated")`, or
 *   `cancelled-or-error` with an EMPTY detail when the `Trace` is dropped.
 *   `DialOutcome::Refused` is set by `note_refusal` into the dial-failure
 *   cache and never reaches the trace.
 */

/**
 * The two timeouts that kill a silent (unreachable, NAT'd) holder's dial, and
 * the tolerance either side of each.
 *
 * There are two, and only measuring against one of them undercounts:
 *
 * * **`REQUEST_TIMEOUT_MS = 10_000`**, reached through the bare `timeout(..)`
 *   helper wrapping the `open_rx`/`ended` select in `Connection::open`
 *   ("WebRTC DataChannel opening timed out"). This is the common case.
 * * **`CONNECTION_SETUP_TIMEOUT_MS = 30_000`**, which `ensure_connected` wraps
 *   around the WHOLE of `Connection::open`. It fires when the dial hung before
 *   the channel-open select was even armed -- in ICE gathering or SDP, which
 *   are awaited first -- so the 10 s timeout never got the chance.
 *
 * Measured, not assumed. In the committed capture this module's fixture comes
 * from, of 43 dials that were attempted and never opened a channel: **41** died
 * between 10,003 and 10,008 ms, **1** at 30,002.5 ms, and 1 at 4,082 ms (some
 * other failure, counted as neither). So both windows are real and the 10 s one
 * dominates.
 */
export const CHANNEL_OPEN_TIMEOUT_MS = 10_000;
export const CONNECTION_SETUP_TIMEOUT_MS = 30_000;
/**
 * Half-width of the window around each timeout. Generous because `elapsed_ms`
 * is taken in the page, so it carries the event-loop jitter of a browser that
 * is also decrypting chunks -- and because the two timeouts are 20 s apart,
 * so no plausible tolerance makes them overlap.
 */
export const STALL_TOLERANCE_MS = 1000;
export const STALL_TIMEOUTS_MS = [CHANNEL_OPEN_TIMEOUT_MS, CONNECTION_SETUP_TIMEOUT_MS];

/**
 * The client's own refusal window, `REFUSAL_DETECTION_WINDOW = 3 s`.
 *
 * `note_refusal` calls a dial refused when the association is dead, ICE never
 * connected, and `checks_started.elapsed()` is inside this window. It records
 * that verdict in the dial-failure cache, which the trace never sees — so the
 * count below is a **timing heuristic reconstructing the same condition** from
 * what the trace does carry: the dial was made, no channel ever opened, and it
 * died this fast. It is not the client's own verdict.
 */
export const REFUSAL_WINDOW_MS = 3000;

/** The phase a dial reaches when the data channel actually opened. */
const CHANNEL_OPEN = "channel-open";
/** The phase a dial ends in when it did not. */
const FAILED = "cancelled-or-error";
/** The phase that proves a dial was actually attempted. */
const DIAL = "dial";

/** `discovery` finish details are `"N peers"` and nothing else. */
const PEER_COUNT = /^(\d+)\s+peers?$/;

/**
 * Whether an `rpc` record is a chunk read.
 *
 * A non-empty `target` IS the GET-ness: `Trace::new("rpc", ..)` fills `target`
 * with the chunk hex only for a `GetRequest` and leaves it empty for every
 * other `ChunkMessageBody`. Matching on the detail (which is empty on `start`
 * and `found N bytes` / `NotFound { .. }` on `finish`) finds nothing.
 */
function isChunkRead(record) {
  return record.operation === "rpc" && !!record.target;
}

/** Group a trace into one entry per dial, keyed on the record `id`. */
function dialsById(records) {
  const dials = new Map();
  for (const record of records) {
    if (record.operation !== "association") continue;
    const key = String(record.id);
    let dial = dials.get(key);
    if (!dial) {
      dial = { id: key, phases: [], elapsedMs: 0, peer: record.peer };
      dials.set(key, dial);
    }
    dial.phases.push(record.phase);
    if (record.peer) dial.peer = record.peer;
    if (typeof record.elapsed_ms === "number") {
      // `elapsed_ms` is measured from the trace's own start, so the largest is
      // how long the dial ran for. `max` rather than `last` survives a trace
      // written out of order.
      dial.elapsedMs = Math.max(dial.elapsedMs, record.elapsed_ms);
    }
  }
  return [...dials.values()];
}

/** Dials that were attempted, never opened a channel, and then failed. */
function failedDials(records) {
  return dialsById(records).filter(
    (d) =>
      // `dial` is required, and excludes the dial-failure-cache early return:
      // that path returns Err BEFORE `trace.event("dial", ..)`, so its
      // `cancelled-or-error` arrives at ~0 ms having dialled nothing, and
      // counting it would report a suppression as a refusal.
      d.phases.includes(DIAL) && d.phases.includes(FAILED) && !d.phases.includes(CHANNEL_OPEN),
  );
}

/**
 * Dials that stalled: attempted, never opened a channel, and died on one of the
 * transport's two setup timeouts — an unreachable (NAT'd) holder.
 *
 * All the conditions matter. A dial that reached `channel-open` and failed
 * afterwards failed at the authentication layer, and one that died in 200 ms
 * was refused rather than unreachable. A failure at neither timeout is counted
 * as neither: it is a third thing, and guessing which would be worse than
 * leaving it out of both.
 */
export function countStalls(records) {
  return failedDials(records).filter((d) =>
    STALL_TIMEOUTS_MS.some((t) => Math.abs(d.elapsedMs - t) <= STALL_TOLERANCE_MS),
  ).length;
}

/**
 * Dials the far side appears to have refused — a **timing heuristic**, because
 * the trace carries no refusal marker (see :data:`REFUSAL_WINDOW_MS`).
 *
 * Counted separately from a stall so the two cannot be confused: at four
 * sessions per source IPv4 address, a fifth instance on the VM would see
 * refusals on every dial, and reading that as a NAT'd fleet would be the wrong
 * conclusion about the network from a mistake in the manifest.
 */
export function countRefusedDials(records) {
  return failedDials(records).filter((d) => d.elapsedMs < REFUSAL_WINDOW_MS).length;
}

/**
 * For each chunk the download read, the holder its FIRST read went to and how
 * many holders discovery reported for that chunk.
 *
 * The first read is the one that matters: a client that fell back to a second
 * holder has already paid the first one's `NotFound` or silence.
 *
 * **`known_holders` is the count for the whole download, NOT the count known at
 * the moment of the read**, and that is a measured limitation rather than a
 * shortcut. The only holder count the trace carries is the integer in a
 * `discovery` `finish` detail (`"N peers"`), and in all fourteen committed
 * ant-client captures that record arrives AFTER the first read of its own
 * target -- the client reads peers it already holds while discovery is still
 * running:
 *
 *     discovery start → rpc start → discovery finish "7 peers" → rpc finish
 *
 * So a strictly-preceding reading would be `null` on every real trace and the
 * field would say nothing at all. This is the obtainable quantity: how many
 * holders discovery found for this chunk during this download.
 *
 * `null` means no `discovery` for that target ever reported a count -- it
 * legitimately ends in `cancelled-or-error` with an empty detail, which it does
 * for 8 of the 10 chunks in the fixture. Null, not 0: "nothing reported how
 * many" is not "the client knew of no holders".
 */
export function firstReads(records) {
  // Two passes, because the count arrives after the read it describes.
  const counts = new Map();
  for (const record of records) {
    if (record.operation !== "discovery" || record.phase !== "finish" || !record.target) continue;
    const match = PEER_COUNT.exec(String(record.detail || "").trim());
    if (match) counts.set(record.target, Number(match[1]));
  }

  const seen = new Map();
  for (const record of records) {
    if (!isChunkRead(record) || seen.has(record.target) || !record.peer) continue;
    seen.set(record.target, {
      chunk: record.target,
      holder: record.peer,
      known_holders: counts.has(record.target) ? counts.get(record.target) : null,
    });
  }
  return [...seen.values()];
}

/** Every derived field for one download, ready to put on the Influx point. */
export function deriveFields(records) {
  return {
    stalls: countStalls(records),
    refused_dials: countRefusedDials(records),
    first_reads: firstReads(records),
  };
}

/** Parse a trace JSONL body, skipping the header line and anything malformed. */
export function parseTraceJsonl(text) {
  const out = [];
  for (const line of String(text).split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let record;
    try {
      record = JSON.parse(trimmed);
    } catch {
      continue;
    }
    if (record && record.header) continue;
    out.push(record);
  }
  return out;
}
