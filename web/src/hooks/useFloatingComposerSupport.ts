import { useEffect } from "react";

import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { floatingComposerBridge, getDesktopFeatures } from "@/lib/nativeBridge";

/** The panel version this bundle renders at `/compose/floating`; the shell accepts only it. */
const FLOATING_COMPOSER_VERSION = 1;

/**
 * Tells the desktop shell that this main window's page can render the floating New Agent
 * panel, so ⇧⌘Space opens it, when `floating_composer` is on and the shell supports it.
 * The shell forgets this when the page is replaced, so each document announces again.
 */
export function useFloatingComposerSupport(): void {
  const enabled = isFeatureEnabled(useServerInfo(), "floating_composer");
  useEffect(() => {
    const bridge = floatingComposerBridge();
    if (!enabled || !bridge) return;
    let cancelled = false;
    void getDesktopFeatures()
      .then((features) => {
        if (!cancelled && features?.floatingComposer === true) {
          bridge.announceSupport(FLOATING_COMPOSER_VERSION);
        }
      })
      // The bridge call can throw while the page or its preload is going away.
      .catch((err: unknown) => console.warn("[floatingComposer] couldn't announce support:", err));
    return () => {
      cancelled = true;
    };
  }, [enabled]);
}
