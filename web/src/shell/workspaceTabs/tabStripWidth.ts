// How wide each tab in the workspace strip is. Every tab shares one width, so
// the close button sits at the same spot on each; after a pointer close the
// width holds until the pointer leaves the strip, so the next tab's × lands
// under the pointer and tabs can be closed one after another without moving it.

/** The narrowest a tab gets before the strip scrolls instead. */
export const MIN_TAB_WIDTH = 96;
/** The widest a tab gets, however few tabs there are. */
export const MAX_TAB_WIDTH = 210;
/** The gap between tabs, matching the strip's list. */
export const TAB_GAP = 2;

/**
 * How wide the pinned tabs may get in a strip `areaWidth` wide, beside `reserved` px of its
 * own controls: half the strip, and never so much that one tab no longer fits beside them.
 */
export function pinnedTabsMaxWidth(areaWidth: number, reserved: number): number {
  return Math.max(0, Math.min(areaWidth / 2, areaWidth - reserved - MIN_TAB_WIDTH));
}

/** The shared width for `count` tabs in `available` pixels. */
export function sharedTabWidth(available: number, count: number): number {
  if (count <= 0 || !Number.isFinite(available) || available <= 0) return MAX_TAB_WIDTH;
  const split = (available - TAB_GAP * (count - 1)) / count;
  return Math.floor(Math.min(MAX_TAB_WIDTH, Math.max(MIN_TAB_WIDTH, split)));
}

export interface StripWidthState {
  pointerInside: boolean;
  /** The width held since a pointer close, until the pointer leaves the strip. */
  heldWidth: number | null;
}

export type StripWidthEvent =
  | { type: "pointer-enter" }
  | { type: "pointer-leave" }
  /** A tab was closed by its × or a middle click, while tabs were `width` wide. */
  | { type: "closed-by-pointer"; width: number }
  /** A tab was closed from the keyboard or a menu: lay out again at once. */
  | { type: "closed-by-keyboard" }
  /** A tab was added, which never keeps an old width. */
  | { type: "tab-added" };

export const INITIAL_STRIP_WIDTH_STATE: StripWidthState = { pointerInside: false, heldWidth: null };

export function stripWidthReducer(state: StripWidthState, event: StripWidthEvent): StripWidthState {
  switch (event.type) {
    case "pointer-enter":
      return state.pointerInside ? state : { ...state, pointerInside: true };
    case "pointer-leave":
      return { pointerInside: false, heldWidth: null };
    case "closed-by-pointer":
      // A close by pointer is from inside the strip, even if no enter was seen.
      return { pointerInside: true, heldWidth: state.heldWidth ?? event.width };
    case "closed-by-keyboard":
    case "tab-added":
      return state.heldWidth === null ? state : { ...state, heldWidth: null };
  }
}

/** The width tabs show: the held one after a pointer close, otherwise the shared width. */
export function effectiveTabWidth(
  state: StripWidthState,
  available: number,
  count: number,
): number {
  return state.heldWidth ?? sharedTabWidth(available, count);
}
