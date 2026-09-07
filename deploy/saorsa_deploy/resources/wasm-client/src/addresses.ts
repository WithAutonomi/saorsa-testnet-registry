/**
 * Address handoff between the WASM uploader and downloader tiers.
 *
 * Deliberately the same mechanism as the native clients: uploaders flush their
 * published addresses through a presigned S3 PUT (minted by `clients provision`,
 * so the VM never signs anything and a clock-skewed client keeps working), and
 * downloaders list and fetch them over unsigned public HTTPS. The WASM tier uses
 * its own key prefix, so its downloaders consume its uploaders' addresses and
 * not the native tier's -- keeping the two comparisons separable.
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

  async record(address: string): Promise<void> {
    await mkdir(dirname(this.opts.addressesFile), { recursive: true });
    await appendFile(this.opts.addressesFile, `${address}\n`);
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

/**
 * Reads addresses published by the WASM uploaders.
 *
 * Unsigned public HTTPS against the public-read addresses bucket, matching
 * `downloader.sh`: the VM signs nothing, so a skewed clock cannot get its
 * requests rejected.
 */
export class AddressSource {
  private cache: string[] = [];
  private lastRefresh = 0;

  constructor(
    private readonly bucket: string,
    private readonly prefix: string,
    private readonly refreshSecs = 60,
    private readonly staticFile?: string,
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
    const collected: string[] = [];
    if (this.staticFile) {
      try {
        const text = await readFile(this.staticFile, "utf8");
        collected.push(...text.split("\n").map((s) => s.trim()).filter(Boolean));
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
          collected.push(...text.split("\n").map((s) => s.trim()).filter(Boolean));
        }
      } catch (e) {
        console.warn(`WARN: address refresh failed: ${String(e)}`);
      }
    }
    if (collected.length > 0) {
      this.cache = [...new Set(collected)];
      this.lastRefresh = Date.now();
    }
  }

  /** A random published address, or undefined while none exist yet. */
  async pick(): Promise<string | undefined> {
    const stale = Date.now() - this.lastRefresh >= this.refreshSecs * 1000;
    if (this.cache.length === 0 || stale) await this.refresh();
    if (this.cache.length === 0) return undefined;
    return this.cache[Math.floor(Math.random() * this.cache.length)];
  }
}

/** Write the local static address list, used when the deploy seeds one. */
export async function writeStaticAddresses(path: string, addresses: string[]): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, `${addresses.join("\n")}\n`);
}
