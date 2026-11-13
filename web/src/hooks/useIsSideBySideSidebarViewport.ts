// Reactive "should the open sidebar sit beside the chat?" hook for the mobile layout.

import { useSyncExternalStore } from "react";

// Wide phones (an unfolded foldable, a small phone in landscape) are still below
// `md`, but have room to show the sidebar beside the chat instead of over it:
// Material's "medium" width class (600dp+) at a squarish-or-wider aspect ratio.
// Tall narrow tablets and narrow desktop windows (fine pointer) stay on the
// overlay drawer.
const SIDE_BY_SIDE_QUERY =
  "(pointer: coarse) and (min-width: 600px) and (max-width: 767.98px) and (min-aspect-ratio: 3/4)";

function subscribeSideBySide(callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mql = window.matchMedia(SIDE_BY_SIDE_QUERY);
  mql.addEventListener("change", callback);
  return () => mql.removeEventListener("change", callback);
}

/** Non-reactive read of {@link useIsSideBySideSidebarViewport}, for event handlers. */
export function isSideBySideSidebarViewport(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(SIDE_BY_SIDE_QUERY).matches;
}

/**
 * True on a mobile-layout viewport wide enough that the open sidebar pushes the
 * chat aside rather than covering it, so both stay usable at once.
 */
export function useIsSideBySideSidebarViewport(): boolean {
  return useSyncExternalStore(subscribeSideBySide, isSideBySideSidebarViewport, () => false);
}
