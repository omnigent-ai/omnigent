import { createContext, useCallback, useContext } from "react";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
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
 * Click handler for sub-agent links. For a plain desktop click with the setting
 * on, it opens the tab, cancels the link's navigation and returns true.
 */
export function useOpenSubagentInPanel(): (
  childSessionId: string,
  event: SubagentLinkClick,
) => boolean {
  const opener = useContext(SubagentPanelOpenerContext);
  const isMobile = useIsMobileViewport();
  return useCallback(
    (childSessionId, event) => {
      if (opener === null || isMobile || !readOpenSubagentsInPanel()) return false;
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
        return false;
      }
      if (!opener(childSessionId)) return false;
      event.preventDefault();
      return true;
    },
    [opener, isMobile],
  );
}
