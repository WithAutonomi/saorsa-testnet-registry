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

test("the scheduling knobs default to the pre-INFRA-532 behaviour", () => {
  // Every existing cohort runs every round, starts on the boundary and closes
  // its page the moment its download ends.
  const config = loadConfig(BASE);
  assert.equal(config.roundParity, "all");
  assert.equal(config.startStaggerSecs, 0);
  assert.equal(config.seedUntilSecs, 0);
  assert.equal(config.clientIndex, 1);
});

test("the three knobs are read from the env file", () => {
  const config = loadConfig({
    ...BASE,
    CLIENT_INDEX: "3",
    ROUND_PARITY: "even",
    START_STAGGER_SECS: "2",
    SEED_UNTIL_SECS: "480",
    DOWNLOAD_TIMEOUT_SECS: "420",
  });
  assert.equal(config.clientIndex, 3);
  assert.equal(config.roundParity, "even");
  assert.equal(config.startStaggerSecs, 2);
  assert.equal(config.seedUntilSecs, 480);
});

test("an unknown parity is refused rather than silently taken as `all`", () => {
  assert.throws(() => loadConfig({ ...BASE, ROUND_PARITY: "EVEN" }), /ROUND_PARITY must be one of/);
  assert.throws(() => loadConfig({ ...BASE, ROUND_PARITY: "third" }), /ROUND_PARITY must be one of/);
});

test("a negative stagger is refused", () => {
  assert.throws(() => loadConfig({ ...BASE, START_STAGGER_SECS: "-1" }), /must be >= 0/);
});

test("a seed window shorter than the deadline is refused", () => {
  // It would shut a page that was still downloading, which is a failure the
  // cohort would read as the network's.
  assert.throws(
    () => loadConfig({ ...BASE, DOWNLOAD_TIMEOUT_SECS: "420", SEED_UNTIL_SECS: "400" }),
    /must be >= DOWNLOAD_TIMEOUT_SECS/,
  );
});

test("a seed window that runs into the next round is refused", () => {
  // A page still seeding when the next round launches holds a session the next
  // instance needs, and every node refuses the fifth from one address.
  assert.throws(
    () => loadConfig({ ...BASE, ROUND_PERIOD_SECS: "600", SEED_UNTIL_SECS: "590" }),
    /must be < ROUND_PERIOD_SECS - 30/,
  );
  assert.throws(
    () => loadConfig({ ...BASE, ROUND_PERIOD_SECS: "600", SEED_UNTIL_SECS: "570" }),
    /must be < ROUND_PERIOD_SECS - 30/,
  );
});

test("the widest legal seed window is accepted", () => {
  const config = loadConfig({
    ...BASE,
    ROUND_PERIOD_SECS: "600",
    DOWNLOAD_TIMEOUT_SECS: "420",
    SEED_UNTIL_SECS: "569",
  });
  assert.equal(config.seedUntilSecs, 569);
});

test("the two seed bounds do not apply when seeding is off", () => {
  // 0 is "close as soon as the download ends", so neither bound is meaningful.
  const config = loadConfig({ ...BASE, DOWNLOAD_TIMEOUT_SECS: "420", SEED_UNTIL_SECS: "0" });
  assert.equal(config.seedUntilSecs, 0);
});
