import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { classifyError } from "../dist/classify.js";
import {
  DEFAULT_BUILD_INFO_PATH,
  emit,
  exitCodeFor,
  failureResult,
  markPaymentSubmitted,
  parseArgs,
  parseBootstraps,
  readClientBuild,
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
  "client_build",
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

test("the client build is read from the bundle's build-info.json", async () => {
  // The commit is what identifies the code a transfer ran: a moving branch name
  // does not, and `deployments upgrade-wasm-client` replaces the bundle under a
  // running service, so the transfers either side of it must be separable.
  const dir = await mkdtemp(join(tmpdir(), "ant-wasm-build-"));
  const path = join(dir, "build-info.json");
  await writeFile(
    path,
    JSON.stringify({ commit: "9112d683abcdef", ref: "ant-cli-v0.3.8" }),
  );
  assert.equal(await readClientBuild(path), "9112d683abcdef");
});

test("a missing or unusable build-info.json reads as null", async () => {
  // A bundle built before provenance was recorded has no file at all, and an
  // unreadable one must never cost a transfer: null means "unknown build".
  const dir = await mkdtemp(join(tmpdir(), "ant-wasm-build-"));
  assert.equal(await readClientBuild(join(dir, "absent.json")), null);

  const malformed = join(dir, "malformed.json");
  await writeFile(malformed, "not json");
  assert.equal(await readClientBuild(malformed), null);

  const empty = join(dir, "empty.json");
  await writeFile(empty, JSON.stringify({ commit: "" }));
  assert.equal(await readClientBuild(empty), null);
});

test("the default build-info path is the bundle root, beside dist/", () => {
  // The CLI runs from `<bundle>/dist/cli.js`, and the tarball puts
  // build-info.json at the bundle root — its own location is the only thing
  // that names the bundle it was actually unpacked with.
  assert.ok(DEFAULT_BUILD_INFO_PATH.endsWith("/build-info.json"));
  assert.ok(!DEFAULT_BUILD_INFO_PATH.includes("/dist/"));
});

const PACKAGE_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

test("a failure thrown before the transfer starts still carries the build", async () => {
  // Anything that throws before `main`'s own try — resolving the payment
  // network from a node's HELLO, constructing the client — reaches the
  // top-level handler, and those are exactly the failures an operator reads as
  // "did the bundle we just installed break it?". Found on the live
  // verification of V2-1346: an ant-client API drift threw out of
  // fetchPaymentNetwork and every resulting row said client_build: null.
  const buildInfo = join(PACKAGE_ROOT, "build-info.json");
  await writeFile(buildInfo, JSON.stringify({ commit: "a04b9fc7c4df8db52170" }));
  try {
    // A WASM_DIR that does not exist makes ant-core's own module load throw,
    // which is the same escape route the real API drift took.
    const { stdout } = await promisify(execFile)(
      process.execPath,
      [join(PACKAGE_ROOT, "dist/cli.js"), "file", "download", "abc", "-o", "/dev/null"],
      {
        env: {
          ...process.env,
          BOOTSTRAP_MULTIADDRS: "/ip4/1.2.3.4/udp/1/webrtc-direct",
          WASM_DIR: "/nonexistent-pkg",
        },
      },
    ).catch((e) => e);
    const result = JSON.parse(stdout.trim().split("\n").pop());
    assert.equal(result.client_build, "a04b9fc7c4df8db52170");
    assert.ok(result.error);
  } finally {
    await rm(buildInfo, { force: true });
  }
});
