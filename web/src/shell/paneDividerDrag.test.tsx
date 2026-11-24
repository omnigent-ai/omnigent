import { describe, expect, it, vi } from "vitest";

import { createPaneDividerDrag } from "@/shell/paneDividerDrag";

describe("createPaneDividerDrag", () => {
  it("drags while any divider holds the pointer, and tells subscribers when that changes", () => {
    const drag = createPaneDividerDrag();
    const listener = vi.fn();
    drag.subscribe(listener);
    expect(drag.dragging()).toBe(false);
    const endFirst = drag.begin();
    const endSecond = drag.begin();
    expect(drag.dragging()).toBe(true);
    expect(listener).toHaveBeenCalledTimes(1);
    endFirst();
    endFirst();
    expect(drag.dragging()).toBe(true);
    endSecond();
    expect(drag.dragging()).toBe(false);
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("stops telling a subscriber once it unsubscribes", () => {
    const drag = createPaneDividerDrag();
    const listener = vi.fn();
    drag.subscribe(listener)();
    drag.begin()();
    expect(listener).not.toHaveBeenCalled();
  });
});
