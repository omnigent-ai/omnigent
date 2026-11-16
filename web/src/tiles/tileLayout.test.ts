import { afterEach, describe, expect, it } from "vitest";
import {
  clampSpan,
  displayKey,
  effectiveColumns,
  fittingColumns,
  MAX_SPAN,
  setTileColumns,
  setTileSpan,
  spanFromDrag,
  TILE_GAP_PX,
  TILE_ROW_HEIGHT_PX,
} from "./tileLayout";

afterEach(() => localStorage.clear());

describe("clampSpan", () => {
  it("snaps to whole slots within the limits and the visible columns", () => {
    expect(clampSpan({ columns: 2.4, rows: 1.6 })).toEqual({ columns: 2, rows: 2 });
    expect(clampSpan({ columns: 9, rows: 9 })).toEqual(MAX_SPAN);
    expect(clampSpan({ columns: 0, rows: -3 })).toEqual({ columns: 1, rows: 1 });
    expect(clampSpan({ columns: 4, rows: 1 }, 2)).toEqual({ columns: 2, rows: 1 });
  });
});

describe("columns", () => {
  it("fits at least one column and caps the chosen count by the window", () => {
    expect(fittingColumns(100)).toBe(1);
    expect(fittingColumns(360 * 3 + TILE_GAP_PX * 2)).toBe(3);
    expect(effectiveColumns(0, 1500)).toBe(fittingColumns(1500));
    expect(effectiveColumns(8, 800)).toBe(fittingColumns(800));
    expect(effectiveColumns(1, 1500)).toBe(1);
  });
});

describe("spanFromDrag", () => {
  it("grows by whole slots as the pointer passes each slot", () => {
    const slot = 400;
    const oneWide = spanFromDrag(slot, TILE_ROW_HEIGHT_PX, 0, 0, slot, 4);
    expect(oneWide).toEqual({ columns: 1, rows: 1 });
    const grown = spanFromDrag(
      slot,
      TILE_ROW_HEIGHT_PX,
      slot + TILE_GAP_PX,
      TILE_ROW_HEIGHT_PX,
      slot,
      4,
    );
    expect(grown).toEqual({ columns: 2, rows: 2 });
    expect(spanFromDrag(slot, TILE_ROW_HEIGHT_PX, 5000, 0, slot, 3).columns).toBe(3);
  });
});

describe("persistence", () => {
  it("drops 1×1 spans and clamps the stored column choice", () => {
    setTileSpan("a", { columns: 2, rows: 1 });
    setTileSpan("b", { columns: 1, rows: 1 });
    expect(JSON.parse(localStorage.getItem("omnigent:tiles-spans") ?? "{}")).toEqual({
      a: { columns: 2, rows: 1 },
    });
    setTileColumns(42);
    const stored = JSON.parse(localStorage.getItem("omnigent:tiles-columns") ?? "{}");
    expect(stored.byDisplay[displayKey()]).toBe(8);
    expect(stored.last).toBe(8);
  });
});
