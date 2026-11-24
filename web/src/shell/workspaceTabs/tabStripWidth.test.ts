import { describe, expect, it } from "vitest";

import {
  effectiveTabWidth,
  INITIAL_STRIP_WIDTH_STATE,
  MAX_TAB_WIDTH,
  MIN_TAB_WIDTH,
  pinnedTabsMaxWidth,
  sharedTabWidth,
  stripWidthReducer,
  TAB_GAP,
  type StripWidthEvent,
} from "./tabStripWidth";

describe("sharedTabWidth", () => {
  it.each([
    [420, 1, MAX_TAB_WIDTH],
    [420, 2, MAX_TAB_WIDTH - 1],
    [420, 3, Math.floor((420 - 2 * TAB_GAP) / 3)],
    [420, 12, MIN_TAB_WIDTH],
    [80, 1, MIN_TAB_WIDTH],
    [100, 1, 100],
    [0, 3, MAX_TAB_WIDTH],
    [420, 0, MAX_TAB_WIDTH],
  ])("%i px for %i tabs → %i px", (available, count, width) => {
    expect(sharedTabWidth(available, count)).toBe(width);
  });

  it("gives every tab the same width whatever its title, so they fill the room together", () => {
    const width = sharedTabWidth(600, 4);
    expect(width * 4 + TAB_GAP * 3).toBeLessThanOrEqual(600);
    expect(width * 4 + TAB_GAP * 3).toBeGreaterThan(600 - 4);
  });
});

describe("holding widths after a pointer close", () => {
  const run = (events: StripWidthEvent[]) =>
    events.reduce(stripWidthReducer, INITIAL_STRIP_WIDTH_STATE);

  it("holds the width from the close until the pointer leaves the strip", () => {
    const held = run([{ type: "pointer-enter" }, { type: "closed-by-pointer", width: 120 }]);
    expect(effectiveTabWidth(held, 420, 3)).toBe(120);
    const second = stripWidthReducer(held, { type: "closed-by-pointer", width: 140 });
    expect(effectiveTabWidth(second, 420, 2)).toBe(120);
    const left = stripWidthReducer(second, { type: "pointer-leave" });
    expect(effectiveTabWidth(left, 420, 2)).toBe(sharedTabWidth(420, 2));
  });

  it("lays out at once after a keyboard close or a new tab", () => {
    const held = run([{ type: "closed-by-pointer", width: 120 }]);
    expect(effectiveTabWidth(stripWidthReducer(held, { type: "closed-by-keyboard" }), 420, 3)).toBe(
      sharedTabWidth(420, 3),
    );
    expect(effectiveTabWidth(stripWidthReducer(held, { type: "tab-added" }), 420, 5)).toBe(
      sharedTabWidth(420, 5),
    );
  });

  it("keeps state unchanged for events that change nothing", () => {
    expect(stripWidthReducer(INITIAL_STRIP_WIDTH_STATE, { type: "closed-by-keyboard" })).toBe(
      INITIAL_STRIP_WIDTH_STATE,
    );
    const inside = run([{ type: "pointer-enter" }]);
    expect(stripWidthReducer(inside, { type: "pointer-enter" })).toBe(inside);
  });
});

describe("pinnedTabsMaxWidth", () => {
  it("lets pinned tabs take half a wide strip", () => {
    expect(pinnedTabsMaxWidth(600, 40)).toBe(300);
  });

  it("leaves room for one tab beside them in a narrow strip", () => {
    expect(pinnedTabsMaxWidth(240, 40)).toBe(240 - 40 - MIN_TAB_WIDTH);
  });

  it("gives them nothing where even one tab doesn't fit", () => {
    expect(pinnedTabsMaxWidth(100, 40)).toBe(0);
  });
});
