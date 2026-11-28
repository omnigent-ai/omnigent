import { describe, expect, it } from "vitest";

import { cssCubicBezier, TAB_STRIP_MOTION, tabStripMotionStyle } from "./tabStripMotion";

describe("tabStripMotionStyle", () => {
  it("hands the CSS the same durations and curves the model's clock uses", () => {
    expect(tabStripMotionStyle(TAB_STRIP_MOTION)).toEqual({
      "--tab-enter-duration": "200ms",
      "--tab-leave-duration": "130ms",
      "--tab-reflow-duration": "180ms",
      "--tab-enter-easing": cssCubicBezier(TAB_STRIP_MOTION.enterEasing),
      "--tab-leave-easing": cssCubicBezier(TAB_STRIP_MOTION.leaveEasing),
      "--tab-reflow-easing": cssCubicBezier(TAB_STRIP_MOTION.reflowEasing),
    });
    expect(cssCubicBezier(TAB_STRIP_MOTION.enterEasing)).toBe("cubic-bezier(0.2, 0, 0, 1)");
  });
});
