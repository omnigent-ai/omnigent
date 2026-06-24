import { useEffect } from "react";

import { isIOSShell } from "@/lib/nativeBridge";

/**
 * Lock the iOS shell to the visual viewport so the soft keyboard can never pan
 * the whole document.
 *
 * The native shell keeps the WKWebView full-height when the keyboard opens
 * (`.ignoresSafeArea(.keyboard)`), and the shell is otherwise sized to the
 * large viewport (`100lvh`). So when the keyboard appears the composer/inputs
 * sit *behind* it, and WebKit pans the entire document up to reveal the focused
 * field — hiding the header and letting the user scroll the whole page, which
 * breaks the fixed-shell layout.
 *
 * Instead we publish the live `visualViewport.height` to
 * `--omnigent-viewport-height`; the app-shell sizes to that (see index.css), so
 * the focused input is always within the visible area. WebKit then never needs
 * to pan, the header stays put, and only the inner scroll panes (conversation
 * history, terminal, page bodies) move. As a safety net we also snap any
 * residual document pan back to the top.
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

    const apply = () => {
      frame = 0;
      root.style.setProperty("--omnigent-viewport-height", `${Math.round(viewport.height)}px`);
      // With the shell sized to the visual viewport there is nothing to scroll,
      // so counter any transient pan WebKit applies while revealing a field.
      if (viewport.offsetTop !== 0 || window.scrollY !== 0) window.scrollTo(0, 0);
    };

    // Coalesce the burst of resize/scroll events the keyboard animation fires.
    const schedule = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(apply);
    };

    apply();
    viewport.addEventListener("resize", schedule);
    viewport.addEventListener("scroll", schedule);
    window.addEventListener("orientationchange", schedule);

    return () => {
      if (frame) window.cancelAnimationFrame(frame);
      viewport.removeEventListener("resize", schedule);
      viewport.removeEventListener("scroll", schedule);
      window.removeEventListener("orientationchange", schedule);
      root.style.removeProperty("--omnigent-viewport-height");
    };
  }, []);
}
