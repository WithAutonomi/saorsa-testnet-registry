/**
 * `ant-wasm`: one transfer, then exit — a drop-in for `ant` over the browser
 * path.
 *
 * The long-running harness in `main.ts` is the right shape for a testnet tier
 * that uploads synthesised payloads forever. It is the wrong shape for the
 * production client kinds, which already have Python services that walk an
 * address list, keep a per-op stdout/stderr pair on disk, and shell out to a
 * transfer binary. Giving the browser path that same shape means those services
 * — their units, their restart policies, their logs, their Influx emission —
 * are unchanged by the transport, and the ONE thing that differs is which
 * binary they spawn.
 *
 * It also buys crash accounting for free. The WASM process is by far the
 * largest RSS on a client VM (a browser-path download holds ~4x the file), so
 * an OOM kill lands on it. As a child, that surfaces to the parent as a signal
 * exit, which the parent records and then carries on to the next address — no
 * checkpoint file, no restart policy, no inflight journal.
 *
 * Contract:
 *   ant-wasm file upload --public [--merkle|--no-merkle] <path>
 *   ant-wasm file download <address> -o <path>
 *
 * Input comes from the environment (`BOOTSTRAP_MULTIADDRS`, `SECRET_KEY` for an
 * upload, `RUST_LOG`, `WASM_DIR`), never from flags, because that is how the
 * invoking services already pass configuration. There is no `--evm-network`:
 * the payment identity is read from a node's own HELLO.
 *
 * Output is a single JSON line on stdout in the shape of `ant --json`, so a
 * caller parses one thing whichever transport ran. Progress goes to stderr.
 */

import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import { classifyError, type ErrorKind } from "./classify.js";
import {
  type AntCore,
  type BrowserNetworkClient,
  fetchPaymentNetwork,
  initAntCore,
  installWebRtc,
} from "./client.js";
import { describeError } from "./errors.js";
import {
  makeMerklePayer,
  makeWallet,
  type PaymentNetwork,
  type PayForMerkle,
  type PayForQuotes,
} from "./wallet.js";

export type Command = "upload" | "download";

export interface ParsedArgs {
  command: Command;
  /** Source path for an upload, destination path for a download. */
  path: string;
  /** Download only. */
  address?: string;
  /** Upload only: ant-core payment mode, as `main.ts` resolves PAYMENT_TYPE. */
  paymentMode: "auto" | "single" | "merkle";
  /** Where to record that a payment transaction went out. */
  paymentMarker?: string;
  /** Download concurrency; ant-core clamps this to 6. */
  concurrency: number;
}

/**
 * The `ant --json` result shape, plus the three fields the browser path needs
 * on top of it.
 *
 * `null` rather than 0 for anything this transport does not report: a download
 * has no chunk counts, and writing 0 would read as "zero chunks stored", which
 * is what a total failure looks like.
 */
export interface OpResult {
  address: string | null;
  chunks_stored: number | null;
  chunks_failed: number | null;
  total_chunks: number | null;
  elapsed_secs: number;
  storage_cost_atto: string | null;
  gas_cost_wei: string | null;
  file_size: number | null;
  /** True once a payment transaction has been submitted. */
  payment_sent: boolean;
  error: string | null;
  reason: string | null;
  error_kind: ErrorKind | null;
  /** ant-client commit this bundle was built from, or null if unrecorded. */
  client_build: string | null;
}

/**
 * Build provenance written into the bundle root by `build ant-wasm`.
 *
 * Relative to this module rather than to `WASM_DIR` or a constant: the CLI runs
 * from `<bundle>/dist/cli.js`, so its own location is the one thing that always
 * names the bundle it was unpacked with — including after `deployments
 * upgrade-wasm-client` has replaced that bundle underneath a running service.
 */
export const DEFAULT_BUILD_INFO_PATH = fileURLToPath(
  new URL("../build-info.json", import.meta.url),
);

/**
 * The ant-client commit this bundle was built from, or null.
 *
 * Every failure is null rather than an error: a bundle built before provenance
 * was recorded has no file at all, and an unreadable one must not cost a
 * transfer. Null is what the recording side stores, and it reads as "unknown
 * build", which is exactly what it is.
 */
export async function readClientBuild(
  path: string = DEFAULT_BUILD_INFO_PATH,
): Promise<string | null> {
  try {
    const parsed = JSON.parse(await readFile(path, "utf8")) as { commit?: unknown };
    return typeof parsed?.commit === "string" && parsed.commit ? parsed.commit : null;
  } catch {
    return null;
  }
}

const USAGE = [
  "usage:",
  "  ant-wasm file upload --public [--merkle|--no-merkle] [--payment-marker PATH] <path>",
  "  ant-wasm file download <address> -o <path> [--concurrency N]",
].join("\n");

export class UsageError extends Error {}

