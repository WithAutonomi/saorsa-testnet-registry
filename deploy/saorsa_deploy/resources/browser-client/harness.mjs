#!/usr/bin/env node
/**
 * The browser-downloader harness: N headless Chromium instances per VM, all
 * reading one chosen hot file on a shared wall-clock schedule.
 *
 * One process per VM, running for the life of the deployment. Each round:
 *
 *   1. read the hot-file manifest (fresh, every round — the operator may
 *      replace it);
 *   2. for each of INSTANCES instances concurrently, launch a FRESH Chromium,
 *      a fresh context and a fresh page, and do one download;
 *   3. append that download's trace to the forwarded trace log (telegraf ships
 *      it to the testnet's Elasticsearch) and write one `browser_download`
 *      point to InfluxDB.
 *
 * Nothing persists between rounds — not a browser, not a profile, not a peer
 * cache — because the measurement is what a *new* browser client experiences,
 * which is what a real user is.
 *
 * The orchestration lives here and the arithmetic, the classification and the
 * derived fields live in `lib/`, where `node --test` can reach them without a
 * browser.
 */

import { chromium } from "playwright";

import { classifyError, errorText } from "./lib/classify.mjs";
import { loadConfig } from "./lib/config.mjs";
import { deriveFields } from "./lib/derive.mjs";
import { fetchHotFiles } from "./lib/hotfiles.mjs";
import { buildPoint, submitMetric } from "./lib/influx.mjs";
import { makeLogger, summaryLine } from "./lib/logger.mjs";
import { downloadInPage } from "./lib/page.mjs";
import { hotFileIndexFor, nextBoundaryAfter, roundIdAt } from "./lib/rounds.mjs";
import { startBundleServer } from "./lib/server.mjs";
import { appendTrace, traceLines } from "./lib/trace.mjs";

const config = loadConfig();
const log = makeLogger({ forwardLogs: config.forwardLogs });
const HOST = process.env.HOSTNAME_OVERRIDE || (await import("node:os")).hostname();

/** Instances still running from the previous round, by instance number. */
const running = new Set();

/**
 * How long past the in-page deadline the harness waits before abandoning the
 * page. Enough for the in-page race to resolve and return its trace through
 * Playwright on a loaded VM, short enough that a hung round cannot eat the
 * next one (`download_timeout_secs < round_period_secs` leaves the slack).
 */
const PAGE_GRACE_MS = 15_000;

async function emit(result) {
  log.log(summaryLine(result));
  await submitMetric(config.influx, buildPoint(result));
}

function baseResult(round, instance) {
  return {
    host: HOST,
    testnetName: config.testnetName,
    region: config.region,
    roundId: round.roundId,
    instance,
    hotFileIndex: round.hotFileIndex,
    seeds: config.seeds.join(","),
    // The round's boundary in nanoseconds, as a BigInt: 1.66e18 is two orders
    // of magnitude past Number.MAX_SAFE_INTEGER, so a Number here would round
    // every recorded start to the nearest 256 ns.
    startNs: BigInt(round.boundary) * 1_000_000_000n,
    startedLateMs: Date.now() - round.boundary * 1000,
  };
}

