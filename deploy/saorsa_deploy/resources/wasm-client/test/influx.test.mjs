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

import { buildLine, escapeFieldString, escapeTag, i64Field } from "../dist/influx.js";

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

test("int64 fields are emitted with the i suffix", () => {
  // The native uploader.sh types storage_cost_atto and gas_cost_wei as int64.
  // Influx fixes a field's type on first write and REJECTS THE WHOLE POINT of
  // any later write that disagrees — observed on DEV-01-wasm, where every WASM
  // upload metric was dropped with "field type conflict: storage_cost_atto is
  // type string, already exists as type integer".
  const line = buildLine("upload", {}, { storage_cost_atto: i64Field("46875000000000000") });
  assert.equal(line, "upload storage_cost_atto=46875000000000000i");
});

test("an int64 field accepts a bigint as well as a string", () => {
  assert.deepEqual(i64Field(42n), { i64: 42n });
  assert.deepEqual(i64Field("42"), { i64: 42n });
});

test("a value beyond int64 is dropped rather than sent as a string", () => {
  // uploader.sh's i64() helper appends the field only when it is in range;
  // falling back to a string would recreate the type conflict.
  assert.equal(i64Field((2n ** 63n).toString()), undefined);
  assert.equal(i64Field((-(2n ** 63n)).toString()), undefined);
});

test("empty and unparseable values are dropped", () => {
  for (const v of [null, undefined, "", "not-a-number"]) {
    assert.equal(i64Field(v), undefined);
  }
});

test("a dropped field leaves the rest of the line intact", () => {
  const line = buildLine(
    "upload",
    { host: "h" },
    { status: { int: 0 }, storage_cost_atto: i64Field("nonsense"), file_size: { int: 10 } },
  );
  assert.equal(line, "upload,host=h status=0i,file_size=10i");
});
