/**
 * The derived per-download fields, against the committed fixture trace.
 *
 * The fixture (`deploy/tests/fixtures/browser-trace/hot-read.jsonl`) and its
 * hand-checked expectations (`expected.json` beside it) are shared with the
 * Python suite, so the two cannot disagree about what a trace means. Every
 * number in `expected.json` is a decision the fixture was written to force;
 * `expected.json`'s own `_comment` says which.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  STALL_MAX_MS,
  STALL_MIN_MS,
  countRefusedDials,
  countStalls,
  deriveFields,
  firstReads,
  parseTraceJsonl,
} from "../lib/derive.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, "..", "..", "..", "..", "tests", "fixtures", "browser-trace");

const records = parseTraceJsonl(readFileSync(join(FIXTURES, "hot-read.jsonl"), "utf8"));
const expected = JSON.parse(readFileSync(join(FIXTURES, "expected.json"), "utf8"));

test("the header line is not a trace record", () => {
  // It carries `round_id` and `address`, not an operation; counting it would
  // put a non-dial into every dial-derived figure.
  assert.ok(records.length > 0);
  assert.ok(records.every((r) => !r.header));
});

test("stalls counts the dials that hit the channel-open timeout", () => {
  assert.equal(countStalls(records), expected.stalls);
});

test("a dial that reached channel-open is never a stall", () => {
  // dial-5 in the fixture fails at 9600 ms, inside the window, having opened
  // its channel: an authentication failure, not an unreachable holder.
  const reopened = records.filter((r) => r.id === "dial-5");
  assert.ok(reopened.some((r) => r.phase === "channel-open"));
  assert.equal(countStalls(reopened), 0);
});

test("a dial that failed fast is not a stall", () => {
  const fast = records.filter((r) => r.id === "dial-4");
  assert.equal(countStalls(fast), 0);
});

test("the stall window brackets the client's 10s channel-open timeout", () => {
  assert.ok(STALL_MIN_MS < 10_000 && STALL_MAX_MS > 10_000);
});

test("refused dials are counted from the detail, separately from stalls", () => {
  assert.equal(countRefusedDials(records), expected.refused_dials);
});

test("first reads name the holder and how many were known at that moment", () => {
  assert.deepEqual(firstReads(records), expected.first_reads);
});

test("only the FIRST read of a chunk is recorded", () => {
  // A fallback to a second holder has already paid the first one's latency, so
  // it is not what the read-spreading question asks about.
  const chunk01 = firstReads(records).filter((r) => r.chunk === "chunk01");
  assert.equal(chunk01.length, 1);
  assert.equal(chunk01[0].holder, "peerA");
});

test("a non-GET rpc is not a read", () => {
  assert.ok(!firstReads(records).some((r) => r.chunk === "chunk05"));
});

test("a read with no peer is skipped rather than recorded holder-less", () => {
  assert.ok(!firstReads(records).some((r) => r.chunk === "chunk04"));
});

test("known_holders counts only the candidates that PRECEDED the read", () => {
  // Counting the whole trace's candidates would include peers the client
  // learned about afterwards and make every spread look wider than it was.
  const trailing = [
    { id: "r", operation: "rpc", target: "c", peer: "p1", phase: "start", detail: "GET chunk" },
    { id: "l", operation: "lookup-progress", target: "c", peer: "p2", phase: "start" },
    { id: "l", operation: "lookup-progress", target: "c", peer: "p3", phase: "start" },
  ];
  assert.deepEqual(firstReads(trailing), [{ chunk: "c", holder: "p1", known_holders: 0 }]);
});

test("deriveFields returns all three under their Influx field names", () => {
  assert.deepEqual(deriveFields(records), {
    stalls: expected.stalls,
    refused_dials: expected.refused_dials,
    first_reads: expected.first_reads,
  });
});

test("an empty trace derives zeros rather than throwing", () => {
  assert.deepEqual(deriveFields([]), { stalls: 0, refused_dials: 0, first_reads: [] });
});

test("a malformed line is skipped, not fatal", () => {
  const parsed = parseTraceJsonl('{"operation":"rpc"}\nnot json\n\n{"operation":"connect"}');
  assert.equal(parsed.length, 2);
});
