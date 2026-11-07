/**
 * The in-page download's DEADLINE, exercised by running the real
 * `downloadInPage` (INFRA-532 review round 1).
 *
 * `page.test.mjs` reads the function's source because it cannot run it in a
 * browser. It does not need one: the function touches nothing but `Date.now`,
 * `setTimeout`, `performance.now` and a dynamic `import()` of
 * `${bundleUrl}/pkg/ant_core.js` — so with `bundleUrl` as a `file://` directory
 * and a stub module written into it, Node runs the actual code path, including
 * the import, the connect and the download.
 *
 * What that buys is the one assertion the source could not make: the deadline is
 * an absolute instant covering ALL THREE stages. As a duration counted from the
 * moment the download race began, a slow connect bought the download its own
 * budget over again — with a 300 s deadline, a 100 s connect let a 250 s
 * download report success at 350 s. The outer backstop had been failing those
 * late downloads by accident, and moving it out to the seed deadline (so a
 * seeding page is not abandoned) removed the accident.
 *
 * Durations here are tens of milliseconds, not fake timers: the function reads
 * the real clock in four places, and a faked one would be a second
 * implementation of the thing under test.
 */
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { test } from "node:test";

import { downloadInPage } from "../lib/page.mjs";

/**
 * A `file://` bundle whose client behaves to order.
 *
 * `connectMs` / `downloadMs` are how long each stage takes; `connectHangs`
 * never resolves at all, which is the case a seed window must not be allowed to
 * absorb.
 */
async function stubBundle({
  connectMs = 0,
  downloadMs = 0,
  connectHangs = false,
  bytes = 10,
  withSwarm = false,
  progress = [],
} = {}) {
  const dir = await mkdtemp(join(tmpdir(), "browser-page-"));
  await mkdir(join(dir, "pkg"), { recursive: true });
  const source = `
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export default async function init() {}
export function setBrowserTrace(push) {
  push({ id: "d1", operation: "dial", phase: "dial", target: "", peer: "p1", elapsed_ms: 1, detail: "" });
}
export class BrowserNetworkClient {
  constructor(seeds) { this.seeds = seeds; }
  async connect() {
    ${connectHangs ? "await new Promise(() => {});" : `await sleep(${connectMs});`}
  }
  async downloadPublicFile(address, concurrency, onProgress) {
    for (const line of ${JSON.stringify(progress)}) onProgress && onProgress(line);
    await sleep(${downloadMs});
    return { content: new Uint8Array(${bytes}), hash: "ab", file: {} };
  }
  ${withSwarm ? "async swarmStats() { return { records_from_peers: 7, discovery: { lookups: 3 } }; }" : ""}
  ${withSwarm ? 'async swarmRoomStats() { return [{ tag: "room-1" }]; }' : ""}
  async close() {}
  free() {}
}
`;
  await writeFile(join(dir, "pkg", "ant_core.js"), source);
  return pathToFileURL(dir).href;
}

test("a download that finishes inside the budget succeeds", async () => {
  const bundleUrl = await stubBundle({ connectMs: 20, downloadMs: 20, bytes: 10 });
  const out = await downloadInPage({
    bundleUrl,
    address: "ab",
    expectedBytes: 10,
    seeds: ["s1"],
    deadlineAtMs: Date.now() + 2000,
    seedUntilMs: 0,
  });
  assert.equal(out.ok, true);
  assert.equal(out.byteLength, 10);
  assert.equal(out.seededMs, 0);
});

test("a slow connect does NOT buy the download its budget over again", async () => {
  // The reviewer's case, in miniature: the connect eats most of the budget and
  // the download runs past it. As a duration started at the download race this
  // reported `ok` — the deadline is now the instant, so it fails.
  const bundleUrl = await stubBundle({ connectMs: 120, downloadMs: 120 });
  const started = Date.now();
  const out = await downloadInPage({
    bundleUrl,
    address: "ab",
    expectedBytes: 10,
    seeds: ["s1"],
    deadlineAtMs: started + 150,
    seedUntilMs: 0,
  });
  assert.equal(out.ok, false);
  assert.match(out.error, /harness deadline/);
  // And it gave up AT the deadline rather than running the download out.
  assert.ok(Date.now() - started < 230, `took ${Date.now() - started}ms`);
});

test("a seed window does not extend the download budget", async () => {
  // Moving the outer backstop out to the seed deadline is what made this
  // reachable: the page must still fail the transfer on the download deadline
  // and only then be allowed to sit there.
  const started = Date.now();
  const bundleUrl = await stubBundle({ connectMs: 120, downloadMs: 120 });
  const out = await downloadInPage({
    bundleUrl,
    address: "ab",
    expectedBytes: 10,
    seeds: ["s1"],
    deadlineAtMs: started + 150,
    seedUntilMs: started + 5000,
  });
  assert.equal(out.ok, false);
  assert.match(out.error, /harness deadline/);
  // A failed download closes at once: it holds nothing worth serving.
  assert.equal(out.seededMs, 0);
  assert.ok(Date.now() - started < 400, `took ${Date.now() - started}ms`);
});

