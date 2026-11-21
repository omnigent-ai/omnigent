// The floating panes design: the session's panes as rounded cards on the sidebar's color,
// below one top row. It's styling keyed on the shell's data-floating-panes.

import { useTouchClient } from "@/hooks/useTouchClient";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";

/**
 * Whether the server turns `floating_panes` on, which by default follows `workspace_sidebar_v2`.
 * Touch clients keep the edge-to-edge layout, as they keep the classic sidebar.
 */
export function useFloatingPanes(): boolean {
  const serverInfo = useServerInfo();
  const touchClient = useTouchClient();
  return isFeatureEnabled(serverInfo, "floating_panes") && !touchClient;
}
