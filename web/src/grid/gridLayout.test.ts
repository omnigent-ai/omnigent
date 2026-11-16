import { describe, expect, it } from "vitest";
import {
  gridLayoutForSessions,
  gridPanes,
  gridSessionIds,
  parseSessionGrids,
  removeGridSession,
  splitGrid,
  swapGridSessions,
  type SplitEdge,
} from "./gridLayout";

describe("session grid layouts", () => {
  it.each<SplitEdge>(["left", "right", "top", "bottom"])(
    "splits %s, fills 2×2, and rejects duplicates and excess panes",
    (edge) => {
      const two = splitGrid("a", "a", "b", edge)!;
      expect(gridSessionIds(two)).toEqual(
        edge === "left" || edge === "top" ? ["b", "a"] : ["a", "b"],
      );
      expect(splitGrid(two, "a", "c", edge)).toBeNull();
      const perpendicular = edge === "left" || edge === "right" ? "bottom" : "right";
      const three = splitGrid(two, "a", "c", perpendicular)!;
      expect(gridPanes(three)).toHaveLength(3);
      const four = splitGrid(three, "b", "d", perpendicular)!;
      expect(
        gridPanes(four)
          .map((pane) => pane.area)
          .sort(),
      ).toEqual([
        "1 / 1 / span 1 / span 1",
        "1 / 2 / span 1 / span 1",
        "2 / 1 / span 1 / span 1",
        "2 / 2 / span 1 / span 1",
      ]);
      expect(splitGrid(two, "a", "b", perpendicular)).toBeNull();
      expect(splitGrid(two, "missing", "c", perpendicular)).toBeNull();
      expect(splitGrid(four, "a", "e", perpendicular)).toBeNull();
      for (const layout of [two, three, four]) {
        const swapped = swapGridSessions(layout, "a", "b");
        expect(gridPanes(swapped)).toEqual(
          gridPanes(layout).map((pane) => ({
            ...pane,
            sessionId: pane.sessionId === "a" ? "b" : pane.sessionId === "b" ? "a" : pane.sessionId,
          })),
        );
        expect(swapGridSessions(swapped, "a", "b")).toEqual(layout);
        expect(swapGridSessions(layout, "a", "a")).toBe(layout);
        expect(swapGridSessions(layout, "a", "missing")).toBe(layout);
        expect(swapGridSessions(layout, "a", "a", edge)).toBe(layout);
        expect(swapGridSessions(layout, "a", "missing", edge)).toBe(layout);
        if (gridSessionIds(layout).length > 2)
          expect(swapGridSessions(layout, "a", "b", edge)).toBe(layout);
      }
      for (const original of [two, splitGrid("a", "a", "b", perpendicular)!]) {
        expect(swapGridSessions(original, "b", "a", edge)).toEqual({
          axis: edge === "left" || edge === "right" ? "columns" : "rows",
          children: edge === "left" || edge === "top" ? ["b", "a"] : ["a", "b"],
        });
      }
      expect(removeGridSession(four, "d")).toEqual(three);
      expect(removeGridSession(three, "c")).toEqual(two);
      expect(removeGridSession(two, "b")).toBe("a");
    },
  );

  it("lays out two to four sessions left to right, then top to bottom", () => {
    const areas = (ids: string[]) =>
      gridPanes(gridLayoutForSessions(ids)!).map(({ sessionId, area }) => [sessionId, area]);
    expect(areas(["a", "b"])).toEqual([
      ["a", "1 / 1 / span 2 / span 1"],
      ["b", "1 / 2 / span 2 / span 1"],
    ]);
    expect(areas(["a", "b", "c"])).toEqual([
      ["a", "1 / 1 / span 2 / span 1"],
      ["b", "1 / 2 / span 1 / span 1"],
      ["c", "2 / 2 / span 1 / span 1"],
    ]);
    expect(areas(["a", "b", "c", "d"])).toEqual([
      ["a", "1 / 1 / span 1 / span 1"],
      ["b", "1 / 2 / span 1 / span 1"],
      ["c", "2 / 1 / span 1 / span 1"],
      ["d", "2 / 2 / span 1 / span 1"],
    ]);
    for (const ids of [
      ["a", "b"],
      ["a", "b", "c"],
      ["a", "b", "c", "d"],
    ]) {
      const grid = { id: ids.join(""), run: "g1", layout: gridLayoutForSessions(ids)! };
      expect(parseSessionGrids(JSON.stringify([grid]))).toEqual([grid]);
    }
    // A run tag must be a non-empty string.
    for (const run of ["", 7, null]) {
      const grid = { id: "bad", run, layout: gridLayoutForSessions(["a", "b"])! };
      expect(parseSessionGrids(JSON.stringify([grid]))).toEqual([]);
    }
    for (const ids of [
      [],
      ["a"],
      ["a", "b", "c", "d", "e"],
      ["a", "a"],
      ["a", "temp:b"],
      ["a", ""],
    ])
      expect(gridLayoutForSessions(ids)).toBeNull();
  });

  it("reads valid local grids and rejects corrupt, duplicate and out-of-bounds layouts", () => {
    const grid = { id: "saved", name: "My work", layout: splitGrid("a", "a", "b", "right")! };
    expect(parseSessionGrids(JSON.stringify([grid, grid]))).toEqual([grid]);
    for (const raw of [
      null,
      "not json",
      "null",
      "{}",
      JSON.stringify([{ ...grid, name: 123 }]),
      JSON.stringify([{ ...grid, name: "x".repeat(201) }]),
      JSON.stringify([{ id: "bad", layout: "a" }]),
      JSON.stringify([{ id: "bad", layout: { axis: "columns", children: ["a", "a"] } }]),
      JSON.stringify([{ id: "bad", layout: { axis: "columns", children: [grid.layout, "c"] } }]),
      JSON.stringify([{ id: "bad", layout: { axis: "rows", children: ["temp:a", "b"] } }]),
    ])
      expect(parseSessionGrids(raw)).toEqual([]);
  });
});
