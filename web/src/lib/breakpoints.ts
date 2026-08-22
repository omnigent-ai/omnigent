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

/**
 * Subscribe to change events on a set of media queries. SSR-safe (no-op
 * teardown when matchMedia is unavailable). Returns the unsubscribe function
 * expected by `useSyncExternalStore` subscribe callbacks.
 */
export function subscribeMatchMedia(queries: readonly string[], callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const lists = queries.map((q) => window.matchMedia(q));
  for (const mql of lists) mql.addEventListener("change", callback);
  return () => {
    for (const mql of lists) mql.removeEventListener("change", callback);
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
  return !window.matchMedia(MD_MIN_WIDTH_QUERY).matches;
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
  // eslint-disable-next-line no-underscore-dangle -- bridge-global naming
  window.__omnigentIsMobileViewport = isMobileViewport;
}
