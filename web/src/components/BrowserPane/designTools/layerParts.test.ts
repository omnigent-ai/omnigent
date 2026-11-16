// Where Annotate's request card goes: beside the picked element, never over it.
import { describe, expect, it } from "vitest";

import { cardPosition } from "./layerParts";

const frame = { width: 800, height: 600 };

describe("cardPosition", () => {
  it("puts the card under the element when there's room", () => {
    expect(cardPosition({ x: 20, y: 40, width: 100, height: 20 }, frame)).toEqual({
      left: 20,
      top: 70,
    });
  });

  it("puts it over an element near the page's bottom", () => {
    expect(cardPosition({ x: 20, y: 520, width: 100, height: 20 }, frame)).toEqual({
      left: 20,
      top: 390,
    });
  });

  it("puts it beside an element that fills the page's height, on the page", () => {
    expect(cardPosition({ x: 20, y: 0, width: 100, height: 600 }, frame)).toEqual({
      left: 130,
      top: 8,
    });
    // No room to its right: to its left, at least 8px in.
    const right = cardPosition({ x: 600, y: 0, width: 190, height: 600 }, frame);
    expect(right).toEqual({ left: 270, top: 8 });
  });

  it("keeps it inside the page's right edge", () => {
    expect(cardPosition({ x: 700, y: 40, width: 50, height: 20 }, frame).left).toBe(472);
  });
});
