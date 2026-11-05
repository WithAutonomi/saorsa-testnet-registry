/**
 * Short, stable classification of a transfer failure, for the `error_kind`
 * field.
 *
 * Shared by the long-running harness (`main.ts`) and the single-operation CLI
 * (`cli.ts`) so the two can never disagree about what a failure was. Two
 * consumers reading the same `error_kind` out of the same Influx measurement
 * and Postgres column is the whole point of the field.
 */

export type ErrorKind =
  | "payment"
  | "quorum"
  | "timeout"
  | "connection"
  | "not_found"
  | "over_cap"
  | "other";

/**
 * ant-core's refusal of an UPLOAD above `MAX_BROWSER_FILE_BYTES`, raised in
 * `ant-core/src/browser.rs` before the file reaches the network. Checked FIRST
 * and on its own: it is the one failure that is not a failure of the network —
 * this transport simply will not carry the file. Everything downstream records
 * it as a skip, so misclassifying it as `other` would show up as a real
 * failure.
 *
 * It cannot arise on a DOWNLOAD: ant-client#214 removed the browser download
 * cap. The pattern used to be `invalid public file size`, which is not the cap
 * at all but `browser/manifest.rs`'s self-encryption / `MAX_SAFE_JS_INTEGER`
 * bounds check — so an ordinary manifest-validation failure was reported as an
 * over-cap skip and dropped from every success-rate denominator (INFRA-511).
 */
const OVER_CAP = /browser files are limited to/i;

export function classifyError(error: unknown): ErrorKind {
  const msg = String(error);
  if (OVER_CAP.test(msg)) return "over_cap";
  if (/payment|quote|insufficient|funds/i.test(msg)) return "payment";
  if (/peers|quorum|close group|witness/i.test(msg)) return "quorum";
  if (/timeout|timed out/i.test(msg)) return "timeout";
  if (/connect|channel|transport|webrtc|dial/i.test(msg)) return "connection";
  if (/not found|missing|record/i.test(msg)) return "not_found";
  return "other";
}
