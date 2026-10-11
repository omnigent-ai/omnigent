// Canonical `md` (768px) layout breakpoint: every JS reader derives from this
// one `min-width` query so it can't drift from Tailwind's `md:` variants.
// Input capability (touch, hover) is a separate axis: `useInputCapabilities`.
//
// The predicate is "mobile unless provably md+": at fractional widths just under
// 768 (browser zoom) neither `md:` nor `max-md:` matches and the page renders
// its mobile base styles, so a max-md predicate would disagree with the screen.

export const MD_BREAKPOINT_PX = 768;

/** `(min-width: …)` media query — matches Tailwind's `md:` variant. */
export const MD_MIN_WIDTH_QUERY = `(min-width: ${MD_BREAKPOINT_PX}px)`;

// One MediaQueryList for the breakpoint, shared by every reader and
// subscriber (the sidebar calls `isMobileViewport` per row per render, so a
// fresh `matchMedia` each time is measurable). Created lazily and dropped when
// the last subscriber leaves, so tests that swap `window.matchMedia` see the
// replacement on the next read.
let mdList: MediaQueryList | null = null;
const subscribers = new Set<() => void>();

function list(): MediaQueryList {
  return (mdList ??= window.matchMedia(MD_MIN_WIDTH_QUERY));
}

function emit(): void {
  for (const subscriber of subscribers) subscriber();
}

/**
 * Subscribe to the breakpoint's change events. SSR-safe (no-op teardown when
 * matchMedia is unavailable). Returns the unsubscribe function expected by
 * `useSyncExternalStore` subscribe callbacks.
 */
export function subscribeMdBreakpoint(callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  if (subscribers.size === 0) list().addEventListener("change", emit);
  subscribers.add(callback);
  return () => {
    subscribers.delete(callback);
    if (subscribers.size !== 0 || !mdList) return;
    mdList.removeEventListener("change", emit);
    mdList = null;
  };
}

/**
 * True on mobile-layout viewports: anything not provably at `md`+ (see the
 * module comment on the pole choice). Non-reactive point-in-time check for
 * event handlers; components that must re-render on breakpoint crossings use
 * `useIsMobileViewport` instead — its snapshot IS this function, so the two
 * can never disagree. SSR-safe (returns false when window is undefined).
 */
export function isMobileViewport(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  // Read through the cached list only while a subscriber keeps it alive;
  // otherwise query fresh so a swapped `matchMedia` (tests) is honored.
  return !(mdList ?? window.matchMedia(MD_MIN_WIDTH_QUERY)).matches;
}

declare global {
  interface Window {
    /** Layout signal consumed by native shells (see publication below). */
    __omnigentIsMobileViewport?: () => boolean;
  }
}

// Native shells (e.g. the Android back handler in NativeBridgeScript.kt)
// consume the web layer's breakpoint signal instead of re-deriving it from
// their own copy of the literal.
if (typeof window !== "undefined") {
  window["__omnigentIsMobileViewport"] = isMobileViewport;
}
