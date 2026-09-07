/**
 * InfluxDB line-protocol emission.
 *
 * The WASM client writes the SAME `upload` and `download` measurements as the
 * native testnet clients (`resources/scripts/uploader.sh`, `downloader.sh`), so
 * `record-results`, the per-op Postgres tables, `print-uploads` and
 * `deployments compare` all work on WASM operations with no change. The two
 * transports are told apart by the client's ROLE, which the deployment already
 * records per VM -- not by a distinct measurement name.
 *
 * No timestamp is sent, deliberately, matching the native scripts: a
 * clock-skewed VM would otherwise place its points wherever its own clock says.
 */

export interface InfluxTarget {
  url?: string;
  token?: string;
  org: string;
  bucket: string;
}

/** Escape a tag key/value per the line-protocol rules. */
export function escapeTag(value: string): string {
  return value.replace(/([,= ])/g, "\\$1");
}

/** Escape a string field value. */
export function escapeFieldString(value: string): string {
  return value.replace(/(["\\])/g, "\\$1");
}

export type FieldValue = number | string | { int: number };

export function buildLine(
  measurement: string,
  tags: Record<string, string>,
  fields: Record<string, FieldValue>,
): string {
  const tagPart = Object.entries(tags)
    .filter(([, v]) => v !== undefined && v !== "")
    .map(([k, v]) => `${escapeTag(k)}=${escapeTag(v)}`)
    .join(",");
  const fieldPart = Object.entries(fields)
    .map(([k, v]) => {
      if (typeof v === "number") return `${k}=${v}`;
      if (typeof v === "string") return `${k}="${escapeFieldString(v)}"`;
      return `${k}=${Math.trunc(v.int)}i`;
    })
    .join(",");
  return tagPart ? `${measurement},${tagPart} ${fieldPart}` : `${measurement} ${fieldPart}`;
}

/**
 * POST one line to InfluxDB. Never throws: a metrics outage must not stop the
 * client doing the work being measured, exactly as the bash clients behave.
 */
export async function submitMetric(target: InfluxTarget, line: string): Promise<void> {
  if (!target.url || !target.token) return;
  const url =
    `${target.url}/api/v2/write?org=${encodeURIComponent(target.org)}` +
    `&bucket=${encodeURIComponent(target.bucket)}&precision=ns`;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Token ${target.token}`,
        "Content-Type": "text/plain",
      },
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
