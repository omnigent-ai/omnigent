// Exercises the real virtualizer, which the shared test setup replaces with a
// pass-through that renders every row and would hide windowing bugs.
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { useRef, type RefObject } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { VirtualSessionRows, virtualRowStyle } from "./VirtualSessionRows";

vi.unmock("@tanstack/react-virtual");

const VIEWPORT_HEIGHT = 320;
// Differs from the component's initial estimate, so sizing must follow the first measured row.
const ROW_HEIGHT = 36;
const ITEM_COUNT = 2_000;
const scrollIntoView = vi.fn();
const items = Array.from({ length: ITEM_COUNT }, (_, index) => ({ id: `session-${index}` }));

const restorers: (() => void)[] = [];
function stub<Key extends keyof HTMLElement>(key: Key, descriptor: PropertyDescriptor): void {
  const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, key);
  Object.defineProperty(HTMLElement.prototype, key, { configurable: true, ...descriptor });
  restorers.push(() => {
    if (original) Object.defineProperty(HTMLElement.prototype, key, original);
    else Reflect.deleteProperty(HTMLElement.prototype, key);
  });
}
const isScroller = (element: HTMLElement) => element.dataset.testid === "scroller";

beforeAll(() => {
  // jsdom does no layout: give the scroller a viewport and every row a height.
  stub("clientHeight", {
    get(this: HTMLElement) {
      return isScroller(this) ? VIEWPORT_HEIGHT : 0;
    },
  });
  // The virtualizer measures rows by offsetHeight; keep it in step with their rects.
  stub("offsetHeight", {
    get(this: HTMLElement) {
      if (isScroller(this)) return VIEWPORT_HEIGHT;
      return this.tagName === "LI" ? ROW_HEIGHT : 0;
    },
  });
  stub("offsetWidth", {
    get(this: HTMLElement) {
      return isScroller(this) ? 300 : 0;
    },
  });
  stub("scrollHeight", {
    get(this: HTMLElement) {
      const list = this.querySelector<HTMLElement>("ul");
      return Number.parseFloat(list?.style.height ?? "0") || 0;
    },
  });
  stub("getBoundingClientRect", {
    value(this: HTMLElement) {
      if (isScroller(this)) {
        return {
          top: 0,
          left: 0,
          right: 300,
          bottom: VIEWPORT_HEIGHT,
          width: 300,
          height: VIEWPORT_HEIGHT,
          x: 0,
          y: 0,
        };
      }
      const height = this.tagName === "LI" ? ROW_HEIGHT : 0;
      // Content moves up as its scroller scrolls; a row sits at its own offset in the list.
      const scroller = this.closest<HTMLElement>("[data-testid=scroller]");
      const scrolledBy = scroller?.scrollTop ?? 0;
      const rowTop = this.tagName === "LI" ? Number.parseFloat(this.style.top || "0") : 0;
      const top = (this.tagName === "UL" || this.tagName === "LI" ? -scrolledBy : 0) + rowTop;
      return { top, left: 0, right: 300, bottom: top + height, width: 300, height, x: 0, y: top };
    },
  });
  const scrollTops = new WeakMap<HTMLElement, number>();
  stub("scrollTop", {
    get(this: HTMLElement) {
      return scrollTops.get(this) ?? 0;
    },
    set(this: HTMLElement, value: number) {
      scrollTops.set(this, Math.max(0, value));
    },
  });
  stub("scrollTo", {
    value(this: HTMLElement, options: ScrollToOptions) {
      this.scrollTop = options.top ?? this.scrollTop;
      this.dispatchEvent(new Event("scroll"));
    },
  });
  stub("scrollIntoView", { value: scrollIntoView });
});

afterAll(() => {
  for (const restore of restorers.reverse()) restore();
});

afterEach(() => {
  cleanup();
  scrollIntoView.mockClear();
});

function Harness({
  rows = items,
  revealId = null,
  revealReady,
  scrollerRef,
}: {
  rows?: readonly { id: string }[];
  revealId?: string | null;
  revealReady?: boolean;
  scrollerRef?: (scroller: RefObject<HTMLElement | null>) => void;
}) {
  const scroller = useRef<HTMLElement | null>(null);
  scrollerRef?.(scroller);
  return (
    <nav ref={scroller} data-testid="scroller" style={{ overflowY: "auto" }}>
      <VirtualSessionRows
        items={rows}
        scrollElementRef={scroller}
        revealId={revealId}
        revealReady={revealReady}
        renderRow={(item, placement) => (
          <li
            key={item.id}
            ref={placement.measureRef}
            data-index={placement.index}
            data-session={item.id}
            style={virtualRowStyle(placement.top)}
          >
            <button type="button">{item.id}</button>
          </li>
        )}
      />
    </nav>
  );
}

const scroller = () => document.querySelector<HTMLElement>("[data-testid=scroller]")!;
const row = (id: string) => document.querySelector(`[data-session="${id}"]`);
const scrollTo = (top: number) =>
  act(() => {
    scroller().scrollTop = top;
    scroller().dispatchEvent(new Event("scroll"));
  });

