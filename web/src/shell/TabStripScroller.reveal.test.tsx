import { afterEach, describe, expect, it, vi } from "vitest";

import { scrollTabIntoView } from "./TabStripScroller";

function rect(left: number, width: number): DOMRect {
  return { left, right: left + width, width, top: 0, bottom: 20, height: 20 } as DOMRect;
}

/** A 200px-wide tab strip at x=100 holding one 50px tab at `tabLeft`; `clientWidth` 0 before layout. */
function strip(tabLeft: number, tabWidth = 50, clientWidth = 200) {
  const page = document.createElement("div");
  const viewport = document.createElement("div");
  viewport.setAttribute("data-tab-strip-viewport", "");
  const tab = document.createElement("div");
  viewport.append(tab);
  page.append(viewport);
  document.body.append(page);
  let scrollLeft = 40;
  Object.defineProperty(viewport, "scrollLeft", {
    get: () => scrollLeft,
    set: (value: number) => (scrollLeft = value),
  });
  Object.defineProperty(viewport, "clientWidth", { value: clientWidth });
  viewport.getBoundingClientRect = () => rect(100, 200);
  tab.getBoundingClientRect = () => rect(tabLeft, tabWidth);
  return { page, viewport, tab };
}

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

// The scrolling arithmetic is shared with the classic strip and tested there; these
// cases cover what the tab strip adds.
describe("scrollTabIntoView", () => {
  it("shows the whole entry a tab sits in, with its close button", () => {
    const { viewport, tab } = strip(250);
    const entry = document.createElement("div");
    entry.setAttribute("data-tab-strip-entry", "");
    viewport.append(entry);
    entry.append(tab);
    entry.getBoundingClientRect = () => rect(250, 80);
    scrollTabIntoView(tab);
    expect(viewport.scrollLeft).toBe(70);
  });

  it("measures a strip that has no layout yet by its box", () => {
    const { viewport, tab } = strip(280, 50, 0);
    scrollTabIntoView(tab);
    expect(viewport.scrollLeft).toBe(70);
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
