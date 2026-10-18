/**
 * Harness logging: the console plus, when `forward_logs` is set, the
 * append-only files telegraf tails.
 *
 * Tee'd to files rather than left in the journal, because the client→ES
 * forwarder is a `tail` input over
 * `/var/log/ant-client-forward/*.log` — the same mechanism and the same 1 GiB
 * coarse cap the native roles use (`uploader.sh`'s `append_forward_log`), so
 * one telegraf config shape covers every role.
 */

import { appendFileSync, mkdirSync, statSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export const FORWARD_LOG_MAX_BYTES = 1073741824;

function append(path, line, maxBytes) {
  try {
    mkdirSync(dirname(path), { recursive: true });
    appendFileSync(path, `${line}\n`);
    // Coarse: truncated wholesale once past the cap, exactly as the native
    // scripts do. Rotation would need the forwarder restarted to re-open the
    // new inode, and a browser VM's logs are a few lines a round.
    if (statSync(path).size > maxBytes) writeFileSync(path, "");
  } catch {
    /* logging must never be the thing that fails a round */
  }
}

export function makeLogger({
  forwardLogs = false,
  stdoutPath = "/var/log/ant-client-forward/browser-downloader.stdout.log",
  stderrPath = "/var/log/ant-client-forward/browser-downloader.stderr.log",
  maxBytes = FORWARD_LOG_MAX_BYTES,
} = {}) {
  return {
    log(line) {
      console.log(line);
      if (forwardLogs) append(stdoutPath, line, maxBytes);
    },
    warn(line) {
      console.warn(line);
      if (forwardLogs) append(stderrPath, line, maxBytes);
    },
  };
}

/**
 * The one-line, grep-able summary of a download.
 *
 * Fixed `key=value` order so a run can be read with grep and cut rather than a
 * JSON parser — this is what an operator looks at while the cohort is running,
 * before anything has reached Influx.
 */
export function summaryLine(result) {
  return (
    `round=${result.roundId} instance=${result.instance} ` +
    `status=${result.status} kind=${result.errorKind || ""} ` +
    `total_ms=${Math.round(result.totalMs || 0)} address=${result.address || ""}`
  );
}
