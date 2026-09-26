import assert from "node:assert/strict";
import { mkdtemp, readFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { classifyError } from "../dist/classify.js";
import {
  emit,
  exitCodeFor,
  failureResult,
  markPaymentSubmitted,
  parseArgs,
  parseBootstraps,
  successResult,
  UsageError,
} from "../dist/cli.js";

// Every field `ant --json` emits, plus the three the browser path adds. A
// caller parses ONE shape whichever transport ran, so a missing key is a
// contract break even when its value would be null.
const CONTRACT_KEYS = [
  "address",
  "chunks_stored",
  "chunks_failed",
  "total_chunks",
  "elapsed_secs",
  "storage_cost_atto",
  "gas_cost_wei",
  "file_size",
  "payment_sent",
  "error",
  "reason",
  "error_kind",
];

test("download argv parses into an address and an output path", () => {
  const args = parseArgs(["file", "download", "abc123", "-o", "/tmp/out.bin"]);
  assert.equal(args.command, "download");
  assert.equal(args.address, "abc123");
  assert.equal(args.path, "/tmp/out.bin");
});

test("upload argv parses, and --public is accepted", () => {
  // The native argv carries --public; a caller building one argv for both
  // transports must not be rejected for passing it.
  const args = parseArgs(["file", "upload", "--public", "/data/file.bin"]);
  assert.equal(args.command, "upload");
  assert.equal(args.path, "/data/file.bin");
  assert.equal(args.paymentMode, "auto");
});

test("payment mode is only forced when asked for", () => {
  assert.equal(parseArgs(["file", "upload", "--merkle", "/f"]).paymentMode, "merkle");
  assert.equal(parseArgs(["file", "upload", "--no-merkle", "/f"]).paymentMode, "single");
});

test("a download with no -o is a usage error", () => {
  assert.throws(() => parseArgs(["file", "download", "abc"]), UsageError);
});

test("an unknown option is a usage error, not silently ignored", () => {
  // Notably --evm-network: there is no such thing on this path (the payment
  // identity comes from the node's HELLO), so a caller passing it has built the
  // wrong argv and must hear about it.
  assert.throws(() => parseArgs(["file", "download", "a", "-o", "/f", "--evm-network", "arbitrum-one"]), UsageError);
});

test("an empty BOOTSTRAP_MULTIADDRS is refused", () => {
  assert.throws(() => parseBootstraps(""), /at least one address/);
  assert.throws(() => parseBootstraps(undefined), /at least one address/);
});

test("bootstrap multiaddrs split on commas only", () => {
  const raw = "/ip4/1.2.3.4/udp/10001/webrtc-direct/certhash/uEiA/p2p/12D3, /ip4/5.6.7.8/udp/10001/webrtc-direct/p2p/12D4";
  assert.deepEqual(parseBootstraps(raw), [
    "/ip4/1.2.3.4/udp/10001/webrtc-direct/certhash/uEiA/p2p/12D3",
    "/ip4/5.6.7.8/udp/10001/webrtc-direct/p2p/12D4",
  ]);
});

test("a success result carries every contract key", () => {
  const r = successResult({ address: "abc", file_size: 10, elapsed_secs: 1.5 });
  for (const key of CONTRACT_KEYS) assert.ok(key in r, `missing ${key}`);
  assert.equal(r.error, null);
  assert.equal(r.error_kind, null);
  assert.equal(r.payment_sent, false);
  // null, not 0: a download has no chunk counts, and 0 reads as "nothing
  // stored", which is what total failure looks like.
  assert.equal(r.chunks_stored, null);
});

test("a failure result carries every contract key plus a reason", () => {
  const r = failureResult(new Error("connection refused"), { elapsed_secs: 2 });
  for (const key of CONTRACT_KEYS) assert.ok(key in r, `missing ${key}`);
  assert.equal(r.error_kind, "connection");
  assert.ok(r.error);
  // Both spellings are populated: `ant --json` emits both and a caller may read
  // either, so neither may be absent.
  assert.equal(r.reason, r.error);
});

test("ant-core's size refusal is over_cap, not a network failure", () => {
  const e = new Error("invalid public file size 1048576000");
  assert.equal(classifyError(e), "over_cap");
  assert.equal(failureResult(e, { elapsed_secs: 0 }).error_kind, "over_cap");
});

test("over_cap wins over the substrings the other kinds match", () => {
  // The message contains "size" and could plausibly be caught by a later
  // pattern in a differently-ordered classifier; the order is load-bearing
  // because everything downstream records over_cap as a SKIP, not a failure.
  const e = new Error("failed to download record: invalid public file size 2000000000");
  assert.equal(classifyError(e), "over_cap");
});

test("the emitted line is a single parseable JSON object", () => {
  const written = [];
  const original = process.stdout.write;
  process.stdout.write = (chunk) => {
    written.push(String(chunk));
    return true;
  };
  try {
    emit(successResult({ address: "abc", elapsed_secs: 0.25 }));
  } finally {
    process.stdout.write = original;
  }
  assert.equal(written.length, 1);
  assert.ok(written[0].endsWith("\n"));
  assert.equal(written[0].trimEnd().includes("\n"), false);
  assert.equal(JSON.parse(written[0]).address, "abc");
});

test("the payment marker is written at submission", async () => {
  const dir = await mkdtemp(join(tmpdir(), "ant-wasm-cli-"));
  const marker = join(dir, "paid");
  let flagged = false;
  const note = markPaymentSubmitted(marker, () => {
    flagged = true;
  });
  await note();
  assert.equal(flagged, true);
  assert.ok((await readFile(marker, "utf8")).trim().length > 0);
});

test("no marker exists when the failure came before payment", async () => {
  const dir = await mkdtemp(join(tmpdir(), "ant-wasm-cli-"));
  const marker = join(dir, "paid");
  // The callback is never invoked, which is exactly the pre-payment failure
  // case: nothing was submitted, so there must be nothing on disk claiming it
  // was.
  markPaymentSubmitted(marker, () => {});
  await assert.rejects(() => stat(marker));
});

test("a marker path that cannot be written does not fail the operation", async () => {
  // Best effort: the in-memory flag still reaches the JSON line, and losing the
  // marker must never turn a successful paid upload into a failure.
  let flagged = false;
  const note = markPaymentSubmitted("/proc/definitely/not/writable", () => {
    flagged = true;
  });
  await note();
  assert.equal(flagged, true);
});

test("a success exits 0 and a failure exits non-zero", () => {
  // The invoking service branches on the exit code before it parses anything:
  // non-zero is what makes it keep the per-op stderr and record a failure.
  assert.equal(exitCodeFor(successResult({ elapsed_secs: 1 })), 0);
  assert.equal(exitCodeFor(failureResult(new Error("nope"), { elapsed_secs: 1 })), 1);
});

test("an over-cap refusal still exits non-zero", () => {
  // The SKIP classification happens in the calling service, from error_kind —
  // the CLI itself did not succeed, so it must not claim it did.
  const r = failureResult(new Error("invalid public file size 2000000000"), {
    elapsed_secs: 1,
  });
  assert.equal(r.error_kind, "over_cap");
  assert.equal(exitCodeFor(r), 1);
});
