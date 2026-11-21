// The floating panes design: the session's panes as rounded cards on the sidebar's color,
// below one top row. It's styling keyed on the shell's data-floating-panes.

import { useSyncExternalStore } from "react";

import { useTouchClient } from "@/hooks/useTouchClient";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";

/** index.css floats the panes from 48rem, which a media query reads as 768px whatever the root size. */
const FLOATING_PANES_QUERY = "(min-width: 768px)";

/**
 * Whether the server turns `floating_panes` on, which by default follows `workspace_sidebar_v2`.
 * Touch clients keep the edge-to-edge layout, as they keep the classic sidebar.
 */
export function useFloatingPanes(): boolean {
  const serverInfo = useServerInfo();
  const touchClient = useTouchClient();
  return isFeatureEnabled(serverInfo, "floating_panes") && !touchClient;
}

function subscribeToWidth(callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const query = window.matchMedia(FLOATING_PANES_QUERY);
  query.addEventListener("change", callback);
  return () => query.removeEventListener("change", callback);
}

function wideEnough(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(FLOATING_PANES_QUERY).matches;
}

/** Whether the panes float in this window now: the flag on, and 48rem or wider. */
export function useFloatingPanesShown(): boolean {
  const floatingPanes = useFloatingPanes();
  const wide = useSyncExternalStore(subscribeToWidth, wideEnough, () => false);
  return floatingPanes && wide;
}
