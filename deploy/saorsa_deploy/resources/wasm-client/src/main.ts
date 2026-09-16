/**
 * Continuous WASM upload / download client.
 *
 * The counterpart of `resources/scripts/uploader.sh` and `downloader.sh` for the
 * browser transport. `ant-cli` cannot stand in for this: it links native
 * ant-core, so it exercises the native path no matter how it is invoked. This
 * program imports ant-core compiled to WASM and reaches nodes over
 * WebRTC-Direct, which is the only way to measure the browser path the way the
 * native client is measured.
 *
 * It emits the SAME `upload` / `download` InfluxDB measurements as the native
 * clients, so every downstream consumer (record-results, the per-op Postgres
 * tables, print-uploads, deployments compare) works on WASM operations
 * unchanged.
 */

import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

import { AddressSink, AddressSource } from "./addresses.js";
import {
  type AntCore,
  type BrowserNetworkClient,
  fetchPaymentNetwork,
  initAntCore,
  installWebRtc,
} from "./client.js";
import { type Config, loadConfig } from "./config.js";
import { buildLine, i64Field, type InfluxTarget, submitMetric } from "./influx.js";
import { clearInflight, markInflight, recoverInflight } from "./inflight.js";
import { makeWallet, type PaymentNetwork, type PayForQuotes,
  makeMerklePayer,
  type PayForMerkle,
} from "./wallet.js";

const STATUS_SUCCESS = 0;
const STATUS_FAILURE = 1;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Deterministic pseudo-random payload.
 *
 * Must not be all zeros or a constant byte: self-encryption is
 * content-addressed, so an identical payload yields an identical address and
 * every upload after the first would be a no-op store of content already on the
 * network -- which measures nothing.
 */
function makePayload(sizeBytes: number, seed: number): Uint8Array {
  const out = new Uint8Array(sizeBytes);
  let x = (seed ^ 0x9e3779b9) >>> 0;
  for (let i = 0; i < sizeBytes; i++) {
    x ^= x << 13;
    x >>>= 0;
    x ^= x >> 17;
    x ^= x << 5;
    x >>>= 0;
    out[i] = x & 0xff;
  }
  return out;
}

/** Short, stable classification of a failure, for the `error_kind` field. */
function classify(error: unknown): string {
  const msg = String(error);
  if (/payment|quote|insufficient|funds/i.test(msg)) return "payment";
  if (/peers|quorum|close group|witness/i.test(msg)) return "quorum";
  if (/timeout|timed out/i.test(msg)) return "timeout";
  if (/connect|channel|transport|webrtc|dial/i.test(msg)) return "connection";
  if (/not found|missing|record/i.test(msg)) return "not_found";
  return "other";
}

/** Truncated single-line message, so one failure cannot flood the journal. */
function shortMessage(error: unknown): string {
  return String(error).replace(/\s+/g, " ").slice(0, 500);
}

async function runUploader(
  cfg: Config,
  core: AntCore,
  client: BrowserNetworkClient,
  influx: InfluxTarget,
  host: string,
): Promise<void> {
  const paymentNetwork = (await fetchPaymentNetwork(
    core,
    cfg.bootstrapMultiaddrs[0],
  )) as unknown as PaymentNetwork;
  console.log(
    `payment network from HELLO: chain_id=${paymentNetwork.chain_id} ` +
      `token=${paymentNetwork.payment_token_address} vault=${paymentNetwork.payment_vault_address}`,
  );

  const pay: PayForQuotes = makeWallet(cfg.secretKey!, cfg.rpcUrl!, paymentNetwork);
  // Always supplied: "auto" decides per upload whether the batch settles via
  // merkle, so the callback must be there even when this entry is not forced.
  const payMerkle: PayForMerkle = makeMerklePayer(cfg.secretKey!, cfg.rpcUrl!, paymentNetwork);
  const sink = new AddressSink({
    addressesFile: cfg.addressesFile,
    presignedUrl: cfg.uploadPresignedUrl,
    flushIntervalSecs: cfg.s3FlushIntervalSecs,
    flushEveryN: cfg.s3FlushEveryN,
    tailSize: cfg.addressesTailSize,
  });

  let seq = 0;
  for (;;) {
    seq += 1;
    const content = makePayload(cfg.fileSizeBytes, Date.now() + seq * 7919);
    const name = `wasm-${cfg.region}-${cfg.clientIndex}-${seq}.bin`;
    const startNs = BigInt(Date.now()) * 1_000_000n;
    const t0 = Date.now();
    await markInflight(cfg.logDir, { measurement: "upload", startedAt: t0, fileSize: cfg.fileSizeBytes });

    let status = STATUS_SUCCESS;
    let errorKind = "";
    let records = 0;
    let costAtto = "0";
    try {
      // ant-core refuses a paid upload without a checkpoint callback. Each
      // upload here is a fresh file that is never resumed across a restart,
      // so the journal is held in memory for the life of the attempt only.
      let checkpoint: string | undefined;
      const result = await client.uploadPublicFile(
        content,
        name,
        "application/octet-stream",
        paymentNetwork,
        pay,
        null,
        null,
        (snapshot: string) => {
          checkpoint = snapshot;
        },
        cfg.paymentMode,
        payMerkle,
      );
      void checkpoint;
      records = result.records ?? 0;
      costAtto = result.storageCostAtto ?? "0";
      await sink.record(result.file.address);
      console.log(
        `upload #${seq} ok: ${result.file.address} ` +
          `(${records} records, ${costAtto} atto, ${Date.now() - t0}ms)`,
      );
    } catch (e) {
      status = STATUS_FAILURE;
      errorKind = classify(e);
      console.error(`upload #${seq} failed [${errorKind}]: ${shortMessage(e)}`);
    }

    const totalMs = Date.now() - t0;
    await submitMetric(
      influx,
      buildLine(
        "upload",
        { host, region: cfg.region },
        {
          status: { int: status },
          total_ms: { int: totalMs },
          upload_secs: totalMs / 1000,
          file_size: { int: cfg.fileSizeBytes },
          start_ns: { int: Number(startNs) },
          records: { int: records },
          // int64, matching uploader.sh — a string here conflicts with the
          // field type Influx already holds and drops the whole point.
          storage_cost_atto: i64Field(costAtto),
          error_kind: errorKind,
        },
      ),
    );
    await clearInflight(cfg.logDir);

    if (cfg.runOnce) break;
    await sleep(cfg.sleepSecs * 1000);
  }
  await sink.flush();
}

