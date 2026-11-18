import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  reanchorOvershoot,
  splitThumbMove,
  TRANSCRIPT_SCROLLBAR_DRAG_EVENT,
  TranscriptScrollbar,
} from "./TranscriptScrollbar";

/**
 * Build a scrollable transcript container the scrollbar can attach to.
 * jsdom reports zero layout, so the scroll metrics are stubbed directly.
 */
function makeScroller({ clientHeight = 800, scrollHeight = 3000 } = {}) {
  const el = document.createElement("div");
  const metrics = { scrollHeight, scrollTop: 0 };
  Object.defineProperty(el, "clientHeight", { value: clientHeight });
  Object.defineProperty(el, "scrollHeight", { get: () => metrics.scrollHeight });
  // Like a browser, scrollTop stays within the scroll range.
  Object.defineProperty(el, "scrollTop", {
    get: () => metrics.scrollTop,
    set: (value: number) => {
      metrics.scrollTop = Math.min(Math.max(0, value), metrics.scrollHeight - clientHeight);
    },
  });
  return { el, metrics, stopScroll: vi.fn() };
}

/** Render a scrollbar and grab its thumb at `clientY`, recording drag directions. */
function grabThumb(scroller: ReturnType<typeof makeScroller>, clientY: number) {
  render(<TranscriptScrollbar scroller={scroller} />);
  const thumb = screen.getByTestId("transcript-scrollbar-thumb");
  // jsdom has no pointer capture; stub the API the handlers call.
  thumb.setPointerCapture = vi.fn();
  thumb.hasPointerCapture = vi.fn().mockReturnValue(true);
  thumb.releasePointerCapture = vi.fn();
  const directions: string[] = [];
  scroller.el.addEventListener(TRANSCRIPT_SCROLLBAR_DRAG_EVENT, (event) => {
    directions.push((event as CustomEvent<{ direction: string }>).detail.direction);
  });
  fireEvent.pointerDown(thumb, { pointerId: 1, clientY });
  const moveTo = (y: number) => fireEvent.pointerMove(thumb, { pointerId: 1, clientY: y });
  return { directions, moveTo };
}

// 800px pane over a 3000px document: track 724, thumb 193, travel 531, range 2200.
const SCROLL_PER_PX = 2200 / 531;

afterEach(cleanup);

describe("TranscriptScrollbar thumb", () => {
  it("does not paint a thumb for a rounding-noise scroll range", () => {
    // Fractional content heights and the latest-turn spacer's 1px write
    // hysteresis can leave the document a couple of pixels taller than the
    // viewport. A thumb for that noise advertises hidden content that does
    // not exist, so the scrollbar must not render.
    render(
      <TranscriptScrollbar scroller={makeScroller({ clientHeight: 800, scrollHeight: 808 })} />,
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
    // 809 − 800 sits right on the threshold: the gate is `<`, so a real range
    // of exactly 9px still gets an indicator. Locks the boundary against an
    // accidental flip to `<=`.
    render(
      <TranscriptScrollbar scroller={makeScroller({ clientHeight: 800, scrollHeight: 809 })} />,
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

  it("waits for the pointer to come back after a drag past the top", () => {
    const scroller = makeScroller();
    scroller.el.scrollTop = 100 * SCROLL_PER_PX;
    const { directions, moveTo } = grabThumb(scroller, 300);
    // 100px reaches the top; the next 50px are past it.
    moveTo(150);
    expect(scroller.el.scrollTop).toBe(0);
    // Coming back, the thumb stays put until the pointer is where it held the thumb.
    moveTo(190);
    expect(scroller.el.scrollTop).toBe(0);
    moveTo(210);
    expect(scroller.el.scrollTop).toBeCloseTo(10 * SCROLL_PER_PX, 5);
    // The history loader still hears every move, overshoot included.
    expect(directions).toEqual(["up", "down", "down"]);
  });

  it("waits for the pointer to come back after a drag past the bottom", () => {
    const scroller = makeScroller();
    scroller.el.scrollTop = 2200 - 20 * SCROLL_PER_PX;
    const { directions, moveTo } = grabThumb(scroller, 500);
    moveTo(580);
    expect(scroller.el.scrollTop).toBe(2200);
    moveTo(530);
    expect(scroller.el.scrollTop).toBe(2200);
    moveTo(500);
    expect(scroller.el.scrollTop).toBeCloseTo(2200 - 20 * SCROLL_PER_PX, 5);
    expect(directions).toEqual(["down", "up", "up"]);
  });

  it("keeps the pointer's overshoot measured from the thumb when a history page lands", () => {
    const scroller = makeScroller();
    const { directions, moveTo } = grabThumb(scroller, 300);
    moveTo(200);
    expect(scroller.el.scrollTop).toBe(0);
    // Dragging up at the top fetched an older page; the transcript holds the
    // reader by moving scrollTop down by the page's height.
    scroller.metrics.scrollHeight = 4000;
    scroller.el.scrollTop = 1000;
    // max 3200; track 724, thumb round(724 * 800 / 4000) = 145, travel 579.
    const scrollPerPx = 3200 / 579;
    const heldThumbOffset = 1000 / scrollPerPx;
    // Dragging further up scrolls on from the hold instead of jumping to the top.
    moveTo(190);
    const continued = 1000 - 10 * scrollPerPx;
    expect(scroller.el.scrollTop).toBeCloseTo(continued, 5);
    // The thumb moved down without the pointer, so the way back waits for
    // the pointer to reach it: 100px of overshoot plus the thumb's move.
    const owed = 100 + heldThumbOffset;
    moveTo(190 + owed - 1);
    expect(scroller.el.scrollTop).toBeCloseTo(continued, 5);
    moveTo(190 + owed + 20);
    expect(scroller.el.scrollTop).toBeCloseTo(continued + 20 * scrollPerPx, 5);
    expect(directions).toEqual(["up", "up", "down", "down"]);
  });
});

describe("thumb drag overshoot", () => {
  it("pays overshoot back only with moves toward the thumb", () => {
    expect(splitThumbMove(0, 12)).toEqual({ overshootPx: 0, scrollPx: 12 });
    expect(splitThumbMove(-30, -5)).toEqual({ overshootPx: -30, scrollPx: -5 });
    expect(splitThumbMove(-30, 10)).toEqual({ overshootPx: -20, scrollPx: 0 });
    expect(splitThumbMove(-30, 30)).toEqual({ overshootPx: 0, scrollPx: 0 });
    expect(splitThumbMove(-30, 45)).toEqual({ overshootPx: 0, scrollPx: 15 });
    expect(splitThumbMove(25, -40)).toEqual({ overshootPx: 0, scrollPx: -15 });
  });

  it("measures overshoot from wherever the thumb was moved to", () => {
    // Pointer above the top; a hold moves the thumb 40px further from it.
    expect(reanchorOvershoot(-30, 40)).toBe(-70);
    // Pointer below the bottom; the thumb moves 10px toward it.
    expect(reanchorOvershoot(30, 10)).toBe(20);
    // The thumb reached or passed the pointer: nothing is owed.
    expect(reanchorOvershoot(30, 30)).toBe(0);
    expect(reanchorOvershoot(30, 45)).toBe(0);
    expect(reanchorOvershoot(-30, -50)).toBe(0);
    // Without overshoot, a hold leaves the drag scrolling by its own moves.
    expect(reanchorOvershoot(0, 120)).toBe(0);
  });
});
