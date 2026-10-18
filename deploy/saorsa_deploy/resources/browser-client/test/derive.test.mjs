/**
 * The derived per-download fields, against a REAL `setBrowserTrace` capture.
 *
 * The fixture is ant-client's own committed trace from
 * `browser-tests/read-startup.mjs` (see
 * `deploy/tests/fixtures/browser-trace/README.md` for its provenance and the
 * `jq` query behind every expected value). An earlier revision of this file
 * used a hand-written fixture encoding a format the bundle does not emit —
 * `"GET chunk"` details, a `peer` on `lookup-progress`, a `detail.peers` array,
 * a `"Refused: …"` detail — and every one of these tests passed against it
 * while two of the three fields would have been 0 on every real round. Hence
 * the shape guards at the bottom: they assert what the emitter actually does,
 * so a future fixture cannot reintroduce the fantasy.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { gunzipSync } from "node:zlib";

import {
  CHANNEL_OPEN_TIMEOUT_MS,
  CONNECTION_SETUP_TIMEOUT_MS,
  REFUSAL_WINDOW_MS,
  STALL_TOLERANCE_MS,
  countRefusedDials,
  countStalls,
  deriveFields,
  firstReads,
  parseTraceJsonl,
} from "../lib/derive.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, "..", "..", "..", "..", "tests", "fixtures", "browser-trace");

const records = parseTraceJsonl(
  gunzipSync(readFileSync(join(FIXTURES, "read-startup.jsonl.gz"))).toString(),
);
const expected = JSON.parse(readFileSync(join(FIXTURES, "expected.json"), "utf8"));

test("the fixture is the real capture, whole", () => {
  assert.equal(records.length, 1058);
  assert.deepEqual(new Set(records.map((r) => r.operation)), new Set([
    "association",
    "connect",
    "discovery",
    "lookup-progress",
    "rpc",
  ]));
});

// --------------------------------------------------------------------------- //
// stalls
// --------------------------------------------------------------------------- //
test("stalls counts the dials that died on a setup timeout", () => {
  assert.equal(countStalls(records), expected.stalls);
});

/** A failed dial (attempted, no channel, cancelled) that ran for `ms`. */
function failedDialAt(id, ms) {
  return [
    { id, operation: "association", target: "m", peer: "p", phase: "start", elapsed_ms: 0, detail: "" },
    { id, operation: "association", target: "m", peer: "p", phase: "dial", elapsed_ms: 1, detail: "fresh" },
    { id, operation: "association", target: "m", peer: "p", phase: "cancelled-or-error", elapsed_ms: ms, detail: "" },
  ];
}

test("both of the transport's setup timeouts count", () => {
  // The fixture holds 41 failed dials at ~10,004 ms and one at 30,002.5 ms.
  // Measuring only the 10 s one would report 41; only the 30 s one, 1.
  assert.equal(CHANNEL_OPEN_TIMEOUT_MS, 10_000);
  assert.equal(CONNECTION_SETUP_TIMEOUT_MS, 30_000);
  assert.equal(countStalls(failedDialAt(1, 10_004)), 1);
  assert.equal(countStalls(failedDialAt(2, 30_002.5)), 1);
  assert.equal(
    countStalls([...failedDialAt(1, 10_004), ...failedDialAt(2, 30_002.5)]),
    2,
  );
  // And the fixture really does contain one of each.
  const failures = records
    .filter((r) => r.operation === "association" && r.phase === "cancelled-or-error")
    .map((r) => r.elapsed_ms);
  const near = (t) => failures.filter((ms) => Math.abs(ms - t) <= STALL_TOLERANCE_MS).length;
  assert.equal(near(CHANNEL_OPEN_TIMEOUT_MS), 41);
  assert.equal(near(CONNECTION_SETUP_TIMEOUT_MS), 1);
});

test("a dial that opened a channel is never a stall", () => {
  // It failed at the authentication layer, not at the transport, so counting it
  // would attribute an admission failure to NAT.
  const opened = records.filter(
    (r) => r.operation === "association" && r.phase === "channel-open",
  );
  assert.ok(opened.length > 0);
  for (const record of opened) {
    const dial = records.filter((r) => r.operation === "association" && r.id === record.id);
    assert.equal(countStalls(dial), 0);
  }
});