/** One instance's download for one round. Always emits exactly one point. */
async function runInstance(round, instance, bundleUrl) {
  const result = baseResult(round, instance);

  if (!round.file) {
    // No selection yet. Recorded rather than failed, and no browser launched:
    // the cohort legitimately exists before the operator has chosen, and those
    // rounds are part of the series.
    await emit({ ...result, status: "skipped", errorKind: "no_hot_files" });
    return;
  }

  result.address = round.file.address;
  result.fileSize = round.file.bytes || 0;

  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext();
    const page = await context.newPage();
    page.on("console", (msg) => log.log(`page[${instance}]: ${msg.text()}`));
    await page.goto(bundleUrl, { waitUntil: "load" });

    // The deadline is measured from the round BOUNDARY, not from now: it is a
    // round's budget, and an instance that started late has already spent part
    // of it. A round whose budget is gone before the browser is up still emits
    // its point, which is what makes a missing point a real finding.
    const remainingMs = round.boundary * 1000 + config.downloadTimeoutSecs * 1000 - Date.now();
    const timeoutMs = Math.max(remainingMs, 1000);
    // Two deadlines, and the inner one is meant to win. The in-page race (see
    // `downloadInPage`) returns a result WITH its trace, which is the whole
    // value of the round. But it only covers the download: a `connect()` or a
    // module import that never settles would leave `page.evaluate` pending
    // forever, and the `running` guard would then turn every later round for
    // this instance into a `round_overrun` skip — one hung dial silently ending
    // the instance's contribution for the life of the deployment. So the outer
    // one is a backstop with a grace period, and it gives up the trace to keep
    // the instance.
    const outcome = await Promise.race([
      page.evaluate(downloadInPage, {
        bundleUrl,
        address: round.file.address,
        expectedBytes: round.file.bytes || 0,
        seeds: config.seeds,
        timeoutMs,
      }),
      new Promise((resolve) =>
        setTimeout(
          () => resolve({ ok: false, error: "harness deadline (page unresponsive)", trace: [] }),
          timeoutMs + PAGE_GRACE_MS,
        ),
      ),
    ]);

    const derived = deriveFields(outcome.trace || []);
    // Appended, not written per download: the records are forwarded to the
    // run's Elasticsearch as they are produced, and `analysis browser-reads`
    // reads them from there while the testnet is still up.
    //
    // A failure here must not fail the ROUND: the download happened and its
    // point carries the real result, where letting this throw would hand the
    // classifier a full disk and record it as a network failure. Warned rather
    // than swallowed, because the records simply being absent is what the
    // report would otherwise see.
    try {
      await appendTrace(
        config.traceLog,
        traceLines({
          round,
          instance,
          file: round.file,
          seeds: config.seeds.join(","),
          records: outcome.trace || [],
        }),
      );
    } catch (e) {
      log.warn(
        `round=${round.roundId} instance=${instance} ` +
          `WARN: the trace could not be appended to ${config.traceLog}: ` +
          `${String((e && e.message) || e)}. The round's point is unaffected, ` +
          "but its records will be missing from the per-chunk report.",
      );
    }
    if (outcome.traceHook === false) {
      log.warn(
        `round=${round.roundId} instance=${instance} ` +
          "WARN: the bundle has no setBrowserTrace — every derived field is 0 " +
          "because the trace is empty, not because the network was clean. " +
          "Rebuild the bundle with the test-utils feature.",
      );
    }
    await emit({
      ...result,
      status: outcome.ok ? "success" : "failure",
      errorKind: outcome.ok ? "" : classifyError(outcome.error),
      errorText: outcome.ok ? "" : errorText(outcome.error),
      totalMs: outcome.totalMs,
      connectMs: outcome.connectMs,
      stalls: derived.stalls,
      refusedDials: derived.refused_dials,
      firstReads: derived.first_reads,
    });
  } catch (e) {
    // A browser that would not launch, a page that would not load, a crashed
    // renderer: all of them are this instance's round failing, and all of them
    // get a point. The classifier sees the message, so a launch failure reads
    // as `other` rather than being silently attributed to the network.
    await emit({
      ...result,
      status: "failure",
      errorKind: classifyError(e),
      errorText: errorText(e),
    });
  } finally {
    try {
      if (browser) await browser.close();
    } catch {
      /* a browser that cannot be closed must not stop the next round */
    }
  }
}

async function runRound(round, bundleUrl) {
  await Promise.all(
    Array.from({ length: config.instances }, (_, i) => i + 1).map(async (instance) => {
      if (running.has(instance)) {
        // Cannot happen while download_timeout_secs < round_period_secs, which
        // both the schema and the spec parser enforce — but the guard is one
        // `Set` and the alternative is two browsers per instance quietly
        // doubling this VM's session count against every node.
        await emit({
          ...baseResult(round, instance),
          status: "skipped",
          errorKind: "round_overrun",
        });
        return;
      }
      running.add(instance);
      try {
        await runInstance(round, instance, bundleUrl);
      } finally {
        running.delete(instance);
      }
    }),
  );
}

async function main() {
  if (config.seeds.length === 0) {
    throw new Error(
      "BOOTSTRAP_WEBRTC_MULTIADDRS is empty — a browser client has no other " +
        "way into the network. Run `deployments webrtc-addresses` and " +
        "re-provision.",
    );
  }
  if (!config.addressesBaseUrl) {
    // Refused rather than left to fail per round: without it every round is a
    // `no_hot_files` skip, which is indistinguishable from the operator not
    // having selected yet — so the VM would look like it was waiting for a
    // person when it was in fact misprovisioned.
    throw new Error(
      "ADDRESSES_BASE_URL is empty — the harness has nowhere to read the " +
        "hot-file manifest from. Re-run `clients provision`.",
    );
  }
  await startBundleServer(config.bundleDir, config.bundlePort);
  const bundleUrl = `http://127.0.0.1:${config.bundlePort}`;
  log.log(
    `browser-downloader up: ${config.instances} instance(s), ` +
      `round ${config.roundPeriodSecs}s, timeout ${config.downloadTimeoutSecs}s, ` +
      `${config.seeds.length} seed(s), bundle ${bundleUrl}`,
  );

  for (;;) {
    const nowSecs = Date.now() / 1000;
    const boundary = nextBoundaryAfter(nowSecs, config.roundPeriodSecs);
    await new Promise((resolve) => setTimeout(resolve, Math.max(boundary * 1000 - Date.now(), 0)));

    const roundId = roundIdAt(boundary, config.roundPeriodSecs);
    const manifest = await fetchHotFiles(config.addressesBaseUrl);
    const files = manifest ? manifest.files : [];
    const hotFileIndex = hotFileIndexFor(roundId, files.length);
    const round = {
      roundId,
      boundary,
      hotFileIndex,
      file: hotFileIndex === undefined ? undefined : files[hotFileIndex],
    };
    await runRound(round, bundleUrl);
  }
}

main().catch((e) => {
  log.warn(`FATAL: ${String((e && e.stack) || e)}`);
  process.exit(1);
});
