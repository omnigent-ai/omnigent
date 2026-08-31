import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TranscriptScrollbar } from "./TranscriptScrollbar";

/**
 * Build a scrollable transcript container the scrollbar can attach to.
 * jsdom reports zero layout, so the scroll metrics are stubbed directly.
 */
function makeScroller({ clientHeight = 800, scrollHeight = 3000 } = {}) {
  const el = document.createElement("div");
  Object.defineProperty(el, "clientHeight", { value: clientHeight });
  Object.defineProperty(el, "scrollHeight", { value: scrollHeight });
  el.scrollTop = 0;
  return { el, stopScroll: vi.fn() };
}

afterEach(cleanup);

describe("TranscriptScrollbar thumb", () => {
  it("opts out of native touch panning so a touch drag reaches the pointer handlers", () => {
    // The drag is driven by pointer events with pointer capture. Without
    // `touch-action: none` on the thumb, a touch pointerdown is followed by
    // the browser's pan arbitration firing pointercancel, and the drag dies
    // before the handlers ever track the finger.
    render(<TranscriptScrollbar scroller={makeScroller()} />);
    const thumb = screen.getByTestId("transcript-scrollbar-thumb");
    expect(thumb.className).toContain("touch-none");
  });

  it("tracks a pointer drag regardless of pointer type", () => {
    const scroller = makeScroller();
    render(<TranscriptScrollbar scroller={scroller} />);
    const thumb = screen.getByTestId("transcript-scrollbar-thumb");
    // jsdom has no PointerEvent capture plumbing; stub the capture API the
    // handlers call so the drag lifecycle can run.
    thumb.setPointerCapture = vi.fn();
    thumb.hasPointerCapture = vi.fn().mockReturnValue(true);
    thumb.releasePointerCapture = vi.fn();

    fireEvent.pointerDown(thumb, { pointerId: 1, pointerType: "touch", clientY: 100 });
    expect(scroller.stopScroll).toHaveBeenCalled();
    fireEvent.pointerMove(thumb, { pointerId: 1, pointerType: "touch", clientY: 200 });

    // travel = 800 - 64 - 12 - 56 = 668; max = 3000 - 800 = 2200.
    // A 100px drag maps to 100 / 668 * 2200 of scroll range.
    expect(scroller.el.scrollTop).toBeCloseTo((100 / 668) * 2200, 5);

    fireEvent.pointerUp(thumb, { pointerId: 1, pointerType: "touch", clientY: 200 });
    // Drag ended: further moves must not scroll.
    const settled = scroller.el.scrollTop;
    fireEvent.pointerMove(thumb, { pointerId: 1, pointerType: "touch", clientY: 300 });
    expect(scroller.el.scrollTop).toBe(settled);
  });
});
