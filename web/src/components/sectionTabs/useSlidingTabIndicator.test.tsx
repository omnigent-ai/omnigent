import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

import {
  TAB_UNDERLINE_GAP_PX,
  tabIndicatorPlacement,
  useSlidingTabIndicator,
} from "./useSlidingTabIndicator";

/** Each tab's box, by its value, as layout would give it; the test changes them to resize. */
const boxes: Record<string, { left: number; width: number }> = {};
const TAB_TOP = 3;
const TAB_HEIGHT = 26;
let hidden = false;

function stubLayout() {
  const box = (element: HTMLElement) => boxes[element.dataset.value ?? ""];
  vi.spyOn(HTMLElement.prototype, "offsetLeft", "get").mockImplementation(function (
    this: HTMLElement,
  ) {
    return box(this)?.left ?? 0;
  });
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockImplementation(function (
    this: HTMLElement,
  ) {
    return hidden ? 0 : (box(this)?.width ?? 0);
  });
  vi.spyOn(HTMLElement.prototype, "offsetTop", "get").mockReturnValue(TAB_TOP);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(TAB_HEIGHT);
}

/** A resize observer the test fires, as the browser would when a tab's width changes. */
const resizes = new Set<() => void>();
class FakeResizeObserver {
  constructor(callback: () => void) {
    resizes.add(() => callback());
  }
  observe() {}
  unobserve() {}
  disconnect() {
    resizes.clear();
  }
}

function SectionTabs({ initial = "summary" }: { initial?: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  useSlidingTabIndicator(containerRef);
  return (
    <div ref={containerRef}>
      <Tabs defaultValue={initial}>
        <TabsList variant="line" aria-label="Pull request" className="relative">
          {["summary", "commits", "changes"].map((value) => (
            <TabsTrigger key={value} value={value} data-value={value}>
              {value}
            </TabsTrigger>
          ))}
          <span aria-hidden data-tab-indicator />
        </TabsList>
      </Tabs>
    </div>
  );
}

const list = () => screen.getByRole("tablist", { name: "Pull request" });
const placement = () => ({
  x: list().style.getPropertyValue("--tab-indicator-x"),
  y: list().style.getPropertyValue("--tab-indicator-y"),
  width: list().style.getPropertyValue("--tab-indicator-width"),
});
const nextFrame = () =>
  act(async () => {
    await new Promise((resolve) => {
      requestAnimationFrame(resolve);
    });
  });

beforeEach(() => {
  Object.assign(boxes, {
    summary: { left: 0, width: 90 },
    commits: { left: 94, width: 100 },
    changes: { left: 198, width: 92 },
  });
  hidden = false;
  stubLayout();
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  resizes.clear();
});

describe("tabIndicatorPlacement", () => {
  it("puts the underline where a line tab draws its own: its width, just below it", () => {
    expect(
      tabIndicatorPlacement({ offsetLeft: 40, offsetTop: 3, offsetWidth: 80, offsetHeight: 26 }),
    ).toEqual({ x: 40, y: 3 + 26 + TAB_UNDERLINE_GAP_PX, width: 80 });
  });
});

describe("useSlidingTabIndicator", () => {
  it("starts under the shown tab, and only then lets it slide", async () => {
    render(<SectionTabs initial="commits" />);
    expect(placement()).toEqual({ x: "94px", y: `${TAB_TOP + TAB_HEIGHT + 3}px`, width: "100px" });
    // Placed before it may animate, so it never slides in from the list's edge.
    expect(list().hasAttribute("data-indicator-placed")).toBe(false);
    await nextFrame();
    expect(list().hasAttribute("data-indicator-placed")).toBe(true);
  });

  it("follows the selection, by pointer and by keyboard", async () => {
    render(<SectionTabs />);
    await userEvent.click(screen.getByRole("tab", { name: "changes" }));
    expect(placement()).toMatchObject({ x: "198px", width: "92px" });
    await userEvent.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "commits" }).getAttribute("aria-selected")).toBe("true");
    expect(placement()).toMatchObject({ x: "94px", width: "100px" });
  });

  it("moves with the tabs as they resize, as a narrow header drops their icons", () => {
    render(<SectionTabs initial="changes" />);
    Object.assign(boxes, {
      summary: { left: 0, width: 70 },
      commits: { left: 74, width: 80 },
      changes: { left: 158, width: 72 },
    });
    act(() => resizes.forEach((fire) => fire()));
    expect(placement()).toMatchObject({ x: "158px", width: "72px" });
  });

  it("leaves the last placement while the tabs are hidden behind the section picker", () => {
    render(<SectionTabs initial="commits" />);
    hidden = true;
    act(() => resizes.forEach((fire) => fire()));
    expect(placement()).toMatchObject({ x: "94px", width: "100px" });
  });
});
