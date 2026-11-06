import { afterEach, describe, expect, it, vi } from "vitest";
import { MAX_ELEMENTS, type WhiteboardElement } from "./whiteboardScene";
import {
  readWhiteboard,
  WHITEBOARD_VERSION,
  whiteboardStorageKey,
  writeWhiteboard,
} from "./whiteboardStorage";

afterEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
});

const drawing: WhiteboardElement[] = [
  {
    id: "wb-stroke",
    kind: "stroke",
    points: [
      { x: 1, y: 2 },
      { x: 3, y: 4 },
    ],
  },
  { id: "wb-frame", kind: "rectangle", x: -10, y: 20, width: 320, height: 192, text: "Release" },
  { id: "wb-note", kind: "note", x: 0, y: 0, width: 192, height: 128, text: "Ask\nreviewer" },
  { id: "wb-arrow", kind: "arrow", from: { bind: "session-a" }, to: { x: 400, y: 50 } },
];

describe("whiteboard storage", () => {
  it("scopes the entry to the server, the viewer and the canvas", () => {
    expect(whiteboardStorageKey("user_1", "main")).toBe(
      `omnigent:canvas-whiteboard:${window.location.origin}:user_1:main`,
    );
    expect(whiteboardStorageKey(null, "proj-1")).toBe(
      `omnigent:canvas-whiteboard:${window.location.origin}:anonymous:proj-1`,
    );
  });

  it("round-trips every element kind per canvas", () => {
    writeWhiteboard(drawing, "u", "main");
    expect(readWhiteboard("u", "main")).toEqual(drawing);
    expect(readWhiteboard("u", "proj-1")).toEqual([]);
    expect(readWhiteboard("someone-else", "main")).toEqual([]);
  });

  it("reads an empty board when nothing valid is stored", () => {
    const key = whiteboardStorageKey("u", "main");
    window.localStorage.setItem(key, "{not json");
    expect(readWhiteboard("u", "main")).toEqual([]);
    window.localStorage.setItem(
      key,
      JSON.stringify({ version: WHITEBOARD_VERSION + 1, elements: [] }),
    );
    expect(readWhiteboard("u", "main")).toEqual([]);
  });

  it("drops malformed and duplicate elements and bounds the rest", () => {
    window.localStorage.setItem(
      whiteboardStorageKey("u", "main"),
      JSON.stringify({
        version: WHITEBOARD_VERSION,
        elements: [
          { id: "wb-a", kind: "stroke", points: [1, 2, 3] },
          { id: "wb-b", kind: "note", box: [0, 0, -5, 10], text: "" },
          { id: "wb-c", kind: "arrow", from: [0, 0], to: { bind: "" } },
          { id: "wb-d", kind: "circle" },
          { kind: "stroke", points: [0, 0, 1, 1] },
          { id: "wb-e", kind: "rectangle", box: [5_000_000, 0, 10, 10], text: 7 },
          { id: "wb-e", kind: "note", box: [0, 0, 10, 10], text: "duplicate" },
        ],
      }),
    );
    expect(readWhiteboard("u", "main")).toEqual([
      { id: "wb-e", kind: "rectangle", x: 1_000_000, y: 0, width: 10, height: 10, text: "" },
    ]);
  });

  it("keeps only the newest elements beyond the cap", () => {
    const many = Array.from({ length: MAX_ELEMENTS + 3 }, (_, index): WhiteboardElement => ({
      id: `wb-${index}`,
      kind: "stroke",
      points: [{ x: index, y: 0 }],
    }));
    writeWhiteboard(many, "u", "main");
    const stored = readWhiteboard("u", "main");
    expect(stored).toHaveLength(MAX_ELEMENTS);
    expect(stored[0].id).toBe("wb-3");
  });

  it("removes the entry for an empty board and throws when storage is full", () => {
    writeWhiteboard(drawing, "u", "main");
    writeWhiteboard([], "u", "main");
    expect(window.localStorage.getItem(whiteboardStorageKey("u", "main"))).toBeNull();

    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    expect(() => writeWhiteboard(drawing, "u", "main")).toThrow();
  });
});
