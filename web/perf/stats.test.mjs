import assert from "node:assert/strict";
import { test } from "node:test";
import { bootstrapDelta, percentile } from "./ab.mjs";
import { armOrder } from "./cdp.mjs";

test("arm order counterbalances adjacent pairs", () => {
  assert.deepEqual([0, 1, 2, 3].flatMap(armOrder), ["a", "b", "b", "a", "a", "b", "b", "a"]);
});

test("nearest-rank percentiles include endpoints", () => {
  const values = [4, 1, 3, 2];
  assert.equal(percentile(values, 0), 1);
  assert.equal(percentile(values, 50), 2);
  assert.equal(percentile(values, 75), 3);
  assert.equal(percentile(values, 100), 4);
  assert.throws(() => percentile([], 50));
  assert.throws(() => percentile(values, NaN));
});

test("paired bootstrap preserves an exact shift", () => {
  const a = [1, 3, 5, 7];
  const b = a.map((value) => value + 4);
  assert.deepEqual(bootstrapDelta(a, b, 75, 100), [4, 4]);
  assert.throws(() => bootstrapDelta(a, [1], 50));
});
