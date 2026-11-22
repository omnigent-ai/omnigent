// Whether a session's main area shows as panes: the workspace sidebar v2's tabs, on a desktop
// window. Splitting the panes also needs a viewer who owns the session.

import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";

import { useTabsSidebar } from "./useTabsSidebar";

/** Whether the main area shows the panes view: `workspace_sidebar_v2`, outside touch and phone layouts. */
export function usePanesView(): boolean {
  const tabsSidebar = useTabsSidebar();
  const isMobile = useIsMobileViewport();
  return tabsSidebar && !isMobile;
}

/** Whether a viewer may split and close the panes view's panes: it shows, and they own the session. */
export function usePanesSplitsAllowed(readOnly: boolean): boolean {
  return usePanesView() && !readOnly;
}
