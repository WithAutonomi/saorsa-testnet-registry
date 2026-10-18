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
 */
export async function downloadInPage({ bundleUrl, address, expectedBytes, seeds, timeoutMs }) {
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
  try {
    const connectStart = performance.now();
    client = new mod.BrowserNetworkClient(seeds);
    await client.connect();
    connectMs = performance.now() - connectStart;

    const deadline = new Promise((_resolve, reject) => {
      setTimeout(() => reject(new Error("harness deadline")), timeoutMs);
    });
    const result = await Promise.race([client.downloadPublicFile(address), deadline]);
    byteLength = resultByteLength(result);
    if (expectedBytes && byteLength !== expectedBytes) {
      return {
        ok: false,
        error: `size mismatch: got ${byteLength}`,
        trace,
        traceHook,
        connectMs,
        totalMs: Date.now() - startedAt,
        byteLength,
      };
    }
    return {
      ok: true,
      trace,
      traceHook,
      connectMs,
      totalMs: Date.now() - startedAt,
      byteLength,
    };
  } catch (e) {
    return {
      ok: false,
      error: String((e && e.message) || e),
      trace,
      traceHook,
      connectMs,
      totalMs: Date.now() - startedAt,
      byteLength,
    };
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
