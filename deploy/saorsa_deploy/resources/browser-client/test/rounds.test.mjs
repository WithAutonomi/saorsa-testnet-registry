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
