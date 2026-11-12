import { afterEach, describe, expect, it, vi } from "vitest";
import { isRowFullyVisible } from "./sidebarRowVisibility";

function placeAt(element: HTMLElement, top: number, bottom: number): void {
  vi.spyOn(element, "getBoundingClientRect").mockReturnValue({ top, bottom } as DOMRect);
}

function rowInScroller(overflowY: string): { scroller: HTMLElement; row: HTMLElement } {
  const scroller = document.createElement("div");
  scroller.style.overflowY = overflowY;
  const group = document.createElement("ul");
  const row = document.createElement("li");
  group.append(row);
  scroller.append(group);
  document.body.append(scroller);
  return { scroller, row };
}

describe("isRowFullyVisible", () => {
  afterEach(() => {
    document.body.replaceChildren();
    vi.restoreAllMocks();
  });

  it("measures against the nearest scrolling ancestor, skipping non-scrolling wrappers", () => {
    const { scroller, row } = rowInScroller("auto");
    placeAt(scroller, 100, 400);
    placeAt(row, 100, 400);
    expect(isRowFullyVisible(row)).toBe(true);
  });

  it("treats a row cut off at either edge of the scroller as not fully visible", () => {
    const { scroller, row } = rowInScroller("scroll");
    placeAt(scroller, 100, 400);
    placeAt(row, 90, 120);
    expect(isRowFullyVisible(row)).toBe(false);
    placeAt(row, 380, 410);
    expect(isRowFullyVisible(row)).toBe(false);
  });

  it("falls back to the window when no ancestor scrolls", () => {
    const { row } = rowInScroller("visible");
    placeAt(row, 0, window.innerHeight);
    expect(isRowFullyVisible(row)).toBe(true);
    placeAt(row, window.innerHeight - 10, window.innerHeight + 10);
    expect(isRowFullyVisible(row)).toBe(false);
  });
});
