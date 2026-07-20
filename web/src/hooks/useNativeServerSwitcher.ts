import { useEffect, useState } from "react";

import { isIOSShell, setNativeServerSwitcherHidden } from "@/lib/nativeBridge";

/**
 * Tracks whether `surface` is the frontmost element at its own centre — i.e.
 * not covered by a drawer / sidebar / sheet. Returns false when inactive,
 * outside the iOS shell, or while obscured. Re-checks on the layout signals a
 * drawer transition emits (mutations, transitions, viewport changes). Both the
 * native server switcher and the native Chat/Terminal bar hide off this signal
 * so neither floats over an opened panel.
 */
export function useSurfaceFrontmost(surface: HTMLElement | null, active: boolean): boolean {
  const [frontmost, setFrontmost] = useState(false);
  useEffect(() => {
    if (!isIOSShell() || !active) {
      setFrontmost(false);
      return;
    }

    let frame = 0;
    const sync = () => {
      frame = 0;
      setFrontmost(isSurfaceFrontmost(surface));
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
      setFrontmost(false);
    };
  }, [active, surface]);
  return frontmost;
}

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
  const frontmost = useSurfaceFrontmost(surface, active);
  useEffect(() => {
    if (!isIOSShell()) return;
    setNativeServerSwitcherHidden(!frontmost);
  }, [frontmost]);
  useEffect(() => {
    if (!isIOSShell()) return;
    return () => setNativeServerSwitcherHidden(true);
  }, []);
}

export function isSurfaceFrontmost(surface: HTMLElement | null): boolean {
  if (!surface) return false;
  const rect = surface.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return false;

  const xInset = Math.min(24, Math.max(1, rect.width / 4));
  const yInset = Math.min(24, Math.max(1, rect.height / 4));
  const x = clamp(window.innerWidth / 2, rect.left + xInset, rect.right - xInset);
  const y = clamp(rect.top + rect.height * 0.38, rect.top + yInset, rect.bottom - yInset);
  const topElement = document.elementFromPoint(x, y);

  // A Radix dropdown / select / popover sets `pointer-events: none` on the body
  // while open WITHOUT covering the surface, so elementFromPoint falls through
  // to the document root (or null). That's a transient layer, not a panel —
  // keep the surface "frontmost" so the native overlays don't blink out. BUT a
  // menu opened from inside the mobile sidebar (e.g. a conversation-row kebab)
  // means the sidebar overlay is up: the dropped pointer-events hide it from
  // the hit test, so probe the sidebar directly and treat the surface as
  // obscured when it covers the probe point.
  if (!topElement || topElement === document.documentElement || topElement === document.body) {
    return !isProbeCoveredByOpenSidebar(x, y);
  }
  // Likewise if a popover/menu/listbox actually covers the probe point: those
  // are transient, unlike a persistent drawer/sidebar/sheet — unless the
  // sidebar overlay is what's behind the menu (see above).
  if (
    topElement.closest(
      '[data-radix-popper-content-wrapper], [role="menu"], [role="listbox"], [role="tooltip"]',
    )
  ) {
    return !isProbeCoveredByOpenSidebar(x, y);
  }

  return surface.contains(topElement);
}

/**
 * Whether the open mobile sidebar overlay covers `(x, y)`. Used to keep the
 * native overlays hidden when a Radix menu opened from within the sidebar sits
 * on top of it — the menu's `pointer-events: none` on the body otherwise hides
 * the sidebar from elementFromPoint, misreading the surface as frontmost. The
 * sidebar drops `data-collapsed` when open; on desktop it's a floating card
 * that never spans the centre probe, so the rect test naturally excludes it.
 */
function isProbeCoveredByOpenSidebar(x: number, y: number): boolean {
  const sidebar = document.querySelector("aside.conversations-sidebar:not([data-collapsed])");
  if (!sidebar) return false;
  const r = sidebar.getBoundingClientRect();
  return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
}

function clamp(value: number, min: number, max: number): number {
  if (max < min) return min;
  return Math.min(Math.max(value, min), max);
}
