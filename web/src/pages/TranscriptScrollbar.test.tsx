import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TRANSCRIPT_SCROLLBAR_DRAG_EVENT, TranscriptScrollbar } from "./TranscriptScrollbar";

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
  it("does not paint a thumb for a rounding-noise scroll range", () => {
    // Fractional content heights and the latest-turn spacer's 1px write
    // hysteresis can leave the document a couple of pixels taller than the
    // viewport. A thumb for that noise advertises hidden content that does
    // not exist, so the scrollbar must not render.
    render(
      <TranscriptScrollbar scroller={makeScroller({ clientHeight: 800, scrollHeight: 803 })} />,
    );
    expect(screen.queryByTestId("transcript-scrollbar-thumb")).toBeNull();
  });

  it("paints a thumb once the scroll range is real", () => {
    render(
      <TranscriptScrollbar scroller={makeScroller({ clientHeight: 800, scrollHeight: 850 })} />,
    );
    expect(screen.getByTestId("transcript-scrollbar-thumb")).toBeTruthy();
  });

  it("paints a thumb at exactly the minimum scroll range", () => {
    // 804 − 800 sits right on the threshold: the gate is `<`, so a real range
    // of exactly 4px still gets an indicator. Locks the boundary against an
    // accidental flip to `<=`.
    render(
      <TranscriptScrollbar scroller={makeScroller({ clientHeight: 800, scrollHeight: 804 })} />,
    );
    expect(screen.getByTestId("transcript-scrollbar-thumb")).toBeTruthy();
  });

  it("draws no thumb when the pane is too short for one", () => {
    // track = 100 - 64 - 12 = 24px: no room for a grab-sized thumb plus travel.
    render(
      <TranscriptScrollbar scroller={makeScroller({ clientHeight: 100, scrollHeight: 400 })} />,
    );
    expect(screen.queryByTestId("transcript-scrollbar-thumb")).toBeNull();
  });

  it("sizes the thumb to the visible share of the document", () => {
    // One screen plus a few lines: the thumb spans nearly the whole track, so
    // a few pixels of scrolling move it a few pixels rather than end to end.
    const { rerender } = render(
      <TranscriptScrollbar scroller={makeScroller({ clientHeight: 800, scrollHeight: 831 })} />,
    );
    // track = 724; round(724 * 800 / 831) = 697
    expect(screen.getByTestId("transcript-scrollbar-thumb")).toHaveStyle({ height: "697px" });

    // A long document: the thumb shrinks, down to a grab-sized minimum.
    rerender(
      <TranscriptScrollbar scroller={makeScroller({ clientHeight: 800, scrollHeight: 30_000 })} />,
    );
    expect(screen.getByTestId("transcript-scrollbar-thumb")).toHaveStyle({ height: "56px" });
  });

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

    const directions: string[] = [];
    scroller.el.addEventListener(TRANSCRIPT_SCROLLBAR_DRAG_EVENT, (event) => {
      directions.push((event as CustomEvent<{ direction: string }>).detail.direction);
    });
    // A right-click keeps its context menu and leaves the bottom lock alone.
    fireEvent.pointerDown(thumb, { pointerId: 1, pointerType: "mouse", button: 2, clientY: 100 });
    expect(scroller.stopScroll).not.toHaveBeenCalled();

    fireEvent.pointerDown(thumb, { pointerId: 1, pointerType: "touch", clientY: 100 });
    expect(scroller.stopScroll).toHaveBeenCalled();
    fireEvent.pointerMove(thumb, { pointerId: 1, pointerType: "touch", clientY: 200 });
    // The transcript's history loader hears which way the reader is dragging.
    expect(directions).toEqual(["down"]);

    // track = 800 - 64 - 12 = 724; thumb = round(724 * 800 / 3000) = 193;
    // travel = 724 - 193 = 531; max = 3000 - 800 = 2200.
    // A 100px drag maps to 100 / 531 * 2200 of scroll range.
    expect(scroller.el.scrollTop).toBeCloseTo((100 / 531) * 2200, 5);

    // A history page lands mid-drag and the transcript holds position by
    // moving scrollTop; the next move must build on that, not recompute from
    // where the thumb was grabbed.
    const held = scroller.el.scrollTop + 500;
    scroller.el.scrollTop = held;
    fireEvent.pointerMove(thumb, { pointerId: 1, pointerType: "touch", clientY: 210 });
    expect(scroller.el.scrollTop).toBeCloseTo(held + (10 / 531) * 2200, 5);

    fireEvent.pointerMove(thumb, { pointerId: 1, pointerType: "touch", clientY: 150 });
    expect(directions).toEqual(["down", "down", "up"]);

    fireEvent.pointerUp(thumb, { pointerId: 1, pointerType: "touch", clientY: 150 });
    // Drag ended: further moves must not scroll.
    const settled = scroller.el.scrollTop;
    fireEvent.pointerMove(thumb, { pointerId: 1, pointerType: "touch", clientY: 300 });
    expect(scroller.el.scrollTop).toBe(settled);
  });
});
