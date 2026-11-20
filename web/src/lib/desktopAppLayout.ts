// The desktop app and popped-out session windows keep the desktop layout at
// every window width. Browsers and the mobile apps pick it from the viewport width.

import { isSessionWindow, startsAsSessionWindow } from "./sessionWindow";

/**
 * True in the desktop app and in a popped-out session window, where a window
 * narrower than `md` still gets the desktop layout. Reads the preload's
 * `window.omnigentDesktop` directly, so layout checks work wherever nativeBridge
 * is mocked.
 */
export function keepsDesktopLayout(): boolean {
  if (typeof window === "undefined") return false;
  const desktop = (window as { omnigentDesktop?: { kind?: string } }).omnigentDesktop;
  return desktop?.kind === "electron" || isSessionWindow();
}

/**
 * Mark `<html>` with `data-desktop-app` for the `md` / `max-md` variants in
 * index.css, and `data-session-window` on a popped-out session window. Neither
 * changes at runtime, so call once before first paint.
 */
export function applyDesktopAppAttribute(): void {
  if (startsAsSessionWindow()) document.documentElement.dataset.sessionWindow = "true";
  if (keepsDesktopLayout()) document.documentElement.dataset.desktopApp = "true";
}
