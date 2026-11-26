// Whether a session's main area shows as panes: the workspace sidebar v2's tabs, on a desktop
// window. Splitting the panes also needs a viewer who owns the session.

import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";

import { useChatPaneBodyHost, useSplitBesideChat } from "./chatPaneToolbarHost";
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

export interface ChatInPanes {
  /** The chat shows as the agent's pane of a split, so the panes view stays on screen in Chat. */
  inPanes: boolean;
  /** The chat pane's body to render the chat into, once that pane has mounted. */
  host: HTMLElement | null;
  /** The panes view shows the chat, split or not, so the pane keys read it as the agent's pane. */
  marked: boolean;
}

/** Where a session's web chat renders while it shows: its own column, or its pane of a split. */
export function useChatInPanes(
  conversationId: string | null | undefined,
  chatShown: boolean,
): ChatInPanes {
  const panesView = usePanesView();
  const splitBesideChat = useSplitBesideChat(conversationId);
  const host = useChatPaneBodyHost(conversationId);
  const inPanes = panesView && chatShown && splitBesideChat;
  return { inPanes, host: inPanes ? host : null, marked: panesView && chatShown };
}
