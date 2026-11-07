/**
 * The harness's configuration, which is entirely its environment file.
 *
 * Written by `clients provision` from the manifest entry, so every knob an
 * operator set has exactly one hop to get here and nothing else tells the
 * harness what to do.
 */

import { BROWSER_MAX_INSTANCES } from "./limits.mjs";
import { ROUND_PARITIES, ROUND_PARITY_ALL, SEED_TAIL_GAP_SECS } from "./rounds.mjs";

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
  const roundParity = env.ROUND_PARITY || ROUND_PARITY_ALL;
  if (!ROUND_PARITIES.includes(roundParity)) {
    throw new Error(
      `ROUND_PARITY must be one of ${ROUND_PARITIES.join(", ")}, got '${roundParity}'.`,
    );
  }
  const startStaggerSecs = int(env, "START_STAGGER_SECS", 0);
  if (startStaggerSecs < 0) {
    throw new Error(
      `START_STAGGER_SECS must be >= 0 (0 = every instance starts on the ` +
        `boundary), got ${startStaggerSecs}.`,
    );
  }
  // The same two bounds the manifest schema and the spec parser apply, checked
  // here as well because this is the only one of the three that sees what
  // actually reached the VM. A seed window shorter than the deadline would shut
  // a page that was still downloading; one that runs into the next boundary
  // would leave this instance's WebRTC sessions open when the next round's
  // browser dials, making it the fifth session from this address.
  const seedUntilSecs = int(env, "SEED_UNTIL_SECS", 0);
  if (seedUntilSecs < 0) {
    throw new Error(`SEED_UNTIL_SECS must be >= 0 (0 = no seeding), got ${seedUntilSecs}.`);
  }
  if (seedUntilSecs > 0) {
    if (seedUntilSecs < downloadTimeoutSecs) {
      throw new Error(
        `SEED_UNTIL_SECS (${seedUntilSecs}) must be >= DOWNLOAD_TIMEOUT_SECS ` +
          `(${downloadTimeoutSecs}): a page cannot stop seeding before its own ` +
          `download was allowed to finish.`,
      );
    }
    if (seedUntilSecs >= roundPeriodSecs - SEED_TAIL_GAP_SECS) {
      throw new Error(
        `SEED_UNTIL_SECS (${seedUntilSecs}) must be < ROUND_PERIOD_SECS - ` +
          `${SEED_TAIL_GAP_SECS} (${roundPeriodSecs - SEED_TAIL_GAP_SECS}): a page ` +
          `still seeding when the next round launches holds a session the next ` +
          `instance needs.`,
      );
    }
  }
  return {
    testnetName: env.TESTNET_NAME || "",
    hostRole: env.HOST_ROLE || "browser_downloader",
    region: env.REGION || "",
    // 1-based, as `clients infra` allocates it. Load-bearing: it is half of
    // every instance's start offset, so a VM that did not receive its own index
    // would start its whole cohort of instances on the boundary together.
    clientIndex: int(env, "CLIENT_INDEX", 1),
    instances,
    roundPeriodSecs,
    downloadTimeoutSecs,
    roundParity,
    startStaggerSecs,
    seedUntilSecs,
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
    // ONE append-only file under the directory telegraf tails, not a file per
    // download under /var/lib: the records go to the run's Elasticsearch as
    // they are produced, so nothing has to be fetched off the VM afterwards.
    traceLog: env.TRACE_LOG || "/var/log/ant-client-forward/browser-trace.jsonl",
    forwardLogs: env.FORWARD_LOGS === "true",
    bundleDir: env.BUNDLE_DIR || "/opt/ant-browser-downloader/bundle",
    bundlePort: int(env, "BUNDLE_PORT", 8723),
  };
}
