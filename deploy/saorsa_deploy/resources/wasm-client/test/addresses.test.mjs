import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  AddressSink,
  AddressSource,
  eligibleAddresses,
  formatAddressLine,
  isNonFileKey,
  parseAddressLine,
} from "../dist/addresses.js";

const CAP = 1_000_000_000;
const OVER = 1_048_576_000; // the native 1000 MiB uploader, as seen on DEV-03 run 588
const UNDER = 20_971_520;

test("a published line is `<address> <bytes>` and parses back", () => {
  const line = formatAddressLine("abc", UNDER);
  assert.equal(line, `abc ${UNDER}`);
  assert.deepEqual(parseAddressLine(line), { address: "abc", sizeBytes: UNDER });
});

test("a bare pre-change line parses with no size", () => {
  assert.deepEqual(parseAddressLine("abc"), { address: "abc" });
  assert.deepEqual(parseAddressLine("  abc  \r"), { address: "abc" });
  assert.equal(parseAddressLine(""), undefined);
  assert.equal(parseAddressLine("   "), undefined);
});

test("a malformed size keeps the address and drops the size", () => {
  assert.deepEqual(parseAddressLine("abc notanumber"), { address: "abc" });
  assert.deepEqual(parseAddressLine("abc -5"), { address: "abc" });
});

test("eligibleAddresses skips only entries known to be over the cap", () => {
  const entries = [
    { address: "big", sizeBytes: OVER },
    { address: "small", sizeBytes: UNDER },
    { address: "edge", sizeBytes: CAP },
    { address: "legacy" },
  ];
  assert.deepEqual(
    eligibleAddresses(entries, CAP).map((e) => e.address),
    ["small", "edge", "legacy"],
  );
  assert.deepEqual(eligibleAddresses(entries, 0).map((e) => e.address), [
    "big",
    "small",
    "edge",
    "legacy",
  ]);
});

test("AddressSink records the size beside the address", async () => {
  const dir = await mkdtemp(join(tmpdir(), "addr-"));
  const file = join(dir, "addresses.txt");
  const sink = new AddressSink({
    addressesFile: file,
    flushIntervalSecs: 3600,
    flushEveryN: 1000,
    tailSize: 100,
  });
  await sink.record("abc", UNDER);
  assert.equal(await readFile(file, "utf8"), `abc ${UNDER}\n`);
});

test("a capped AddressSource never picks an over-cap address", async () => {
  const dir = await mkdtemp(join(tmpdir(), "addr-"));
  const file = join(dir, "addresses.txt");
  await writeFile(file, `big ${OVER}\nsmall ${UNDER}\nlegacy\n`);
  const source = new AddressSource("", "", 60, file, CAP);
  const seen = new Set();
  for (let i = 0; i < 50; i++) seen.add(await source.pick());
  assert.ok(!seen.has("big"));
  assert.ok(seen.has("small") && seen.has("legacy"));
});

test("an uncapped AddressSource still picks everything", async () => {
  const dir = await mkdtemp(join(tmpdir(), "addr-"));
  const file = join(dir, "addresses.txt");
  await writeFile(file, `big ${OVER}\nsmall ${UNDER}\n`);
  const source = new AddressSource("", "", 60, file);
  const seen = new Set();
  for (let i = 0; i < 50; i++) seen.add(await source.pick());
  assert.deepEqual([...seen].sort(), ["big", "small"]);
});

test("a pool with only over-cap entries yields nothing rather than a doomed pick", async () => {
  const dir = await mkdtemp(join(tmpdir(), "addr-"));
  const file = join(dir, "addresses.txt");
  await writeFile(file, `big ${OVER}\n`);
  const source = new AddressSource("", "", 60, file, CAP);
  assert.equal(await source.pick(), undefined);
});

test("a pointer client's published list is never read as file addresses", () => {
  // The pointer tier publishes under the SAME deployment prefix, and this
  // downloader lists the whole prefix (that is how it draws native uploads).
  // A pointer address is not a data map, so fetching one fails every time and
  // the failure lands in the download success rate as lost content.
  assert.equal(isNonFileKey("saorsa-deploy/addresses/DEV-01/pointer-lon1-1.txt"), true);
  assert.equal(isNonFileKey("pointer-syd1-2.txt"), true);
  assert.equal(isNonFileKey("saorsa-deploy/addresses/DEV-01/upload-lon1-1.txt"), false);
  assert.equal(isNonFileKey("saorsa-deploy/addresses/DEV-01/wasm-upload-lon1-1.txt"), false);
});
