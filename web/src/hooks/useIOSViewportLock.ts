import { useEffect } from "react";

import { isIOSShell } from "@/lib/nativeBridge";

/**
 * Lock the iOS shell to the visual viewport so the soft keyboard can never pan
 * the whole document.
 *
 * The native shell keeps the WKWebView full-height when the keyboard opens
 * (`.ignoresSafeArea(.keyboard)`), so the layout viewport (`window.innerHeight`,
 * `100lvh`) stays full while the keyboard overlays the bottom. Left alone, the
 * composer/inputs sit *behind* the keyboard and WebKit pans the whole document
 * up to reveal the focused field — hiding the header and letting the user scroll
 * the entire page.
 *
 * `visualViewport.height` *does* track the keyboard here (verified: it drops to
 * the area above the keyboard while `innerHeight` stays full), so we publish it
 * to `--omnigent-viewport-height`; the app-shell sizes to that (see index.css).
 * The focused input is then always within the visible area, WebKit never needs
 * to pan, the header stays put, and only the inner scroll panes move. As a
 * safety net we also snap any residual document pan back to the top.
 *
 * No-op off the iOS shell — the browser and Electron handle the keyboard via
 * normal layout, and the CSS var falls back to `100lvh`.
 */
export function useIOSViewportLock(): void {
  useEffect(() => {
    if (!isIOSShell()) return;
    const viewport = window.visualViewport;
    if (!viewport) return;

    const root = document.documentElement;
    let frame = 0;

    // With the shell sized to the visual viewport there is nothing to scroll, so
    // counter any pan WebKit applies while revealing a focused field — that pan
    // is what moves the whole page (and the absolute header). Wired as its own
    // listener too, so it fires the instant WebKit scrolls, not just on the
    // rAF-coalesced resize.
    const resetPan = () => {
      if (viewport.offsetTop !== 0 || window.scrollY !== 0) window.scrollTo(0, 0);
    };

    const apply = () => {
      frame = 0;
      root.style.setProperty("--omnigent-viewport-height", `${Math.round(viewport.height)}px`);
      resetPan();
    };

    // Coalesce the burst of resize/scroll events the keyboard animation fires.
    const schedule = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(apply);
    };

    apply();
    viewport.addEventListener("resize", schedule);
    viewport.addEventListener("scroll", schedule);
    window.addEventListener("scroll", resetPan, { passive: true });
    window.addEventListener("orientationchange", schedule);

    return () => {
      if (frame) window.cancelAnimationFrame(frame);
      viewport.removeEventListener("resize", schedule);
      viewport.removeEventListener("scroll", schedule);
      window.removeEventListener("scroll", resetPan);
      window.removeEventListener("orientationchange", schedule);
      root.style.removeProperty("--omnigent-viewport-height");
    };
  }, []);
}
