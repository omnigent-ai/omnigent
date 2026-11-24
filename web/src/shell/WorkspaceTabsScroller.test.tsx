import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";

import {
  scrollTabIntoView,
  scrollViewportToShow,
  shouldCompactTabs,
  useCompactWorkspaceTabs,
  WorkspaceTabsScroller,
} from "./WorkspaceTabsScroller";

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
  vi.unstubAllGlobals();
});

describe("scrollTabIntoView", () => {
  // Without a visible width, showing a box uses the strip's whole width, as a tab does.
  it.each([
    ["a tab", (_viewport: HTMLElement, tab: HTMLElement) => scrollTabIntoView(tab)],
    [
      "a box",
      (viewport: HTMLElement, tab: HTMLElement) =>
        scrollViewportToShow(viewport, tab.getBoundingClientRect()),
    ],
  ])("scrolls only the strip to show %s past its right edge", (_shape, show) => {
    const { viewport, tab } = strip(280);
    const scrollIntoView = vi.spyOn(Element.prototype, "scrollIntoView");
    show(viewport, tab);
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

describe("scrollViewportToShow", () => {
  it("shows a box within the strip's visible width, when part of the strip is covered", () => {
    const { viewport } = strip(0);
    // The box ends at x=280: inside the strip's 200px, but past its first 150px.
    scrollViewportToShow(viewport, rect(230, 50));
    expect(viewport.scrollLeft).toBe(40);
    scrollViewportToShow(viewport, rect(230, 50), 150);
    expect(viewport.scrollLeft).toBe(70);
  });
});

describe("shouldCompactTabs", () => {
  it("compacts labelled tabs once they overflow the strip", () => {
    expect(
      shouldCompactTabs({ compact: false, contentWidth: 300, viewportWidth: 200, fullWidth: 300 }),
    ).toBe(true);
    expect(
      shouldCompactTabs({ compact: false, contentWidth: 200, viewportWidth: 200, fullWidth: 200 }),
    ).toBe(false);
  });

  it("shows labels again only when their measured width fits", () => {
    expect(
      shouldCompactTabs({ compact: true, contentWidth: 80, viewportWidth: 250, fullWidth: 300 }),
    ).toBe(true);
    expect(
      shouldCompactTabs({ compact: true, contentWidth: 80, viewportWidth: 300, fullWidth: 300 }),
    ).toBe(false);
  });

  it("keeps the current state while the strip has no layout", () => {
    expect(
      shouldCompactTabs({ compact: true, contentWidth: 0, viewportWidth: 0, fullWidth: 300 }),
    ).toBe(true);
  });
});

describe("WorkspaceTabsScroller compact mode", () => {
  function CompactProbe() {
    return <span data-testid="probe">{useCompactWorkspaceTabs() ? "icons" : "labels"}</span>;
  }

  it("keeps the selected tab in view once tabs switch to icons", () => {
    // A 100px strip whose selected middle tab sits at 400px with labels and 150px as an icon.
    let scrollLeft = 0;
    const isViewport = (element: Element) => element.hasAttribute("data-workspace-tabs-viewport");
    vi.spyOn(Element.prototype, "scrollLeft", "get").mockImplementation(function (this: Element) {
      return isViewport(this) ? scrollLeft : 0;
    });
    vi.spyOn(Element.prototype, "scrollLeft", "set").mockImplementation(function (
      this: Element,
      value: number,
    ) {
      if (isViewport(this)) scrollLeft = value;
    });
    vi.spyOn(Element.prototype, "clientWidth", "get").mockImplementation(function (this: Element) {
      return isViewport(this) ? 100 : 0;
    });
    vi.spyOn(Element.prototype, "scrollWidth", "get").mockImplementation(function (this: Element) {
      return this.parentElement && isViewport(this.parentElement) ? 600 : 0;
    });
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (
      this: Element,
    ) {
      if (isViewport(this)) return rect(0, 100);
      if (this.getAttribute("aria-current") !== "true") return rect(0, 0);
      return this.closest("[data-compact]")
        ? rect(150 - scrollLeft, 30)
        : rect(400 - scrollLeft, 60);
    });

    render(
      <WorkspaceTabsScroller tabCount={12}>
        <span aria-current="true">selected</span>
      </WorkspaceTabsScroller>,
    );

    const box = screen.getByText("selected").getBoundingClientRect();
    expect(document.querySelector("[data-compact]")).not.toBeNull();
    expect(box.left).toBeGreaterThanOrEqual(0);
    expect(box.right).toBeLessThanOrEqual(100);
  });

  it("compacts crowded tabs, restores labels when they fit, and remeasures on a tab change", () => {
    const resizeCallbacks: ResizeObserverCallback[] = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: ResizeObserverCallback) {
          resizeCallbacks.push(callback);
        }
        observe() {}
        disconnect() {}
      },
    );
    let viewportWidth = 100;
    // Labelled tabs at their narrowest; icon-only tabs are narrower still.
    let labelledWidth = 300;
    const compactWidth = 80;
    vi.spyOn(Element.prototype, "clientWidth", "get").mockImplementation(function (this: Element) {
      return this.hasAttribute("data-workspace-tabs-viewport") ? viewportWidth : 0;
    });
    vi.spyOn(Element.prototype, "scrollWidth", "get").mockImplementation(function (this: Element) {
      if (!this.parentElement?.hasAttribute("data-workspace-tabs-viewport")) return 0;
      return this.hasAttribute("data-compact") ? compactWidth : labelledWidth;
    });
    const resize = () =>
      act(() => {
        for (const callback of resizeCallbacks) callback([], {} as ResizeObserver);
      });

    const view = render(
      <WorkspaceTabsScroller tabCount={3}>
        <CompactProbe />
      </WorkspaceTabsScroller>,
    );
    expect(screen.getByTestId("probe")).toHaveTextContent("icons");

    // Wide enough for the labelled tabs again.
    viewportWidth = 300;
    resize();
    expect(screen.getByTestId("probe")).toHaveTextContent("labels");

    viewportWidth = 100;
    resize();
    expect(screen.getByTestId("probe")).toHaveTextContent("icons");

    // Closing tabs remeasures with labels, which now fit the narrow strip.
    labelledWidth = 90;
    view.rerender(
      <WorkspaceTabsScroller tabCount={1}>
        <CompactProbe />
      </WorkspaceTabsScroller>,
    );
    expect(screen.getByTestId("probe")).toHaveTextContent("labels");
  });
});
