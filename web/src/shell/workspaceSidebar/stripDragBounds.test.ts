import { describe, expect, it } from "vitest";

import { clampAlongStrip, stripDragBounds } from "./stripDragBounds";

const first = { left: 100, right: 180 };
const middle = { left: 260, right: 340 };
const last = { left: 500, right: 580 };

describe("stripDragBounds", () => {
  it("lets a tab travel back to the first tab's start and on to the last tab's end", () => {
    expect(stripDragBounds(middle, first, last)).toEqual({ min: -160, max: 240 });
  });

  it("keeps the first tab from going back and the last from going on", () => {
    expect(stripDragBounds(first, first, last)).toEqual({ min: 0, max: 400 });
    expect(stripDragBounds(last, first, last)).toEqual({ min: -400, max: 0 });
  });

  it("lets a lone tab go nowhere", () => {
    expect(stripDragBounds(first, first, first)).toEqual({ min: 0, max: 0 });
  });
});

describe("clampAlongStrip", () => {
  const bounds = { min: -160, max: 240 };

  it("leaves an offset inside the bounds as it is", () => {
    expect(clampAlongStrip(50, 0, bounds)).toBe(50);
    expect(clampAlongStrip(-160, 0, bounds)).toBe(-160);
    expect(clampAlongStrip(240, 0, bounds)).toBe(240);
  });

  it("stops a tab held past either end at that end", () => {
    expect(clampAlongStrip(900, 0, bounds)).toBe(240);
    expect(clampAlongStrip(-900, 0, bounds)).toBe(-160);
  });

  it("counts what the strip scrolled since the drag began against the bounds", () => {
    // Scrolled 200 px on: the row already carried the tab 200 px, leaving 40 px to its end.
    expect(clampAlongStrip(100, 200, bounds)).toBe(40);
    expect(clampAlongStrip(30, 200, bounds)).toBe(30);
    // Scrolled 100 px back: the tab can go 60 px further back before the first tab.
    expect(clampAlongStrip(-300, -100, bounds)).toBe(-60);
  });
});
