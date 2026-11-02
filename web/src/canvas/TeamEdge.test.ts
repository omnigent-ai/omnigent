import { describe, expect, it } from "vitest";
import { boxBorderPoint } from "./TeamEdge";

const BOX = { x: 0, y: 0, width: 100, height: 40 };

describe("boxBorderPoint", () => {
  it("leaves through the side facing the other card", () => {
    expect(boxBorderPoint(BOX, { x: 500, y: 20 })).toEqual({ x: 100, y: 20 });
    expect(boxBorderPoint(BOX, { x: 50, y: 500 })).toEqual({ x: 50, y: 40 });
    expect(boxBorderPoint(BOX, { x: -500, y: 20 })).toEqual({ x: 0, y: 20 });
  });

  it("stays on the border for a diagonal line", () => {
    const point = boxBorderPoint(BOX, { x: 150, y: 120 });
    expect(point.y).toBe(40);
    expect(point.x).toBeCloseTo(70);
  });

  it("stays at the center when the other card sits exactly on top", () => {
    expect(boxBorderPoint(BOX, { x: 50, y: 20 })).toEqual({ x: 50, y: 20 });
  });
});
