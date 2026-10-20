/**
 * The per-download trace, rendered as self-describing lines on ONE forwarded
 * file (V2-1650).
 *
 * It used to be one `<round>-<instance>.jsonl` file per download under
 * `/var/lib/ant-browser-downloader/trace/`, fetched with `scp` after the run.
 * On DEV-01 (registry id 685) that fetch took 2 h 47 min to pull 2,676 files
 * (2.5 GB) off 35 VMs — long enough to push the run past its ceiling and let
 * the node fleet fill its disks. The VM already forwards its logs to the
 * testnet's Elasticsearch, so the records go there instead, as they are
 * produced, and `analysis browser-reads` queries them before teardown.
 *
 * Two properties make that work and neither is cosmetic:
 *
 * * **Every line is self-describing.** The file name used to carry the round
 *   and the instance; a shared append-only file carries nothing, so each line
 *   repeats them along with the file address and the round boundary. A document
 *   in Elasticsearch has no neighbours it can be read in the context of.
 * * **`seq` is unique within a download**, from 0 (the header) upwards. The
 *   report pages the records with `search_after` on
 *   `[round_id, host, instance, seq]`, and a tie in that tuple would silently
 *   drop a document — so the header takes 0 and the records follow from 1.
 *
 * The `seeds` string is written once, as the header record: it is ~600 bytes
 * and repeating it on every one of a download's ~3,000 lines would be most of
 * the traffic.
 */

import { mkdir, open, stat, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

import { FORWARD_LOG_MAX_BYTES } from "./logger.mjs";

/**
 * One download's records as JSON lines, header first.
 *
 * The header is a record like any other (`operation: "header"`, `seq: 0`) so
 * the reader needs no special case: a document whose operation is not one the
 * report asks for is simply not matched.
 */
export function traceLines({
  round,
  instance,
  file,
  seeds,
  records,
  startedAt = Math.floor(Date.now() / 1000),
}) {
  const common = {
    round_id: round.roundId,
    instance,
    hot_file_index: round.hotFileIndex,
    address: file ? file.address : "",
    boundary: round.boundary,
    started_at: startedAt,
  };
  const lines = [JSON.stringify({ ...common, seq: 0, operation: "header", seeds })];
  for (const [i, record] of (records || []).entries()) {
    lines.push(
      JSON.stringify({
        ...common,
        seq: i + 1,
        id: record.id,
        operation: record.operation,
        target: record.target,
        peer: record.peer,
        phase: record.phase,
        elapsed_ms: record.elapsed_ms,
        detail: record.detail,
      }),
    );
  }
  return lines;
}

/**
 * Appends are serialised across the whole process.
 *
 * `runInstance` runs INSTANCES downloads concurrently under one `Promise.all`,
 * so without this their appends overlap — and a block is far too big to be one
 * write. On DEV-01 (id 685) every one of 2,676 per-download traces was between
 * 599 KB and 1.30 MB (median 942 KB), so each append is several
 * `write(2)` calls whichever API issues them, and two of them overlapping cut
 * a line of each in half at the boundary. Telegraf's JSON parser then drops
 * both halves and nothing reports it: a steady trickle of lost records, some of
 * them the `rpc finish` carrying `found` that the holder definition keys on.
 *
 * The chain carries no rejection forward, so an append that fails cannot stop
 * the next round's from being queued; the caller still sees its own error.
 */
let appendQueue = Promise.resolve();

/**
 * Append a download's lines to the forwarded trace file.
 *
 * **Serialised, and written through one `filehandle.write` of the whole block**
 * rather than `appendFile`. `fs/promises` splits a buffer into 512 KiB
 * (`kWriteFileMaxChunkSize`) chunks and awaits each one, which is what makes a
 * ~1 MB block several interleavable writes. One `write` on a handle opened
 * `"a"` is one `write(2)`, which `O_APPEND` makes atomic against other writers
 * on the same file; the loop exists only for a short write, which cannot
 * interleave here because the queue above means there is no concurrent writer
 * in this process.
 *
 * Rotation is the coarse rule the native roles and `pointer_client.py` use —
 * truncated wholesale once past the cap, checked BEFORE the append so the block
 * that crosses it is kept whole. A real rotation would need the forwarder
 * restarted to re-open the new inode.
 */
export function appendTrace(path, lines, maxBytes = FORWARD_LOG_MAX_BYTES) {
  const run = appendQueue.then(() => appendNow(path, lines, maxBytes));
  appendQueue = run.then(
    () => undefined,
    () => undefined,
  );
  return run;
}

async function appendNow(path, lines, maxBytes) {
  await mkdir(dirname(path), { recursive: true });
  try {
    const info = await stat(path);
    if (info.size > maxBytes) await writeFile(path, "");
  } catch {
    /* no file yet: nothing to rotate */
  }
  const buffer = Buffer.from(`${lines.join("\n")}\n`);
  const handle = await open(path, "a");
  try {
    let written = 0;
    while (written < buffer.length) {
      const { bytesWritten } = await handle.write(buffer, written, buffer.length - written);
      if (bytesWritten <= 0) {
        // Rather than spin: a write that moves nothing means the file cannot
        // take the rest, and the caller warns and keeps the round's point.
        throw new Error(`wrote ${written} of ${buffer.length} bytes to ${path}`);
      }
      written += bytesWritten;
    }
  } finally {
    await handle.close();
  }
}
