import { describe, expect, it } from "vitest";
import { applyTileOrder, moveTile, moveTileBy, pruneTileOrder } from "./tileOrder";

describe("applyTileOrder", () => {
  it("rearranges moved tiles among their own slots", () => {
    expect(applyTileOrder(["a", "b", "c", "d"], ["d", "b"])).toEqual(["a", "d", "c", "b"]);
  });

  it("keeps the natural order without a saved order", () => {
    expect(applyTileOrder(["a", "b"], [])).toEqual(["a", "b"]);
  });
});

describe("moveTile", () => {
  it("moves a tile to the target's place", () => {
    expect(moveTile([], ["a", "b", "c"], "c", "a")).toEqual(["c", "a", "b"]);
    expect(moveTile([], ["a", "b", "c"], "a", "c")).toEqual(["b", "c", "a"]);
  });

  it("keeps hidden tiles' saved places", () => {
    expect(moveTile(["x", "b"], ["a", "b"], "b", "a")).toEqual(["b", "a", "x"]);
  });

  it("ignores unknown tiles", () => {
    expect(moveTile(["a"], ["a", "b"], "z", "a")).toEqual(["a"]);
  });
});

describe("moveTileBy", () => {
  it("moves one place and stops at the ends", () => {
    expect(moveTileBy([], ["a", "b", "c"], "b", 1)).toEqual(["a", "c", "b"]);
    expect(moveTileBy([], ["a", "b", "c"], "a", -1)).toEqual([]);
  });
});

describe("pruneTileOrder", () => {
  it("forgets sessions that are gone", () => {
    expect(pruneTileOrder(["a", "gone"], new Set(["a"]))).toEqual(["a"]);
  });
});
