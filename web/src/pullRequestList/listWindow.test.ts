// The list's fixed-height layout: where each item sits, what a window draws, and which rows
// the viewport itself holds.

import { describe, expect, it } from "vitest";

import {
  drawnItems,
  LIST_GROUP_HEADER_HEIGHT as HEADER,
  LIST_ROW_HEIGHT as ROW,
  listLayout,
  rowsIn,
  windowSlices,
} from "./listWindow";

const urls = (prefix: string, count: number) =>
  Array.from({ length: count }, (_value, index) => `${prefix}${index + 1}`);

const GROUPS = [
  { id: "open", header: true, urls: urls("o", 10) },
  { id: "draft", header: true, urls: [] },
  { id: "merged", header: true, urls: urls("m", 5) },
];

describe("the list's layout", () => {
  it("places headers and rows one after another at their fixed heights", () => {
    const layout = listLayout(GROUPS);
    expect(layout.items).toHaveLength(1 + 10 + 1 + 1 + 5);
    expect(layout.offsets[1]).toBe(HEADER);
    expect(layout.offsets[11]).toBe(HEADER + 10 * ROW);
    expect(layout.totalHeight).toBe(3 * HEADER + 15 * ROW);
    expect(layout.rowIndex.get("m1")).toBe(13);
  });

  const items = (from: number, to: number) =>
    Array.from({ length: to - from + 1 }, (_value, offset) => from + offset);

  it("draws only the window's rows, keeps each drawn group's header, and spaces the rest", () => {
    const layout = listLayout(GROUPS);
    // Items 4 to 8: rows o4 to o8 of the first group.
    const { slices, before, after } = windowSlices(layout, GROUPS, items(4, 8));
    expect([...slices.keys()]).toEqual(["open"]);
    expect(slices.get("open")).toEqual({
      above: 0,
      urls: ["o4", "o5", "o6", "o7", "o8"],
      spaceAbove: [3 * ROW, 0, 0, 0, 0],
      after: 2 * ROW,
    });
    expect(before).toBe(0);
    expect(after).toBe(2 * HEADER + 5 * ROW);
    // Every drawn height plus the spaces adds up to the whole list.
    const drawn = HEADER + 3 * ROW + 5 * ROW + 2 * ROW;
    expect(before + drawn + after).toBe(layout.totalHeight);
  });

  it("spans groups, including an empty one between them", () => {
    const layout = listLayout(GROUPS);
    const { slices, before, after } = windowSlices(layout, GROUPS, items(9, 14));
    expect(slices.get("open")).toEqual({
      above: 0,
      urls: ["o9", "o10"],
      spaceAbove: [8 * ROW, 0],
      after: 0,
    });
    expect(slices.get("draft")).toEqual({ above: 0, urls: [], spaceAbove: [], after: 0 });
    expect(slices.get("merged")).toEqual({
      above: 0,
      urls: ["m1", "m2"],
      spaceAbove: [0, 0],
      after: 3 * ROW,
    });
    expect(before + after).toBe(0);
  });

  it("keeps a row far from the window, with space for what lies between", () => {
    const layout = listLayout(GROUPS);
    // The window holds m3 to m5 (items 15 to 17); o2 (item 2) is kept drawn on its own.
    const { slices, before, after } = windowSlices(layout, GROUPS, [2, ...items(15, 17)]);
    expect(slices.get("open")).toEqual({
      above: 0,
      urls: ["o2"],
      spaceAbove: [ROW],
      after: 8 * ROW,
    });
    expect(slices.has("draft")).toBe(false);
    expect(slices.get("merged")).toEqual({
      above: HEADER,
      urls: ["m3", "m4", "m5"],
      spaceAbove: [2 * ROW, 0, 0],
      after: 0,
    });
    expect(before + after).toBe(0);
    const drawn = 2 * HEADER + ROW + 3 * ROW;
    const spaces = ROW + 8 * ROW + HEADER + 2 * ROW;
    expect(drawn + spaces).toBe(layout.totalHeight);
  });

  it("names only the rows inside the viewport's range, not its headers", () => {
    const layout = listLayout(GROUPS);
    expect(rowsIn(layout, { start: 10, end: 13 })).toEqual(["o10", "m1"]);
    expect(rowsIn(layout, null)).toEqual([]);
  });

  it("draws nothing, and spaces the whole list, without a range", () => {
    const layout = listLayout(GROUPS);
    expect(windowSlices(layout, GROUPS, null)).toEqual({
      slices: new Map(),
      before: 0,
      after: layout.totalHeight,
    });
  });

  it("draws the viewport with its overscan, and keeps the active row wherever it is", () => {
    const range = { startIndex: 40, endIndex: 45, overscan: 2, count: 100 };
    expect(drawnItems(range, undefined)).toEqual([38, 39, 40, 41, 42, 43, 44, 45, 46, 47]);
    expect(drawnItems(range, 3)).toEqual([3, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47]);
    expect(drawnItems(range, 90).at(-1)).toBe(90);
    expect(drawnItems(range, 42)).toHaveLength(10);
    expect(drawnItems({ ...range, startIndex: 0, endIndex: 1 }, 500)).toEqual([0, 1, 2, 3]);
  });
});
