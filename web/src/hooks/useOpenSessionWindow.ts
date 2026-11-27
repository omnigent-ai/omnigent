import { useCallback } from "react";
import { toast } from "sonner";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useIsEmbedded } from "@/lib/embedded";
import {
  isElectronShell,
  isNativeShell,
  openDesktopSessionWindow,
  supportsDesktopSessionWindows,
} from "@/lib/nativeBridge";
import { openBrowserSessionWindow } from "@/lib/sessionWindow";

/**
 * Whether this runtime can pop a session out into its own window. An older
 * desktop shell without the bridge sends `window.open` to the system browser,
 * and the mobile shells have no windows, so neither offers it.
 */
export function canOpenSessionWindow(): boolean {
  if (isElectronShell()) return supportsDesktopSessionWindows();
  return !isNativeShell();
}

/**
 * "Open in new window" for session menus: a desktop app window in the desktop
 * shell, a browser popup otherwise. Null where no window can open (mobile
 * layouts, an embedding host page, shells without support).
 */
export function useOpenSessionWindow(): ((sessionId: string) => void) | null {
  const isMobile = useIsMobileViewport();
  const isEmbedded = useIsEmbedded();
  const open = useCallback((sessionId: string) => {
    if (isElectronShell()) {
      void openDesktopSessionWindow(sessionId).then((opened) => {
        if (!opened) toast.error("Couldn't open a new window");
      });
    } else if (!openBrowserSessionWindow(sessionId)) {
      toast.error("Your browser blocked the new window. Allow pop-ups for this site.");
    }
  }, []);
  return isMobile || isEmbedded || !canOpenSessionWindow() ? null : open;
}
