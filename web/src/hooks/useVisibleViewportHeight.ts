import { useEffect } from "react";

import { isIOSShell } from "@/lib/nativeBridge";

/**
 * Publish the live visual viewport to CSS, and on the iOS shell lock the
 * document against keyboard panning.
 *
 * `position: fixed` anchors to the LAYOUT viewport, which stays tall while the
 * mobile URL bar or the soft keyboard cover its bottom — so a fixed, centered
 * overlay can land below the visible area. `visualViewport` tracks what the
 * user can actually see, so we publish it as `--omnigent-viewport-height` /
 * `--omnigent-viewport-offset`; `index.css` folds those into
 * `--omnigent-visible-*` (with a `100svh` / `0px` pre-JS fallback) and
 * `DialogContent` sizes and centers against them. This runs on every platform:
 * mobile browsers and the Android shell need it as much as iOS does, and off
 * mobile the values simply equal the layout viewport.
 *
 * The iOS shell additionally needs a pan lock. Its native container keeps the
 * WKWebView full-height when the keyboard opens (`.ignoresSafeArea(.keyboard)`),
 * so WebKit pans the whole document up to reveal a focused field — hiding the
 * header and letting the user scroll the entire page. `.app-shell` sizes to
 * `--omnigent-viewport-height` there (see index.css), leaving nothing to
 * scroll, and we snap any residual pan back to the top.
 */
export function useVisibleViewportHeight(): void {
  useEffect(() => {
    const viewport = window.visualViewport;
    if (!viewport) return;

    const root = document.documentElement;
    // Only the iOS shell suffers the keyboard pan; elsewhere the browser's own
    // offset is real and must be reported, not fought.
    const lockPan = isIOSShell();
    let frame = 0;

    const resetPan = () => {
      if (viewport.offsetTop !== 0 || window.scrollY !== 0) window.scrollTo(0, 0);
    };

    const apply = () => {
      frame = 0;
      root.style.setProperty("--omnigent-viewport-height", `${Math.round(viewport.height)}px`);
      root.style.setProperty("--omnigent-viewport-offset", `${Math.round(viewport.offsetTop)}px`);
      if (lockPan) resetPan();
    };

    // Coalesce the burst of resize/scroll events the keyboard animation fires.
    const schedule = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(apply);
    };

    apply();
    viewport.addEventListener("resize", schedule);
    viewport.addEventListener("scroll", schedule);
    if (lockPan) window.addEventListener("scroll", resetPan, { passive: true });
    window.addEventListener("orientationchange", schedule);

    return () => {
      if (frame) window.cancelAnimationFrame(frame);
      viewport.removeEventListener("resize", schedule);
      viewport.removeEventListener("scroll", schedule);
      if (lockPan) window.removeEventListener("scroll", resetPan);
      window.removeEventListener("orientationchange", schedule);
      root.style.removeProperty("--omnigent-viewport-height");
      root.style.removeProperty("--omnigent-viewport-offset");
    };
  }, []);
}
