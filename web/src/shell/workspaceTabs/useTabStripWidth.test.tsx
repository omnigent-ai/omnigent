import { act, cleanup, render } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TAB_STRIP_MOTION } from "@/shell/workspaceSidebar/tabStripMotion";

import { sharedTabWidth } from "./tabStripWidth";
import { useTabStripWidth } from "./useTabStripWidth";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("useTabStripWidth", () => {
  it("follows the area's width under StrictMode, whose effect replay must not leave it unobserved", () => {
    let areaWidth = 430;
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(() => areaWidth);
    const observers: { callback: ResizeObserverCallback; active: boolean }[] = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        entry: { callback: ResizeObserverCallback; active: boolean };
        constructor(callback: ResizeObserverCallback) {
          this.entry = { callback, active: false };
          observers.push(this.entry);
        }
        observe() {
          this.entry.active = true;
        }
        disconnect() {
          this.entry.active = false;
        }
      },
    );
    let seen = 0;
    function Probe() {
      const strip = useTabStripWidth(3, 0);
      seen = strip.width;
      return <div ref={strip.areaRef} />;
    }
    render(
      <StrictMode>
        <Probe />
      </StrictMode>,
    );
    expect(seen).toBe(sharedTabWidth(430, 3));

    areaWidth = 600;
    act(() => {
      for (const observer of observers.filter((entry) => entry.active)) {
        observer.callback([], {} as ResizeObserver);
      }
    });
    expect(observers.some((entry) => entry.active)).toBe(true);
    expect(seen).toBe(sharedTabWidth(600, 3));
  });

  it("keeps a scrolled row's width on a pointer close, and lets an unscrolled row shrink", () => {
    let strip: ReturnType<typeof useTabStripWidth> | null = null;
    function Probe() {
      strip = useTabStripWidth(3, 0);
      return (
        <div ref={strip.areaRef}>
          <div data-testid="viewport">
            <div ref={strip.rowRef} data-testid="row" />
          </div>
        </div>
      );
    }
    const { getByTestId } = render(<Probe />);
    const viewport = getByTestId("viewport");
    const row = getByTestId("row");
    Object.defineProperty(row, "scrollWidth", { configurable: true, value: 640 });

    act(() => strip!.closedByPointer());
    expect(strip!.holding).toBe(true);
    expect(row.style.minWidth).toBe("");

    viewport.scrollLeft = 120;
    act(() => strip!.closedByPointer());
    expect(row.style.minWidth).toBe("640px");
  });

  describe("reflowing", () => {
    const REFLOW_WINDOW = TAB_STRIP_MOTION.reflowMs + TAB_STRIP_MOTION.settleSlackMs;
    function setup() {
      vi.useFakeTimers();
      let areaWidth = 600;
      vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(() => areaWidth);
      const observers: ResizeObserverCallback[] = [];
      vi.stubGlobal(
        "ResizeObserver",
        class {
          constructor(callback: ResizeObserverCallback) {
            observers.push(callback);
          }
          observe() {}
          disconnect() {}
        },
      );
      let strip: ReturnType<typeof useTabStripWidth> | null = null;
      function Probe({ count }: { count: number }) {
        strip = useTabStripWidth(count, 0);
        return <div ref={strip.areaRef} />;
      }
      const view = render(<Probe count={2} />);
      return {
        strip: () => strip!,
        setCount: (count: number) => view.rerender(<Probe count={count} />),
        resize: (width: number) => {
          areaWidth = width;
          act(() => observers.forEach((observer) => observer([], {} as ResizeObserver)));
        },
      };
    }
    afterEach(() => vi.useRealTimers());

    it("eases tabs to their width when a tab comes or goes, for the reflow's duration", () => {
      const view = setup();
      expect(view.strip().reflowing).toBe(false);
      view.setCount(3);
      expect(view.strip().reflowing).toBe(true);
      act(() => vi.advanceTimersByTime(REFLOW_WINDOW - 1));
      expect(view.strip().reflowing).toBe(true);
      // Another tab during the reflow starts it over.
      view.setCount(4);
      act(() => vi.advanceTimersByTime(REFLOW_WINDOW - 1));
      expect(view.strip().reflowing).toBe(true);
      act(() => vi.advanceTimersByTime(1));
      expect(view.strip().reflowing).toBe(false);
    });

    it("lets the tabs follow a pane resize at once, even one that starts mid-reflow", () => {
      const view = setup();
      view.resize(500);
      expect(view.strip().reflowing).toBe(false);
      view.setCount(3);
      expect(view.strip().reflowing).toBe(true);
      view.resize(420);
      expect(view.strip().reflowing).toBe(false);
      expect(view.strip().width).toBe(sharedTabWidth(420, 3));
    });

    it("eases tabs out to their width when a held pointer close lets go", () => {
      const view = setup();
      act(() => view.strip().closedByPointer());
      view.setCount(1);
      act(() => vi.advanceTimersByTime(REFLOW_WINDOW));
      expect(view.strip().reflowing).toBe(false);
      act(() => view.strip().pointerHandlers.onPointerLeave());
      expect(view.strip().holding).toBe(false);
      expect(view.strip().reflowing).toBe(true);
    });
  });
});
