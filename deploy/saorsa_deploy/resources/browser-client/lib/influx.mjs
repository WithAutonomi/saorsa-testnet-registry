/**
 * InfluxDB line-protocol emission for the `browser_download` measurement.
 *
 * A measurement of its own, in the `wasm-client-metrics` bucket beside the WASM
 * roles' operations. NOT the existing `download` measurement: a synchronised
 * cohort hammering one file and a single client drawing random files are two
 * unlike workloads, and sharing a series would let any panel average them.
 *
 * `status` is a STRING field here, unlike the prod download kinds' integer:
 * nothing in Postgres reads this measurement yet (that is the persistence
 * issue's job), so there is no `OpRecordStatus` for it to agree with, and a
 * human reading the bucket gets `success` rather than `0`.
 *
 * No timestamp is sent, matching every other client here: a clock-skewed VM
 * would otherwise place its points wherever its own clock says — and this
 * role's whole point is when the read happened.
 */

export const MEASUREMENT = "browser_download";

export function escapeTag(value) {
  return String(value).replace(/([,= ])/g, "\\$1");
}

export function escapeFieldString(value) {
  return String(value).replace(/(["\\])/g, "\\$1");
}

/**
 * Build one line.
 *
 * Integer fields are passed as `{int: n}` so they carry the `i` suffix: Influx
 * types a field on first write and drops every later point that disagrees, so a
 * field written as a float once is a float forever.
 *
 * A **BigInt** is the route for a value that does not fit a JS Number.
 * `start_ns` is nanoseconds since the epoch — about 1.66e18, two hundred times
 * `Number.MAX_SAFE_INTEGER` — so passing it as a Number silently rounds it to
 * the nearest multiple of 256 ns and every round's recorded start is a lie.
 */
export function buildLine(measurement, tags, fields) {
  const tagPart = Object.entries(tags)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${escapeTag(k)}=${escapeTag(v)}`)
    .join(",");
  const fieldPart = Object.entries(fields)
    .filter(([, v]) => v !== undefined && v !== null)
    .map(([k, v]) => {
      if (typeof v === "number") return `${k}=${v}`;
      if (typeof v === "string") return `${k}="${escapeFieldString(v)}"`;
      if (typeof v === "bigint") return `${k}=${v}i`;
      if (typeof v === "object" && "int" in v) return `${k}=${Math.trunc(v.int)}i`;
      return `${k}="${escapeFieldString(String(v))}"`;
    })
    .join(",");
  return tagPart ? `${measurement},${tagPart} ${fieldPart}` : `${measurement} ${fieldPart}`;
}

/**
 * POST one line. Never throws: a metrics outage must not stop the cohort doing
 * the work being measured, exactly as the other clients here behave.
 */
export async function submitMetric(target, line) {
  if (!target.url || !target.token) return;
  const url =
    `${target.url}/api/v2/write?org=${encodeURIComponent(target.org)}` +
    `&bucket=${encodeURIComponent(target.bucket)}&precision=ns`;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { Authorization: `Token ${target.token}`, "Content-Type": "text/plain" },
      body: line,
      signal: AbortSignal.timeout(15_000),
    });
    if (!res.ok) {
      console.warn(`WARN: InfluxDB write returned ${res.status}: ${await res.text()}`);
    }
  } catch (e) {
    console.warn(`WARN: failed to submit metric to InfluxDB: ${String(e)}`);
  }
}

/**
 * The point for one download, as tags plus fields.
 *
 * One point per instance per round, ALWAYS — including the rounds that did
 * nothing. A round with no point from an instance is itself a finding (the
 * harness was not running, or the VM was gone), and that reading only works if
 * a skip is a point rather than a silence.
 */
export function buildPoint(result) {
  const tags = {
    host: result.host,
    testnet_name: result.testnetName,
    host_role: "browser_downloader",
    region: result.region,
    round_id: String(result.roundId),
    instance: String(result.instance),
    // The hot file this round read. Absent on a skip that never chose one, and
    // an empty tag is dropped rather than written blank.
    hot_file_index: result.hotFileIndex === undefined ? "" : String(result.hotFileIndex),
  };
  const fields = {
    status: result.status,
    error_kind: result.errorKind || "",
    error_text: result.errorText || "",
    address: result.address || "",
    file_size: { int: result.fileSize || 0 },
    total_ms: { int: Math.round(result.totalMs || 0) },
    connect_ms: { int: Math.round(result.connectMs || 0) },
    // A BigInt: nanoseconds since the epoch overflow a JS Number's exact
    // integer range by two orders of magnitude.
    start_ns: result.startNs === undefined ? 0n : BigInt(result.startNs),
    started_late_ms: { int: Math.round(result.startedLateMs || 0) },
    seeds: result.seeds || "",
    stalls: { int: result.stalls || 0 },
    refused_dials: { int: result.refusedDials || 0 },
    // A JSON array as a string field. It is per-chunk and variable-length, so
    // it cannot be tags (unbounded cardinality) or numeric fields; the
    // persistence issue unpacks it into rows.
    first_reads: JSON.stringify(result.firstReads || []),
  };
  return buildLine(MEASUREMENT, tags, fields);
}
