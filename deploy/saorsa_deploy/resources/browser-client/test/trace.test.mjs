/**
 * The forwarded trace file (V2-1650).
 *
 * The three properties asserted here are the ones the report downstream
 * depends on and none of them is visible from reading a single line: every line
 * self-describing, `seq` unique within a download, and a concurrent append that
 * never splits a line.
 */

import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { appendTrace, traceLines } from "../lib/trace.mjs";

const ROUND = { roundId: 2987404, boundary: 1792442400, hotFileIndex: 1 };
const FILE = { address: "486d9615d25f849ed814b3896ea07b7e3447374589c1a437e440643fee9ebd12" };
const SEEDS = "/ip4/1.2.3.4/udp/35740/webrtc-direct/p2p/aaaa,/ip4/5.6.7.8/udp/51088/webrtc-direct/p2p/bbbb";

function records(n) {
  return Array.from({ length: n }, (_, i) => ({
    id: i + 10,
    operation: "rpc",
    target: "chunk-a",
    peer: `peer-${i}`,
    phase: i % 2 === 0 ? "start" : "finish",
    elapsed_ms: 1.5 * i,
    detail: i % 2 === 0 ? "" : "found 4190237 bytes",
  }));
}

function lines(instance, n) {
  return traceLines({
    round: ROUND,
    instance,
    file: FILE,
    seeds: SEEDS,
    records: records(n),
    startedAt: 1792442435,
  });
}

test("the header is seq 0 and carries the seeds exactly once", () => {
  const out = lines(1, 3).map((l) => JSON.parse(l));
  assert.equal(out[0].seq, 0);
  assert.equal(out[0].operation, "header");
  assert.equal(out[0].seeds, SEEDS);
  assert.equal(
    out.filter((r) => r.seeds !== undefined).length,
    1,
    "the seed list is ~600 bytes; repeating it per record would be most of the traffic",
  );
});

test("every line repeats the download's identity, so it stands alone", () => {
  for (const line of lines(3, 4)) {
    const r = JSON.parse(line);
    assert.equal(r.round_id, 2987404);
    assert.equal(r.instance, 3);
    assert.equal(r.hot_file_index, 1);
    assert.equal(r.address, FILE.address);
    assert.equal(r.boundary, 1792442400);
    assert.equal(r.started_at, 1792442435);
  }
});

test("seq runs from 0 and is unique within the download", () => {
  const out = lines(1, 5).map((l) => JSON.parse(l));
  assert.deepEqual(
    out.map((r) => r.seq),
    [0, 1, 2, 3, 4, 5],
  );
  assert.equal(new Set(out.map((r) => r.seq)).size, out.length);
});

test("a record keeps its own fields", () => {
  const second = JSON.parse(lines(1, 2)[2]);
  assert.equal(second.id, 11);
  assert.equal(second.operation, "rpc");
  assert.equal(second.target, "chunk-a");
  assert.equal(second.peer, "peer-1");
  assert.equal(second.phase, "finish");
  assert.equal(second.elapsed_ms, 1.5);
  assert.equal(second.detail, "found 4190237 bytes");
});

test("a round with no hot file still produces a header with an empty address", () => {
  const first = JSON.parse(
    traceLines({ round: ROUND, instance: 1, file: undefined, seeds: SEEDS, records: [] })[0],
  );
  assert.equal(first.address, "");
});

/**
 * A DEV-01-sized block, which is the only size at which concurrent appends can
 * split a line.
 *
 * `fs/promises` writes a buffer in 512 KiB chunks and awaits each one, so a
 * block under that is one `write(2)` and interleaves with nothing — which is
 * why a 200-record test passed against an implementation that loses records on
 * every real round. Every one of id 685's 2,676 per-download traces was 599 KB
 * to 1.30 MB; 3,000 records of the fixture shape is ~1.3 MB, so each block is
 * three chunks.
 */
const DEV01_RECORDS = 3000;