export function parseArgs(argv: readonly string[]): ParsedArgs {
  const args = [...argv];
  if (args.shift() !== "file") throw new UsageError(USAGE);
  const verb = args.shift();
  if (verb !== "upload" && verb !== "download") throw new UsageError(USAGE);

  let path = "";
  let address: string | undefined;
  let paymentMarker: string | undefined;
  let concurrency = 4;
  // Unset means ant-core's "auto", exactly as PAYMENT_TYPE does in the harness:
  // never assume a mode, because "auto" needs a merkle wallet above ant-core's
  // own chunk-count threshold and a forced mode below it does not.
  let paymentMode: ParsedArgs["paymentMode"] = "auto";
  const positional: string[] = [];

  while (args.length) {
    const token = args.shift() as string;
    switch (token) {
      // Accepted and ignored: every public upload this tool does is public, but
      // the flag is in the argv the native path uses and a caller that built
      // one argv for both transports must not be rejected.
      case "--public":
        break;
      case "--merkle":
        paymentMode = "merkle";
        break;
      case "--no-merkle":
        paymentMode = "single";
        break;
      case "-o":
      case "--output":
        path = args.shift() ?? "";
        if (!path) throw new UsageError("-o needs a path");
        break;
      case "--payment-marker":
        paymentMarker = args.shift();
        if (!paymentMarker) throw new UsageError("--payment-marker needs a path");
        break;
      case "--concurrency": {
        const raw = args.shift();
        const n = Number(raw);
        if (!Number.isFinite(n) || n <= 0) throw new UsageError(`bad --concurrency '${raw}'`);
        concurrency = n;
        break;
      }
      default:
        if (token.startsWith("-")) throw new UsageError(`unknown option '${token}'\n${USAGE}`);
        positional.push(token);
    }
  }

  if (verb === "upload") {
    if (positional.length !== 1) throw new UsageError(USAGE);
    path = positional[0];
  } else {
    if (positional.length !== 1) throw new UsageError(USAGE);
    address = positional[0];
    if (!path) throw new UsageError("download needs -o <path>");
  }
  return { command: verb, path, address, paymentMode, paymentMarker, concurrency };
}

