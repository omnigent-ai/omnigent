// Where Annotate's request card goes: beside the picked element, never over it.
import { describe, expect, it } from "vitest";

import { cardPosition } from "./layerParts";

const frame = { width: 800, height: 600 };

describe("cardPosition", () => {
  it("puts the card under the element when there's room", () => {
    expect(cardPosition({ x: 20, y: 40, width: 100, height: 20 }, frame)).toEqual({
      left: 20,
      top: 70,
      width: 320,
    });
  });

  it("puts it over an element near the page's bottom", () => {
    expect(cardPosition({ x: 20, y: 520, width: 100, height: 20 }, frame)).toEqual({
      left: 20,
      top: 390,
      width: 320,
    });
  });

  it("puts it beside an element that fills the page's height, on the page", () => {
    expect(cardPosition({ x: 20, y: 0, width: 100, height: 600 }, frame)).toEqual({
      left: 130,
      top: 8,
      width: 320,
    });
    // No room to its right: to its left, at least 8px in.
    const right = cardPosition({ x: 600, y: 0, width: 190, height: 600 }, frame);
    expect(right).toEqual({ left: 270, top: 8, width: 320 });
  });

  it("keeps a card grown by a long request, its Send and Cancel too, on the page", () => {
    const element = { x: 20, y: 400, width: 100, height: 20 };
    // Room under it for the usual card, not for one a few lines taller: over it instead.
    expect(cardPosition(element, frame).top).toBe(430);
    expect(cardPosition(element, frame, 206).top).toBe(184);
    // Too tall for over or under: beside it, its bottom still inside the frame.
    const tall = cardPosition({ x: 20, y: 200, width: 100, height: 300 }, frame, 206);
    expect(tall.top + 206).toBeLessThanOrEqual(frame.height - 8);
  });

  it("keeps it inside the page's right edge", () => {
    expect(cardPosition({ x: 700, y: 40, width: 50, height: 20 }, frame).left).toBe(472);
  });

  it("narrows it to fit a narrow frame, wherever it goes", () => {
    const narrow = { width: 260, height: 600 };
    for (const element of [
      { x: 20, y: 40, width: 100, height: 20 }, // under
      { x: 200, y: 520, width: 40, height: 20 }, // over, near the right edge
      { x: 20, y: 0, width: 100, height: 600 }, // beside, no room right
      { x: 140, y: 0, width: 100, height: 600 }, // beside, no room left
    ]) {
      const card = cardPosition(element, narrow);
      expect(card.width).toBe(244);
      expect(card.left).toBeGreaterThanOrEqual(8);
      expect(card.left + card.width).toBeLessThanOrEqual(narrow.width - 8);
    }
  });
});
