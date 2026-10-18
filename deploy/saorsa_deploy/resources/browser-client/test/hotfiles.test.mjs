/**
 * Reading the hot-file manifest.
 *
 * The manifest is the one thing that makes a cohort a cohort, so "could not
 * read it" has to be a recorded skip rather than a crash, and a partially
 * usable manifest has to parse identically on every VM — `hot_file_index` is an
 * index into this list.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { HOT_FILES_OBJECT_NAME, parseHotFiles } from "../lib/hotfiles.mjs";

const DOC = {
  deployment: "DEV-01",
  selected_at: "2026-10-18T12:00:00Z",
  size_bytes: 52_428_800,
  files: [
    { index: 0, address: "aa", bytes: 52_428_800, source_key: "upload-lon1-1.txt", line: 1 },
    { index: 1, address: "bb", bytes: 52_428_800, source_key: "upload-lon1-1.txt", line: 7 },
  ],
};

test("the object name matches what select-hot-files writes", () => {
  assert.equal(HOT_FILES_OBJECT_NAME, "browser-hot-files.json");
});

test("a well-formed manifest parses to its files", () => {
  const parsed = parseHotFiles(JSON.stringify(DOC));
  assert.equal(parsed.files.length, 2);
  assert.equal(parsed.files[1].address, "bb");
  assert.equal(parsed.size_bytes, 52_428_800);
});

test("an entry with no address is dropped", () => {
  const parsed = parseHotFiles(JSON.stringify({ files: [{ index: 0 }, { address: "bb" }] }));
  assert.deepEqual(
    parsed.files.map((f) => f.address),
    ["bb"],
  );
});

test("an empty or file-less manifest reads as absent", () => {
  // Absent and unusable are the same thing to the harness: a recorded skip,
  // retried next round.
  assert.equal(parseHotFiles(JSON.stringify({ files: [] })), null);
  assert.equal(parseHotFiles("{}"), null);
});

test("a malformed body reads as absent rather than throwing", () => {
  assert.equal(parseHotFiles("<?xml version=1?>"), null);
});
