// The floating panes design: the session's panes as rounded cards on the sidebar's color,
// below one top row. It's styling keyed on the shell's data-floating-panes.

import { useMemo, useSyncExternalStore } from "react";

import { useTouchClient } from "@/hooks/useTouchClient";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  type PaneCornersChoice,
  paneCornersPreference,
  resolveFloatingPanes,
} from "@/lib/paneCornersPreference";

/** index.css floats the panes from 48rem; a media query reads rem from the browser's font size. */
const FLOATING_PANES_QUERY = "(min-width: 48rem)";

export interface PaneCornersState {
  /** Whether this client can show floating panes at all: sidebar v2 or the policy on, and no touch. */
  available: boolean;
  /** What the user chose on this device. */
  choice: PaneCornersChoice;
  /** What Automatic gives now: the server's `floating_panes` policy. */
  policyFloating: boolean;
  /** Whether the panes float now, before the window's width is taken into account. */
  floating: boolean;
}

/**
 * The server's `floating_panes` policy (which follows `workspace_sidebar_v2` by default) combined
 * with this device's choice. Touch clients keep the edge-to-edge layout, as they keep the classic sidebar.
 */
export function usePaneCorners(): PaneCornersState {
  const serverInfo = useServerInfo();
  const touchClient = useTouchClient();
  const choice = paneCornersPreference.use();
  const policyFloating = isFeatureEnabled(serverInfo, "floating_panes");
  const available =
    !touchClient && (policyFloating || isFeatureEnabled(serverInfo, "workspace_sidebar_v2"));
  return useMemo(
    () => ({
      available,
      choice,
      policyFloating,
      floating: resolveFloatingPanes({ choice, policyFloating, designAvailable: available }),
    }),
    [available, choice, policyFloating],
  );
}

/** Whether the panes float: the policy or this device's choice, never on a touch client. */
export function useFloatingPanes(): boolean {
  return usePaneCorners().floating;
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
