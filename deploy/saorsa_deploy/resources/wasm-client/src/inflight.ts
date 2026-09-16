/**
 * Crash accounting for the operation in flight.
 *
 * A metric is only emitted when an operation returns, so an operation the
 * process dies inside -- an OOM kill on a large download being the case that
 * exposed this -- would otherwise leave no record at all, and the success
 * rate would be computed over survivors only. Measured on DEV-01-wasm
 * (2026-09-16): a 900 MB WASM download reaches ~3.7 GB RSS and was killed on
 * a 4 GB VM three times with zero failures recorded.
 *
 * So each operation writes a marker before it starts and removes it after its
 * metric is submitted. On startup a leftover marker is reported as a failed
 * operation with `error_kind=crash`, then removed. The marker survives a
 * process death (it is a file) but not a VM rebuild, which is the right
 * scope: a replaced VM is a new instrument.
 */

import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";

import { buildLine, type InfluxTarget, submitMetric } from "./influx.js";

export const STATUS_FAILURE = 1;

export interface Inflight {
  measurement: "upload" | "download";
  /** ms since epoch. */
  startedAt: number;
  /** Bytes; the intended upload size, or 0 for a download of unknown size. */
  fileSize: number;
  /** Download address, when applicable. */
  address?: string;
}

export function inflightPath(logDir: string): string {
  return join(logDir, "inflight.json");
}

export async function markInflight(logDir: string, op: Inflight): Promise<void> {
  try {
    await mkdir(logDir, { recursive: true });
    await writeFile(inflightPath(logDir), JSON.stringify(op));
  } catch {
    /* best effort: never let bookkeeping stop the measurement */
  }
}

export async function clearInflight(logDir: string): Promise<void> {
  try {
    await rm(inflightPath(logDir), { force: true });
  } catch {
    /* best effort */
  }
}

/**
 * Report an operation the previous process died inside, if any, then clear
 * the marker. Returns the recovered marker for logging, or null.
 */
export async function recoverInflight(
  logDir: string,
  influx: InfluxTarget,
  tags: { host: string; region: string },
  now: number = Date.now(),
): Promise<Inflight | null> {
  let op: Inflight;
  try {
    op = JSON.parse(await readFile(inflightPath(logDir), "utf8")) as Inflight;
  } catch {
    return null;
  }
  const totalMs = Math.max(0, now - op.startedAt);
  const secsField = op.measurement === "upload" ? "upload_secs" : "download_secs";
  await submitMetric(
    influx,
    buildLine(op.measurement, tags, {
      status: { int: STATUS_FAILURE },
      total_ms: { int: totalMs },
      [secsField]: totalMs / 1000,
      file_size: { int: op.fileSize },
      start_ns: { int: op.startedAt * 1_000_000 },
      error_kind: "crash",
    }),
  );
  await clearInflight(logDir);
  return op;
}
