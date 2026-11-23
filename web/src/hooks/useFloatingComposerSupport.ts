import { useEffect, useRef } from "react";

import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { floatingComposerBridge, getDesktopFeatures } from "@/lib/nativeBridge";
import { takeLaunchOffer } from "@/shell/floatingComposer/floatingLaunch";

/** The panel version this bundle renders at `/compose/floating`; the shell accepts only it. */
const FLOATING_COMPOSER_VERSION = 1;

/**
 * Tells the desktop shell that this main window's page can render the floating New Agent
 * panel, so ⇧⌘Space opens it, when `floating_composer` is on and the shell supports it. It also
 * takes the first messages the panel launches into this window's chats. The shell forgets this
 * when the page is replaced, so each document announces again.
 */
export function useFloatingComposerSupport(navigate: (path: string) => void): void {
  const enabled = isFeatureEnabled(useServerInfo(), "floating_composer");
  // The offer listener outlives renders, so it reads the latest navigate from a ref.
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;
  useEffect(() => {
    const bridge = floatingComposerBridge();
    if (!enabled || !bridge) return;
    let cancelled = false;
    let unsubscribeOffers: (() => void) | undefined;
    void getDesktopFeatures()
      .then((features) => {
        if (cancelled || features?.floatingComposer !== true) return;
        bridge.announceSupport(FLOATING_COMPOSER_VERSION);
        // Subscribing tells the shell this page takes launches, so it goes after the announcement.
        unsubscribeOffers = bridge.onLaunchOffer?.((offer) => {
          takeLaunchOffer(offer, (path) => navigateRef.current(path)).catch((err: unknown) =>
            console.error("[floatingComposer] couldn't open a launched session:", err),
          );
        });
      })
      // The bridge call can throw while the page or its preload is going away.
      .catch((err: unknown) => console.warn("[floatingComposer] couldn't announce support:", err));
    return () => {
      cancelled = true;
      unsubscribeOffers?.();
    };
  }, [enabled]);
}
