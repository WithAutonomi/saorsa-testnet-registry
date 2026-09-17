/**
 * One-line failure descriptions that keep the reason.
 *
 * A failure is logged as a single journal line, capped so one error cannot
 * flood the journal. The cap used to be a plain prefix, which loses exactly
 * the part that matters for a payment failure: viem front-loads its
 * `RPC Request failed` message with the request body -- the ERC-20 `approve`
 * calldata alone is hundreds of hex characters -- and puts the reason
 * (`Details: HTTP request failed. Status: 429`) at the very end. Measured on
 * DEV-03 run 590 (2026-09-17): the only in-window WASM upload failure was cut
 * at `Estimate Gas Arguments: from: 0x14BE479edAc`, so whether the public RPC
 * throttled or timed out is unrecoverable.
 *
 * So a description is built from the error's structured fields when it has
 * them (viem's `shortMessage` / `details` and the `cause` chain), long hex
 * blobs are elided, and when the line still exceeds the cap it keeps the
 * head AND the tail rather than the head alone.
 */

const DEFAULT_LIMIT = 500;
/** Anything this long is calldata or a signed payload, never an identifier. */
const HEX_BLOB = /0x[0-9a-fA-F]{130,}/g;
const MAX_CAUSE_DEPTH = 5;

type Structured = {
  name?: unknown;
  message?: unknown;
  shortMessage?: unknown;
  details?: unknown;
  cause?: unknown;
};

function asStructured(error: unknown): Structured | null {
  return typeof error === "object" && error !== null ? (error as Structured) : null;
}

function str(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

/**
 * The most specific message an error carries. viem errors expose a one-line
 * `shortMessage` and a `details` string holding the RPC's own reason; a plain
 * `Error` only has `message`. The `cause` chain is walked because viem wraps
 * the transport error (429 / timeout) inside the contract error.
 */
function structuredParts(error: unknown): string[] {
  const parts: string[] = [];
  let current: unknown = error;
  for (let depth = 0; depth < MAX_CAUSE_DEPTH && current !== undefined && current !== null; depth++) {
    const s = asStructured(current);
    if (!s) {
      parts.push(String(current));
      break;
    }
    const head = str(s.shortMessage) ?? str(s.message);
    const name = str(s.name);
    const details = str(s.details);
    let line = head ?? "";
    if (name && name !== "Error" && line && !line.startsWith(name)) line = `${name}: ${line}`;
    if (details && (!line || !line.includes(details))) line = line ? `${line} (details: ${details})` : details;
    if (line) parts.push(line);
    current = s.cause;
  }
  return parts;
}

function collapse(text: string): string {
  return text.replace(HEX_BLOB, (m) => `0x…(${m.length - 2} hex chars)`).replace(/\s+/g, " ").trim();
}

/** Keep both ends: the reason is usually at the end, the context at the start. */
function clip(text: string, limit: number): string {
  if (text.length <= limit) return text;
  const marker = " … ";
  const tail = Math.floor((limit - marker.length) * 0.4);
  const head = limit - marker.length - tail;
  return `${text.slice(0, head)}${marker}${text.slice(text.length - tail)}`;
}

/**
 * Single-line description of `error`, at most `limit` characters, that keeps
 * the RPC's reason when there is one.
 */
export function describeError(error: unknown, limit = DEFAULT_LIMIT): string {
  const s = asStructured(error);
  // viem's String(error) is the long form (request body first); its
  // shortMessage + details + cause chain is the same information in the
  // useful order. A bare Error's String() already equals its message.
  const structured = s !== null && (str(s.shortMessage) || str(s.details) || s.cause !== undefined);
  const text = structured ? structuredParts(error).join(" ← ") : String(error);
  return clip(collapse(text), limit);
}

/** Everything the error carries, for the on-disk record of a failed operation. */
export function fullErrorText(error: unknown): string {
  const lines = [String(error)];
  const stack = asStructured(error) ? (error as Error).stack : undefined;
  if (typeof stack === "string") lines.push(stack);
  const parts = structuredParts(error);
  if (parts.length > 1) lines.push("cause chain:", ...parts.map((p, i) => `  ${i}: ${p}`));
  return `${lines.join("\n")}\n`;
}
