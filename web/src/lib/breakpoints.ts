// Canonical encoding of the shell's `md` layout breakpoint.
//
// Tailwind's `md` breakpoint (768px) is the shell's single mobile/desktop
// layout pivot, used both by CSS (`md:` / `max-md:` classes) and by JS call
// sites. Every JS encoding of that line derives from this module so the
// variants (767px / 767.98px / 768) can't drift apart. Viewport width is a
// LAYOUT concern only — input capability (touch, hover) is a separate axis,
// exposed by `useInputCapabilities`.
//
// Pole choice: the layout predicate is "mobile unless provably md+", i.e.
// `!(min-width: 768px)`. Tailwind's `md:` overrides are what produce the
// desktop layout, and they apply only at >= 768px — at fractional widths in
// (767.98, 768) (reachable under browser zoom) neither `md:` nor `max-md:`
// matches, so the page renders its mobile base styles. A max-md-based
// predicate would call that sliver "desktop" and disagree with what's on
// screen; deliberately none exists here.

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
