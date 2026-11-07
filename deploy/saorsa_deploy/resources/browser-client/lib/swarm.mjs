/**
 * What the swarm did, as the page can report it (INFRA-532).
 *
 * Two independent readings, both from the branch bundle and both absent from a
 * pinned older one:
 *
 * * the per-chunk SOURCE, parsed out of `downloadPublicFile`'s progress
 *   messages — a node, the local cache, or another reader, which is the one
 *   figure that says the swarm served this download rather than merely existing;
 * * the client's own `swarmStats()` / `swarmRoomStats()` counters, the same
 *   twenty-five the native reader takes off `ant --json` (INFRA-531), so the
 *   two cohorts of one run are read with one vocabulary.
 *
 * It lives here rather than inside `downloadInPage` so `node --test` can
 * exercise it without a browser: the page accumulates the raw progress strings
 * and returns them with its result, exactly as it already returns the whole
 * per-dial trace, and the counting happens on this side. The trace is
 * 599 KB–1.30 MB per download, so a few hundred progress lines cross that
 * boundary for free.
 *
 * **A bundle without `swarmStats` must not break the round.** The fields are
 * written as 0 with `swarm_present = 0` beside them, which is what tells a
 * pinned client's zeros from a swarm that formed and shared nothing — the same
 * distinction `swarm_present` carries on the native role.
 */

/** The ten top-level `SwarmStats` counters, in the order they are recorded. */
export const SWARM_COUNTERS = [
  "records_from_peers",
  "bytes_from_peers",
  "records_rejected",
  "asks_busy",
  "asks_missing",
  "asks_failed",
  "records_served",
  "bytes_served",
  "links",
  "rooms",
];

/**
 * The fifteen `discovery` counters, recorded under a `discovery_` prefix.
 *
 * The prefix is what keeps them apart from the record counters they sit beside:
 * Influx has no nested fields, so `links` and `discovery_links_proven` are two
 * flat names rather than one object.
 */
export const SWARM_DISCOVERY_COUNTERS = [
  "lookups",
  "announces",
  "announces_failed",
  "announces_timed_out",
  "announces_refused",
  "bytes_sent",
  "bytes_received",
  "offers_made",
  "offers_delivered",
  "offers_received",
  "answers_sent",
  "answers_received",
  "channels_opened",
  "links_proven",
  "links_failed",
];

/** Per-chunk source counts, and the room tag. */
export const CHUNK_SOURCE_COUNTS = [
  "chunks_total",
  "chunks_from_nodes",
  "chunks_from_cache",
  "chunks_from_readers",
];

/** The literal `{from}` values that are not a peer id. */
const FROM_CACHE = "cache";
const FROM_READER = "a reader";

/**
 * `Downloaded chunk {n}/{total} from {from} after {attempts} attempts`.
 *
 * `{from}` is matched loosely on purpose — it is a hex peer id, `cache` or
 * `a reader`, and the last of those carries a space, so a tight token pattern
 * would drop exactly the line this whole measurement is about. Anchored at both
 * ends so a log line that merely contains the phrase is not counted.
 */
const CHUNK_RE = /^Downloaded chunk (\d+)\/(\d+) from (.+) after (\d+) attempts?$/;

/** `Swarm room {tag}` — the room this download joined. */
const ROOM_RE = /^Swarm room (\S+)$/;

/**
 * One progress line, or `null` when it is not one of the two we read.
 *
 * `null` rather than a partially-filled object: a malformed line is not a chunk
 * that came from an unknown source, it is a line this parser does not know, and
 * counting it would put it in the denominator of a per-source share.
 */
export function parseProgressLine(line) {
  if (typeof line !== "string") return null;
  const text = line.trim();
  const chunk = CHUNK_RE.exec(text);
  if (chunk) {
    return {
      kind: "chunk",
      index: Number.parseInt(chunk[1], 10),
      total: Number.parseInt(chunk[2], 10),
      from: chunk[3].trim(),
      attempts: Number.parseInt(chunk[4], 10),
    };
  }
  const room = ROOM_RE.exec(text);
  if (room) return { kind: "room", tag: room[1] };
  return null;
}

/**
 * Where this download's chunks came from.
 *
 * `chunks_total` is the `{total}` the lines declare (the largest one seen, so a
 * download whose first lines were lost still reports the file's chunk count)
 * rather than the number of lines parsed — the two differ exactly when a chunk
 * was never read, which is worth being able to see.
 *
 * Anything that is neither `cache` nor `a reader` counts as a NODE, rather than
 * being tested against a hex pattern: a future bundle that renders a peer id
 * differently would otherwise have its chunks vanish from every count, and
 * "served by something that is not a peer and not the cache" is a node in every
 * case the branch can produce.
 */
