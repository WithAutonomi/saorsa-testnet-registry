/**
 * The in-page download's handling of the result object.
 *
 * `downloadInPage` itself cannot run here — it imports the WASM bundle and
 * needs a browser — but the one piece of it that decides success or failure
 * can, and it got this wrong: `downloadPublicFile` resolves to a
 * `BrowserDownloadResult {content: Uint8Array, hash, file, dataMapNode?}`
 * (serde_wasm_bindgen, ant-client `wasm_transport.rs`), not to the bytes.
 * Reading `.length` off the result is `undefined`, and a fallback to 0 then
 * fails the size check on every SUCCESSFUL download — the whole cohort would
 * report 0 % completion against a healthy network.
 *
 * The helper is declared inside `downloadInPage` because Playwright serialises
 * that function's source and evaluates it in the page, so it can close over
 * nothing. It is reached here the same way Playwright reaches it: out of the
 * source text. That is ugly, and it is still worth it — this is the assertion
 * that would have caught the defect.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { downloadInPage } from "../lib/page.mjs";

function resultByteLength() {
  const source = downloadInPage.toString();
  const match = source.match(/const resultByteLength = ([\s\S]*?\n {2}};)/);
  assert.ok(match, "resultByteLength must stay a self-contained const in downloadInPage");
  return eval(`(${match[1].replace(/;$/, "")})`);
}

test("the length comes from result.content, not from the result", () => {
  const byteLength = resultByteLength();
  const content = new Uint8Array(52_428_800);
  assert.equal(byteLength({ content, hash: "ab".repeat(32), file: {} }), 52_428_800);
});

test("a result read as if it were the bytes would have measured 0", () => {
  // The defect, pinned: the object has no own length, so the old code's
  // fallback produced 0 and `size mismatch: got 0` on every healthy round.
  const result = { content: new Uint8Array(10), hash: "ab", file: {} };
  assert.equal(result.length, undefined);
  assert.equal(resultByteLength()(result), 10);
});

test("a future bundle resolving to the bytes directly still measures", () => {
  assert.equal(resultByteLength()(new Uint8Array(7)), 7);
});

test("nothing, or a result with no content, measures 0 rather than throwing", () => {
  const byteLength = resultByteLength();
  for (const value of [null, undefined, {}, { hash: "ab" }]) {
    assert.equal(byteLength(value), 0);
  }
});

test("the trace hook is installed before the client is constructed", () => {
  // The bootstrap dials are what the stall and refusal counts are read off; a
  // hook installed afterwards misses them entirely.
  const source = downloadInPage.toString();
  assert.ok(source.indexOf("setBrowserTrace") < source.indexOf("new mod.BrowserNetworkClient"));
});

test("a bundle with no trace hook is reported, not ignored", () => {
  // Without it every derived field is 0 — a clean network, not a missing
  // instrument.
  assert.ok(downloadInPage.toString().includes("traceHook"));
});

test("the progress callback is the THIRD argument of downloadPublicFile", () => {
  // The second is the concurrency, and the client's own default has to govern
  // it (INFRA-493): passing a number there would make this role measure a
  // concurrency nothing else in the run uses.
  const source = downloadInPage.toString();
  assert.ok(source.includes("client.downloadPublicFile(address, undefined, onProgress)"));
});

test("the progress lines are returned raw, not counted in the page", () => {
  // Counted by `lib/swarm.mjs`, where `node --test` can reach the parser.
  const source = downloadInPage.toString();
  assert.ok(source.includes("progress.push"));
  assert.ok(source.includes("progress,"));
});

test("the swarm stats are read before the client is closed", () => {
  // The counters are the client's own, and a closed one has nothing to report.
  const source = downloadInPage.toString();
  assert.ok(source.indexOf("swarmStats") < source.indexOf("client.close"));
  assert.ok(source.includes("client.swarmRoomStats"));
});

test("a bundle with neither swarm method is tolerated", () => {
  // The pinned-older-client case: `swarm_present = 0` and zeros, never a round
  // that failed over a method that does not exist.
  const source = downloadInPage.toString();
  assert.ok(source.includes('typeof client.swarmStats === "function"'));
  assert.ok(source.includes('typeof client.swarmRoomStats === "function"'));
});

test("totalMs is taken when the download completed, not when the page returns", () => {
  // A seeding page returns minutes later, and a `totalMs` that included the
  // wait would put the seed window into every transfer percentile.
  const source = downloadInPage.toString();
  assert.ok(source.includes("const completedAt = Date.now()"));
  assert.ok(source.includes("completedAt - startedAt"));
});

test("only a successful download is held open to seed", () => {
  // A short read or a failure holds nothing worth serving, so both close at
  // once, exactly as they did before seeding existed.
  const source = downloadInPage.toString();
  const mismatch = source.indexOf("size mismatch");
  const seedWait = source.indexOf("if (seedUntilMs)");
  assert.ok(mismatch < seedWait, "the size-mismatch return must precede the seed wait");
});
