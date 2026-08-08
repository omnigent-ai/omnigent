/** Hide the embedded browser's native WebContentsView while a DOM overlay
 *  (dialog/modal) is open. The native view paints ABOVE every DOM element —
 *  no z-index can put a modal over it — so overlays that must appear on top
 *  suppress it for their open lifetime. Ref-counted across instances so
 *  overlapping overlays don't unhide the view early; no-op outside Electron
 *  and on desktop shells that predate the browser bridge. */
import { useEffect } from "react";
import { supportsBrowser } from "@/lib/nativeBridge";

/** Slice of `window.omnigentDesktop` this hook calls. Typed locally (like
 *  BrowserPane) so the hook doesn't depend on the full nativeBridge type;
 *  optional because an older shell may predate the method. */
interface OverlayBridge {
  browserSetOverlaySuppressed?: (suppressed: boolean) => Promise<{ ok: boolean; error?: string }>;
}

function getBridge(): OverlayBridge | null {
  if (!supportsBrowser()) return null;
  const w = window as unknown as { omnigentDesktop?: OverlayBridge };
  return w.omnigentDesktop ?? null;
}

// Count of currently-open overlays across all hook instances. The view is
// hidden while the count is > 0 and restored only when it returns to 0.
let openOverlays = 0;

export function useSuppressBrowserView(active: boolean) {
  useEffect(() => {
    if (!active) return;
    const bridge = getBridge();
    if (!bridge?.browserSetOverlaySuppressed) return;
    openOverlays += 1;
    if (openOverlays === 1) void bridge.browserSetOverlaySuppressed(true);
    return () => {
      openOverlays -= 1;
      if (openOverlays === 0) void getBridge()?.browserSetOverlaySuppressed?.(false);
    };
  }, [active]);
}
