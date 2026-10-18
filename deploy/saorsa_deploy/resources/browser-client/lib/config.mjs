/**
 * The harness's configuration, which is entirely its environment file.
 *
 * Written by `clients provision` from the manifest entry, so every knob an
 * operator set has exactly one hop to get here and nothing else tells the
 * harness what to do.
 */

import { BROWSER_MAX_INSTANCES } from "./limits.mjs";

function int(env, name, fallback) {
  const raw = env[name];
  if (raw === undefined || raw === "") return fallback;
  const value = Number.parseInt(raw, 10);
  if (!Number.isFinite(value)) throw new Error(`${name} is not an integer: ${raw}`);
  return value;
}

export function loadConfig(env = process.env) {
  const instances = int(env, "INSTANCES", 4);
  if (instances < 1 || instances > BROWSER_MAX_INSTANCES) {
    // Refused here as well as in the manifest schema and the spec parser: this
    // is the only one of the three that sees what actually reached the VM, and
    // an over-quota cohort measures the per-IP session limit rather than the
    // read path.
    throw new Error(
      `INSTANCES must be between 1 and ${BROWSER_MAX_INSTANCES}, got ${instances}: ` +
        "a node admits at most that many WebRTC sessions per source IPv4 address.",
    );
  }
  const roundPeriodSecs = int(env, "ROUND_PERIOD_SECS", 600);
  const downloadTimeoutSecs = int(env, "DOWNLOAD_TIMEOUT_SECS", 300);
  if (downloadTimeoutSecs >= roundPeriodSecs) {
    throw new Error(
      `DOWNLOAD_TIMEOUT_SECS (${downloadTimeoutSecs}) must be < ` +
        `ROUND_PERIOD_SECS (${roundPeriodSecs}).`,
    );
  }
  return {
    testnetName: env.TESTNET_NAME || "",
    hostRole: env.HOST_ROLE || "browser_downloader",
    region: env.REGION || "",
    instances,
    roundPeriodSecs,
    downloadTimeoutSecs,
    seeds: (env.BOOTSTRAP_WEBRTC_MULTIADDRS || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
    addressesBaseUrl: env.ADDRESSES_BASE_URL || "",
    influx: {
      url: env.INFLUX_URL || "",
      token: env.INFLUX_TOKEN || "",
      org: env.INFLUX_ORG || "ant",
      bucket: env.INFLUX_BUCKET || "wasm-client-metrics",
    },
    traceDir: env.TRACE_DIR || "/var/lib/ant-browser-downloader/trace",
    forwardLogs: env.FORWARD_LOGS === "true",
    bundleDir: env.BUNDLE_DIR || "/opt/ant-browser-downloader/bundle",
    bundlePort: int(env, "BUNDLE_PORT", 8723),
  };
}
