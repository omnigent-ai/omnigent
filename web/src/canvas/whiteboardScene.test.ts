import { describe, expect, it } from "vitest";
import {
  addElement,
  appendStrokePoint,
  boxAt,
  DEFAULT_RECTANGLE_HEIGHT,
  DEFAULT_RECTANGLE_WIDTH,
  edgePoint,
  MAX_ELEMENTS,
  MAX_STROKE_POINTS,
  MAX_TEXT_LENGTH,
  moveElement,
  NOTE_HEIGHT,
  NOTE_WIDTH,
  noteBoxAt,
  rectangleBox,
  removeElement,
  resolveArrow,
  snapToGrid,
  strokePath,
  withText,
  type ArrowElement,
  type WhiteboardBox,
  type WhiteboardElement,
} from "./whiteboardScene";

const card: WhiteboardBox = { x: 0, y: 0, width: 100, height: 50 };
const frame: WhiteboardElement = {
  id: "wb-frame",
  kind: "rectangle",
  x: 300,
  y: 0,
  width: 100,
  height: 50,
  text: "",
};
const boxes = new Map<string, WhiteboardBox>([
  ["session-a", card],
  ["wb-frame", frame as WhiteboardBox],
]);
const resolveBox = (id: string) => boxes.get(id) ?? null;

describe("whiteboard geometry", () => {
  it("uses the dragged box, or the default size for a click", () => {
    expect(rectangleBox({ x: 50, y: 40 }, { x: 10, y: 0 })).toEqual({
      x: 10,
      y: 0,
      width: 40,
      height: 40,
    });
    expect(rectangleBox({ x: 5, y: 5 }, { x: 8, y: 30 })).toEqual({
      x: 5,
      y: 5,
      width: DEFAULT_RECTANGLE_WIDTH,
      height: DEFAULT_RECTANGLE_HEIGHT,
    });
  });

  it("centers a new note on the click", () => {
    expect(noteBoxAt({ x: 100, y: 100 })).toEqual({
      x: 100 - NOTE_WIDTH / 2,
      y: 100 - NOTE_HEIGHT / 2,
      width: NOTE_WIDTH,
      height: NOTE_HEIGHT,
    });
  });

  it("drops pen jitter and caps a stroke's samples", () => {
    const start = [{ x: 0, y: 0 }];
    expect(appendStrokePoint(start, { x: 1, y: 1 })).toEqual(start);
    expect(appendStrokePoint(start, { x: 10.4, y: 0.6 })).toEqual([...start, { x: 10, y: 1 }]);
    const full = Array.from({ length: MAX_STROKE_POINTS }, (_, x) => ({ x: x * 10, y: 0 }));
    expect(appendStrokePoint(full, { x: -50, y: -50 })).toHaveLength(MAX_STROKE_POINTS);
  });

  it("smooths a stroke into an SVG path and keeps a single tap visible", () => {
    expect(strokePath([])).toBe("");
    expect(strokePath([{ x: 1, y: 2 }])).toBe("M 1 2 l 0.01 0");
    expect(
      strokePath([
        { x: 0, y: 0 },
        { x: 10, y: 0 },
        { x: 20, y: 10 },
      ]),
    ).toBe("M 0 0 Q 10 0 15 5 L 20 10");
  });

  it("clips a line from the box center at its border", () => {
    expect(edgePoint(card, { x: 250, y: 25 })).toEqual({ x: 100, y: 25 });
    expect(edgePoint(card, { x: 50, y: -100 })).toEqual({ x: 50, y: 0 });
    expect(edgePoint(card, { x: 50, y: 25 })).toEqual({ x: 50, y: 25 });
  });

  it("finds the topmost box under a point", () => {
    const stack: [string, WhiteboardBox][] = [
      ["frame", { x: 0, y: 0, width: 500, height: 500 }],
      ["card", card],
    ];
    expect(boxAt({ x: 10, y: 10 }, stack)).toBe("card");
    expect(boxAt({ x: 400, y: 400 }, stack)).toBe("frame");
    expect(boxAt({ x: 600, y: 10 }, stack)).toBeNull();
  });
});

describe("whiteboard arrows", () => {
  const bound: ArrowElement = {
    id: "wb-arrow",
    kind: "arrow",
    from: { bind: "session-a" },
    to: { bind: "wb-frame" },
  };

  it("draws a bound arrow between the borders of its card and shape", () => {
    expect(resolveArrow(bound, resolveBox)).toEqual({
      start: { x: 100, y: 25 },
      end: { x: 300, y: 25 },
    });
    expect(
      resolveArrow({ from: { x: 50, y: 200 }, to: { bind: "session-a" } }, resolveBox),
    ).toEqual({ start: { x: 50, y: 200 }, end: { x: 50, y: 50 } });
  });

  it("hides an arrow whose card is not on this canvas", () => {
    expect(resolveArrow({ ...bound, from: { bind: "gone" } }, resolveBox)).toBeNull();
  });

  it("moves free ends and leaves bound ends attached", () => {
    const arrow: ArrowElement = { ...bound, to: { x: 10, y: 10 } };
    expect(moveElement(arrow, 5, -5)).toEqual({ ...arrow, to: { x: 15, y: 5 } });
    expect(moveElement(frame, 10.6, 0)).toMatchObject({ x: 311, y: 0 });
    expect(moveElement({ id: "s", kind: "stroke", points: [{ x: 0, y: 0 }] }, 2, 3).points).toEqual(
      [{ x: 2, y: 3 }],
    );
  });

  it("keeps arrows pointing at a deleted shape where they were drawn", () => {
    const elements: WhiteboardElement[] = [frame, bound];
    expect(removeElement(elements, "wb-frame", resolveBox)).toEqual([
      { ...bound, to: { x: 300, y: 25 } },
    ]);
  });

  it("drops an arrow it can no longer place", () => {
    const orphan: ArrowElement = { ...bound, from: { bind: "gone" } };
    expect(removeElement([frame, orphan], "wb-frame", resolveBox)).toEqual([]);
  });
});

describe("whiteboard elements", () => {
  it("snaps frames and notes to the card grid and leaves strokes alone", () => {
    expect(snapToGrid({ ...frame, x: 47, y: -17 })).toMatchObject({ x: 32, y: -32 });
    const stroke: WhiteboardElement = { id: "s", kind: "stroke", points: [{ x: 47, y: 3 }] };
    expect(snapToGrid(stroke)).toBe(stroke);
  });

  it("caps the text of a frame or note", () => {
    const [updated] = withText([frame], "wb-frame", "x".repeat(MAX_TEXT_LENGTH + 5));
    expect(updated).toMatchObject({ text: "x".repeat(MAX_TEXT_LENGTH) });
  });

  it("keeps only the newest elements beyond the cap", () => {
    const many = Array.from({ length: MAX_ELEMENTS }, (_, index): WhiteboardElement => ({
      id: `wb-${index}`,
      kind: "stroke",
      points: [{ x: index, y: 0 }],
    }));
    const next = addElement(many, frame);
    expect(next).toHaveLength(MAX_ELEMENTS);
    expect(next[0].id).toBe("wb-1");
    expect(next.at(-1)).toBe(frame);
  });
});
