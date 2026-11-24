// Each pane row's hover, from the page's pointer moves, its frames' reports and the row's focus.
// Moves, not enter and leave, drive it, since those don't cross origins reliably: a move the page
// sees means no frame has the pointer, so a frame whose hover drops is told to report afresh.

/** What a pane's frame says about the pointer over it; a "typing" report still means "over". */
export type FrameHoverState = "over" | "out" | "typing";

export interface PaneHoverState {
  /** The page saw the pointer inside the pane (a session pane, or a frame pane's strip). */
  pageOver: boolean;
  /** The pane's frame said the pointer is over it. */
  frameOver: boolean;
  /** Something in the strip has the keyboard. */
  focusInStrip: boolean;
  /** Just left: still shown until the grace ends, so crossing into the frame doesn't flicker. */
  grace: boolean;
}

const IDLE: PaneHoverState = Object.freeze({
  pageOver: false,
  frameOver: false,
  focusInStrip: false,
  grace: false,
});

/** How long a strip stays after the pointer leaves, so moving between strip and frame keeps it. */
export const HOVER_GRACE_MS = 150;

/** Whether `state`'s strip shows (pinned parts aside). */
export function stripShown(state: PaneHoverState, dividerDragging: boolean): boolean {
  if (state.focusInStrip) return true;
  if (dividerDragging) return false;
  return state.pageOver || state.frameOver || state.grace;
}

export interface PaneHover {
  get: (paneKey: string) => PaneHoverState;
  subscribe: (listener: () => void) => () => void;
  /** The page saw the pointer move at a point inside `paneKey`, or (null) in none of them. */
  pointerMoved: (paneKey: string | null) => void;
  /** The pointer left the panes for the page outside them. */
  pointerLeft: () => void;
  /** A frame's report for its pane. */
  frameReported: (paneKey: string, state: FrameHoverState) => void;
  setFocusInStrip: (paneKey: string, focused: boolean) => void;
  /** Forget `paneKey`'s hover: the window blurred, the pane hid, its frame went or reloaded. */
  reset: (paneKey: string) => void;
  /** {@link reset} for every pane. */
  resetAll: () => void;
  dispose: () => void;
}

export function createPaneHover({
  onFrameHoverDropped = () => {},
  setTimer = (run, ms) => setTimeout(run, ms),
  clearTimer = (timer) => clearTimeout(timer as ReturnType<typeof setTimeout>),
}: {
  /** The page stopped trusting a frame's "over": the frame must report its next move again. */
  onFrameHoverDropped?: (paneKey: string) => void;
  setTimer?: (run: () => void, ms: number) => unknown;
  clearTimer?: (timer: unknown) => void;
} = {}): PaneHover {
  const states = new Map<string, PaneHoverState>();
  const graceTimers = new Map<string, unknown>();
  const listeners = new Set<() => void>();
  const notify = () => {
    for (const listener of listeners) listener();
  };
  const get = (paneKey: string) => states.get(paneKey) ?? IDLE;
  const clearGrace = (paneKey: string) => {
    const timer = graceTimers.get(paneKey);
    if (timer === undefined) return;
    clearTimer(timer);
    graceTimers.delete(paneKey);
  };
  const update = (paneKey: string, change: Partial<PaneHoverState>) => {
    const before = get(paneKey);
    const next = { ...before, ...change };
    const wasOver = before.pageOver || before.frameOver;
    const over = next.pageOver || next.frameOver;
    // Leaving starts the grace; coming back or a reset ends it.
    if (over || change.grace === false) {
      clearGrace(paneKey);
      next.grace = false;
    } else if (wasOver) {
      clearGrace(paneKey);
      next.grace = true;
      graceTimers.set(
        paneKey,
        setTimer(() => {
          graceTimers.delete(paneKey);
          update(paneKey, { grace: false });
        }, HOVER_GRACE_MS),
      );
    }
    const keys = Object.keys(next) as (keyof PaneHoverState)[];
    if (keys.every((key) => before[key] === next[key])) return;
    if (keys.every((key) => next[key] === false)) states.delete(paneKey);
    else states.set(paneKey, next);
    notify();
  };
  /** The page saw the pointer outside `paneKey`: its frame's "over" is stale, so rearm the frame. */
  const leavePane = (paneKey: string) => {
    const state = get(paneKey);
    if (!state.pageOver && !state.frameOver) return;
    update(paneKey, { pageOver: false, frameOver: false });
    if (state.frameOver) onFrameHoverDropped(paneKey);
  };
  return {
    get,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    pointerMoved(paneKey) {
      // The page saw the pointer, so it isn't over any frame: every other pane's hover is stale.
      for (const key of [...states.keys()]) if (key !== paneKey) leavePane(key);
      if (paneKey === null) return;
      const { frameOver } = get(paneKey);
      update(paneKey, { pageOver: true, frameOver: false });
      if (frameOver) onFrameHoverDropped(paneKey);
    },
    pointerLeft() {
      for (const key of [...states.keys()]) leavePane(key);
    },
    frameReported(paneKey, state) {
      if (state === "out") {
        update(paneKey, { frameOver: false });
        return;
      }
      // The pointer is in this frame, so in no other pane: crossing between frames needn't pass
      // through the page, and boundary events across origins don't arrive reliably.
      for (const key of [...states.keys()]) if (key !== paneKey) leavePane(key);
      update(paneKey, { frameOver: true, pageOver: false });
    },
    setFocusInStrip(paneKey, focused) {
      update(paneKey, { focusInStrip: focused });
    },
    reset(paneKey) {
      update(paneKey, { pageOver: false, frameOver: false, grace: false });
      // Whatever the frame last sent, its next move must report again.
      onFrameHoverDropped(paneKey);
    },
    resetAll() {
      for (const key of [...states.keys()]) {
        update(key, { pageOver: false, frameOver: false, grace: false });
        onFrameHoverDropped(key);
      }
    },
    dispose() {
      for (const key of [...graceTimers.keys()]) clearGrace(key);
      states.clear();
      listeners.clear();
    },
  };
}
