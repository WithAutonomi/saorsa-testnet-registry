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