test("a dial with no `dial` phase is neither a stall nor a refusal", () => {
  // The dial-failure-cache early return bails BEFORE `trace.event("dial", ..)`,
  // so its `cancelled-or-error` arrives having dialled nothing. Counting it
  // would report a local suppression as a network refusal.
  const suppressed = [
    { id: 9001, operation: "association", phase: "start", elapsed_ms: 0, detail: "" },
    { id: 9001, operation: "association", phase: "cancelled-or-error", elapsed_ms: 0.4, detail: "" },
  ];
  assert.equal(countStalls(suppressed), 0);
  assert.equal(countRefusedDials(suppressed), 0);
});

test("a failure at neither timeout is counted as neither", () => {
  // The fixture holds one at 4,082.6 ms: a third thing, and guessing which
  // would be worse than leaving it out of both.
  const odd = [
    { id: 9002, operation: "association", phase: "start", elapsed_ms: 0, detail: "" },
    { id: 9002, operation: "association", phase: "dial", elapsed_ms: 1, detail: "fresh" },
    { id: 9002, operation: "association", phase: "cancelled-or-error", elapsed_ms: 4082.6, detail: "" },
  ];
  assert.equal(countStalls(odd), 0);
  assert.equal(countRefusedDials(odd), 0);
});

// --------------------------------------------------------------------------- //
// refused dials
// --------------------------------------------------------------------------- //
test("refused dials are a timing heuristic, not a trace marker", () => {
  // No record ever carries a refusal: `DialOutcome::Refused` goes to the
  // dial-failure cache, and a dropped Trace emits `cancelled-or-error` with an
  // EMPTY detail. So the count mirrors `note_refusal`'s own condition.
  assert.ok(!records.some((r) => /refus/i.test(String(r.detail))));
  assert.equal(countRefusedDials(records), expected.refused_dials);
});

test("a fast failed dial is counted as refused", () => {
  const fast = [
    { id: 9003, operation: "association", phase: "start", elapsed_ms: 0, detail: "" },
    { id: 9003, operation: "association", phase: "dial", elapsed_ms: 2, detail: "fresh" },
    { id: 9003, operation: "association", phase: "cancelled-or-error", elapsed_ms: 74, detail: "" },
  ];
  assert.ok(74 < REFUSAL_WINDOW_MS);
  assert.equal(countRefusedDials(fast), 1);
  assert.equal(countStalls(fast), 0);
});

// --------------------------------------------------------------------------- //
// first reads
// --------------------------------------------------------------------------- //
test("first reads are found by a non-empty rpc target, not by the detail", () => {
  // `Trace::new("rpc", <chunk hex if GetRequest else "">, <peer hex>)`. The
  // detail is empty on `start` and `found N bytes` / `NotFound { .. }` on
  // `finish` — the word GET appears nowhere, so a detail predicate finds none.
  assert.ok(!records.some((r) => /\bget\b/i.test(String(r.detail))));
  assert.deepEqual(firstReads(records), expected.first_reads);
});

test("an rpc with an empty target is not a read", () => {
  assert.equal(
    firstReads([
      { id: 1, operation: "rpc", target: "", peer: "abc", phase: "start", detail: "" },
    ]).length,
    0,
  );
});

test("only the FIRST read of a chunk is recorded", () => {
  // A fallback to a second holder has already paid the first one's NotFound.
  const chunk = expected.first_reads[0].chunk;
  const reads = records.filter((r) => r.operation === "rpc" && r.target === chunk && r.peer);
  const holders = new Set(reads.map((r) => r.peer));
  assert.ok(holders.size > 1, "the fixture must hold a fallback to exercise this");
  assert.deepEqual(
    firstReads(records).filter((r) => r.chunk === chunk),
    [expected.first_reads[0]],
  );
});

test("known_holders comes from the discovery peer count", () => {
  const counted = firstReads(records).filter((r) => r.known_holders !== null);
  assert.equal(counted.length, 2);
  for (const read of counted) assert.equal(read.known_holders, 7);
});

