/**
 * Round arithmetic.
 *
 * Rounds are anchored to the wall clock rather than to an interval slept after
 * each one, for the same reason the prod continuous downloader's cycle is: a
 * sleep taken after each round moves the start by the round's own duration, so
 * two VMs that started minutes apart would drift apart indefinitely — and the
 * whole measurement is "every instance in the cohort reads the same file at the
 * same moment". With the anchor, a VM that restarts mid-run rejoins the cohort
 * at the next boundary with no coordination at all.
 */

/** The round a point in time belongs to. */
export function roundIdAt(epochSecs, periodSecs) {
  return Math.floor(epochSecs / periodSecs);
}

/** The wall-clock second a round begins on. */
export function boundaryOf(roundId, periodSecs) {
  return roundId * periodSecs;
}

/**
 * The next boundary strictly after `epochSecs`.
 *
 * Strictly after, so a harness that finishes a round early and loops does not
 * immediately re-run the round it has just completed.
 */
export function nextBoundaryAfter(epochSecs, periodSecs) {
  return (Math.floor(epochSecs / periodSecs) + 1) * periodSecs;
}

/**
 * Which of the selected hot files this round reads.
 *
 * `roundId mod len(files)`, so a selection of three files is cycled and every
 * file gets the same number of rounds — and, because the round id is derived
 * from the wall clock, every VM in the cohort picks the same one without being
 * told. Returns undefined when there are no files to choose from, which is the
 * "operator has not selected yet" case and is recorded, not failed.
 */
export function hotFileIndexFor(roundId, fileCount) {
  if (!fileCount || fileCount <= 0) return undefined;
  // `%` on a negative round id would give a negative index; round ids are
  // derived from the epoch and cannot be negative, but the clamp costs nothing.
  return ((roundId % fileCount) + fileCount) % fileCount;
}

/**
 * Which rounds a cohort owns.
 *
 * Two cohorts on one deployment — a branch build on `even` and a released
 * control on `odd` — then never read the same file at the same moment, so
 * neither is inside the other's swarm. The same closed vocabulary the native
 * hot-file reader uses (`HOT_FILE_PARITIES` in `client_roles.py`), because the
 * two roles are configured side by side in one manifest and two spellings of
 * "even" between them would be a trap.
 */
export const ROUND_PARITY_ALL = "all";
export const ROUND_PARITY_EVEN = "even";
export const ROUND_PARITY_ODD = "odd";
export const ROUND_PARITIES = [ROUND_PARITY_EVEN, ROUND_PARITY_ODD, ROUND_PARITY_ALL];

/**
 * Seconds of slack a seeding page must leave before the next round is due.
 *
 * A page that seeds right up to the boundary is still holding its WebRTC
 * sessions when the next round's browser launches, and the next instance is
 * then the fifth session from this address — which every node refuses. The gap
 * is what the schema's `seed_until_secs < round_period_secs - 30` enforces.
 */
export const SEED_TAIL_GAP_SECS = 30;

/** Whether this cohort owns the round. */
export function isOurRound(roundId, parity) {
  if (parity === ROUND_PARITY_EVEN) return roundId % 2 === 0;
  if (parity === ROUND_PARITY_ODD) return Math.abs(roundId % 2) === 1;
  return true;
}

/**
 * One instance's start offset from the round boundary, in seconds.
 *
 * `((k − 1) × instances + (i − 1)) × stagger`, where `k` is the VM's
 * `client_index` and `i` the instance number — both 1-based, as `clients infra`
 * allocates them and as the harness numbers its instances. Deterministic from
 * the inventory, so the whole cohort spaces itself out with no coordinator at
 * all, and identical to `reader_offset_secs` in
 * `resources/scripts/hot_file_reader.py` so a native and a browser cohort on
 * one deployment interleave the way the operator wrote them down.
 */
export function instanceOffsetSecs(clientIndex, instance, instances, stagger) {
  if (!stagger || stagger <= 0) return 0;
  return ((Math.max(clientIndex, 1) - 1) * instances + (Math.max(instance, 1) - 1)) * stagger;
}

/**
 * Whether this instance's start is at or past its own round deadline.
 *
 * The deadline is an absolute offset from the BOUNDARY, so a stagger long
 * enough to push an instance's start to or beyond it is a start that can never
 * run — and the harness must refuse it BEFORE sleeping to it. Sleeping anyway
 * does not merely waste the instance: it launches Chromium inside the next
 * round, which on a parity-split deployment is the other cohort's, so the
 * contamination lands in the measurement. `runRound` awaits every instance, so
 * the main loop also stops observing boundaries for as long as the longest
 * sleep.
 *
 * The same rule `hot_file_reader.py` applies to a native reader (`offset >=
 * download_timeout_secs`), and it is a pure predicate here so `node --test` can
 * exercise it without a browser.
 */
export function startOffsetExhausted(offsetSecs, downloadTimeoutSecs) {
  if (!downloadTimeoutSecs || downloadTimeoutSecs <= 0) return false;
  return (offsetSecs || 0) >= downloadTimeoutSecs;
}
