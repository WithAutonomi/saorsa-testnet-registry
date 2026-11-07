/**
 * The code that runs INSIDE Chromium, and the page it runs in.
 *
 * It has to be one self-contained function: Playwright serialises
 * `page.evaluate`'s argument and evaluates it in the page, so it can close over
 * nothing from this module. Everything it needs arrives in the single argument.
 *
 * The page is served from loopback by the harness rather than loaded from
 * `file://`: WASM plus WebRTC in Chromium needs a secure context with a real
 * origin, and a local origin also means the page itself reaches no external
 * network — the only traffic it makes is the client's own.
 */

/** The document Chromium loads. Deliberately empty — all the work is injected. */
export const PAGE_HTML =
  "<!doctype html><html><head><meta charset=utf-8>" +
  "<title>ant browser downloader</title></head><body></body></html>";

/**
 * Download one public file in the page and return the result plus its trace.
 *
 * Notes on the shape, all load-bearing:
 *
 * * `setBrowserTrace` is installed BEFORE the client is constructed, so the
 *   bootstrap dials — the ones the stall and refusal counts are read off — are
 *   in the trace. It exists only in a `test-utils` build; its absence is
 *   reported rather than ignored, because an empty trace reads as a clean
 *   network.
 * * The deadline is enforced in the page with `Promise.race`, so a client that
 *   hangs inside WASM still yields a result (and its trace so far) instead of
 *   the harness having to kill the browser and lose both.
 * * The byte length is compared against what the manifest declared. A short
 *   read that resolved successfully is a correctness finding, not a slow
 *   download, and nothing else would notice it.
 * * **`downloadPublicFile` resolves to an OBJECT, not to the bytes.** It is a
 *   `BrowserDownloadResult { content: Uint8Array, hash: String, file,
 *   dataMapNode? }` serialised with `serde_wasm_bindgen` (ant-client
 *   `wasm_transport.rs`), so the payload is `result.content` and its length is
 *   `result.content.length`. Reading `.length` off the result itself yields
 *   `undefined`, and a fallback to 0 then fails the size check on every
 *   SUCCESSFUL download -- the whole cohort would report 0% completion against
 *   a healthy network. (`result.hash` is a blake3 hex of the same bytes and a
 *   stronger integrity check than the length; it is left to V2-1618, which owns
 *   the result schema.)
 * * **The progress messages are returned RAW, and counted on the harness side**
 *   (`lib/swarm.mjs`). The branch bundle names the source of every chunk — a
 *   node, `cache`, or `a reader` — in `downloadPublicFile`'s third argument, and
 *   that is the one reading which says the swarm actually served this download.
 *   Parsing it here would put the parser somewhere `node --test` cannot reach
 *   it; the lines cross the Playwright boundary beside the per-dial trace, which
 *   is three orders of magnitude larger.
 * * **A page that SUCCEEDED can be held open to go on seeding** (`seedUntilMs`,
 *   from the manifest's `seed_until_secs`). The client stays connected, so the
 *   readers that started later in the stagger can still fetch records from it;
 *   without that the page closes the instant its own download ends and a swarm
 *   of staggered readers never overlaps. A failed or timed-out download closes
 *   at once, because nothing it holds is worth serving.
 * * **The stats are read while the client is still alive**, immediately before
 *   `close()`, and a bundle that has neither method is reported as
 *   `swarm_present = 0` rather than failing the round.
 */