test("known_holders is null when discovery reported no count", () => {
  // Discovery legitimately ends in `cancelled-or-error` with an empty detail —
  // it does for eight of the fixture's ten chunks. Null, not 0: "nothing
  // reported how many" is not "the client knew of no holders".
  assert.equal(firstReads(records).filter((r) => r.known_holders === null).length, 8);
});

test("a discovery count is attributed to its own target only", () => {
  const trace = [
    { id: 1, operation: "discovery", target: "aa", peer: "", phase: "finish", detail: "7 peers" },
    { id: 2, operation: "rpc", target: "aa", peer: "p1", phase: "start", detail: "" },
    { id: 3, operation: "rpc", target: "bb", peer: "p2", phase: "start", detail: "" },
  ];
  assert.deepEqual(firstReads(trace), [
    { chunk: "aa", holder: "p1", known_holders: 7 },
    { chunk: "bb", holder: "p2", known_holders: null },
  ]);
});

test("a discovery count after the read still counts", () => {
  // Forced by the data: in all fourteen committed captures the discovery finish
  // arrives AFTER the first read of its target, so a strictly-preceding
  // reading would be null on every real trace.
  assert.deepEqual(
    firstReads([
      { id: 1, operation: "rpc", target: "aa", peer: "p1", phase: "start", detail: "" },
      { id: 2, operation: "discovery", target: "aa", peer: "", phase: "finish", detail: "7 peers" },
    ]),
    [{ chunk: "aa", holder: "p1", known_holders: 7 }],
  );
});

test("a discovery detail that is not a peer count is ignored", () => {
  assert.equal(
    firstReads([
      { id: 1, operation: "discovery", target: "aa", peer: "", phase: "finish", detail: "closer" },
      { id: 2, operation: "rpc", target: "aa", peer: "p1", phase: "start", detail: "" },
    ])[0].known_holders,
    null,
  );
});

// --------------------------------------------------------------------------- //
// all three, and the parser
// --------------------------------------------------------------------------- //
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

test("the header line is not a trace record", () => {
  const withHeader = `{"header":true,"round_id":1}\n{"id":1,"operation":"connect","phase":"start"}`;
  assert.deepEqual(parseTraceJsonl(withHeader), [
    { id: 1, operation: "connect", phase: "start" },
  ]);
});

test("a malformed line is skipped, not fatal", () => {
  assert.equal(parseTraceJsonl('{"operation":"rpc"}\nnot json\n\n{"operation":"connect"}').length, 2);
});

// --------------------------------------------------------------------------- //
// shape guards on the fixture itself
// --------------------------------------------------------------------------- //
test("every record has exactly the seven keys Trace::event emits", () => {
  const keys = ["detail", "elapsed_ms", "id", "operation", "peer", "phase", "target"];
  for (const record of records) {
    assert.deepEqual(Object.keys(record).sort(), keys);
  }
});

test("detail is ALWAYS a string", () => {
  // It is a `&str` at every call site, so a structured detail — an object, an
  // array of peers — never arrives. `detail.peers` cannot be read.
  for (const record of records) assert.equal(typeof record.detail, "string");
});

test("lookup-progress carries neither a target nor a peer", () => {
  // `Trace::new("lookup-progress", String::new(), String::new())`, so it says
  // nothing about any particular target and cannot supply a candidate.
  const lookups = records.filter((r) => r.operation === "lookup-progress");
  assert.ok(lookups.length > 0);
  for (const record of lookups) {
    assert.equal(record.target, "");
    assert.equal(record.peer, "");
  }
});

test("discovery carries a target but never a peer", () => {
  const discoveries = records.filter((r) => r.operation === "discovery");
  assert.ok(discoveries.length > 0);
  for (const record of discoveries) {
    assert.notEqual(record.target, "");
    assert.equal(record.peer, "");
  }
});

test("cancelled-or-error always has an empty detail", () => {
  // It comes from `Drop for Trace`, which passes "". Nothing can be read off it.
  const dropped = records.filter((r) => r.phase === "cancelled-or-error");
  assert.ok(dropped.length > 0);
  for (const record of dropped) assert.equal(record.detail, "");
});
