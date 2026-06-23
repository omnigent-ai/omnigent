import { useEffect } from "react";

import { isIOSShell, setNativeServerSwitcherHidden } from "@/lib/nativeBridge";

/**
 * Drive the iOS shell's native server switcher overlay so it shows only while
 * `surface` is the frontmost element on screen and `active` is true. The
 * switcher is a native chrome element the web app toggles via the bridge; it
 * must hide whenever the sidebar (or any other overlay) covers the main
 * surface, and whenever the surface is unmounted.
 *
 * No-ops outside the iOS shell. Used by both the in-session main surface
 * (ChatPage) and the new-session landing screen (NewChatDialog).
 */
export function useNativeServerSwitcherForMainSurface(
  surface: HTMLElement | null,
  active: boolean,
) {
  useEffect(() => {
    if (!isIOSShell()) return;
    if (!active) {
      setNativeServerSwitcherHidden(true);
      return;
    }

    let frame = 0;
    const sync = () => {
      frame = 0;
      setNativeServerSwitcherHidden(!isSurfaceFrontmost(surface));
    };
    const schedule = () => {
      if (frame !== 0) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(sync);
    };

    schedule();

    const observer =
      typeof MutationObserver !== "undefined" ? new MutationObserver(schedule) : null;
    observer?.observe(document.body, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["class", "style", "aria-hidden", "data-state", "data-collapsed", "open"],
    });

    window.addEventListener("resize", schedule);
    window.addEventListener("orientationchange", schedule);
    window.addEventListener("scroll", schedule, true);
    window.addEventListener("transitionend", schedule, true);
    window.addEventListener("animationend", schedule, true);
    window.addEventListener("focusin", schedule, true);
    window.addEventListener("focusout", schedule, true);
    window.visualViewport?.addEventListener("resize", schedule);
    window.visualViewport?.addEventListener("scroll", schedule);

    return () => {
      if (frame !== 0) cancelAnimationFrame(frame);
      observer?.disconnect();
      window.removeEventListener("resize", schedule);
      window.removeEventListener("orientationchange", schedule);
      window.removeEventListener("scroll", schedule, true);
      window.removeEventListener("transitionend", schedule, true);
      window.removeEventListener("animationend", schedule, true);
      window.removeEventListener("focusin", schedule, true);
      window.removeEventListener("focusout", schedule, true);
      window.visualViewport?.removeEventListener("resize", schedule);
      window.visualViewport?.removeEventListener("scroll", schedule);
      setNativeServerSwitcherHidden(true);
    };
  }, [active, surface]);
}

function isSurfaceFrontmost(surface: HTMLElement | null): boolean {
  if (!surface) return false;
  const rect = surface.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return false;

  const xInset = Math.min(24, Math.max(1, rect.width / 4));
  const yInset = Math.min(24, Math.max(1, rect.height / 4));
  const x = clamp(window.innerWidth / 2, rect.left + xInset, rect.right - xInset);
  const y = clamp(rect.top + rect.height * 0.38, rect.top + yInset, rect.bottom - yInset);
  const topElement = document.elementFromPoint(x, y);
  return topElement !== null && surface.contains(topElement);
}

function clamp(value: number, min: number, max: number): number {
  if (max < min) return min;
  return Math.min(Math.max(value, min), max);
}