/** Comma-separated, because a multiaddr contains '/' and ':' but never ','. */
export function parseBootstraps(raw: string | undefined): string[] {
  const list = (raw ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (list.length === 0) throw new Error("BOOTSTRAP_MULTIADDRS must contain at least one address");
  return list;
}

export function successResult(fields: Partial<OpResult> & { elapsed_secs: number }): OpResult {
  return {
    address: null,
    chunks_stored: null,
    chunks_failed: null,
    total_chunks: null,
    storage_cost_atto: null,
    gas_cost_wei: null,
    file_size: null,
    payment_sent: false,
    error: null,
    reason: null,
    error_kind: null,
    client_build: null,
    ...fields,
  };
}

export function failureResult(
  error: unknown,
  fields: Partial<OpResult> & { elapsed_secs: number },
): OpResult {
  const reason = describeError(error);
  return {
    ...successResult(fields),
    error: reason,
    // `error` and `reason` carry the same text deliberately: `ant --json` emits
    // both and a caller may read either, so neither may be absent on a failure.
    reason,
    error_kind: classifyError(error),
  };
}

/**
 * Exit status for a finished operation.
 *
 * A separate function because the invoking service branches on the exit code
 * before it parses anything: a non-zero exit is what makes it keep the per-op
 * stderr and record a failure, so the mapping must be exactly `ant`'s.
 */
export function exitCodeFor(result: OpResult): number {
  return result.error === null ? 0 : 1;
}

/** The one line a caller parses. Always last, always on stdout. */
export function emit(result: OpResult): void {
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

function progress(message: string): void {
  process.stderr.write(`${message}\n`);
}

/**
 * Wrap a payment callback so the marker file lands the instant a transaction is
 * submitted.
 *
 * A paid upload that is killed after the money went out but before it printed
 * anything is the one failure a caller cannot recover from the exit code: the
 * chunks may be partly stored and the ETH is definitely spent. The marker is
 * written before the callback returns, so it survives a SIGKILL of this
 * process.
 */
export function markPaymentSubmitted(
  markerPath: string | undefined,
  onSent: () => void,
): (() => Promise<void>) {
  return async () => {
    onSent();
    if (!markerPath) return;
    try {
      await writeFile(markerPath, `${new Date().toISOString()}\n`);
    } catch {
      /* best effort: the in-memory flag still reaches the JSON line */
    }
  };
}

async function runUpload(
  args: ParsedArgs,
  core: AntCore,
  client: BrowserNetworkClient,
  bootstraps: string[],
  startedAt: number,
): Promise<OpResult> {
  const secretKey = process.env.SECRET_KEY;
  if (!secretKey) throw new Error("SECRET_KEY must be set for an upload");
  const rpcUrl = process.env.RPC_URL;
  if (!rpcUrl) throw new Error("RPC_URL must be set for an upload");

  const content = new Uint8Array(await readFile(args.path));
  const name = args.path.split("/").pop() || "upload.bin";

  const paymentNetwork = (await fetchPaymentNetwork(
    core,
    bootstraps[0],
  )) as unknown as PaymentNetwork;
  progress(
    `payment network from HELLO: chain_id=${paymentNetwork.chain_id} ` +
      `vault=${paymentNetwork.payment_vault_address}`,
  );

  let paymentSent = false;
  const noteSubmission = markPaymentSubmitted(args.paymentMarker, () => {
    paymentSent = true;
  });
  const pay: PayForQuotes = wrapSubmission(
    makeWallet(secretKey, rpcUrl, paymentNetwork),
    noteSubmission,
  ) as PayForQuotes;
  // Always supplied: "auto" decides per upload whether the batch settles via
  // merkle, so the callback must be there even when the mode is not forced.
  const payMerkle: PayForMerkle = wrapSubmission(
    makeMerklePayer(secretKey, rpcUrl, paymentNetwork),
    noteSubmission,
  ) as PayForMerkle;

  try {
    const result = await client.uploadPublicFile(
      content,
      name,
      "application/octet-stream",
      paymentNetwork,
      pay,
      (msg: string) => progress(msg),
      null,
      // ant-core refuses a paid upload without a checkpoint callback. A single
      // operation is never resumed, so the journal is held for the life of the
      // attempt only.
      () => undefined,
      args.paymentMode,
      payMerkle,
    );
    return successResult({
      address: result.file.address,
      chunks_stored: result.records ?? null,
      chunks_failed: 0,
      total_chunks: result.records ?? null,
      storage_cost_atto: result.storageCostAtto ?? null,
      file_size: content.length,
      payment_sent: paymentSent,
      elapsed_secs: (Date.now() - startedAt) / 1000,
    });
  } catch (e) {
    return failureResult(e, {
      file_size: content.length,
      payment_sent: paymentSent,
      elapsed_secs: (Date.now() - startedAt) / 1000,
    });
  }
}

/**
 * Call `onSubmitted` after the wrapped payment callback resolves.
 *
 * The callback's own resolution IS the submission point: both wallet paths wait
 * on the payment receipt before returning, so by the time it resolves the
 * transaction is mined. Wrapping the callback rather than reaching into the
 * wallet keeps the wallet module unchanged and shared with the harness.
 */
function wrapSubmission(inner: unknown, onSubmitted: () => Promise<void>): unknown {
  const fn = inner as (...a: unknown[]) => Promise<unknown>;
  return async (...a: unknown[]) => {
    const out = await fn(...a);
    await onSubmitted();
    return out;
  };
}

async function runDownload(
  args: ParsedArgs,
  client: BrowserNetworkClient,
  startedAt: number,
): Promise<OpResult> {
  const address = args.address as string;
  try {
    const result = await client.downloadPublicFile(address, args.concurrency, (msg: string) =>
      progress(msg),
    );
    await writeFile(args.path, result.content);
    return successResult({
      address,
      file_size: result.content.length,
      elapsed_secs: (Date.now() - startedAt) / 1000,
    });
  } catch (e) {
    return failureResult(e, {
      address,
      elapsed_secs: (Date.now() - startedAt) / 1000,
    });
  }
}

export async function main(argv: readonly string[] = process.argv.slice(2)): Promise<number> {
  let args: ParsedArgs;
  try {
    args = parseArgs(argv);
  } catch (e) {
    process.stderr.write(`${String(e instanceof Error ? e.message : e)}\n`);
    return 2;
  }

  const startedAt = Date.now();
  // Read once, up front: every line this process emits carries it, including
  // the ones written before a client exists. It is what lets results recorded
  // either side of an in-place bundle upgrade be told apart.
  const clientBuild = await readClientBuild();
  let bootstraps: string[];
  try {
    bootstraps = parseBootstraps(process.env.BOOTSTRAP_MULTIADDRS);
  } catch (e) {
    emit({ ...failureResult(e, { elapsed_secs: 0 }), client_build: clientBuild });
    return 1;
  }

  await installWebRtc();
  const core = await initAntCore(process.env.WASM_DIR || "/opt/ant-wasm-client/pkg");
  progress(`ant-core WASM initialised; bootstraps=${bootstraps.length}`);

  const client = new core.BrowserNetworkClient(bootstraps);
  let result: OpResult;
  try {
    result =
      args.command === "upload"
        ? await runUpload(args, core, client, bootstraps, startedAt)
        : await runDownload(args, client, startedAt);
  } finally {
    try {
      client.close();
    } catch {
      /* already closed */
    }
  }
  result.client_build = clientBuild;
  emit(result);
  return exitCodeFor(result);
}

// `process.argv[1]` is the script systemd/the service actually ran. Guarding on
// it keeps the module importable by the unit tests, which exercise the pure
// halves without touching the network.
if (process.argv[1] && process.argv[1].endsWith("cli.js")) {
  main()
    .then((code) => process.exit(code))
    .catch(async (e) => {
      // Stamped here too, not just inside `main`. Anything that throws before
      // the transfer begins — resolving the payment network from a node's
      // HELLO, constructing the client — lands here, and those are exactly the
      // failures an operator reads as "did the bundle we just installed break
      // it?". An unstamped row cannot answer that. Measured on the live
      // verification of V2-1346: an ant-client API drift threw out of
      // `fetchPaymentNetwork` and every resulting row said `client_build:
      // null`.
      emit({ ...failureResult(e, { elapsed_secs: 0 }), client_build: await readClientBuild() });
      process.exit(1);
    });
}
