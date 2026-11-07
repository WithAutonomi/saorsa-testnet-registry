/**
 * Round arithmetic.
 *
 * The whole measurement rests on these three functions agreeing across VMs
 * that never talk to each other: two instances in different regions read the
 * same file at the same second only because both derive the round id, the
 * boundary and the file index from the wall clock and nothing else.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  boundaryOf,
  hotFileIndexFor,
  instanceOffsetSecs,
  isOurRound,
  nextBoundaryAfter,
  roundIdAt,
} from "../lib/rounds.mjs";

test("the round id is the epoch divided by the period, floored", () => {
  assert.equal(roundIdAt(1_660_795_200, 600), 2_767_992);
  // Anywhere inside the round gives the same id, which is what makes a VM that
  // joined late part of the same round as one that joined on time.
  assert.equal(roundIdAt(1_660_795_200 + 1, 600), 2_767_992);
  assert.equal(roundIdAt(1_660_795_200 + 599, 600), 2_767_992);
  assert.equal(roundIdAt(1_660_795_200 + 600, 600), 2_767_993);
});

test("the boundary is the round id times the period", () => {
  assert.equal(boundaryOf(2_767_992, 600), 1_660_795_200);
  assert.equal(roundIdAt(boundaryOf(2_767_992, 600), 600), 2_767_992);
});

test("the next boundary is strictly after the time given", () => {
  // Strictly: a harness that finished a round early and looped must not
  // immediately re-run the round it has just completed.
  assert.equal(nextBoundaryAfter(1_660_795_200, 600), 1_660_795_800);
  assert.equal(nextBoundaryAfter(1_660_795_201, 600), 1_660_795_800);
  assert.equal(nextBoundaryAfter(1_660_795_799, 600), 1_660_795_800);
});

test("the hot file index cycles the selection", () => {
  assert.equal(hotFileIndexFor(2_767_992, 3), 0);
  assert.equal(hotFileIndexFor(2_767_993, 3), 1);
  assert.equal(hotFileIndexFor(2_767_994, 3), 2);
  assert.equal(hotFileIndexFor(2_767_995, 3), 0);
});

test("one selected file means every round reads it", () => {
  for (const id of [1, 2, 3, 99]) assert.equal(hotFileIndexFor(id, 1), 0);
});

test("no selected files gives no index rather than index 0", () => {
  // Index 0 of an empty list is not a file, and a harness that took it as one
  // would report a failure to download nothing. Undefined is what makes the
  // round a recorded SKIP.
  assert.equal(hotFileIndexFor(2_767_992, 0), undefined);
  assert.equal(hotFileIndexFor(2_767_992, undefined), undefined);
});

test("a parity splits the rounds in half, and `all` takes them all", () => {
  // Two cohorts on one deployment -- a branch build on `even` and a released
  // control on `odd` -- then never read the same file at the same moment, so
  // neither is inside the other's swarm.
  assert.equal(isOurRound(2_767_992, "even"), true);
  assert.equal(isOurRound(2_767_993, "even"), false);
  assert.equal(isOurRound(2_767_993, "odd"), true);
  assert.equal(isOurRound(2_767_992, "odd"), false);
  for (const id of [2_767_992, 2_767_993]) assert.equal(isOurRound(id, "all"), true);
});

test("an unknown parity owns every round rather than none", () => {
  // The three input paths all refuse an unknown value, so this is unreachable
  // in practice; a harness that silently ran NO rounds would be the worse of
  // the two failure modes, since it looks exactly like a VM that is down.
  assert.equal(isOurRound(2_767_992, ""), true);
  assert.equal(isOurRound(2_767_993, undefined), true);
});

test("no stagger starts every instance on the boundary", () => {
  for (const i of [1, 2, 3, 4]) assert.equal(instanceOffsetSecs(1, i, 4, 0), 0);
  assert.equal(instanceOffsetSecs(9, 4, 4, 0), 0);
});

test("the offset walks across instances and then across VMs", () => {
  // `((k - 1) x instances + (i - 1)) x stagger`, which is what makes a cohort
  // of 4 VMs x 4 instances with a 2 s stagger start one instance every two
  // seconds for thirty seconds -- with no coordinator at all.
  assert.equal(instanceOffsetSecs(1, 1, 4, 2), 0);
  assert.equal(instanceOffsetSecs(1, 4, 4, 2), 6);
  assert.equal(instanceOffsetSecs(2, 1, 4, 2), 8);
  assert.equal(instanceOffsetSecs(4, 4, 4, 2), 30);
});

test("the offset is keyed on the index, not on the VM count", () => {
  // An extension's indexes CONTINUE from whatever the deployment already uses,
  // so a second cohort of four VMs is indexes 5..8 and its last instance starts
  // far later than a first cohort's would.
  assert.equal(instanceOffsetSecs(8, 4, 4, 10), 310);
});

test("the offset formula is the native reader's", () => {
  // `reader_offset_secs` in `resources/scripts/hot_file_reader.py`, so a native
  // and a browser cohort on one deployment interleave the way the operator
  // wrote them down. The Python suite pins the same four values.
  assert.equal(instanceOffsetSecs(1, 1, 4, 2), 0);
  assert.equal(instanceOffsetSecs(2, 3, 4, 2), 12);
  assert.equal(instanceOffsetSecs(3, 2, 4, 5), 45);
  assert.equal(instanceOffsetSecs(0, 0, 4, 5), 0);
});
