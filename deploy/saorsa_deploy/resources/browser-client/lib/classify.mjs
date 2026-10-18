/**
 * Short, stable classification of a browser-path download failure, for the
 * `error_kind` field of the `browser_download` measurement.
 *
 * Deliberately its own classifier rather than a reuse of the WASM client's: the
 * kinds differ. `busy` is new and is the one this role exists to see — a node
 * refusing a read because its holders are saturated is the behaviour ADR-0023
 * changes — and there is no `over_cap` here, because the file is chosen by the
 * operator from the deployment's own pool rather than drawn at random.
 *
 * The order is load-bearing and is the order the issue specifies: a message can
 * satisfy several patterns at once ("payment quote peers are busy"), and the
 * first match wins.
 */

export const ERROR_KINDS = [
  "busy",
  "not_found",
  "payment",
  "quorum",
  "timeout",
  "connection",
  "other",
];

/** The harness's own deadline message, matched before any network pattern. */
export const HARNESS_DEADLINE = "harness deadline";

export function classifyError(error) {
  const msg = String(error && error.message ? error.message : error);
  if (/are busy/i.test(msg)) return "busy";
  if (/not found/i.test(msg)) return "not_found";
  if (/payment/i.test(msg)) return "payment";
  if (/quorum/i.test(msg)) return "quorum";
  // The harness deadline is reported as `timeout` with this exact text; a
  // client-side timeout message lands here too, which is the same finding.
  if (new RegExp(HARNESS_DEADLINE, "i").test(msg) || /timeout|timed out/i.test(msg)) {
    return "timeout";
  }
  if (/connect|dial|channel/i.test(msg)) return "connection";
  return "other";
}

/**
 * `error_text` as it is written to Influx.
 *
 * Verbatim, truncated: the string is the only record of what the client
 * actually said, and ant-core messages can carry a whole chunk address list.
 * Influx would take a much longer string, but a field nobody can read in a
 * table is not worth the series size.
 */
export const ERROR_TEXT_MAX = 2000;

export function errorText(error) {
  const msg = String(error && error.message ? error.message : error);
  return msg.length > ERROR_TEXT_MAX ? msg.slice(0, ERROR_TEXT_MAX) : msg;
}
