import { useEffect } from "react";

// The embedded browser is a native Electron WebContentsView that paints ABOVE
// the entire renderer DOM, so CSS z-index can't lift an overlay over it (issue
// #3980). While a suppressing overlay is open we ask the main process to hide
// the view in place (setVisible(false)); it reappears when the last one closes.
//
// Applied narrowly — modal dialogs and the rail's "Open new" menu — not to
// every popover/tooltip. Those can still stack (a dialog opening the menu),
// so a module-level ref count gates the IPC: only 0→1 suppresses and only 1→0
// restores.

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
 * Renders nothing; suppresses the embedded browser view for as long as it is
 * mounted.
 *
 * MUST be mounted only WHILE an overlay is open. Radix `Content` WRAPPERS
 * (DialogContent, TooltipContent, …) stay mounted in the JSX tree even when
 * closed, so putting this in a wrapper body would suppress forever. Radix only
 * mounts the actual `<Primitive.Content>` element (and its children) while
 * open, so render this as a CHILD inside that element — then its mount/unmount
 * tracks the overlay's open state exactly. No-op outside a browser-capable shell.
 */
export function SuppressBrowserView(): null {
  useEffect(() => {
    if (!browserSuppressor()) return;
    openOverlayCount += 1;
    if (openOverlayCount === 1) void browserSuppressor()?.(true);
    return () => {
      openOverlayCount -= 1;
      if (openOverlayCount === 0) void browserSuppressor()?.(false);
    };
  }, []);
  return null;
}
