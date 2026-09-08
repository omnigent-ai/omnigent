import { afterEach, describe, expect, it } from "vitest";
import {
  canvasLayoutStorageKey,
  EMPTY_CANVAS_LAYOUT,
  LAYOUT_VERSION,
  MAX_SAVED_POSITIONS,
  readCanvasLayout,
  withoutCanvas,
  withPosition,
  withViewport,
  writeCanvasLayout,
} from "./canvasStorage";

afterEach(() => {
  window.localStorage.clear();
});

describe("canvas layout storage", () => {
  it("scopes the entry to the server identity", () => {
    expect(canvasLayoutStorageKey()).toBe(`omnigent:canvas-layout:${window.location.origin}`);
  });

  it("reads an empty layout when nothing valid is stored", () => {
    expect(readCanvasLayout()).toEqual(EMPTY_CANVAS_LAYOUT);
    window.localStorage.setItem(canvasLayoutStorageKey(), "{not json");
    expect(readCanvasLayout()).toEqual(EMPTY_CANVAS_LAYOUT);
    window.localStorage.setItem(
      canvasLayoutStorageKey(),
      JSON.stringify({ version: LAYOUT_VERSION + 1, positions: { a: [1, 2] }, viewports: {} }),
    );
    expect(readCanvasLayout()).toEqual(EMPTY_CANVAS_LAYOUT);
  });

  it("round-trips positions and viewports, dropping malformed entries", () => {
    writeCanvasLayout({
      positions: { a: { x: 10.4, y: -20.6 }, b: { x: 5_000_000, y: 0 } },
      viewports: { main: { x: 1, y: 2, zoom: 0.5, width: 800, height: 600 } },
    });
    const raw = JSON.parse(window.localStorage.getItem(canvasLayoutStorageKey()) ?? "{}") as {
      positions: Record<string, unknown>;
      viewports: Record<string, unknown>;
    };
    raw.positions.broken = ["x", 1];
    raw.viewports.broken = { x: 1, y: 2, zoom: 0 };
    window.localStorage.setItem(canvasLayoutStorageKey(), JSON.stringify(raw));

    expect(readCanvasLayout()).toEqual({
      positions: { a: { x: 10, y: -21 }, b: { x: 1_000_000, y: 0 } },
      viewports: { main: { x: 1, y: 2, zoom: 0.5, width: 800, height: 600 } },
    });
  });

  it("keeps only the most recently placed cards past the cap", () => {
    // Insertion order is placement order; the last entry is the newest card.
    const positions = Object.fromEntries(
      Array.from({ length: MAX_SAVED_POSITIONS + 1 }, (_, index) => [
        `s${index}`,
        { x: index, y: 0 },
      ]),
    );
    writeCanvasLayout({ positions, viewports: {} });
    const stored = readCanvasLayout().positions;
    expect(Object.keys(stored)).toHaveLength(MAX_SAVED_POSITIONS);
    expect(stored.s0).toBeUndefined();
    expect(stored[`s${MAX_SAVED_POSITIONS}`]).toEqual({ x: MAX_SAVED_POSITIONS, y: 0 });
  });

  it("moves a re-placed card to the end so it survives the cap", () => {
    const layout = withPosition(
      withPosition(withPosition(EMPTY_CANVAS_LAYOUT, "a", { x: 0, y: 0 }), "b", { x: 1, y: 1 }),
      "a",
      { x: 2.4, y: 2.6 },
    );
    expect(Object.keys(layout.positions)).toEqual(["b", "a"]);
    expect(layout.positions.a).toEqual({ x: 2, y: 3 });
  });

  it("rounds saved viewports and forgets one canvas at a time", () => {
    const layout = withViewport(
      withViewport(EMPTY_CANVAS_LAYOUT, "main", { x: 1.6, y: 2.4, zoom: 0.98765 }),
      "proj_a",
      { x: 0, y: 0, zoom: 1, width: 100.4, height: 50.6 },
    );
    expect(layout.viewports.main).toEqual({ x: 2, y: 2, zoom: 0.988 });
    expect(layout.viewports.proj_a).toEqual({ x: 0, y: 0, zoom: 1, width: 100, height: 51 });

    const placed = withPosition(withPosition(layout, "onMain", { x: 1, y: 1 }), "onProject", {
      x: 2,
      y: 2,
    });
    expect(withoutCanvas(placed, "main", ["onMain"])).toEqual({
      positions: { onProject: { x: 2, y: 2 } },
      viewports: { proj_a: { x: 0, y: 0, zoom: 1, width: 100, height: 51 } },
    });
  });
});
