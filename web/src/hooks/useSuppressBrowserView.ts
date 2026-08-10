import { useEffect } from "react";

// The embedded browser is a native Electron WebContentsView that paints ABOVE
// the entire renderer DOM, so CSS z-index can't lift a dialog/menu/tooltip/toast
// over it (issue #3980). While any such overlay is open we ask the main process
// to hide the view in place (setVisible(false)); it reappears when the last one
// closes.
//
// Overlays stack (a toast can auto-dismiss while a dialog is still open), so a
// module-level ref count gates the IPC: only 0→1 suppresses and only 1→0
// restores. A plain boolean per-overlay would let the toast's cleanup un-hide
// the view out from under the dialog.

let openOverlayCount = 0;

/**
 * The desktop shell's suppress method, or undefined outside a browser-capable
 * Electron shell. Read straight off `window.omnigentDesktop` (the same pattern
 * BrowserPane uses) rather than through nativeBridge, so this hook adds no new
 * import that partial-mock tests would have to stub. Gating on the actual
 * method means an older shell without it simply no-ops.
 */
function browserSuppressor(): ((suppressed: boolean) => unknown) | undefined {
  if (typeof window === "undefined") return undefined;
  const w = window as unknown as {
    omnigentDesktop?: { browserSetSuppressed?: (suppressed: boolean) => Promise<unknown> };
  };
  return w.omnigentDesktop?.browserSetSuppressed;
}

/**
 * Suppress the embedded browser view for the lifetime of the calling component.
 * Mount an overlay's Content through this so the native view is hidden while it
 * shows. No-op outside a browser-capable Electron shell.
 */
export function useSuppressBrowserView(): void {
  useEffect(() => {
    if (!browserSuppressor()) return;
    openOverlayCount += 1;
    if (openOverlayCount === 1) void browserSuppressor()?.(true);
    return () => {
      openOverlayCount -= 1;
      if (openOverlayCount === 0) void browserSuppressor()?.(false);
    };
  }, []);
}
