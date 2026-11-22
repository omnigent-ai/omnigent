import { describe, expect, it } from "vitest";

import {
  PANE_STRIP_MIN_WIDTH,
  paneStripLayout,
  paneStripMinWidth,
  paneStripShownWidth,
  STRIP_BUTTON_WIDTH,
  STRIP_FIND_MAX_WIDTH,
  STRIP_FIND_MIN_WIDTH,
  STRIP_GAP,
  STRIP_PADDING,
  type PaneStripParts,
} from "./paneStripLayout";

// Every row a pane kind draws: a session or local pane, maximized or not, find open or not, and
// the lone shell with its status and Show Chat.
const ROWS: readonly [string, PaneStripParts][] = [
  ["split pane", { actions: 4, restore: false }],
  ["maximized pane", { actions: 3, restore: true }],
  ["agent pane without Close", { actions: 3, restore: false }],
  ["lone splittable pane", { actions: 2, restore: false }],
  ["identity-only pane", { actions: 0, restore: false }],
  ["lone shell", { actions: 0, restore: false, fixed: [72, STRIP_BUTTON_WIDTH] }],
  ["split pane, find open", { actions: 4, restore: false, findOpen: true }],
  ["maximized pane, find open", { actions: 3, restore: true, findOpen: true }],
];

describe("paneStripLayout", () => {
  it("keeps a split's floor at icon, title, Restore and one action's room", () => {
    expect(PANE_STRIP_MIN_WIDTH).toBe(STRIP_PADDING + 22 + 48 + 20 + 20 + 3 * STRIP_GAP);
  });

  it.each(ROWS)("never draws past the width at or above its floor: %s", (_name, parts) => {
    const floor = paneStripMinWidth(parts);
    for (let width = floor; width <= 900; width += 1) {
      const layout = paneStripLayout(width, parts);
      expect(layout.compact, `at ${width}`).toBe(false);
      expect(paneStripShownWidth(layout, parts), `at ${width}`).toBeLessThanOrEqual(width);
      // Identity stays: find floats over the terminal before it takes the title's room.
      expect(layout.title, `at ${width}`).toBe(true);
    }
  });

  it.each(ROWS)(
    "below the floor keeps only the controls, still inside the row: %s",
    (_name, parts) => {
      const floor = paneStripMinWidth(parts);
      const controls = paneStripShownWidth(
        { compact: true, title: false, actions: 0, find: null },
        parts,
      );
      for (let width = 0; width < floor; width += 1) {
        const layout = paneStripLayout(width, parts);
        if (layout.find?.placement === "row") continue;
        expect(layout.compact, `at ${width}`).toBe(true);
        expect(layout.actions, `at ${width}`).toBe(0);
        // Narrower than its controls, the row shows just them and overflows.
        const shown = paneStripShownWidth(layout, parts);
        if (width >= controls) expect(shown, `at ${width}`).toBeLessThanOrEqual(width);
        else expect(shown, `at ${width}`).toBe(controls);
      }
    },
  );

  it("folds Close first, then Maximize, Split Down and Split Right", () => {
    const parts = { actions: 4, restore: false };
    const floor = paneStripMinWidth(parts);
    const step = STRIP_BUTTON_WIDTH + STRIP_GAP;
    expect(paneStripLayout(floor, parts).actions).toBe(0);
    expect(paneStripLayout(floor + step - 1, parts).actions).toBe(0);
    expect(paneStripLayout(floor + step, parts).actions).toBe(1);
    expect(paneStripLayout(floor + 3 * step, parts).actions).toBe(3);
    expect(paneStripLayout(floor + 4 * step, parts).actions).toBe(4);
    expect(paneStripLayout(2000, parts).actions).toBe(4);
  });

  it("gives a maximized pane's actions the room the label used to take", () => {
    const parts = { actions: 1, restore: true };
    const floor = paneStripMinWidth(parts);
    const step = STRIP_BUTTON_WIDTH + STRIP_GAP;
    expect(paneStripLayout(floor + step - 1, parts).actions).toBe(0);
    expect(paneStripLayout(floor + step, parts).actions).toBe(1);
  });

  it("gives find a usable width, or floats it", () => {
    const parts = { actions: 4, restore: true, findOpen: true };
    for (let width = 0; width <= 900; width += 1) {
      const { find } = paneStripLayout(width, parts);
      expect(find).not.toBeNull();
      if (find?.placement === "row") {
        expect(find.width).toBeGreaterThanOrEqual(STRIP_FIND_MIN_WIDTH);
        expect(find.width).toBeLessThanOrEqual(STRIP_FIND_MAX_WIDTH);
      }
    }
    // The title stays; find floats over the terminal when it can't dock beside it.
    expect(paneStripLayout(300, parts)).toMatchObject({ title: true, find: { placement: "row" } });
    const narrow = paneStripLayout(paneStripMinWidth(parts) + 70, parts);
    expect(narrow).toMatchObject({ title: true, find: { placement: "float" } });
    // A 204 px split pane with find open: with no ⋯ to keep, find docks beside the title.
    const split = { actions: 4, restore: false, findOpen: true };
    expect(paneStripLayout(204, split)).toMatchObject({
      title: true,
      find: { placement: "row", width: 204 - (STRIP_PADDING + 22 + 48 + 2 * STRIP_GAP), fold: 2 },
    });
    expect(paneStripLayout(PANE_STRIP_MIN_WIDTH, parts).find).toEqual({ placement: "float" });
    expect(paneStripLayout(1200, parts).find).toEqual({
      placement: "row",
      width: STRIP_FIND_MAX_WIDTH,
      fold: 0,
    });
  });
});