describe("VirtualSessionRows", () => {
  it("mounts only a window of rows while sizing the list for all of them", () => {
    render(<Harness />);
    expect(document.querySelectorAll("li").length).toBeLessThan(60);
    expect(scroller().scrollHeight).toBeGreaterThanOrEqual(ITEM_COUNT * ROW_HEIGHT);
  });

  it("renders the rows at any scroll position at once", () => {
    render(<Harness />);
    scrollTo(1_500 * (ROW_HEIGHT + 1));
    expect(row("session-1500")).not.toBeNull();
    expect(row("session-0")).toBeNull();
  });

  it("keeps the row last pointed at mounted after scrolling it away", () => {
    render(<Harness />);
    fireEvent.pointerDown(row("session-3")!.querySelector("button")!);
    scrollTo(1_000 * (ROW_HEIGHT + 1));
    expect(row("session-3")).not.toBeNull();
    expect(row("session-1000")).not.toBeNull();
  });

  it("keeps the row holding focus mounted while another row is pressed to scroll", () => {
    render(<Harness />);
    // A rename field has focus in session-3; a touch scroll starts on another row.
    act(() => row("session-3")!.querySelector("button")!.focus());
    fireEvent.pointerDown(row("session-5")!.querySelector("button")!);
    scrollTo(1_000 * (ROW_HEIGHT + 1));
    expect(row("session-3")).not.toBeNull();
    expect(row("session-5")).not.toBeNull();
    expect(document.activeElement).toBe(row("session-3")!.querySelector("button"));
  });

  it("releases the focused row when focus moves outside, even under the same data-index", () => {
    render(<Harness />);
    const outside = document.createElement("div");
    outside.dataset.index = "3";
    outside.innerHTML = '<button type="button">elsewhere</button>';
    document.body.append(outside);
    try {
      act(() => row("session-3")!.querySelector("button")!.focus());
      act(() => outside.querySelector("button")!.focus());
      fireEvent.pointerDown(row("session-5")!.querySelector("button")!);
      scrollTo(1_000 * (ROW_HEIGHT + 1));
      expect(row("session-3")).toBeNull();
    } finally {
      outside.remove();
    }
  });

  it("lets a row scroll away once focus has left it", () => {
    render(<Harness />);
    const button = row("session-3")!.querySelector("button")!;
    act(() => button.focus());
    act(() => button.blur());
    fireEvent.pointerDown(row("session-5")!.querySelector("button")!);
    scrollTo(1_000 * (ROW_HEIGHT + 1));
    expect(row("session-3")).toBeNull();
  });

  it("reveals the row once when revealId changes, not again on later renders", () => {
    const { rerender } = render(<Harness />);
    rerender(<Harness revealId="session-1500" />);
    expect(scroller().scrollTop).toBeGreaterThan(1_400 * ROW_HEIGHT);
    expect(row("session-1500")).not.toBeNull();
    scrollTo(0);
    rerender(<Harness revealId="session-1500" />);
    expect(scroller().scrollTop).toBe(0);
  });

  it("reveals a row given on first mount, when the scroller mounts in the same commit", async () => {
    render(<Harness revealId="session-1500" />);
    // The first scroll uses the initial estimate; the virtualizer re-aims on later frames
    // once rows measure taller, so the row still ends up on screen.
    await waitFor(() => {
      expect(scroller().scrollTop).toBeGreaterThan(1_400 * ROW_HEIGHT);
      expect(row("session-1500")).not.toBeNull();
    });
  });

  it("watches the scroller's content for layout changes when both mount in the same commit", () => {
    const observed: Element[] = [];
    const original = globalThis.ResizeObserver;
    globalThis.ResizeObserver = class {
      observe(target: Element) {
        observed.push(target);
      }
      unobserve() {}
      disconnect() {}
    };
    try {
      render(<Harness />);
      // The virtualizer observes the scroller itself; the list offset needs its content too.
      expect(observed).toContain(scroller().querySelector("ul"));
    } finally {
      globalThis.ResizeObserver = original;
    }
  });

  it("leaves a fully visible revealed row in place and scrolls a cut-off mounted row into view", () => {
    const { rerender } = render(<Harness />);
    rerender(<Harness revealId="session-2" />);
    expect(scrollIntoView).not.toHaveBeenCalled();
    // Row 9 starts at 9 * (ROW_HEIGHT + gap) = 333 px, past the 320 px viewport, but is mounted as overscan.
    rerender(<Harness revealId="session-9" />);
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(scrollIntoView.mock.contexts[0]).toBe(row("session-9"));
  });

  it("doesn't walk every row's key again when the list is unchanged", () => {
    const rowsRead = new Set<string>();
    const countedRows = items.map(({ id }) => ({
      get id() {
        rowsRead.add(id);
        return id;
      },
    }));
    const { rerender } = render(<Harness rows={countedRows} />);
    rowsRead.clear();
    scrollTo(500 * (ROW_HEIGHT + 1));
    scrollTo(510 * (ROW_HEIGHT + 1));
    rerender(<Harness rows={countedRows} />);
    // Reading the rows near the viewport is expected; re-keying all 2,000 is not.
    expect(rowsRead.size).toBeGreaterThan(0);
    expect(rowsRead.size).toBeLessThan(100);
  });

  it("follows a reordered list to the rows' new positions", () => {
    const { rerender } = render(<Harness />);
    expect(row("session-0")?.getAttribute("data-index")).toBe("0");
    rerender(<Harness rows={[...items].reverse()} />);
    expect(row("session-1999")?.getAttribute("data-index")).toBe("0");
    expect(row("session-0")).toBeNull();
  });

  it("waits for revealReady before revealing", () => {
    const { rerender } = render(<Harness revealId="session-1500" revealReady={false} />);
    expect(scroller().scrollTop).toBe(0);
    rerender(<Harness revealId="session-1500" revealReady />);
    expect(scroller().scrollTop).toBeGreaterThan(1_400 * ROW_HEIGHT);
  });
});
