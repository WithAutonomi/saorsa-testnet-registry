/**
 * The hot-file manifest, read at every round start.
 *
 * Fetched fresh each round rather than cached: the operator may replace the
 * selection mid-run (`clients select-hot-files --replace`), and a cohort that
 * cached it would split into VMs reading the old file and VMs reading the new
 * one — which would be indistinguishable in the data from the read spreading
 * being measured.
 *
 * Unsigned HTTPS against the public-read addresses bucket, matching the native
 * downloader: the VM signs nothing, so a skewed clock cannot have its requests
 * rejected.
 */

/** Written by `clients select-hot-files`. */
export const HOT_FILES_OBJECT_NAME = "browser-hot-files.json";

/**
 * The manifest, or null when the operator has not selected yet.
 *
 * Null is a normal state, not an error: a cohort provisioned by `deployments
 * up` exists before any upload does, so the rounds before selection are
 * recorded (`status=skipped`, `error_kind=no_hot_files`) rather than failed.
 * A malformed or unreachable manifest is also null, with a warning — the same
 * handling, because the harness cannot read a file either way and the next
 * round tries again.
 */
export async function fetchHotFiles(baseUrl) {
  const url = `${baseUrl.replace(/\/$/, "")}/${HOT_FILES_OBJECT_NAME}`;
  let text;
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(30_000) });
    if (res.status === 404 || res.status === 403) return null;
    if (!res.ok) {
      console.warn(`WARN: hot-file manifest fetch returned ${res.status}`);
      return null;
    }
    text = await res.text();
  } catch (e) {
    console.warn(`WARN: hot-file manifest fetch failed: ${String(e)}`);
    return null;
  }
  return parseHotFiles(text);
}

/** Parse and sanity-check a manifest body. Returns null when it is unusable. */
export function parseHotFiles(text) {
  let doc;
  try {
    doc = JSON.parse(text);
  } catch (e) {
    console.warn(`WARN: hot-file manifest is not JSON: ${String(e)}`);
    return null;
  }
  const files = Array.isArray(doc && doc.files) ? doc.files : [];
  // Entries missing an address are dropped rather than taken as a hole in the
  // cycle: `hot_file_index` is an index into THIS list, so every VM has to
  // derive the same list from the same bytes.
  const usable = files.filter((f) => f && typeof f.address === "string" && f.address);
  if (usable.length === 0) return null;
  return { ...doc, files: usable };
}
