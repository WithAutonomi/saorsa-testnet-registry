/**
 * Line-protocol construction for the `browser_download` measurement.
 *
 * Influx types a field on first write and DROPS every later point that
 * disagrees — the whole point, not just the offending field — so the integer
 * fields carrying their `i` suffix is load-bearing rather than cosmetic.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { MEASUREMENT, buildLine, buildPoint, escapeTag } from "../lib/influx.mjs";

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
