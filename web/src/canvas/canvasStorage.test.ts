import { afterEach, describe, expect, it } from "vitest";
import {
  canvasLayoutStorageKey,
  EMPTY_CANVAS_LAYOUT,
  LAYOUT_VERSION,
  MAX_SAVED_POSITIONS,
  readCanvasLayout,
  withoutPositions,
  withPosition,
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
      JSON.stringify({ version: LAYOUT_VERSION + 1, positions: { a: [1, 2] } }),
    );
    expect(readCanvasLayout()).toEqual(EMPTY_CANVAS_LAYOUT);
  });

  it("round-trips positions, dropping malformed entries and ignoring unknown keys", () => {
    writeCanvasLayout({ positions: { a: { x: 10.4, y: -20.6 }, b: { x: 5_000_000, y: 0 } } });
    const raw = JSON.parse(window.localStorage.getItem(canvasLayoutStorageKey()) ?? "{}") as {
      positions: Record<string, unknown>;
      viewports?: unknown;
    };
    raw.positions.broken = ["x", 1];
    // Layouts saved before the view stopped being persisted carry this key.
    raw.viewports = { main: { x: 1, y: 2, zoom: 0.5 } };
    window.localStorage.setItem(canvasLayoutStorageKey(), JSON.stringify(raw));

    expect(readCanvasLayout()).toEqual({
      positions: { a: { x: 10, y: -21 }, b: { x: 1_000_000, y: 0 } },
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
    writeCanvasLayout({ positions });
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

  it("forgets only the given cards' spots", () => {
    const placed = withPosition(
      withPosition(EMPTY_CANVAS_LAYOUT, "onMain", { x: 1, y: 1 }),
      "onProject",
      { x: 2, y: 2 },
    );
    expect(withoutPositions(placed, ["onMain"])).toEqual({
      positions: { onProject: { x: 2, y: 2 } },
    });
  });
});
