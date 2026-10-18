/**
 * The loopback HTTP server that serves the WASM bundle to the page.
 *
 * Chromium will not give a `file://` document the secure context WASM +
 * WebRTC needs, so the bundle has to come from an origin. Loopback rather than
 * anything routable: nothing outside the VM has any business reading it, and
 * the page then makes no external request of its own — every packet the round
 * produces is the client's.
 */

import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import { join, normalize } from "node:path";

import { PAGE_HTML } from "./page.mjs";

const CONTENT_TYPES = {
  ".js": "text/javascript",
  ".mjs": "text/javascript",
  ".wasm": "application/wasm",
  ".json": "application/json",
  ".html": "text/html",
};

function contentType(path) {
  const dot = path.lastIndexOf(".");
  return (dot === -1 ? undefined : CONTENT_TYPES[path.slice(dot)]) || "application/octet-stream";
}

/**
 * Resolve a request path inside `root`, or null when it escapes.
 *
 * The server is loopback-only and serves a directory this harness wrote, so
 * traversal is not a threat model here — but a `..` that escaped would serve
 * some other part of the VM's disk to the page and the failure would be
 * baffling rather than obviously a path bug.
 */
export function resolvePath(root, urlPath) {
  const decoded = decodeURIComponent(urlPath.split("?")[0]);
  const resolved = normalize(join(root, decoded));
  if (!resolved.startsWith(normalize(root))) return null;
  return resolved;
}

export function startBundleServer(root, port) {
  const server = createServer(async (req, res) => {
    if (req.url === "/" || req.url.startsWith("/?")) {
      res.writeHead(200, { "Content-Type": "text/html" });
      res.end(PAGE_HTML);
      return;
    }
    const path = resolvePath(root, req.url);
    if (!path) {
      res.writeHead(403);
      res.end();
      return;
    }
    try {
      const info = await stat(path);
      if (!info.isFile()) throw new Error("not a file");
      res.writeHead(200, { "Content-Type": contentType(path), "Content-Length": info.size });
      createReadStream(path).pipe(res);
    } catch {
      res.writeHead(404);
      res.end();
    }
  });
  return new Promise((resolve, reject) => {
    server.on("error", reject);
    server.listen(port, "127.0.0.1", () => resolve(server));
  });
}