export function countChunkSources(lines) {
  const out = {
    chunks_total: 0,
    chunks_from_nodes: 0,
    chunks_from_cache: 0,
    chunks_from_readers: 0,
    swarm_room_tag: "",
  };
  for (const line of lines || []) {
    const parsed = parseProgressLine(line);
    if (parsed === null) continue;
    if (parsed.kind === "room") {
      out.swarm_room_tag = parsed.tag;
      continue;
    }
    if (Number.isFinite(parsed.total)) {
      out.chunks_total = Math.max(out.chunks_total, parsed.total);
    }
    if (parsed.from === FROM_CACHE) out.chunks_from_cache += 1;
    else if (parsed.from === FROM_READER) out.chunks_from_readers += 1;
    else out.chunks_from_nodes += 1;
  }
  return out;
}

/** `records_from_peers` -> `recordsFromPeers`, for the camelCase lookup below. */
function camel(name) {
  return name.replace(/_([a-z])/g, (_m, c) => c.toUpperCase());
}

/**
 * A counter as a number, 0 for anything that is not one.
 *
 * Zero rather than null: these are counters, and a download whose client
 * reported no swarm records explicit zeros with `swarm_present = 0` beside
 * them. A BigInt is tolerated because `serde_wasm_bindgen` renders a Rust `u64`
 * as one whenever the bundle is built with BigInt support.
 */
function counter(value) {
  if (typeof value === "number") return Number.isFinite(value) ? Math.trunc(value) : 0;
  if (typeof value === "bigint") return Number(value);
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "string") {
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return 0;
}

/**
 * Read one counter off a stats object under either naming convention.
 *
 * Which one the bundle uses is a property of how its `SwarmStats` is
 * serialised, and both spellings are live possibilities: `serde_wasm_bindgen`
 * keeps the Rust field names (snake_case) while `wasm_bindgen`'s own getters
 * are camelCase. Reading both costs one property lookup and means the harness
 * does not have to be re-released when the bundle's serialisation changes —
 * which, since the point fields are snake_case either way, nothing downstream
 * would even notice.
 */
function pick(source, name) {
  if (!source || typeof source !== "object") return 0;
  if (name in source) return counter(source[name]);
  const alias = camel(name);
  if (alias in source) return counter(source[alias]);
  return 0;
}

function nested(source, name) {
  if (!source || typeof source !== "object") return undefined;
  if (source[name] && typeof source[name] === "object") return source[name];
  const alias = camel(name);
  if (source[alias] && typeof source[alias] === "object") return source[alias];
  return undefined;
}

/**
 * The twenty-five counters, `swarm_present`, and `swarm_rooms` as JSON.
 *
 * `stats` is whatever `client.swarmStats()` returned and `rooms` whatever
 * `client.swarmRoomStats()` did; either being absent (a bundle that has no such
 * method, or one that threw) is `swarm_present = 0` and zeros throughout, never
 * an error — that is what lets a cohort run a pinned older client at all.
 *
 * `swarm_rooms` is a JSON string because it is a variable-length per-room
 * detail: it can be neither a tag (unbounded cardinality) nor a numeric field,
 * and `record-results` unpacks it into the row's JSON column.
 */
export function mapSwarmStats(stats, rooms) {
  const present = !!stats && typeof stats === "object";
  const out = { swarm_present: present ? 1 : 0 };
  for (const name of SWARM_COUNTERS) {
    out[name] = present ? pick(stats, name) : 0;
  }
  const discovery = present ? nested(stats, "discovery") : undefined;
  for (const name of SWARM_DISCOVERY_COUNTERS) {
    out[`discovery_${name}`] = discovery ? pick(discovery, name) : 0;
  }
  out.swarm_rooms = JSON.stringify(Array.isArray(rooms) ? rooms : []);
  return out;
}

/**
 * Every swarm-derived field of a point, with the zeros a round that measured
 * nothing carries.
 *
 * One function so the skip path, the failure path and the success path cannot
 * write three different field sets — Influx types a field on first write, and a
 * point missing one is a gap the per-round sums would read as a zero anyway.
 */
export function swarmFields(outcome) {
  const counts = countChunkSources((outcome && outcome.progress) || []);
  const stats = mapSwarmStats(
    outcome ? outcome.swarmStats : undefined,
    outcome ? outcome.swarmRoomStats : undefined,
  );
  // The room tag from the client's own stats wins over the progress line's when
  // both are there: `swarmRoomStats()` is the structured answer and the line is
  // a log message. Neither is required.
  const fromRooms = roomTagFromStats(outcome ? outcome.swarmRoomStats : undefined);
  return {
    ...counts,
    ...stats,
    swarm_room_tag: fromRooms || counts.swarm_room_tag,
  };
}

/** The first room tag `swarmRoomStats()` reported, or `""`. */
export function roomTagFromStats(rooms) {
  if (!Array.isArray(rooms)) return "";
  for (const room of rooms) {
    if (!room || typeof room !== "object") continue;
    const tag = room.tag;
    if (typeof tag === "string" && tag) return tag;
  }
  return "";
}
