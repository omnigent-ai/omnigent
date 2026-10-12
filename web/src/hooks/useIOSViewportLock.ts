import { useEffect } from "react";

import {
  getIOSKeyboardViewportHeight,
  isIOSShell,
  onNativeKeyboardViewportChanged,
  setIOSDocumentScrollEnabled,
} from "@/lib/nativeBridge";

/** Keep content above the docked keyboard without shrinking for floating iPad controls. */
export function useIOSViewportLock(): void {
  useEffect(() => {
    if (!isIOSShell()) return;
    const viewport = window.visualViewport;
    if (!viewport) return;

    const root = document.documentElement;
    let frame = 0;

    // WebKit otherwise animates the root to reveal the caret before our scroll
    // correction runs. The shell scrolls its inner panes instead of the document.
    setIOSDocumentScrollEnabled(false);

    // WebKit may still pan the document when an input focuses. Keep the header
    // fixed while the inner panes scroll within the keyboard-aware shell.
    const resetPan = () => {
      if (viewport.offsetTop !== 0 || window.scrollY !== 0) window.scrollTo(0, 0);
    };

    const apply = () => {
      frame = 0;
      root.style.setProperty(
        "--omnigent-viewport-height",
        `${Math.round(getIOSKeyboardViewportHeight() ?? viewport.height)}px`,
      );
      resetPan();
    };

    // Coalesce the burst of resize/scroll events the keyboard animation fires.
    const schedule = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(apply);
    };

    const unsubscribe = onNativeKeyboardViewportChanged(schedule);
    apply();
    viewport.addEventListener("resize", schedule);
    viewport.addEventListener("scroll", schedule);
    window.addEventListener("scroll", resetPan, { passive: true });
    window.addEventListener("orientationchange", schedule);
    window.addEventListener("resize", schedule);

    return () => {
      setIOSDocumentScrollEnabled(true);
      unsubscribe();
      if (frame) window.cancelAnimationFrame(frame);
      viewport.removeEventListener("resize", schedule);
      viewport.removeEventListener("scroll", schedule);
      window.removeEventListener("scroll", resetPan);
      window.removeEventListener("orientationchange", schedule);
      window.removeEventListener("resize", schedule);
      root.style.removeProperty("--omnigent-viewport-height");
    };
  }, []);
}
