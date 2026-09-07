/**
 * Loading and initialising the ant-core WASM package under Node.js.
 *
 * `ant-core` is built with `wasm-pack --target web`, which produces an ESM
 * module expecting an explicit init. It runs under plain Node -- there is no DOM
 * dependency -- with one exception: it needs a real `RTCPeerConnection`, which
 * Node does not provide. `node-datachannel` supplies one, and it MUST be on
 * `globalThis` before the WASM is initialised.
 *
 * Verified against a live network: HELLO, find_node, a 5 MiB download and a paid
 * upload all succeed from Node with this polyfill.
 */

import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

export interface AntCore {
  BrowserNetworkClient: new (endpoints: unknown) => BrowserNetworkClient;
  BrowserNodeClient: new (endpoint: unknown) => BrowserNodeClient;
  contentAddress(content: Uint8Array): string;
  default(init: { module_or_path: Uint8Array }): Promise<unknown>;
}

export interface PublicFileDescriptor {
  name: string;
  address: string;
  size: number;
  [key: string]: unknown;
}

export interface UploadResult {
  file: PublicFileDescriptor;
  transactionHash?: string;
  storageCostAtto: string;
  records: number;
}

export interface DownloadResult {
  content: Uint8Array;
  hash: string;
  file: PublicFileDescriptor;
}

export interface BrowserNetworkClient {
  uploadPublicFile(
    content: Uint8Array,
    name: string,
    contentType: string,
    paymentNetwork: unknown,
    payForQuotes: unknown,
    onProgress?: ((msg: string) => void) | null,
  ): Promise<UploadResult>;
  downloadPublicFile(
    file: unknown,
    concurrency: number,
    onProgress?: ((msg: string) => void) | null,
  ): Promise<DownloadResult>;
  close(): void;
}

export interface BrowserNodeClient {
  connect(): Promise<void>;
  hello(): Promise<Record<string, unknown>>;
  close(): void;
  readonly peerId: string | undefined;
}

/** Install the WebRTC polyfill. Must run before {@link initAntCore}. */
export async function installWebRtc(): Promise<void> {
  const nd = await import("node-datachannel/polyfill");
  const mod = (nd as { default?: Record<string, unknown> }).default ??
    (nd as unknown as Record<string, unknown>);
  const g = globalThis as unknown as Record<string, unknown>;
  g.RTCPeerConnection = mod.RTCPeerConnection;
  if (mod.RTCSessionDescription) g.RTCSessionDescription = mod.RTCSessionDescription;
  if (mod.RTCIceCandidate) g.RTCIceCandidate = mod.RTCIceCandidate;
  if (typeof g.RTCPeerConnection !== "function") {
    throw new Error("node-datachannel did not provide RTCPeerConnection");
  }
}

export async function initAntCore(wasmDir: string): Promise<AntCore> {
  const jsPath = join(wasmDir, "ant_core.js");
  const wasmPath = join(wasmDir, "ant_core_bg.wasm");
  const mod = (await import(pathToFileURL(jsPath).href)) as unknown as AntCore;
  await mod.default({ module_or_path: await readFile(wasmPath) });
  return mod;
}

/**
 * Read the payment identity from a node's HELLO.
 *
 * Preferred over configuring chain id and contract addresses locally: ant-core
 * checks the payment network against every node it selects, so taking it from
 * the network itself makes a mismatch impossible by construction.
 */
export async function fetchPaymentNetwork(
  core: AntCore,
  endpoint: string,
): Promise<Record<string, unknown>> {
  const node = new core.BrowserNodeClient(endpoint);
  try {
    await node.connect();
    const hello = (await node.hello()) as Record<string, unknown>;
    const payment = (hello.payment ?? hello.payment_network) as Record<string, unknown> | undefined;
    if (!payment) throw new Error("node HELLO carried no payment network");
    return payment;
  } finally {
    try {
      node.close();
    } catch {
      /* already closed */
    }
  }
}
