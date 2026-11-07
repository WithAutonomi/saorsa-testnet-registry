/**
 * Line-protocol construction for the `browser_download` measurement.
 *
 * Influx types a field on first write and DROPS every later point that
 * disagrees — the whole point, not just the offending field — so the integer
 * fields carrying their `i` suffix is load-bearing rather than cosmetic.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MEASUREMENT,
  SWARM_POINT_COUNTERS,
  buildLine,
  buildPoint,
  escapeTag,
} from "../lib/influx.mjs";
import { swarmFields } from "../lib/swarm.mjs";

const RESULT = {
  host: "DEV-01-ant-client-browser-download-lon1-1",
  testnetName: "DEV-01",
  region: "lon1",
  roundId: 2_767_992,
  instance: 2,
  hotFileIndex: 1,
  status: "success",
  address: "aa11",
  fileSize: 52_428_800,
  totalMs: 8123.7,
  connectMs: 912.4,
  startNs: 1_660_795_200n * 1_000_000_000n,
  startedLateMs: 240.6,
  seeds: "/ip4/10.0.0.1/udp/10001/webrtc-direct,/ip4/10.0.0.2/udp/10001/webrtc-direct",
  stalls: 2,
  refusedDials: 1,
  firstReads: [{ chunk: "c1", holder: "p1", known_holders: 3 }],
};

test("a point goes to its own measurement, not to `download`", () => {
  // A synchronised cohort hammering one file and a single client drawing random
  // files are two unlike workloads; sharing a series would let any panel
  // average them.
  assert.equal(MEASUREMENT, "browser_download");
  assert.ok(buildPoint(RESULT).startsWith("browser_download,"));
});

test("the role tag is fixed", () => {
  assert.ok(buildPoint(RESULT).includes("host_role=browser_downloader"));
});

test("round, instance and hot file index are tags", () => {
  const line = buildPoint(RESULT);
  assert.ok(line.includes("round_id=2767992"));
  assert.ok(line.includes("instance=2"));
  assert.ok(line.includes("hot_file_index=1"));
});

test("start_ns survives as an exact nanosecond value", () => {
  // 1.66e18 is two orders of magnitude past Number.MAX_SAFE_INTEGER: as a
  // Number it would round to the nearest 256 ns and every recorded round start
  // would be a lie.
  assert.ok(buildPoint(RESULT).includes("start_ns=1660795200000000000i"));
  assert.ok(!Number.isSafeInteger(1_660_795_200_000_000_000));
});

test("a bigint field carries the i suffix", () => {
  assert.equal(buildLine("m", {}, { f: 2n ** 62n }), `m f=${2n ** 62n}i`);
});

test("integer fields carry the i suffix and are rounded", () => {
  const line = buildPoint(RESULT);
  assert.ok(line.includes("total_ms=8124i"));
  assert.ok(line.includes("connect_ms=912i"));
  assert.ok(line.includes("started_late_ms=241i"));
  assert.ok(line.includes("file_size=52428800i"));
  assert.ok(line.includes("stalls=2i"));
  assert.ok(line.includes("refused_dials=1i"));
});

test("status and error_kind are strings, with an empty kind on success", () => {
  const line = buildPoint(RESULT);
  assert.ok(line.includes('status="success"'));
  assert.ok(line.includes('error_kind=""'));
});

test("first_reads travels as a JSON string field", () => {
  // Per-chunk and variable-length, so it can be neither a tag (unbounded
  // cardinality) nor numeric fields.
  assert.ok(
    buildPoint(RESULT).includes(
      'first_reads="[{\\"chunk\\":\\"c1\\",\\"holder\\":\\"p1\\",\\"known_holders\\":3}]"',
    ),
  );
});

test("a skip is a point, with no address and no hot file index", () => {
  const line = buildPoint({
    host: "h",
    testnetName: "DEV-01",
    region: "lon1",
    roundId: 1,
    instance: 1,
    status: "skipped",
    errorKind: "no_hot_files",
  });
  // A round with no point from an instance is itself a finding, and that
  // reading only works if a skip is a point rather than a silence.
  assert.ok(line.includes('status="skipped"'));
  assert.ok(line.includes('error_kind="no_hot_files"'));
  assert.ok(!line.includes("hot_file_index="));
  assert.ok(line.includes('address=""'));
  assert.ok(line.includes("first_reads=\"[]\""));
});

test("tag separators are escaped", () => {
  // An unescaped comma or space in a tag silently splits the line into a
  // different series, which Influx accepts without complaint.
  assert.equal(escapeTag("a,b c=d"), "a\\,b\\ c\\=d");
});

test("empty tags are dropped rather than written blank", () => {
  assert.equal(buildLine("m", { a: "x", b: "" }, { f: 1 }), "m,a=x f=1");
});

test("the swarm fields are on every point, a skip included", () => {
  // Influx types a field on first write and DROPS every later point that
  // disagrees, so a field present on a download and absent on a skip is one no
  // per-round sum can read.
  const line = buildPoint({ ...RESULT, status: "skipped", errorKind: "no_hot_files" });
  for (const field of SWARM_POINT_COUNTERS) {
    assert.ok(line.includes(`${field}=0i`), field);
  }
  assert.ok(line.includes("swarm_present=0i"));
  assert.ok(line.includes('swarm_room_tag=""'));
  assert.ok(line.includes('swarm_rooms="[]"'));
});

test("the swarm counters a round measured reach the point", () => {
  const line = buildPoint({
    ...RESULT,
    ...swarmFields({
      progress: [
        "Downloaded chunk 1/3 from 12d3kooweabc123 after 1 attempts",
        "Downloaded chunk 2/3 from cache after 1 attempts",
        "Downloaded chunk 3/3 from a reader after 2 attempts",
        "Swarm room ab12cd34",
      ],
      swarmStats: { records_from_peers: 7, bytes_from_peers: 7_340_032, discovery: { lookups: 11 } },
      swarmRoomStats: [{ tag: "ab12cd34" }],
    }),
  });
  assert.ok(line.includes("chunks_total=3i"));
  assert.ok(line.includes("chunks_from_nodes=1i"));
  assert.ok(line.includes("chunks_from_cache=1i"));
  assert.ok(line.includes("chunks_from_readers=1i"));
  assert.ok(line.includes("records_from_peers=7i"));
  assert.ok(line.includes("bytes_from_peers=7340032i"));
  assert.ok(line.includes("discovery_lookups=11i"));
  assert.ok(line.includes("swarm_present=1i"));
  assert.ok(line.includes('swarm_room_tag="ab12cd34"'));
  assert.ok(line.includes('swarm_rooms="[{\\"tag\\":\\"ab12cd34\\"}]"'));
});

test("the offset and the seed window are integer fields", () => {
  const line = buildPoint({ ...RESULT, startOffsetMs: 8000, seededMs: 452_000 });
  assert.ok(line.includes("start_offset_ms=8000i"));
  assert.ok(line.includes("seeded_ms=452000i"));
});

test("an unstaggered, non-seeding round records both as zero", () => {
  // Which is what makes `start_ns - start_offset_ms` the boundary on every
  // point, including every point written before the field existed.
  const line = buildPoint(RESULT);
  assert.ok(line.includes("start_offset_ms=0i"));
  assert.ok(line.includes("seeded_ms=0i"));
});

test("the point's counter names are the hot-file measurement's", () => {
  // One vocabulary across the native and browser cohorts of one run: a figure
  // named two ways is a figure no comparison can make.
  for (const name of [
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
    "discovery_lookups",
    "discovery_announces",
    "discovery_announces_failed",
    "discovery_announces_timed_out",
    "discovery_announces_refused",
    "discovery_bytes_sent",
    "discovery_bytes_received",
    "discovery_offers_made",
    "discovery_offers_delivered",
    "discovery_offers_received",
    "discovery_answers_sent",
    "discovery_answers_received",
    "discovery_channels_opened",
    "discovery_links_proven",
    "discovery_links_failed",
  ]) {
    assert.ok(SWARM_POINT_COUNTERS.includes(name), name);
  }
  // Twenty-five of the client's own plus the four chunk-source counts.
  assert.equal(SWARM_POINT_COUNTERS.length, 29);
});
