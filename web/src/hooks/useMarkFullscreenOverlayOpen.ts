import { useLayoutEffect } from "react";

import { getStyleRoot } from "@/lib/host";

export const FULLSCREEN_OVERLAY_OPEN_ATTRIBUTE = "data-fullscreen-overlay-open";

let openFullscreenOverlays = 0;

/**
 * Mark the style root while a full-window view (`data-fullscreen-overlay`) is
 * open. CSS keys on this instead of `html:has([data-fullscreen-overlay])`, which
 * restyles the whole page on every DOM edit, such as each composer keystroke.
 */
export function useMarkFullscreenOverlayOpen(open: boolean): void {
  useLayoutEffect(() => {
    const root = getStyleRoot();
    if (!open || !root) return;
    openFullscreenOverlays += 1;
    root.setAttribute(FULLSCREEN_OVERLAY_OPEN_ATTRIBUTE, "");
    return () => {
      openFullscreenOverlays -= 1;
      if (openFullscreenOverlays === 0) root.removeAttribute(FULLSCREEN_OVERLAY_OPEN_ATTRIBUTE);
    };
  }, [open]);
}
