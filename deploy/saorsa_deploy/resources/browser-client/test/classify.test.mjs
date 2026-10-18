/**
 * The error classifier.
 *
 * `error_kind` is what a report groups failures by, so the value has to be
 * stable and the ORDER of the patterns has to be the specified one: real
 * ant-core messages satisfy several at once.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { ERROR_TEXT_MAX, classifyError, errorText } from "../lib/classify.mjs";

test("a saturated holder set is busy", () => {
  // The kind this role exists to see: ADR-0023 changes how a node responds
  // when the holders of a hot record are saturated.
  assert.equal(classifyError(new Error("the holders of this record are busy")), "busy");
});

test("busy wins over every other pattern in the message", () => {
  // Order is the spec. A message naming a payment and a timeout as well is
  // still a refusal because the holders were busy.
  assert.equal(
    classifyError("payment quote expired: peers are busy, connect timed out"),
    "busy",
  );
});

test("a missing record is not_found", () => {
  assert.equal(classifyError(new Error("record not found at 11aa")), "not_found");
});

test("payment and quorum have their own kinds", () => {
  assert.equal(classifyError("payment required for this quote"), "payment");
  assert.equal(classifyError("read quorum not reached (2/5)"), "quorum");
});

test("the harness deadline is a timeout", () => {
  assert.equal(classifyError(new Error("harness deadline")), "timeout");
  assert.equal(classifyError("operation timed out after 30s"), "timeout");
});

test("transport failures are connection", () => {
  for (const msg of ["failed to connect to peer", "dial refused", "datachannel never opened"]) {
    assert.equal(classifyError(msg), "connection");
  }
});

test("a size mismatch is other, not a transport failure", () => {
  // The download SUCCEEDED and returned the wrong number of bytes. Classifying
  // it as `connection` would file a correctness finding under availability.
  assert.equal(classifyError("size mismatch: got 1048576"), "other");
});

test("anything unrecognised is other rather than guessed at", () => {
  assert.equal(classifyError("Illegal invocation"), "other");
});

test("an Error and its message classify identically", () => {
  assert.equal(classifyError(new Error("peers are busy")), classifyError("peers are busy"));
});

test("error_text is verbatim and truncated", () => {
  assert.equal(errorText(new Error("short")), "short");
  const long = "x".repeat(ERROR_TEXT_MAX + 500);
  assert.equal(errorText(long).length, ERROR_TEXT_MAX);
});
