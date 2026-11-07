/**
 * The swarm reading: per-chunk sources out of the progress messages, and the
 * client's own counters (INFRA-532).
 *
 * Both halves are the instrument ADR-0026 is measured with, and both are
 * absent from a pinned older bundle — so the test that matters most here is
 * the one asserting a bundle with neither method reports `swarm_present = 0`
 * and zeros rather than failing the round.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  SWARM_COUNTERS,
  SWARM_DISCOVERY_COUNTERS,
  countChunkSources,
  mapSwarmStats,
  parseProgressLine,
  roomsToJson,
  swarmFields,
} from "../lib/swarm.mjs";

//: The three `{from}` forms ant-core's download progress can carry
//: (`ant-core/src/browser/wasm_transport/download.rs`), plus the room line.
const FROM_NODE = "Downloaded chunk 1/3 from 12d3kooweabc123 after 1 attempts";
const FROM_CACHE = "Downloaded chunk 2/3 from cache after 1 attempts";
const FROM_READER = "Downloaded chunk 3/3 from a reader after 2 attempts";
const ROOM = "Swarm room ab12cd34";

test("a chunk line yields its index, total, source and attempts", () => {
  assert.deepEqual(parseProgressLine(FROM_NODE), {
    kind: "chunk",
    index: 1,
    total: 3,
    from: "12d3kooweabc123",
    attempts: 1,
  });
});

test("`a reader` is one source even though it carries a space", () => {
  // The whole measurement is this line: a tight token pattern for `{from}`
  // would drop exactly the chunks the swarm served.
  assert.equal(parseProgressLine(FROM_READER).from, "a reader");
});

test("the room line is recognised as a room, not as a chunk", () => {
  assert.deepEqual(parseProgressLine(ROOM), { kind: "room", tag: "ab12cd34" });
});

test("a line this parser does not know yields null, not a half-filled chunk", () => {
  // Counting it would put it in the denominator of a per-source share.
  for (const line of [
    "Connected to autonomi network",
    "Downloaded chunk from a reader",
    "Downloaded chunk 1/3 from",
    "",
    undefined,
    null,
    42,
  ]) {
    assert.equal(parseProgressLine(line), null, String(line));
  }
});

test("a log line that merely contains the phrase is not counted", () => {
  assert.equal(
    parseProgressLine("2026-11-07 INFO Downloaded chunk 1/3 from cache after 1 attempts"),
    null,
  );
});

test("the three sources are counted apart and the total comes off the line", () => {
  const counts = countChunkSources([FROM_NODE, FROM_CACHE, FROM_READER, ROOM, "noise"]);
  assert.deepEqual(counts, {
    chunks_total: 3,
    chunks_from_nodes: 1,
    chunks_from_cache: 1,
    chunks_from_readers: 1,
    swarm_room_tag: "ab12cd34",
  });
});

test("the total is the largest declared, not the number of lines parsed", () => {
  // The two differ exactly when a chunk was never read, which is worth seeing.
  const counts = countChunkSources([
    "Downloaded chunk 7/300 from cache after 1 attempts",
    "Downloaded chunk 9/300 from a reader after 1 attempts",
  ]);
  assert.equal(counts.chunks_total, 300);
  assert.equal(counts.chunks_from_cache + counts.chunks_from_readers, 2);
});

test("a source that is neither cache nor a reader counts as a node", () => {
  // Rather than being tested against a hex pattern: a bundle that renders a
  // peer id differently must not have its chunks vanish from every count.
  const counts = countChunkSources(["Downloaded chunk 1/1 from peer-7 after 1 attempts"]);
  assert.equal(counts.chunks_from_nodes, 1);
});

test("no room line leaves the tag empty rather than undefined", () => {
  assert.equal(countChunkSources([FROM_NODE]).swarm_room_tag, "");
  assert.equal(countChunkSources([]).swarm_room_tag, "");
  assert.equal(countChunkSources(undefined).swarm_room_tag, "");
});

//: `SwarmStats` as the branch's serde names render it — the same object the
//: native reader takes off `ant --json` (INFRA-531's `SWARM_RESULT`).
const STATS_SNAKE = {
  records_from_peers: 7,
  bytes_from_peers: 7_340_032,
  records_rejected: 1,
  asks_busy: 2,
  asks_missing: 3,
  asks_failed: 4,
  records_served: 5,
  bytes_served: 5_242_880,
  links: 6,
  rooms: 1,
  discovery: {
    lookups: 11,
    announces: 12,
    announces_failed: 13,
    announces_timed_out: 14,
    announces_refused: 15,
    bytes_sent: 16,
    bytes_received: 17,
    offers_made: 18,
    offers_delivered: 19,
    offers_received: 20,
    answers_sent: 21,
    answers_received: 22,
    channels_opened: 23,
    links_proven: 24,
    links_failed: 25,
  },
};

const ROOMS = [{ tag: "ab12cd34", records_from_peers: 7, open_links: 2 }];

test("the counters map onto the point's snake_case names", () => {
  const mapped = mapSwarmStats(STATS_SNAKE, ROOMS);
  assert.equal(mapped.swarm_present, 1);
  assert.equal(mapped.records_from_peers, 7);
  assert.equal(mapped.bytes_from_peers, 7_340_032);
  assert.equal(mapped.rooms, 1);
  assert.equal(mapped.discovery_lookups, 11);
  assert.equal(mapped.discovery_links_failed, 25);
  assert.equal(mapped.swarm_rooms, JSON.stringify(ROOMS));
});

test("a camelCase bundle maps onto exactly the same fields", () => {
  // Which convention the object uses is a property of how its `SwarmStats` is
  // serialised: serde keeps the Rust names, wasm_bindgen's own getters are
  // camelCase. Reading both costs one property lookup and means the point's
  // field names do not depend on it.
  const camel = {
    recordsFromPeers: 7,
    bytesFromPeers: 7_340_032,
    asksBusy: 2,
    discovery: { lookups: 11, announcesTimedOut: 14, linksFailed: 25 },
  };
  const mapped = mapSwarmStats(camel, ROOMS);
  assert.equal(mapped.records_from_peers, 7);
  assert.equal(mapped.bytes_from_peers, 7_340_032);
  assert.equal(mapped.asks_busy, 2);
  assert.equal(mapped.discovery_lookups, 11);
  assert.equal(mapped.discovery_announces_timed_out, 14);
  assert.equal(mapped.discovery_links_failed, 25);
});

test("a bundle with no swarm methods reports swarm_present=0 and zeros", () => {
  // The case a pinned older client is in, and it must not break the round: a
  // zero `records_from_peers` beside `swarm_present = 0` is a fact about the
  // build, where the same zero without the flag reads as a swarm that formed
  // and shared nothing.
  const mapped = mapSwarmStats(undefined, undefined);
  assert.equal(mapped.swarm_present, 0);
  for (const name of SWARM_COUNTERS) assert.equal(mapped[name], 0, name);
  for (const name of SWARM_DISCOVERY_COUNTERS) {
    assert.equal(mapped[`discovery_${name}`], 0, name);
  }
  assert.equal(mapped.swarm_rooms, "[]");
});

test("stats present with no discovery block still report the ten counters", () => {
  const mapped = mapSwarmStats({ records_from_peers: 3 }, []);
  assert.equal(mapped.swarm_present, 1);
  assert.equal(mapped.records_from_peers, 3);
  assert.equal(mapped.discovery_lookups, 0);
  assert.equal(mapped.swarm_rooms, "[]");
});

test("a u64 counter arriving as a BigInt or a string is still a number", () => {
  const mapped = mapSwarmStats({ bytes_from_peers: 7_340_032n, links: "6" }, []);
  assert.equal(mapped.bytes_from_peers, 7_340_032);
  assert.equal(mapped.links, 6);
});

test("rooms that are not an array are recorded as none", () => {
  assert.equal(mapSwarmStats(STATS_SNAKE, undefined).swarm_rooms, "[]");
  assert.equal(mapSwarmStats(STATS_SNAKE, { tag: "x" }).swarm_rooms, "[]");
});

test("every field of a round that measured nothing is present and zero", () => {
  // A skip writes the same field set as a download: Influx types a field on
  // first write, and a field present on some points and absent on others is one
  // no per-round sum can read.
  const fields = swarmFields(null);
  assert.equal(fields.swarm_present, 0);
  assert.equal(fields.chunks_total, 0);
  assert.equal(fields.chunks_from_readers, 0);
  assert.equal(fields.swarm_room_tag, "");
  assert.equal(fields.swarm_rooms, "[]");
});

test("the room tag prefers the client's own stats over the log line", () => {
  const fields = swarmFields({
    progress: [ROOM],
    swarmStats: STATS_SNAKE,
    swarmRoomStats: [{ tag: "from-stats" }],
  });
  assert.equal(fields.swarm_room_tag, "from-stats");
});

test("the progress line supplies the tag when the stats carry no room", () => {
  const fields = swarmFields({ progress: [ROOM], swarmStats: STATS_SNAKE, swarmRoomStats: [] });
  assert.equal(fields.swarm_room_tag, "ab12cd34");
});

test("an outcome's chunk counts and stats are both in the field set", () => {
  const fields = swarmFields({
    progress: [FROM_NODE, FROM_CACHE, FROM_READER],
    swarmStats: STATS_SNAKE,
    swarmRoomStats: ROOMS,
  });
  assert.equal(fields.chunks_from_readers, 1);
  assert.equal(fields.records_from_peers, 7);
  assert.equal(fields.swarm_present, 1);
});

test("a room's u64 counters serialise rather than throwing", () => {
  // `JSON.stringify` throws "Do not know how to serialize a BigInt", and a
  // room's counters are Rust u64s — the representation `counter()` accepts at
  // the top level. Unhandled, that throw reached the harness's catch and turned
  // a SUCCESSFUL transfer into `failure` / `other` with the swarm zeroed.
  const rooms = [{ tag: "r", records_from_peers: 7n, bytes_from_peers: 7_340_032n }];
  assert.deepEqual(JSON.parse(roomsToJson(rooms)), [
    { tag: "r", records_from_peers: 7, bytes_from_peers: 7340032 },
  ]);
});

test("a BigInt nested deeper than one level is normalised too", () => {
  const rooms = [{ tag: "r", links: [{ peer: "p1", bytes: 10n }], inner: { n: 2n } }];
  assert.deepEqual(JSON.parse(roomsToJson(rooms)), [
    { tag: "r", links: [{ peer: "p1", bytes: 10 }], inner: { n: 2 } },
  ]);
});

test("the whole mapping survives BigInt rooms", () => {
  const mapped = mapSwarmStats(
    { records_from_peers: 7n, discovery: { lookups: 11n } },
    [{ tag: "r", records_from_peers: 7n }],
  );
  assert.equal(mapped.records_from_peers, 7);
  assert.equal(mapped.discovery_lookups, 11);
  assert.deepEqual(JSON.parse(mapped.swarm_rooms), [{ tag: "r", records_from_peers: 7 }]);
});

test("a room payload that cannot be serialised costs the detail and nothing more", () => {
  // A circular reference, a Symbol: the rooms are a diagnostic, and no shape of
  // them may change the transfer's status.
  const circular = { tag: "r" };
  circular.self = circular;
  assert.equal(roomsToJson([circular]), "[]");
  assert.equal(roomsToJson([{ tag: "r", weird: Symbol("x") }]), '[{"tag":"r"}]');
});

test("rooms that are not an array are still recorded as none", () => {
  assert.equal(roomsToJson(undefined), "[]");
  assert.equal(roomsToJson({ tag: "x" }), "[]");
  assert.equal(roomsToJson([]), "[]");
});
