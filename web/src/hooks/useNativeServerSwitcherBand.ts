import { useEffect } from "react";

import {
  isAndroidShell,
  setNativeServerSwitcherBand,
  setNativeServerSwitcherHidden,
} from "@/lib/nativeBridge";

import { useShellReady, useSurfaceFrontmost } from "./useNativeServerSwitcher";

/**
 * Publish the chat column's horizontal extent so the native server switcher can
 * centre itself there instead of over the whole window.
 *
 * The switcher is a native view stacked above the web view, so wherever it
 * lands it swallows taps. The chat column keeps it off adjacent rails; native
 * owns the "band too narrow to fit the pill" policy (its control reserve +
 * minimum width), so degenerate bands are published as-is and hidden there.
 * Obscured or collapsed columns hide the switcher instead of borrowing another
 * surface.
 *
 * Android-only: the iOS shell has no band setter and owns the switcher's
 * visibility from its own frontmost tracking, which a publish here would
 * clobber. No-op outside the Android shell.
 */
export function useNativeServerSwitcherBand(column: HTMLElement | null): void {
  const shellReady = useShellReady(isAndroidShell);

  // Sole Android owner of the switcher's visibility: any overlay covering the
  // column (drawer, sidebar, sheet, maximized rail) drops frontmost and hides
  // the switcher, so a band republish can never re-show it over an overlay.
  const frontmost = useSurfaceFrontmost(column, column !== null, isAndroidShell);
  useEffect(() => {
    if (!shellReady) return;
    if (!column) {
      setNativeServerSwitcherHidden(true);
      return;
    }

    let frame = 0;
    const publish = () => {
      frame = 0;
      if (!frontmost) {
        setNativeServerSwitcherHidden(true);
        return;
      }
      const viewport = window.innerWidth;
      if (viewport <= 0) {
        setNativeServerSwitcherHidden(true);
        return;
      }
      const columnRect = column.getBoundingClientRect();
      const left = Math.max(0, Math.min(1, columnRect.left / viewport));
      const right = Math.max(0, Math.min(1, columnRect.right / viewport));
      if (left >= right) {
        setNativeServerSwitcherHidden(true);
        return;
      }
      setNativeServerSwitcherBand(left, right);
      setNativeServerSwitcherHidden(false);
    };
    // Coalesce to one frame so a drag-resize does not post per pointer event.
    const schedule = () => {
      if (frame !== 0) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(publish);
    };

    schedule();

    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(schedule) : null;
    observer?.observe(column);
    window.addEventListener("resize", schedule);
    window.addEventListener("orientationchange", schedule);

    return () => {
      if (frame !== 0) cancelAnimationFrame(frame);
      setNativeServerSwitcherHidden(true);
      observer?.disconnect();
      window.removeEventListener("resize", schedule);
      window.removeEventListener("orientationchange", schedule);
    };
  }, [column, frontmost, shellReady]);
}
