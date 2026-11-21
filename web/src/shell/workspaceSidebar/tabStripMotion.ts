// How the flat tab strip moves: one source for the durations and easing the CSS reads
// through custom properties, and for the timers that end each phase.

export interface CubicBezier {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface TabStripMotion {
  /** A new tab grows from nothing to its width. */
  enterMs: number;
  /** A closed tab shrinks to nothing while its neighbours close the gap. */
  leaveMs: number;
  /** Tabs ease to a new shared width after a tab comes or goes, or a held width lets go. */
  reflowMs: number;
  /**
   * How long past its duration a phase stays marked. CSS starts an animation at the next
   * style pass, after the commit, so ending a phase on time would cut its last frames;
   * at rest a ghost is already collapsed and a grown tab already full, so the slack is unseen.
   */
  settleSlackMs: number;
  enterEasing: CubicBezier;
  leaveEasing: CubicBezier;
  reflowEasing: CubicBezier;
}

/** Quick to start and gentle to settle, like the tab strips of desktop browsers. */
const SETTLE: CubicBezier = { x1: 0.2, y1: 0, x2: 0, y2: 1 };
/** Picks up speed as the tab goes, so it leaves rather than lingers. */
const DEPART: CubicBezier = { x1: 0.4, y1: 0, x2: 1, y2: 1 };

export const TAB_STRIP_MOTION: TabStripMotion = {
  enterMs: 200,
  leaveMs: 130,
  reflowMs: 180,
  settleSlackMs: 250,
  enterEasing: SETTLE,
  leaveEasing: DEPART,
  reflowEasing: SETTLE,
};

export function cssCubicBezier({ x1, y1, x2, y2 }: CubicBezier): string {
  return `cubic-bezier(${x1}, ${y1}, ${x2}, ${y2})`;
}

/** The custom properties the strip's CSS reads its durations and easing from. */
export function tabStripMotionStyle(motion: TabStripMotion): Record<string, string> {
  return {
    "--tab-enter-duration": `${motion.enterMs}ms`,
    "--tab-leave-duration": `${motion.leaveMs}ms`,
    "--tab-reflow-duration": `${motion.reflowMs}ms`,
    "--tab-enter-easing": cssCubicBezier(motion.enterEasing),
    "--tab-leave-easing": cssCubicBezier(motion.leaveEasing),
    "--tab-reflow-easing": cssCubicBezier(motion.reflowEasing),
  };
}
