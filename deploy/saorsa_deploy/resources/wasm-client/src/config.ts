/**
 * Environment contract for the WASM client service.
 *
 * Mirrors the native clients: everything arrives through a systemd
 * EnvironmentFile (`/etc/ant-wasm-client.env`) written by the provisioner, so
 * the program itself holds no deployment knowledge.
 */

export type Role = "wasm-upload" | "wasm-download";

export interface Config {
  role: Role;
  region: string;
  clientIndex: number;
  /** WebRTC-Direct bootstrap addresses handed to this client. */
  bootstrapMultiaddrs: string[];
  fileSizeBytes: number;
  sleepSecs: number;
  runOnce: boolean;
  /** Concurrency for downloadPublicFile; ant-core clamps this to 6. */
  downloadConcurrency: number;

  // Payment (upload role only).
  secretKey?: string;
  rpcUrl?: string;
  /**
   * ant-core payment mode, mirroring what the native `ant` does with the same
   * manifest `payment:` value (PAYMENT_TYPE): `merkle` -> "merkle" (force the
   * batched vault payment), `no-merkle` -> "single" (one payment per chunk),
   * unset -> "auto" (ant-core's default: merkle at or above its chunk-count
   * threshold, single below). All three need the merkle submission callback
   * to be supplied, which the uploader always does.
   */
  paymentMode: "auto" | "single" | "merkle";

  // Address handoff, shared with the native tier's S3 layout.
  addressesS3Bucket?: string;
  addressesS3Prefix?: string;
  addressesFile: string;
  /**
   * Largest file the downloader will pick, in bytes; 0 = unbounded. Set by
   * `clients provision` to ant-core's browser cap so an over-cap native
   * upload in the shared pool is skipped rather than fetched and rejected.
   */
  maxDownloadBytes: number;
  uploadPresignedUrl?: string;
  s3FlushIntervalSecs: number;
  s3FlushEveryN: number;
  addressesTailSize: number;

  // InfluxDB.
  influxUrl?: string;
  influxToken?: string;
  influxOrg: string;
  influxBucket: string;

  wasmDir: string;
  logDir: string;
}

function req(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`${name} must be set`);
  return v;
}

function num(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return fallback;
  const v = Number(raw);
  if (!Number.isFinite(v)) throw new Error(`${name} must be a number, got '${raw}'`);
  return v;
}

function bool(name: string, fallback: boolean): boolean {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return fallback;
  return raw === "true" || raw === "1";
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const role = req("ROLE") as Role;
  if (role !== "wasm-upload" && role !== "wasm-download") {
    throw new Error(`ROLE must be wasm-upload or wasm-download, got '${role}'`);
  }

  // Comma-separated, because a multiaddr contains '/' and ':' but never ','.
  // More than one: a failed endpoint gets a 30-minute cooldown inside
  // ant-core, so a single dead bootstrap would strand the client for that long.
  const bootstrapMultiaddrs = req("BOOTSTRAP_MULTIADDRS")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (bootstrapMultiaddrs.length === 0) {
    throw new Error("BOOTSTRAP_MULTIADDRS must contain at least one address");
  }

  const cfg: Config = {
    role,
    region: req("REGION"),
    clientIndex: Number(req("CLIENT_INDEX")),
    bootstrapMultiaddrs,
    fileSizeBytes: num("FILE_SIZE_KB", 1024) * 1024,
    sleepSecs: num("SLEEP_SECS", 1),
    runOnce: bool("RUN_ONCE", false),
    downloadConcurrency: num("DOWNLOAD_CONCURRENCY", 4),
    secretKey: env.SECRET_KEY || undefined,
    rpcUrl: env.RPC_URL || undefined,
    paymentMode:
      env.PAYMENT_TYPE === "merkle" ? "merkle" : env.PAYMENT_TYPE === "no-merkle" ? "single" : "auto",
    addressesS3Bucket: env.ADDRESSES_S3_BUCKET || undefined,
    addressesS3Prefix: env.ADDRESSES_S3_PREFIX || undefined,
    addressesFile: env.ADDRESSES_FILE || "/var/lib/ant-wasm-client/addresses.txt",
    maxDownloadBytes: num("MAX_DOWNLOAD_BYTES", 0),
    uploadPresignedUrl: env.UPLOAD_PRESIGNED_URL || undefined,
    s3FlushIntervalSecs: num("S3_FLUSH_INTERVAL_SECS", 30),
    s3FlushEveryN: num("S3_FLUSH_EVERY_N", 50),
    addressesTailSize: num("ADDRESSES_TAIL_SIZE", 10000),
    influxUrl: env.INFLUX_URL || undefined,
    influxToken: env.INFLUX_TOKEN || undefined,
    influxOrg: env.INFLUX_ORG || "ant",
    influxBucket: env.INFLUX_BUCKET || "testnet-metrics",
    wasmDir: env.WASM_DIR || "/opt/ant-wasm-client/pkg",
    logDir: env.LOG_DIR || "/var/log/ant-wasm-client",
  };

  if (cfg.role === "wasm-upload") {
    if (!cfg.secretKey) throw new Error("SECRET_KEY must be set for the wasm-upload role");
    if (!cfg.rpcUrl) throw new Error("RPC_URL must be set for the wasm-upload role");
  }
  return cfg;
}
