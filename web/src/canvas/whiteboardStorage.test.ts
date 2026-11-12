import { afterEach, describe, expect, it, vi } from "vitest";
import {
  MAX_ELEMENTS,
  MAX_STROKE_POINTS,
  MAX_TEXT_LENGTH,
  type WhiteboardElement,
} from "./whiteboardScene";
import {
  readStoredWhiteboard,
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
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    expect(readWhiteboard("u", "main")).toEqual([]);
    // Unreadable is not the same as empty, so a save doesn't treat it as another tab's write.
    expect(readStoredWhiteboard("u", "main")).toBeNull();
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
          { id: "wb-f", kind: "note", box: [0, 0, 10, 10], text: "x".repeat(MAX_TEXT_LENGTH + 9) },
          {
            id: "wb-g",
            kind: "stroke",
            points: Array.from({ length: (MAX_STROKE_POINTS + 5) * 2 }, (_, index) => index),
          },
        ],
      }),
    );
    const [frame, note, stroke] = readWhiteboard("u", "main");
    expect(frame).toEqual({
      id: "wb-e",
      kind: "rectangle",
      x: 1_000_000,
      y: 0,
      width: 10,
      height: 10,
      text: "",
    });
    expect(note).toMatchObject({ id: "wb-f", text: "x".repeat(MAX_TEXT_LENGTH) });
    expect(stroke).toMatchObject({ id: "wb-g", kind: "stroke" });
    expect((stroke as { points: unknown[] }).points).toHaveLength(MAX_STROKE_POINTS);
  });

  it("reads only the newest elements of an oversized entry", () => {
    window.localStorage.setItem(
      whiteboardStorageKey("u", "main"),
      JSON.stringify({
        version: WHITEBOARD_VERSION,
        elements: Array.from({ length: MAX_ELEMENTS + 3 }, (_, index) => ({
          id: `wb-${index}`,
          kind: "stroke",
          points: [index, 0, index, 1],
        })),
      }),
    );
    const stored = readWhiteboard("u", "main");
    expect(stored).toHaveLength(MAX_ELEMENTS);
    expect(stored[0].id).toBe("wb-3");
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
