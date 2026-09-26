import { afterEach, describe, expect, it } from "vitest";
import {
  activeCanvasStorageKey,
  canvasLayoutStorageKey,
  EMPTY_CANVAS_LAYOUT,
  LAYOUT_VERSION,
  MAX_SAVED_POSITIONS,
  readActiveCanvas,
  readCanvasLayout,
  withPosition,
  writeActiveCanvas,
  writeCanvasLayout,
} from "./canvasStorage";

afterEach(() => {
  window.localStorage.clear();
});

describe("canvas layout storage", () => {
  it("scopes the entry to the server identity and the viewer", () => {
    expect(canvasLayoutStorageKey("user_1")).toBe(
      `omnigent:canvas-layout:${window.location.origin}:user_1`,
    );
    expect(canvasLayoutStorageKey(null)).toBe(
      `omnigent:canvas-layout:${window.location.origin}:anonymous`,
    );
  });

  it("reads an empty layout when nothing valid is stored", () => {
    expect(readCanvasLayout("u")).toEqual(EMPTY_CANVAS_LAYOUT);
    window.localStorage.setItem(canvasLayoutStorageKey("u"), "{not json");
    expect(readCanvasLayout("u")).toEqual(EMPTY_CANVAS_LAYOUT);
    window.localStorage.setItem(
      canvasLayoutStorageKey("u"),
      JSON.stringify({ version: LAYOUT_VERSION + 1, positions: { a: [1, 2] } }),
    );
    expect(readCanvasLayout("u")).toEqual(EMPTY_CANVAS_LAYOUT);
  });

  it("round-trips positions, dropping malformed entries and ignoring unknown keys", () => {
    writeCanvasLayout({ positions: { a: { x: 10.4, y: -20.6 }, b: { x: 5_000_000, y: 0 } } }, "u");
    const raw = JSON.parse(window.localStorage.getItem(canvasLayoutStorageKey("u")) ?? "{}") as {
      positions: Record<string, unknown>;
      viewports?: unknown;
    };
    raw.positions.broken = ["x", 1];
    // Layouts saved before the view stopped being persisted carry this key.
    raw.viewports = { main: { x: 1, y: 2, zoom: 0.5 } };
    window.localStorage.setItem(canvasLayoutStorageKey("u"), JSON.stringify(raw));

    expect(readCanvasLayout("u")).toEqual({
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
    writeCanvasLayout({ positions }, "u");
    const stored = readCanvasLayout("u").positions;
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

  it("remembers the selected canvas per server and viewer", () => {
    expect(activeCanvasStorageKey("user_1")).toBe(
      `omnigent:canvas-active:${window.location.origin}:user_1`,
    );
    expect(readActiveCanvas("user_1")).toBeNull();
    writeActiveCanvas("proj_a", "user_1");
    expect(readActiveCanvas("user_1")).toBe("proj_a");
    expect(readActiveCanvas("user_2")).toBeNull();
    writeActiveCanvas("", "user_1");
    expect(readActiveCanvas("user_1")).toBeNull();
  });
});
