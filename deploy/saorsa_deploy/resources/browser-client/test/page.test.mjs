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
