import assert from "node:assert/strict";
import { test } from "node:test";

import { describeError, fullErrorText } from "../dist/errors.js";

// The shape that was cut off on DEV-03 run 590 (2026-09-17): viem's long-form
// message leads with the request body -- the ERC-20 approve calldata is 136
// hex chars, and a merkle payment's is far longer -- and ends with the reason.
const CALLDATA = `0x095ea7b3${"0".repeat(24)}${"ab".repeat(20)}${"0".repeat(64)}`;
const RPC_REASON = "HTTP request failed. Status: 429 URL: https://sepolia-rollup.arbitrum.io/rpc";

function viemEstimateGasError() {
  const body = JSON.stringify({
    method: "eth_estimateGas",
    params: [{ data: CALLDATA + "f".repeat(2000), from: "0x14BE479edAcfd3C6eF991DC320E7a006F6Fd9606", nonce: "0x8a" }],
  });
  const transport = new Error(`HTTP request failed.\n\nStatus: 429\nURL: https://sepolia-rollup.arbitrum.io/rpc\nRequest body: ${body}\n\nDetails: Too Many Requests\nVersion: viem@2.21.0`);
  transport.name = "HttpRequestError";
  transport.shortMessage = "HTTP request failed.";
  transport.details = "Too Many Requests";
  const e = new Error(
    `RPC Request failed.\n\nURL: https://sepolia-rollup.arbitrum.io/rpc\nRequest body: ${body}\n \nEstimate Gas Arguments:\n  from:  0x14BE479edAcfd3C6eF991DC320E7a006F6Fd9606\n  to:    0x4bc1ace0e66170375462cb4e6af42ad4d5ec689c\n  data:  ${CALLDATA}\n\nDetails: ${RPC_REASON}\nVersion: viem@2.21.0`,
    { cause: transport },
  );
  e.name = "EstimateGasExecutionError";
  e.shortMessage = "RPC Request failed.";
  e.details = RPC_REASON;
  return e;
}

test("a viem RPC error keeps its reason within the cap", () => {
  const line = describeError(viemEstimateGasError());
  assert.ok(line.length <= 500, `too long: ${line.length}`);
  assert.ok(!line.includes("\n"));
  assert.match(line, /EstimateGasExecutionError: RPC Request failed\./);
  assert.ok(line.includes("Status: 429"), line);
  assert.ok(line.includes("Too Many Requests"), line);
  // The calldata must not be what fills the line.
  assert.ok(!line.includes("f".repeat(200)), line);
});

test("a plain Error passes through unchanged", () => {
  assert.equal(describeError(new Error("insufficient peers: got 0/1")), "Error: insufficient peers: got 0/1");
  assert.equal(describeError("bare string"), "bare string");
});

test("an over-long plain message keeps its tail, not just its head", () => {
  const msg = `head ${"x".repeat(900)} Details: the reason`;
  const line = describeError(new Error(msg), 200);
  assert.ok(line.length <= 200, `too long: ${line.length}`);
  assert.ok(line.startsWith("Error: head"), line);
  assert.ok(line.endsWith("Details: the reason"), line);
  assert.ok(line.includes(" … "), line);
});

test("hex blobs are elided but hashes are kept", () => {
  const hash = `0x${"a".repeat(64)}`;
  const line = describeError(new Error(`tx ${hash} data ${"0x" + "b".repeat(400)}`));
  assert.ok(line.includes(hash), line);
  assert.ok(line.includes("0x…(400 hex chars)"), line);
});

test("the on-disk record carries the whole error and the cause chain", () => {
  const text = fullErrorText(viemEstimateGasError());
  assert.ok(text.includes(CALLDATA), "full calldata is kept on disk");
  assert.ok(text.includes("cause chain:"), text);
  assert.ok(text.includes("1: HttpRequestError: HTTP request failed. (details: Too Many Requests)"), text);
  assert.ok(text.endsWith("\n"));
});