test("four instances appending DEV-01-sized blocks at once split no line", async () => {
  const dir = await mkdtemp(join(tmpdir(), "browser-trace-"));
  const path = join(dir, "browser-trace.jsonl");
  const blocks = [1, 2, 3, 4].map((i) => lines(i, DEV01_RECORDS));
  assert.ok(
    blocks[0].join("\n").length > 600_000,
    "the block has to exceed the 512 KiB chunk size or this proves nothing",
  );
  await Promise.all(blocks.map((b) => appendTrace(path, b)));

  const raw = (await readFile(path, "utf8")).split("\n").filter((l) => l.length);
  const parsed = [];
  let unparseable = 0;
  for (const line of raw) {
    try {
      parsed.push(JSON.parse(line));
    } catch {
      unparseable += 1;
    }
  }
  assert.equal(unparseable, 0, "a split line leaves two halves telegraf drops silently");
  assert.equal(parsed.length, 4 * (DEV01_RECORDS + 1), "every line of every download is present");
  for (const instance of [1, 2, 3, 4]) {
    const mine = parsed.filter((r) => r.instance === instance);
    assert.deepEqual(
      mine.map((r) => r.seq),
      Array.from({ length: DEV01_RECORDS + 1 }, (_, i) => i),
      `instance ${instance} lost or reordered a record`,
    );
  }
});

test("a download's block lands contiguously, so appends are serialised", async () => {
  // The stronger reading of the same property: not merely that no line was
  // cut, but that no OTHER instance's record appears inside a block. One
  // `write(2)` per block plus the module's append queue is what gives that;
  // either alone would do for a single-process harness, and both is cheap.
  const dir = await mkdtemp(join(tmpdir(), "browser-trace-"));
  const path = join(dir, "browser-trace.jsonl");
  await Promise.all([1, 2, 3, 4].map((i) => appendTrace(path, lines(i, DEV01_RECORDS))));
  const order = (await readFile(path, "utf8"))
    .split("\n")
    .filter((l) => l.length)
    .map((l) => JSON.parse(l).instance);
  const runs = order.filter((v, i) => i === 0 || v !== order[i - 1]);
  assert.equal(runs.length, 4, `expected one unbroken run per instance, got ${runs.length}`);
  assert.deepEqual([...runs].sort(), [1, 2, 3, 4]);
});

test("a failed append does not stop the next one being queued", async () => {
  // The queue must carry no rejection forward: one bad append (a full disk)
  // would otherwise silence the trace for the life of the deployment.
  const dir = await mkdtemp(join(tmpdir(), "browser-trace-"));
  await assert.rejects(() => appendTrace(join(dir, "missing", "x", "\0bad"), lines(1, 1)));
  const path = join(dir, "browser-trace.jsonl");
  await appendTrace(path, lines(2, 1));
  const body = await readFile(path, "utf8");
  assert.equal(body.split("\n").filter((l) => l.length).length, 2);
});

test("a file over the cap is truncated before the append, not after", async () => {
  const dir = await mkdtemp(join(tmpdir(), "browser-trace-"));
  const path = join(dir, "browser-trace.jsonl");
  await writeFile(path, "STALE-".repeat(16));
  await appendTrace(path, lines(1, 1), 8);
  const body = await readFile(path, "utf8");
  assert.ok(!body.includes("STALE"), "the old contents are gone");
  assert.equal(
    body.split("\n").filter((l) => l.length).length,
    2,
    "the block that crossed the cap is kept whole",
  );
});

test("a file under the cap is appended to", async () => {
  const dir = await mkdtemp(join(tmpdir(), "browser-trace-"));
  const path = join(dir, "browser-trace.jsonl");
  await appendTrace(path, lines(1, 1), 1 << 30);
  await appendTrace(path, lines(2, 1), 1 << 30);
  const body = await readFile(path, "utf8");
  assert.equal(body.split("\n").filter((l) => l.length).length, 4);
});
