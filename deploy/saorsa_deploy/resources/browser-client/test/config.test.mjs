/**
 * The harness's configuration, and the two limits it refuses to run outside.
 *
 * Both are checked in the manifest schema and in the `clients infra` spec
 * parser as well. This is the only one of the three that sees what actually
 * reached the VM.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { loadConfig } from "../lib/config.mjs";
import { BROWSER_MAX_INSTANCES } from "../lib/limits.mjs";

const BASE = {
  TESTNET_NAME: "DEV-01",
  REGION: "lon1",
  BOOTSTRAP_WEBRTC_MULTIADDRS: "/ip4/1.2.3.4/udp/10001/webrtc-direct, /ip4/5.6.7.8/udp/10001",
  ADDRESSES_BASE_URL: "https://b.s3.amazonaws.com/saorsa-deploy/addresses/DEV-01",
};

test("the defaults are the manifest's defaults", () => {
  const config = loadConfig(BASE);
  assert.equal(config.instances, 4);
  assert.equal(config.roundPeriodSecs, 600);
  assert.equal(config.downloadTimeoutSecs, 300);
  assert.equal(config.influx.bucket, "wasm-client-metrics");
});

test("seeds are split and trimmed", () => {
  assert.deepEqual(loadConfig(BASE).seeds, [
    "/ip4/1.2.3.4/udp/10001/webrtc-direct",
    "/ip4/5.6.7.8/udp/10001",
  ]);
});

test("more instances than the per-IP session quota is refused", () => {
  assert.throws(
    () => loadConfig({ ...BASE, INSTANCES: String(BROWSER_MAX_INSTANCES + 1) }),
    /WebRTC sessions per source IPv4/,
  );
});

test("a timeout that can outlive its round is refused", () => {
  assert.throws(
    () => loadConfig({ ...BASE, ROUND_PERIOD_SECS: "300", DOWNLOAD_TIMEOUT_SECS: "300" }),
    /must be </,
  );
});

test("a non-integer knob is refused rather than silently defaulted", () => {
  assert.throws(() => loadConfig({ ...BASE, INSTANCES: "four" }), /not an integer/);
});
