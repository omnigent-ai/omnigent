// Whether a session's workspace sidebar shows as tabs, which decides where the app shell
// routes and whether the tabs sidebar's code loads at all.

import { useTouchClient } from "@/hooks/useTouchClient";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";

/**
 * The sidebar shows as tabs where the server turns `workspace_sidebar_v2` on, at any window
 * width; a narrow window shows them in a drawer. Touch clients, the mobile apps and phone or
 * tablet browsers, keep the classic drawers.
 */
export function useTabsSidebar(): boolean {
  const serverInfo = useServerInfo();
  const touchClient = useTouchClient();
  return isFeatureEnabled(serverInfo, "workspace_sidebar_v2") && !touchClient;
}
