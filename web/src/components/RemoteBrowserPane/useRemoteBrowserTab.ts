import { useCallback, useState } from "react";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";

export const REMOTE_BROWSER_LABEL = "Remote browser";

function readOpen(conversationId: string): boolean {
  return readSessionWorkspaceState(conversationId).remoteBrowserOpen === true;
}

/** Whether a session's remote browser tab is open in the workspace rail, remembered per session. */
export function useRemoteBrowserTab(conversationId: string) {
  const [state, setState] = useState(() => ({ conversationId, open: readOpen(conversationId) }));
  // Reload on a session switch during render, so its first frame never shows the last one's tab.
  if (state.conversationId !== conversationId) {
    setState({ conversationId, open: readOpen(conversationId) });
  }
  const setOpen = useCallback(
    (open: boolean) => {
      writeSessionWorkspaceState(conversationId, { remoteBrowserOpen: open });
      setState({ conversationId, open });
    },
    [conversationId],
  );
  return { open: state.conversationId === conversationId && state.open, setOpen };
}
