import { useEffect } from "react";

import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { floatingComposerBridge, getDesktopFeatures } from "@/lib/nativeBridge";
import { acceptLaunchOffer } from "@/shell/floatingComposer/floatingLaunch";

/** The panel version this bundle renders at `/compose/floating`; the shell accepts only it. */
const FLOATING_COMPOSER_VERSION = 1;

/**
 * Tells the desktop shell that this main window's page can render the floating New Agent
 * panel, so ⇧⌘Space opens it, when `floating_composer` is on and the shell supports it.
 * It also takes the first message of a launch the shell offers this page, so its bubble stays.
 * The shell forgets both when the page is replaced, so each document announces again.
 */
export function useFloatingComposerSupport(): void {
  const enabled = isFeatureEnabled(useServerInfo(), "floating_composer");
  useEffect(() => {
    const bridge = floatingComposerBridge();
    if (!enabled || !bridge) return;
    let cancelled = false;
    let stopOffers: (() => void) | undefined;
    void getDesktopFeatures()
      .then((features) => {
        if (cancelled || features?.floatingComposer !== true) return;
        bridge.announceSupport(FLOATING_COMPOSER_VERSION);
        // After the announcement: the shell offers launches only to a page that can show the panel.
        stopOffers = bridge.onLaunchOffer?.((offer) => void acceptLaunchOffer(offer));
      })
      // The bridge call can throw while the page or its preload is going away.
      .catch((err: unknown) =>
        console.warn("[floatingComposer] couldn't announce support or take launches:", err),
      );
    return () => {
      cancelled = true;
      stopOffers?.();
    };
  }, [enabled]);
}
