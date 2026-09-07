/**
 * Line-protocol construction for the WASM client.
 *
 * The point of these is byte-compatibility with the native bash clients: the
 * whole results pipeline (record-results -> deployment_{upload,download}_records
 * -> deployments compare) reads the `upload` and `download` measurements, and a
 * WASM operation has to land in exactly the same shape to be comparable.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { buildLine, escapeFieldString, escapeTag } from "../dist/influx.js";

test("integer fields carry the i suffix", () => {
  const line = buildLine("upload", { host: "h", region: "lon1" }, { status: { int: 0 } });
  assert.equal(line, "upload,host=h,region=lon1 status=0i");
});

test("float fields carry no suffix", () => {
  const line = buildLine("upload", {}, { upload_secs: 1.5 });
  assert.equal(line, "upload upload_secs=1.5");
});

test("string fields are quoted", () => {
  const line = buildLine("upload", {}, { error_kind: "timeout" });
  assert.equal(line, 'upload error_kind="timeout"');
});

test("tag separators are escaped", () => {
  // An unescaped comma or space in a tag silently splits the line into a
  // different series, which Influx accepts without complaint.
  assert.equal(escapeTag("a,b c=d"), "a\\,b\\ c\\=d");
});

test("quotes and backslashes in string fields are escaped", () => {
  assert.equal(escapeFieldString('say "hi"\\'), 'say \\"hi\\"\\\\');
});

test("empty tags are dropped rather than emitted blank", () => {
  const line = buildLine("download", { host: "h", region: "" }, { status: { int: 1 } });
  assert.equal(line, "download,host=h status=1i");
});

test("the upload line matches the native uploader's shape", () => {
  const line = buildLine(
    "upload",
    { host: "DEV-01-ant-client-wasm-upload-lon1-1", region: "lon1" },
    {
      status: { int: 0 },
      total_ms: { int: 1234 },
      upload_secs: 1.234,
      file_size: { int: 1048576 },
      start_ns: { int: 1700000000000000000 },
      records: { int: 4 },
      storage_cost_atto: "46875000000000000",
      error_kind: "",
    },
  );
  assert.match(line, /^upload,host=DEV-01-ant-client-wasm-upload-lon1-1,region=lon1 /);
  assert.match(line, /status=0i/);
  assert.match(line, /file_size=1048576i/);
  // Cost exceeds Number.MAX_SAFE_INTEGER, so it must travel as a string field
  // rather than an integer, exactly as the bash uploader sends wallet balances.
  assert.match(line, /storage_cost_atto="46875000000000000"/);
});

test("no timestamp is appended", () => {
  // Deliberate, matching the native scripts: a clock-skewed client would
  // otherwise stamp its points wherever its own clock says, and the run's
  // metrics would scatter across the timeline.
  const line = buildLine("download", { host: "h" }, { status: { int: 0 } });
  assert.ok(!/\s\d{15,}$/.test(line), `line must not end with a timestamp: ${line}`);
});
