import { afterEach, describe, expect, it, vi } from "vitest";

import { scrollTabIntoView } from "./WorkspaceTabsScroller";

function rect(left: number, width: number): DOMRect {
  return { left, right: left + width, width, top: 0, bottom: 20, height: 20 } as DOMRect;
}

/** A 200px-wide tab strip at x=100 holding one 50px tab at `tabLeft`. */
function strip(tabLeft: number, tabWidth = 50) {
  const page = document.createElement("div");
  const viewport = document.createElement("div");
  viewport.setAttribute("data-workspace-tabs-viewport", "");
  const tab = document.createElement("div");
  viewport.append(tab);
  page.append(viewport);
  document.body.append(page);
  let scrollLeft = 40;
  Object.defineProperty(viewport, "scrollLeft", {
    get: () => scrollLeft,
    set: (value: number) => (scrollLeft = value),
  });
  Object.defineProperty(viewport, "clientWidth", { value: 200 });
  viewport.getBoundingClientRect = () => rect(100, 200);
  tab.getBoundingClientRect = () => rect(tabLeft, tabWidth);
  return { page, viewport, tab };
}

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("scrollTabIntoView", () => {
  it("scrolls only the strip to show a tab past its right edge", () => {
    const { viewport, tab } = strip(280);
    const scrollIntoView = vi.spyOn(Element.prototype, "scrollIntoView");
    scrollTabIntoView(tab);
    expect(viewport.scrollLeft).toBe(70);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("scrolls back to show a tab before its left edge", () => {
    const { viewport, tab } = strip(80);
    scrollTabIntoView(tab);
    expect(viewport.scrollLeft).toBe(20);
  });

  it("keeps the start of a tab wider than the strip in view", () => {
    const { viewport, tab } = strip(150, 300);
    scrollTabIntoView(tab);
    expect(viewport.scrollLeft).toBe(90);
  });

  it("leaves a tab wider than the strip alone while its end is in view", () => {
    const { viewport, tab } = strip(50, 250);
    scrollTabIntoView(tab);
    expect(viewport.scrollLeft).toBe(40);
  });

  it("leaves a visible tab, and a tab outside a strip, alone", () => {
    const { viewport, tab, page } = strip(150);
    scrollTabIntoView(tab);
    expect(viewport.scrollLeft).toBe(40);
    const loose = document.createElement("div");
    page.append(loose);
    expect(() => scrollTabIntoView(loose)).not.toThrow();
    scrollTabIntoView(null);
  });
});