export async function downloadInPage({
  bundleUrl,
  address,
  expectedBytes,
  seeds,
  timeoutMs,
  seedUntilMs,
}) {
  // Declared inside, not imported: Playwright serialises this function's own
  // source and evaluates it in the page, so it can close over nothing.
  const resultByteLength = (value) => {
    if (!value) return 0;
    const content = value.content;
    if (content && typeof content.byteLength === "number") return content.byteLength;
    if (content && typeof content.length === "number") return content.length;
    // A future bundle resolving to the bytes directly lands here rather than
    // silently reporting 0.
    if (typeof value.byteLength === "number") return value.byteLength;
    if (typeof value.length === "number") return value.length;
    return 0;
  };

  const trace = [];
  const push = (record) => {
    try {
      trace.push(typeof record === "string" ? JSON.parse(record) : record);
    } catch {
      trace.push({ raw: String(record) });
    }
  };

  // Every progress message, as a string, in the order the client emitted them.
  // Counted by `lib/swarm.mjs` on the harness side — see the note above.
  const progress = [];
  const onProgress = (message) => {
    try {
      progress.push(typeof message === "string" ? message : String(message));
    } catch {
      /* a progress line that will not stringify must not fail the download */
    }
  };

  const mod = await import(`${bundleUrl}/pkg/ant_core.js`);
  await mod.default();

  let traceHook = true;
  if (typeof mod.setBrowserTrace === "function") {
    mod.setBrowserTrace(push);
  } else {
    traceHook = false;
  }

  const startedAt = Date.now();
  let client;
  let connectMs = 0;
  let byteLength = 0;
  let swarmStats;
  let swarmRoomStats;
  let seededMs = 0;
  // Read while the client is still connected and always before `close()`: the
  // counters are the client's own and a closed one has nothing to report. Each
  // method is optional and each call is guarded on its own, so a bundle that has
  // one and not the other still reports what it has — and a bundle with neither
  // reports `swarm_present = 0`, which is a fact about the build rather than a
  // failed round.
  const readSwarm = async () => {
    try {
      if (client && typeof client.swarmStats === "function") {
        swarmStats = await client.swarmStats();
      }
    } catch {
      /* a client that will not report its swarm must not fail the round */
    }
    try {
      if (client && typeof client.swarmRoomStats === "function") {
        swarmRoomStats = await client.swarmRoomStats();
      }
    } catch {
      /* ditto */
    }
  };
  // Bound to a name so the three return paths cannot carry three different
  // field sets: Influx types a field on first write, and a point missing one
  // reads as a zero wherever the per-round sums add it up.
  const finish = (extra, totalMs) => ({
    ...extra,
    trace,
    traceHook,
    connectMs,
    totalMs,
    byteLength,
    progress,
    swarmStats,
    swarmRoomStats,
    seededMs,
  });
  try {
    const connectStart = performance.now();
    client = new mod.BrowserNetworkClient(seeds);
    await client.connect();
    connectMs = performance.now() - connectStart;

    const deadline = new Promise((_resolve, reject) => {
      setTimeout(() => reject(new Error("harness deadline")), timeoutMs);
    });
    // `undefined` for the second argument (concurrency), so the client's own
    // default governs it, exactly as INFRA-493 left the Node WASM CLI; the
    // third is the progress callback this role exists to read.
    const result = await Promise.race([
      client.downloadPublicFile(address, undefined, onProgress),
      deadline,
    ]);
    // Taken HERE, not at the return: a seeding page returns minutes after its
    // download finished, and a `totalMs` that included the wait would put the
    // seed window into every transfer percentile.
    const completedAt = Date.now();
    byteLength = resultByteLength(result);
    if (expectedBytes && byteLength !== expectedBytes) {
      // A short read is not a success, so nothing it holds is worth serving:
      // the page closes at once, as it did before seeding existed.
      await readSwarm();
      return finish(
        { ok: false, error: `size mismatch: got ${byteLength}` },
        completedAt - startedAt,
      );
    }
    if (seedUntilMs) {
      // Held open so the readers that started later in the stagger can still
      // fetch records from this page. The client stays connected; nothing else
      // happens here.
      const remaining = seedUntilMs - Date.now();
      if (remaining > 0) await new Promise((resolve) => setTimeout(resolve, remaining));
      await readSwarm();
      // Measured from the download completing to the stats read, which is
      // exactly the window this page spent available to its cohort.
      seededMs = Date.now() - completedAt;
    } else {
      await readSwarm();
    }
    return finish({ ok: true }, completedAt - startedAt);
  } catch (e) {
    await readSwarm();
    return finish(
      { ok: false, error: String((e && e.message) || e) },
      Date.now() - startedAt,
    );
  } finally {
    // Both, in this order, and never allowed to mask the result: `close()`
    // shuts the transport down and `free()` releases the WASM allocation. A
    // leaked client would hold its WebRTC sessions open, and the next round's
    // instance would then be the fifth session from this address.
    try {
      if (client && typeof client.close === "function") await client.close();
    } catch {
      /* ignore */
    }
    try {
      if (client && typeof client.free === "function") client.free();
    } catch {
      /* ignore */
    }
  }
}
