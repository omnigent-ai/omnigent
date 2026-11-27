import { afterEach, describe, expect, it, vi } from "vitest";

import { createPaneHover, HOVER_GRACE_MS, stripShown } from "./paneHover";

const created: { dispose: () => void }[] = [];

afterEach(() => {
  for (const hover of created.splice(0)) hover.dispose();
  vi.useRealTimers();
});

function setup() {
  vi.useFakeTimers();
  const dropped: string[] = [];
  const hover = createPaneHover({ onFrameHoverDropped: (key) => dropped.push(key) });
  created.push(hover);
  const shown = (key: string, dividerDragging = false) =>
    stripShown(hover.get(key), dividerDragging);
  return { hover, dropped, shown };
}

describe("createPaneHover", () => {
  it("shows a session pane's strip while the page sees the pointer in it, then after a grace", () => {
    const { hover, shown } = setup();
    hover.pointerMoved("a");
    expect(shown("a")).toBe(true);
    hover.pointerLeft();
    expect(shown("a")).toBe(true);
    vi.advanceTimersByTime(HOVER_GRACE_MS);
    expect(shown("a")).toBe(false);
  });

  it("keeps the strip while the pointer crosses from it into its frame", () => {
    const { hover, shown } = setup();
    hover.pointerMoved("a");
    hover.pointerMoved(null);
    vi.advanceTimersByTime(HOVER_GRACE_MS / 2);
    hover.frameReported("a", "over");
    vi.advanceTimersByTime(HOVER_GRACE_MS * 2);
    expect(shown("a")).toBe(true);
  });

  it("keeps the actions while typing: an older frame's typing report still means over", () => {
    const { hover, shown } = setup();
    hover.frameReported("a", "over");
    hover.frameReported("a", "typing");
    expect(shown("a")).toBe(true);
    hover.frameReported("b", "typing");
    expect(shown("b")).toBe(true);
    expect(shown("a")).toBe(true);
    vi.advanceTimersByTime(HOVER_GRACE_MS);
    expect(shown("a")).toBe(false);
  });

  it("keeps a strip with the keyboard through a divider drag", () => {
    const { hover, shown } = setup();
    hover.setFocusInStrip("a", true);
    expect(shown("a", true)).toBe(true);
    hover.setFocusInStrip("a", false);
    expect(shown("a")).toBe(false);
  });

  it("hides every hovered strip while a divider drags", () => {
    const { hover, shown } = setup();
    hover.frameReported("a", "over");
    expect(shown("a", true)).toBe(false);
    expect(shown("a", false)).toBe(true);
  });

  it("drops a frame's hover the page has seen past, and rearms that frame", () => {
    const { hover, dropped, shown } = setup();
    hover.frameReported("a", "over");
    // A move the page sees means the pointer is over no frame.
    hover.pointerMoved(null);
    vi.advanceTimersByTime(HOVER_GRACE_MS);
    expect(shown("a")).toBe(false);
    expect(dropped).toEqual(["a"]);
  });

  it("doesn't rearm a frame that said out itself", () => {
    const { hover, dropped } = setup();
    hover.frameReported("a", "over");
    hover.frameReported("a", "out");
    expect(dropped).toEqual([]);
  });

  it("resets a pane at once and rearms its frame, so its next move shows the strip", () => {
    const { hover, dropped, shown } = setup();
    hover.frameReported("a", "over");
    hover.reset("a");
    expect(shown("a")).toBe(false);
    expect(dropped).toEqual(["a"]);
    // The rearmed frame's first move reports over again.
    hover.frameReported("a", "over");
    expect(shown("a")).toBe(true);
  });

  it("resets every pane", () => {
    const { hover, dropped, shown } = setup();
    hover.frameReported("a", "over");
    hover.frameReported("b", "typing");
    hover.resetAll();
    expect(shown("a")).toBe(false);
    expect(shown("b")).toBe(false);
    expect(new Set(dropped)).toEqual(new Set(["a", "b"]));
  });

  it("moves the hover from one frame to the next with no page move between", () => {
    const { hover, dropped, shown } = setup();
    hover.frameReported("a", "over");
    hover.frameReported("b", "over");
    vi.advanceTimersByTime(HOVER_GRACE_MS);
    expect(shown("a")).toBe(false);
    expect(shown("b")).toBe(true);
    expect(dropped).toEqual(["a"]);
  });

  it("rearms a frame once the pointer moves onto its strip, so moving back reports over", () => {
    const { hover, dropped, shown } = setup();
    hover.frameReported("a", "over");
    hover.pointerMoved("a");
    expect(dropped).toEqual(["a"]);
    expect(shown("a")).toBe(true);
  });

  it("notifies only on a change", () => {
    const { hover } = setup();
    const listener = vi.fn();
    hover.subscribe(listener);
    hover.frameReported("a", "over");
    hover.frameReported("a", "over");
    expect(listener).toHaveBeenCalledTimes(1);
  });
});
