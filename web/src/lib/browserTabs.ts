/** Stable soft-tab ID for the browser view driven by agents. */
export const AGENT_BROWSER_TAB_ID = "agent-browser";

export function browserViewId(conversationId: string, tabId: string): string {
  return tabId === AGENT_BROWSER_TAB_ID
    ? conversationId
    : `browser-tab:${encodeURIComponent(conversationId)}:${tabId}`;
}

/** Owning session ID for a browser view ID; mirrors the Electron view-registry parser. */
export function browserViewOwnerId(viewId: string): string {
  return parseBrowserViewId(viewId).conversationId;
}

/** Inverse of {@link browserViewId}; a malformed tab key is treated as its own agent view. */
export function parseBrowserViewId(viewId: string): { conversationId: string; tabId: string } {
  const tab = /^browser-tab:([^:]+):([^:]+)$/.exec(viewId);
  if (!tab) return { conversationId: viewId, tabId: AGENT_BROWSER_TAB_ID };
  try {
    return { conversationId: decodeURIComponent(tab[1]), tabId: tab[2] };
  } catch {
    return { conversationId: viewId, tabId: AGENT_BROWSER_TAB_ID };
  }
}
