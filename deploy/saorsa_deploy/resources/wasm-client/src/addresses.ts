/**
 * Address handoff between the WASM uploader and downloader tiers.
 *
 * Deliberately the same mechanism as the native clients: uploaders flush their
 * published addresses through a presigned S3 PUT (minted by `clients provision`,
 * so the VM never signs anything and a clock-skewed client keeps working), and
 * downloaders list and fetch them over unsigned public HTTPS. Uploaders of both
 * tiers publish under the deployment's one prefix, and this downloader lists
 * the whole prefix, so it draws native-uploaded content as well as WASM.
 *
 * Each published line is `<address> <bytes>`. The size is what lets a
 * downloader with a hard cap -- ant-core rejects a browser-path file above
 * MAX_BROWSER_FILE_BYTES after resolving its data map -- skip an entry it
 * could never download instead of spending a fetch to be told so. A bare line
 * (no size; the pre-change format) is never excluded: unknown is not too big.
 */

import { appendFile, mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

export interface AddressSinkOptions {
  addressesFile: string;
  presignedUrl?: string;
  flushIntervalSecs: number;
  flushEveryN: number;
  tailSize: number;
}

/** Accumulates published addresses and flushes them to S3. */
export class AddressSink {
  private pending = 0;
  private lastFlush = Date.now();

  constructor(private readonly opts: AddressSinkOptions) {}

  async record(address: string, sizeBytes: number): Promise<void> {
    await mkdir(dirname(this.opts.addressesFile), { recursive: true });
    await appendFile(this.opts.addressesFile, `${formatAddressLine(address, sizeBytes)}\n`);
    this.pending += 1;
    const dueByCount = this.pending >= this.opts.flushEveryN;
    const dueByTime = Date.now() - this.lastFlush >= this.opts.flushIntervalSecs * 1000;
    if (dueByCount || dueByTime) await this.flush();
  }

  /** Push the tail of the address file to S3. Never throws. */
  async flush(): Promise<void> {
    if (!this.opts.presignedUrl) return;
    let body: string;
    try {
      const all = (await readFile(this.opts.addressesFile, "utf8")).split("\n").filter(Boolean);
      // Only the tail: the file grows for the life of the run and the presigned
      // PUT replaces the object wholesale each time.
      body = `${all.slice(-this.opts.tailSize).join("\n")}\n`;
    } catch {
      return;
    }
    try {
      const res = await fetch(this.opts.presignedUrl, {
        method: "PUT",
        body,
        headers: { "Content-Type": "text/plain" },
        signal: AbortSignal.timeout(60_000),
      });
      if (!res.ok) {
        console.warn(`WARN: address flush returned ${res.status}`);
        return;
      }
      this.pending = 0;
      this.lastFlush = Date.now();
    } catch (e) {
      console.warn(`WARN: address flush failed: ${String(e)}`);
    }
  }
}

/** One published address and, when the publisher recorded it, its size. */
export interface PublishedAddress {
  address: string;
  /** Uploaded size in bytes; undefined for a bare pre-change line. */
  sizeBytes?: number;
}

/** `<address> <bytes>` -- the line format both tiers publish. */
export function formatAddressLine(address: string, sizeBytes: number): string {
  return `${address} ${sizeBytes}`;
}

/**
 * Parse one published line. Returns undefined for a blank line. A second
 * field that is not a non-negative integer is treated as absent rather than
 * rejecting the address: the address is still usable, only the size is not.
 */
export function parseAddressLine(line: string): PublishedAddress | undefined {
  const fields = line.trim().split(/\s+/);
  const address = fields[0];
  if (!address) return undefined;
  const size = fields[1];
  if (size !== undefined && /^\d+$/.test(size)) {
    return { address, sizeBytes: Number(size) };
  }
  return { address };
}

/**
 * The entries a downloader bounded by `maxBytes` may pick. `maxBytes` of 0
 * means unbounded. An entry with no recorded size is kept.
 */
export function eligibleAddresses(
  entries: readonly PublishedAddress[],
  maxBytes: number,
): PublishedAddress[] {
  if (maxBytes <= 0) return [...entries];
  return entries.filter((e) => e.sizeBytes === undefined || e.sizeBytes <= maxBytes);
}

/**
 * Reads addresses published by the uploaders.
 *
 * Unsigned public HTTPS against the public-read addresses bucket, matching
 * `downloader.sh`: the VM signs nothing, so a skewed clock cannot get its
 * requests rejected.
 */
export class AddressSource {
  private cache: PublishedAddress[] = [];
  private lastRefresh = 0;
  private excludedAtLastRefresh = 0;

  constructor(
    private readonly bucket: string,
    private readonly prefix: string,
    private readonly refreshSecs = 60,
    private readonly staticFile?: string,
    /** Largest file to pick, in bytes; 0 = no bound. */
    private readonly maxBytes = 0,
  ) {}

  private baseUrl(): string {
    return `https://${this.bucket}.s3.amazonaws.com`;
  }

  private async listKeys(): Promise<string[]> {
    const url = `${this.baseUrl()}?list-type=2&prefix=${encodeURIComponent(this.prefix)}`;
    const res = await fetch(url, { signal: AbortSignal.timeout(30_000) });
    if (!res.ok) throw new Error(`S3 list returned ${res.status}`);
    const xml = await res.text();
    return [...xml.matchAll(/<Key>([^<]+)<\/Key>/g)].map((m) => m[1]);
  }

  async refresh(): Promise<void> {
    const collected: PublishedAddress[] = [];
    if (this.staticFile) {
      try {
        const text = await readFile(this.staticFile, "utf8");
        collected.push(...parseAddressLines(text));
      } catch {
        /* optional */
      }
    }
    // Skip the remote listing entirely when no bucket is configured (a
    // local-only run, or a deployment whose addresses bucket is unset).
    // Attempting it would warn on every cycle about a URL that was never
    // meant to exist.
    if (this.bucket) {
      try {
        for (const key of await this.listKeys()) {
          const res = await fetch(`${this.baseUrl()}/${key}`, {
            signal: AbortSignal.timeout(30_000),
          });
          if (!res.ok) continue;
          const text = await res.text();
          collected.push(...parseAddressLines(text));
        }
      } catch (e) {
        console.warn(`WARN: address refresh failed: ${String(e)}`);
      }
    }
    if (collected.length > 0) {
      // Dedupe by address; the last occurrence wins so a republished line
      // that gained a size supersedes an older bare one.
      const byAddress = new Map<string, PublishedAddress>();
      for (const entry of collected) byAddress.set(entry.address, entry);
      const all = [...byAddress.values()];
      this.cache = eligibleAddresses(all, this.maxBytes);
      const excluded = all.length - this.cache.length;
      if (excluded !== this.excludedAtLastRefresh) {
        console.log(
          `addresses: ${this.cache.length} eligible, ${excluded} over the ` +
            `${this.maxBytes}-byte cap skipped`,
        );
        this.excludedAtLastRefresh = excluded;
      }
      this.lastRefresh = Date.now();
    }
  }

  /**
   * A random eligible published address, or undefined while none exist yet
   * (which includes "some are published but every one is over the cap").
   */
  async pick(): Promise<string | undefined> {
    const stale = Date.now() - this.lastRefresh >= this.refreshSecs * 1000;
    if (this.cache.length === 0 || stale) await this.refresh();
    if (this.cache.length === 0) return undefined;
    return this.cache[Math.floor(Math.random() * this.cache.length)].address;
  }
}

function parseAddressLines(text: string): PublishedAddress[] {
  const out: PublishedAddress[] = [];
  for (const line of text.split("\n")) {
    const parsed = parseAddressLine(line);
    if (parsed) out.push(parsed);
  }
  return out;
}

/** Write the local static address list, used when the deploy provides one. */
export async function writeStaticAddresses(path: string, addresses: string[]): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, `${addresses.join("\n")}\n`);
}
