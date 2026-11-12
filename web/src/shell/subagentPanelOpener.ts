import { createContext, useCallback, useContext } from "react";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useLocation, useNavigate } from "@/lib/routing";
import { sessionNavigationSearch } from "@/lib/sessionNavigation";
import { FileViewerContext } from "./FileViewerContext";
import { readOpenSubagentsInPanel } from "@/lib/subagentPanelPreferences";

/**
 * Opens a sub-agent as a tab in the main chat's Workspace panel; returns false
 * when it can't (e.g. it is the chat on screen). Other surfaces don't provide one.
 */
export type SubagentPanelOpener = (childSessionId: string) => boolean;

export const SubagentPanelOpenerContext = createContext<SubagentPanelOpener | null>(null);

/** The parts of a click that decide whether it is a plain one. */
interface SubagentLinkClick {
  button: number;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
  preventDefault: () => void;
}

/**
 * Handles plain sub-agent clicks through the file guard, opening a Workspace tab
 * when enabled or navigating to the child. Returns true when the click is handled.
 */
export function useOpenSubagentInPanel(): (
  childSessionId: string,
  event: SubagentLinkClick,
) => boolean {
  const opener = useContext(SubagentPanelOpenerContext);
  const isMobile = useIsMobileViewport();
  const guardFileNavigation = useContext(FileViewerContext)?.guardFileNavigation;
  const navigate = useNavigate();
  const location = useLocation();
  return useCallback(
    (childSessionId, event) => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
        return false;
      }
      const openInPanel = () =>
        opener !== null && !isMobile && readOpenSubagentsInPanel() && opener(childSessionId);
      if (guardFileNavigation) {
        event.preventDefault();
        guardFileNavigation(null, undefined, () => {
          if (!openInPanel()) {
            navigate({
              pathname: `/c/${childSessionId}`,
              search: sessionNavigationSearch(location.search),
            });
          }
        });
        return true;
      }
      if (!openInPanel()) return false;
      event.preventDefault();
      return true;
    },
    [opener, isMobile, guardFileNavigation, navigate, location.search],
  );
}
