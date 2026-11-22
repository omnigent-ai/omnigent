// The desktop app keeps the desktop layout at every window width. Browsers and
// the mobile apps pick the layout from the viewport width.

/**
 * True in the desktop app, where a window narrower than `md` still gets the
 * desktop layout. Reads the preload's `window.omnigentDesktop` directly, so
 * layout checks work wherever nativeBridge is mocked.
 */
export function keepsDesktopLayout(): boolean {
  if (typeof window === "undefined") return false;
  const desktop = (window as { omnigentDesktop?: { kind?: string } }).omnigentDesktop;
  return desktop?.kind === "electron";
}

/**
 * Mark `<html>` with `data-desktop-app` for the `md` / `max-md` variants in
 * index.css. The shell never changes at runtime, so call once before first paint.
 */
export function applyDesktopAppAttribute(): void {
  if (keepsDesktopLayout()) document.documentElement.dataset.desktopApp = "true";
}
