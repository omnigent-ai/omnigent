import { act, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  type StripControlsLayout,
  stripControlsWidth,
  useStripControlsWidth,
} from "./stripControlsWidth";

// Layouts measured in Chromium: the floating strip with ‹ ›, its controls and Collapse, which
// needs 154px. Past the tabs' room, the strip's 8px end padding is clipped first.
const layout = (sidebarWidth: number, tabsAreaWidth: number, stripOverflow: number) =>
  ({
    sidebarWidth,
    tabsAreaWidth,
    stripOverflow,
    stripEndPadding: 8,
  }) satisfies StripControlsLayout;

describe("stripControlsWidth", () => {
  it.each([
    ["open wide", layout(420, 258, 0)],
    ["with little room for tabs", layout(192, 30, 0)],
    ["clipping only the end padding", layout(156, 0, 6)],
    ["clipping controls", layout(92, 0, 70)],
    ["squeezed to 12px mid-slide", layout(12, 0, 150)],
  ])("needs the same width %s", (_case, measured) => {
    expect(stripControlsWidth(measured)).toBe(154);
  });

  it("rounds a fractional layout up, so a button cut by part of a pixel counts as hidden", () => {
    expect(stripControlsWidth({ ...layout(420, 258, 0), tabsAreaWidth: 257.75 })).toBe(155);
  });
});

describe("useStripControlsWidth", () => {
  // jsdom lays nothing out, so each element reports the width its data-width names.
  const rect = vi
    .spyOn(HTMLElement.prototype, "getBoundingClientRect")
    .mockImplementation(function (this: HTMLElement) {
      const width = Number(this.dataset.width ?? 0);
      return { width, height: 0, x: 0, y: 0, top: 0, left: 0, right: width, bottom: 0 } as DOMRect;
    });
  afterAll(() => rect.mockRestore());

  /** Stands in for the browser's observer: what it watches, and a resize the test triggers. */
  const observed = new Set<Element>();
  let callbacks: (() => void)[] = [];
  const resize = () => act(() => callbacks.forEach((callback) => callback()));
  beforeEach(() => {
    observed.clear();
    callbacks = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: () => void) {
          callbacks.push(callback);
        }
        observe(element: Element) {
          observed.add(element);
        }
        unobserve(element: Element) {
          observed.delete(element);
        }
        disconnect() {
          observed.clear();
        }
      },
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    rect.mockClear();
  });

  function Strip({
    onWidth,
    areaWidth = 258,
    extra = false,
  }: {
    onWidth: (width: number) => void;
    areaWidth?: number;
    extra?: boolean;
  }) {
    const areaRef = useRef<HTMLDivElement>(null);
    useStripControlsWidth(areaRef, onWidth);
    return (
      <aside data-width="420">
        <div>
          <div ref={areaRef} data-width={areaWidth} />
          {extra ? <button type="button" data-testid="extra" /> : null}
        </div>
      </aside>
    );
  }

  it("measures when the strip lays out, and a commit after that reads no layout", () => {
    const onWidth = vi.fn();
    const { rerender } = render(<Strip onWidth={onWidth} />);
    expect(onWidth).not.toHaveBeenCalled();
    resize();
    expect(onWidth).toHaveBeenCalledExactlyOnceWith(162);

    rect.mockClear();
    rerender(<Strip onWidth={onWidth} />);
    expect(rect).not.toHaveBeenCalled();
    expect(onWidth).toHaveBeenCalledTimes(1);
  });

  it("reports a new width when a resize changes the tabs' room", () => {
    const onWidth = vi.fn();
    const { rerender } = render(<Strip onWidth={onWidth} />);
    resize();
    rerender(<Strip onWidth={onWidth} areaWidth={200} />);
    resize();
    expect(onWidth).toHaveBeenLastCalledWith(220);
  });

  it("reports the same width again to another session's sidebar after a switch, reading no layout", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = render(<Strip onWidth={first} />);
    resize();
    expect(first).toHaveBeenCalledExactlyOnceWith(162);
    rect.mockClear();
    rerender(<Strip onWidth={second} />);
    expect(second).toHaveBeenCalledExactlyOnceWith(162);
    expect(rect).not.toHaveBeenCalled();
    rerender(<Strip onWidth={second} />);
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("watches a control that appears, and measures after paint once one goes", () => {
    const onWidth = vi.fn();
    const { rerender } = render(<Strip onWidth={onWidth} />);
    resize();
    rerender(<Strip onWidth={onWidth} extra />);
    const extra = screen.getByTestId("extra");
    expect(observed.has(extra)).toBe(true);

    rect.mockClear();
    rerender(<Strip onWidth={onWidth} areaWidth={240} />);
    expect(observed.has(extra)).toBe(false);
    // Removing a control resizes nothing else while the strip overflows, so this measures itself.
    expect(rect).toHaveBeenCalled();
    expect(onWidth).toHaveBeenLastCalledWith(180);
  });
});