test("a connect that never resolves fails on the download budget", async () => {
  const started = Date.now();
  const bundleUrl = await stubBundle({ connectHangs: true });
  const out = await downloadInPage({
    bundleUrl,
    address: "ab",
    expectedBytes: 10,
    seeds: ["s1"],
    deadlineAtMs: started + 120,
    seedUntilMs: started + 5000,
  });
  assert.equal(out.ok, false);
  assert.match(out.error, /harness deadline/);
  assert.ok(Date.now() - started < 400, `took ${Date.now() - started}ms`);
});

test("a successful page is held open to the seed instant, and no longer", async () => {
  const started = Date.now();
  const bundleUrl = await stubBundle({ connectMs: 10, downloadMs: 10, withSwarm: true });
  const out = await downloadInPage({
    bundleUrl,
    address: "ab",
    expectedBytes: 10,
    seeds: ["s1"],
    deadlineAtMs: started + 2000,
    seedUntilMs: started + 200,
  });
  assert.equal(out.ok, true);
  // `totalMs` is the download, NOT the seed wait: a `totalMs` that included it
  // would put the seed window into every transfer percentile.
  assert.ok(out.totalMs < 150, `totalMs ${out.totalMs}`);
  assert.ok(out.seededMs >= 100, `seededMs ${out.seededMs}`);
  const elapsed = Date.now() - started;
  assert.ok(elapsed >= 200 && elapsed < 600, `elapsed ${elapsed}ms`);
});

test("the stats are read off the live client, after the seed wait", async () => {
  const started = Date.now();
  const bundleUrl = await stubBundle({ withSwarm: true, progress: ["Swarm room room-1"] });
  const out = await downloadInPage({
    bundleUrl,
    address: "ab",
    expectedBytes: 10,
    seeds: ["s1"],
    deadlineAtMs: started + 2000,
    seedUntilMs: started + 120,
  });
  assert.equal(out.ok, true);
  assert.deepEqual(out.swarmStats, { records_from_peers: 7, discovery: { lookups: 3 } });
  assert.deepEqual(out.swarmRoomStats, [{ tag: "room-1" }]);
  assert.deepEqual(out.progress, ["Swarm room room-1"]);
});

test("a bundle with no swarm methods still returns a result", async () => {
  const bundleUrl = await stubBundle({ withSwarm: false });
  const out = await downloadInPage({
    bundleUrl,
    address: "ab",
    expectedBytes: 10,
    seeds: ["s1"],
    deadlineAtMs: Date.now() + 2000,
    seedUntilMs: 0,
  });
  assert.equal(out.ok, true);
  assert.equal(out.swarmStats, undefined);
  assert.equal(out.swarmRoomStats, undefined);
});

test("a short read fails and is not held open", async () => {
  const started = Date.now();
  const bundleUrl = await stubBundle({ bytes: 4 });
  const out = await downloadInPage({
    bundleUrl,
    address: "ab",
    expectedBytes: 10,
    seeds: ["s1"],
    deadlineAtMs: started + 2000,
    seedUntilMs: started + 5000,
  });
  assert.equal(out.ok, false);
  assert.match(out.error, /size mismatch: got 4/);
  assert.equal(out.seededMs, 0);
  assert.ok(Date.now() - started < 1000, "a short read must not sit in the seed window");
});

test("a bundle that will not import is a failure, not a throw", async () => {
  // Inside the try and inside the deadline: a module that cannot load reports
  // its message rather than throwing out of `page.evaluate` and taking the
  // trace with it.
  const dir = await mkdtemp(join(tmpdir(), "browser-page-"));
  const out = await downloadInPage({
    bundleUrl: pathToFileURL(dir).href,
    address: "ab",
    expectedBytes: 10,
    seeds: ["s1"],
    deadlineAtMs: Date.now() + 2000,
    seedUntilMs: 0,
  });
  assert.equal(out.ok, false);
  assert.ok(out.error);
  assert.deepEqual(out.trace, []);
});

test("the trace hook is reported missing rather than ignored", async () => {
  const dir = await mkdtemp(join(tmpdir(), "browser-page-"));
  await mkdir(join(dir, "pkg"), { recursive: true });
  await writeFile(
    join(dir, "pkg", "ant_core.js"),
    `export default async function init() {}
     export class BrowserNetworkClient {
       async connect() {}
       async downloadPublicFile() { return { content: new Uint8Array(10) }; }
       async close() {}
     }`,
  );
  const out = await downloadInPage({
    bundleUrl: pathToFileURL(dir).href,
    address: "ab",
    expectedBytes: 10,
    seeds: ["s1"],
    deadlineAtMs: Date.now() + 2000,
    seedUntilMs: 0,
  });
  assert.equal(out.ok, true);
  assert.equal(out.traceHook, false);
});