async function runDownloader(
  cfg: Config,
  client: BrowserNetworkClient,
  influx: InfluxTarget,
  host: string,
): Promise<void> {
  const source = new AddressSource(
    cfg.addressesS3Bucket ?? "",
    cfg.addressesS3Prefix ?? "",
    60,
    cfg.addressesFile,
  );

  let seq = 0;
  for (;;) {
    const address = await source.pick();
    if (!address) {
      // The uploaders have not published anything yet. Normal at the start of a
      // run; not an operation, so nothing is recorded -- emitting a failure here
      // would depress the download success rate for a reason unrelated to the
      // network.
      console.log("no addresses published yet; waiting");
      await sleep(15_000);
      continue;
    }

    seq += 1;
    const startNs = BigInt(Date.now()) * 1_000_000n;
    const t0 = Date.now();
    await markInflight(cfg.logDir, { measurement: "download", startedAt: t0, fileSize: 0, address });
    let status = STATUS_SUCCESS;
    let errorKind = "";
    let size = 0;
    try {
      const result = await client.downloadPublicFile(address, cfg.downloadConcurrency, null);
      size = result.content.length;
      console.log(`download #${seq} ok: ${address} (${size} bytes, ${Date.now() - t0}ms)`);
    } catch (e) {
      status = STATUS_FAILURE;
      errorKind = classify(e);
      console.error(`download #${seq} failed [${errorKind}] ${address}: ${shortMessage(e)}`);
      await preserveFailure(cfg, seq, address, e);
    }

    const totalMs = Date.now() - t0;
    await submitMetric(
      influx,
      buildLine(
        "download",
        { host, region: cfg.region },
        {
          status: { int: status },
          total_ms: { int: totalMs },
          download_secs: totalMs / 1000,
          file_size: { int: size },
          start_ns: { int: Number(startNs) },
          error_kind: errorKind,
        },
      ),
    );
    await clearInflight(cfg.logDir);

    if (cfg.runOnce) break;
    await sleep(cfg.sleepSecs * 1000);
  }
}

/** Keep a per-op record of a failed download, as `downloader.sh` does. */
async function preserveFailure(
  cfg: Config,
  seq: number,
  address: string,
  error: unknown,
): Promise<void> {
  try {
    await mkdir(cfg.logDir, { recursive: true });
    const stem = `${String(seq).padStart(4, "0")}-${address.slice(0, 16)}`;
    await writeFile(join(cfg.logDir, `${stem}.stderr`), `${String(error)}\n`);
  } catch {
    /* best effort */
  }
}

async function main(): Promise<void> {
  const cfg = loadConfig();
  const host = process.env.HOSTNAME || (await import("node:os")).hostname();
  const influx: InfluxTarget = {
    url: cfg.influxUrl,
    token: cfg.influxToken,
    org: cfg.influxOrg,
    bucket: cfg.influxBucket,
  };

  const crashed = await recoverInflight(cfg.logDir, influx, { host, region: cfg.region });
  if (crashed) {
    console.error(
      `previous process died inside a ${crashed.measurement} started at ` +
        `${new Date(crashed.startedAt).toISOString()}` +
        `${crashed.address ? ` (${crashed.address})` : ""}; recorded as a failure [crash]`,
    );
  }

  await installWebRtc();
  const core = await initAntCore(cfg.wasmDir);
  console.log(
    `ant-core WASM initialised; role=${cfg.role} region=${cfg.region} ` +
      `bootstraps=${cfg.bootstrapMultiaddrs.length}`,
  );

  const client = new core.BrowserNetworkClient(cfg.bootstrapMultiaddrs);
  try {
    if (cfg.role === "wasm-upload") {
      await runUploader(cfg, core, client, influx, host);
    } else {
      await runDownloader(cfg, client, influx, host);
    }
  } finally {
    try {
      client.close();
    } catch {
      /* already closed */
    }
  }
}

main().catch((e) => {
  console.error(`fatal: ${String(e)}`);
  process.exit(1);
});
