/**
 * The per-download summary fields, derived from that download's trace.
 *
 * The trace is the point of this role: an Influx point saying a download took
 * 9 seconds says nothing about *why*, while the trace says which holder each
 * chunk's first read went to, how many holders the client knew about at that
 * moment, and which dials stalled or were refused. Those are exactly the
 * quantities ADR-0023 (hot-record read spreading) moves, so they are summarised
 * onto the point itself and the full trace is kept on disk beside it.
 *
 * Trace records are what `setBrowserTrace` emits:
 *
 *   {id, operation, target, peer, phase, elapsed_ms, detail}
 *
 * with `operation` one of association | connect | rpc | discovery |
 * lookup-progress and `phase` one of start | dial | channel-open |
 * authenticated | admitted | finish | cancelled-or-error.
 */

/**
 * The client's channel-open timeout is 10 s, so a dial that ends in
 * `cancelled-or-error` at about 10 s is an unreachable (NAT'd) holder rather
 * than a refusal or a slow one. The window is deliberately wide either side —
 * the elapsed figure is taken in the page, so it carries the event-loop jitter
 * of a browser that is also decrypting chunks.
 */
export const STALL_MIN_MS = 9000;
export const STALL_MAX_MS = 11000;

/** The phase a dial reaches when the data channel actually opened. */
const CHANNEL_OPEN = "channel-open";
/** The phase a dial ends in when it did not. */
const FAILED = "cancelled-or-error";

/** Operations that report which peers are candidates for a target. */
const CANDIDATE_OPERATIONS = new Set(["discovery", "lookup-progress"]);

function detailText(record) {
  const detail = record.detail;
  if (detail === undefined || detail === null) return "";
  return typeof detail === "string" ? detail : JSON.stringify(detail);
}

/** Whether an `rpc` record is a chunk GET rather than some other RPC. */
function isChunkGet(record) {
  if (record.operation !== "rpc") return false;
  return /\bget\b/i.test(detailText(record));
}

/** Group a trace into one entry per dial, keyed on the record `id`. */
function dialsById(records) {
  const dials = new Map();
  for (const record of records) {
    if (record.operation !== "association") continue;
    const key = String(record.id);
    let dial = dials.get(key);
    if (!dial) {
      dial = { id: key, phases: [], elapsedMs: 0, details: [], peer: record.peer };
      dials.set(key, dial);
    }
    dial.phases.push(record.phase);
    if (record.peer) dial.peer = record.peer;
    if (typeof record.elapsed_ms === "number") {
      // The last elapsed figure on the dial is how long it ran for: the records
      // are cumulative from the dial's start, so `max` and `last` agree except
      // when a trace is written out of order, and `max` survives that.
      dial.elapsedMs = Math.max(dial.elapsedMs, record.elapsed_ms);
    }
    const text = detailText(record);
    if (text) dial.details.push(text);
  }
  return [...dials.values()];
}

/**
 * Dials that stalled: ended in `cancelled-or-error`, never reached
 * `channel-open`, and took about the 10 s channel-open timeout.
 *
 * All three conditions together, because each on its own means something else:
 * a failed dial that returned in 200 ms was refused, and one that reached
 * `channel-open` and failed later failed at a different layer.
 */
export function countStalls(records) {
  return dialsById(records).filter(
    (d) =>
      d.phases.includes(FAILED) &&
      !d.phases.includes(CHANNEL_OPEN) &&
      d.elapsedMs >= STALL_MIN_MS &&
      d.elapsedMs <= STALL_MAX_MS,
  ).length;
}

/**
 * Dials the far side refused.
 *
 * A refusal is the node saying no — which, at four sessions per source IPv4
 * address, is what a fifth instance on the VM would see on every dial. Counted
 * separately from a stall precisely so that configuration mistake cannot be
 * read as a NAT'd fleet.
 */
export function countRefusedDials(records) {
  return dialsById(records).filter((d) => d.details.some((t) => /refused/i.test(t))).length;
}

/**
 * For each chunk the download fetched, the holder its FIRST read went to and
 * how many holders the client knew of at that moment.
 *
 * The first read is the one that matters: a client that fell back to a second
 * holder has already paid the latency of the first one's refusal or silence.
 * `known_holders` comes from the `discovery` / `lookup-progress` records that
 * PRECEDE the read, so it is what the client knew when it chose — counting the
 * whole trace's candidates would include peers it learned about afterwards and
 * make every spread look wider than it was.
 */
export function firstReads(records) {
  const candidates = new Map();
  const seen = new Map();
  for (const record of records) {
    const target = record.target;
    if (!target) continue;
    if (CANDIDATE_OPERATIONS.has(record.operation)) {
      if (!candidates.has(target)) candidates.set(target, new Set());
      if (record.peer) candidates.get(target).add(record.peer);
      // A candidate record may instead carry the whole set in its detail, as a
      // `peers` array. Absorbed when it is one, ignored when it is not: a
      // missing holder count is a smaller loss than a wrong one.
      const peers = record.detail && record.detail.peers;
      if (Array.isArray(peers)) {
        for (const peer of peers) candidates.get(target).add(String(peer));
      }
      continue;
    }
    if (!isChunkGet(record) || seen.has(target) || !record.peer) continue;
    seen.set(target, {
      chunk: target,
      holder: record.peer,
      known_holders: (candidates.get(target) || new Set()).size,
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
