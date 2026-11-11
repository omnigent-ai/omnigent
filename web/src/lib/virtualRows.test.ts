import { describe, expect, it } from "vitest";
import { retainedRowIndices } from "./virtualRows";

describe("retainedRowIndices", () => {
  const range = { startIndex: 10, endIndex: 12, overscan: 2, count: 100 };

  it("returns the visible window with overscan", () => {
    expect(retainedRowIndices(range, [])).toEqual([8, 9, 10, 11, 12, 13, 14]);
  });

  it("adds retained rows on either side in ascending order without duplicates", () => {
    expect(retainedRowIndices(range, [90, 3, 11, 3])).toEqual([3, 8, 9, 10, 11, 12, 13, 14, 90]);
  });

  it("ignores retained indices outside the list", () => {
    expect(retainedRowIndices(range, [-1, 100, Number.NaN])).toEqual([8, 9, 10, 11, 12, 13, 14]);
  });

  it("clamps the window to the list and handles an empty list", () => {
    expect(retainedRowIndices({ ...range, startIndex: 0, endIndex: 1 }, [])).toEqual([0, 1, 2, 3]);
    expect(retainedRowIndices({ ...range, count: 0 }, [0])).toEqual([]);
  });
});
