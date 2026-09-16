import assert from "node:assert/strict";
import { mkdtemp, readFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { clearInflight, inflightPath, markInflight, recoverInflight } from "../dist/inflight.js";

const influx = { url: "", token: "t", org: "", bucket: "" };

test("a leftover marker is reported as a crash failure and cleared", async () => {
  const dir = await mkdtemp(join(tmpdir(), "inflight-"));
  const lines = [];
  const origFetch = globalThis.fetch;
  globalThis.fetch = async (_url, init) => { lines.push(String(init.body)); return { ok: true, status: 204, text: async () => "" }; };
  try {
    await markInflight(dir, { measurement: "download", startedAt: 1_000_000, fileSize: 0, address: "abc" });
    const op = await recoverInflight(dir, { ...influx, url: "http://x" }, { host: "h", region: "r" }, 1_061_000);
    assert.equal(op.address, "abc");
    assert.equal(lines.length, 1);
    assert.match(lines[0], /^download,host=h,region=r /);
    assert.match(lines[0], /status=1i/);
    assert.match(lines[0], /total_ms=61000i/);
    assert.match(lines[0], /error_kind="crash"/);
    await assert.rejects(stat(inflightPath(dir)));
  } finally {
    globalThis.fetch = origFetch;
  }
});

test("no marker means nothing is reported", async () => {
  const dir = await mkdtemp(join(tmpdir(), "inflight-"));
  assert.equal(await recoverInflight(dir, influx, { host: "h", region: "r" }), null);
});

test("clearing removes the marker written for the op", async () => {
  const dir = await mkdtemp(join(tmpdir(), "inflight-"));
  await markInflight(dir, { measurement: "upload", startedAt: 5, fileSize: 10 });
  assert.equal(JSON.parse(await readFile(inflightPath(dir), "utf8")).fileSize, 10);
  await clearInflight(dir);
  await assert.rejects(stat(inflightPath(dir)));
});

test("merkle submission pads the gas estimate by 20%, as evmlib does", async () => {
  const { withGasBuffer } = await import("../dist/wallet.js");
  assert.equal(withGasBuffer(777540n), 933048n);
  assert.equal(withGasBuffer(100n), 120n);
});
