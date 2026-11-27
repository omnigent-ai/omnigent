// Which pull request design a device shows. The new page and list (`pull_requests_v2`) show in
// the tabs sidebar (`workspace_sidebar_v2`) only; elsewhere, or with the flag off, the classic
// pull request panel and page show.

import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useTabsSidebar } from "@/shell/useTabsSidebar";

/** Whether the new pull request page and list show: the tabs sidebar with `pull_requests_v2`, live. */
export function usePullRequestsV2(): boolean {
  const serverInfo = useServerInfo();
  const tabsSidebar = useTabsSidebar();
  return tabsSidebar && isFeatureEnabled(serverInfo, "pull_requests_v2");
}
