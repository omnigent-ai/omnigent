// The selection bar's fit: the roomiest arrangement whose whole width fits the list, giving up
// in a fixed order, with More paid for once whenever it lists anything.

import { describe, expect, it } from "vitest";

import {
  ROOMY_SELECTION_BAR,
  type SelectionBarMeasurements,
  fitSelectionBar,
  selectionBarWidth,
} from "./selectionBarFit";

// count 80 (number 20) + gap, divider 9 + gap, three actions, More 28, Clear 28: gaps of 2.
const measurements = (over: Partial<SelectionBarMeasurements> = {}): SelectionBarMeasurements => ({
  available: 1000,
  gap: 2,
  count: { full: 80, number: 20 },
  selectAll: null,
  fixed: [9, 28],
  more: 28,
  staticMore: true,
  actions: [
    { full: 120, compact: 80 },
    { full: 160, compact: 90 },
    { full: 130, compact: 60 },
  ],
  ...over,
});

const at = (over: Partial<SelectionBarMeasurements>) => {
  const base = measurements(over);
  return (available: number) => fitSelectionBar({ ...base, available });
};

describe("selectionBarWidth", () => {
  it("adds every rendered item and the gap between each pair, More once", () => {
    // 80 + 9 + 28 + 120 + 160 + 130 + 28 = 555, and 6 gaps of 2.
    expect(selectionBarWidth(measurements(), ROOMY_SELECTION_BAR)).toBe(567);
    // With nothing in More and nothing folded, no trigger and one gap fewer.
    expect(selectionBarWidth(measurements({ staticMore: false }), ROOMY_SELECTION_BAR)).toBe(537);
    // Folding into a More that's already shown doesn't pay for it twice.
    const folded = { ...ROOMY_SELECTION_BAR, folded: 1 };
    expect(selectionBarWidth(measurements(), folded)).toBe(567 - 130 - 2);
    // A fold into a More that wasn't there pays for the trigger once.
    expect(selectionBarWidth(measurements({ staticMore: false }), folded)).toBe(567 - 130 - 2);
  });

  it("counts Select All at the length the fit gives it", () => {
    const withSelectAll = measurements({ selectAll: { full: 90, short: 40 } });
    expect(selectionBarWidth(withSelectAll, ROOMY_SELECTION_BAR)).toBe(567 + 90 + 2);
    expect(selectionBarWidth(withSelectAll, { ...ROOMY_SELECTION_BAR, selectAll: "short" })).toBe(
      567 + 40 + 2,
    );
  });
});

describe("fitSelectionBar", () => {
  it("gives up, in order, labels, Select All's words, actions from the end, then the count's word", () => {
    const fit = at({ selectAll: { full: 90, short: 40 } });
    expect(fit(659)).toEqual(ROOMY_SELECTION_BAR);
    // Short labels, all at once: 80 + 90 + 9 + 28 + 80 + 90 + 60 + 28 = 465, 7 gaps.
    expect(fit(658)).toEqual({ ...ROOMY_SELECTION_BAR, compact: true });
    expect(fit(479)).toEqual({ ...ROOMY_SELECTION_BAR, compact: true });
    expect(fit(478)).toEqual({ ...ROOMY_SELECTION_BAR, compact: true, selectAll: "short" });
    // "All 12": 415, 7 gaps. Then the last action goes to More, then the one before it.
    expect(fit(429)).toEqual({ compact: true, selectAll: "short", folded: 0, count: "full" });
    expect(fit(428)).toEqual({ compact: true, selectAll: "short", folded: 1, count: "full" });
    expect(fit(367)).toEqual({ compact: true, selectAll: "short", folded: 1, count: "full" });
    expect(fit(366)).toEqual({ compact: true, selectAll: "short", folded: 2, count: "full" });
    expect(fit(274)).toEqual({ compact: true, selectAll: "short", folded: 3, count: "full" });
    expect(fit(193)).toEqual({ compact: true, selectAll: "short", folded: 3, count: "full" });
    // Last, the count keeps its number alone; narrower still, that's as small as it gets.
    expect(fit(192)).toEqual({ compact: true, selectAll: "short", folded: 3, count: "number" });
    expect(fit(40)).toEqual({ compact: true, selectAll: "short", folded: 3, count: "number" });
  });

  it("pays for More with the first fold when it lists nothing else", () => {
    const fit = at({ staticMore: false });
    // Short labels without More: 80 + 9 + 28 + 80 + 90 + 60 = 347, 5 gaps.
    expect(fit(357).folded).toBe(0);
    // One fold drops 60 and its gap but adds the trigger: 347 - 60 + 28 = 315, 5 gaps.
    expect(fit(356)).toEqual({ compact: true, selectAll: "short", folded: 1, count: "full" });
    expect(selectionBarWidth(measurements({ staticMore: false }), fit(356))).toBe(325);
  });

  it("fits a bar exactly as wide as the list, to the browser's layout unit", () => {
    const fit = at({});
    expect(fit(567)).toEqual(ROOMY_SELECTION_BAR);
    expect(fit(567 - 1 / 64)).toEqual(ROOMY_SELECTION_BAR);
    expect(fit(567 - 1 / 32).compact).toBe(true);
  });

  it("measures a large count as it is, not as a short one", () => {
    // 1,000 selected is wider: the same list shortens labels sooner.
    const fit = at({ count: { full: 112, number: 36 } });
    expect(fit(599)).toEqual(ROOMY_SELECTION_BAR);
    expect(fit(598).compact).toBe(true);
  });

  it("keeps Remove and the divider whatever it gives up", () => {
    const fit = at({ fixed: [9, 70, 28] });
    // Everything folded, number alone: 20 + 9 + 70 + 28 + 28 = 155, 4 gaps.
    expect(fit(163).count).toBe("number");
    expect(selectionBarWidth(measurements({ fixed: [9, 70, 28] }), fit(163))).toBe(163);
  });

  it("with no actions, only the count can give anything up", () => {
    const fit = at({ actions: [] });
    expect(fit(200)).toEqual(ROOMY_SELECTION_BAR);
    expect(fit(100)).toEqual({ compact: true, selectAll: "short", folded: 0, count: "number" });
  });
});
